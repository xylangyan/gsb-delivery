"""Migration sequencer.

Searches for an executable order of table switches that moves every
collecting segment from its old extension table to its final table,
under these rules:

* one table is touched per step (每次只动一张表); sink receipts
  (收件点回执) may ride on the same step;
* a retired table stays "in flight" until every sink of its segment
  has been awaited in a later step — only then is the old segment
  counted as cleared (旧段清完) and a borrowed code returned;
* while any mix of versions is in flight, every reader must be able
  to decode every in-flight ext code, and no merged map may collapse
  two entries into one key (checked symbolically, see
  :mod:`migration.checks`);
* plans are ordered lexicographically: fewest borrowed extension
  codes first (少借扩展号), then fewest steps (少实际切换).

If the final tables are unreachable, :func:`plan` reports the earliest
dead-end states together with the exact permission sets or collision
witnesses that block them (最早卡住的一组许可).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .checks import validate_inflight


@dataclass(frozen=True)
class Step:
    index: int
    segment: str = ""           # "" for a drain-only step
    from_table: str = ""
    to_table: str = ""
    ext_code: int = 0
    enables: tuple = ()         # (reader, ext_code) handlers to start
    disables: tuple = ()        # (reader, ext_code) handlers to stop
    awaits: tuple = ()          # (segment, sink) receipts awaited
    cleared: tuple = ()         # (segment, table_id) old segments cleared
    returned: tuple = ()        # borrowed ext codes returned by this step

    def describe(self):
        parts = []
        if self.segment:
            parts.append("switch %s: %s -> %s (ext %d)" % (
                self.segment, self.from_table, self.to_table,
                self.ext_code))
        else:
            parts.append("drain only")
        if self.enables:
            parts.append("start handlers %s" % (list(self.enables),))
        if self.disables:
            parts.append("stop handlers %s" % (list(self.disables),))
        if self.awaits:
            parts.append("await sinks %s" % (list(self.awaits),))
        if self.cleared:
            parts.append("cleared %s" % (list(self.cleared),))
        if self.returned:
            parts.append("return borrowed codes %s" % (list(self.returned),))
        return "; ".join(parts)


@dataclass
class PlanResult:
    ok: bool
    steps: tuple = ()
    borrowed: tuple = ()
    blocking: tuple = ()        # dead-end reports when not ok

    def describe(self):
        if self.ok:
            lines = ["plan: %d step(s), borrowed ext codes %s" % (
                len(self.steps), list(self.borrowed))]
            for step in self.steps:
                lines.append("  %d. %s" % (step.index, step.describe()))
            return "\n".join(lines)
        lines = ["no executable sequence reaches the final tables"]
        for dead in self.blocking:
            lines.append("earliest blocking point after %d step(s): %s"
                         % (dead["depth"], dead["state"]))
            for reason in dead["reasons"]:
                lines.append("  - %s" % reason)
        return "\n".join(lines)


class _State:
    __slots__ = ("currents", "pending", "used")

    def __init__(self, currents, pending, used):
        # currents: tuple of table_ids, one per segment
        # pending: tuple (per segment) of tuples of (table_id, frozenset(sinks))
        # used: frozenset of borrowed ext codes used so far
        self.currents = currents
        self.pending = pending
        self.used = used

    def key(self):
        return (self.currents, self.pending, self.used)

    def __hash__(self):
        return hash(self.key())

    def __eq__(self, other):
        return self.key() == other.key()


class _Planner:
    def __init__(self, handover):
        self.ho = handover
        self.segs = tuple(handover.segments)
        self.seg = handover.segments

    # -- state helpers -----------------------------------------------------

    def inflight(self, currents, pending):
        out = []
        for i, seg in enumerate(self.segs):
            out.append((seg, currents[i]))
            for table_id, _ in pending[i]:
                out.append((seg, table_id))
        return out

    def required_handlers(self, inflight):
        """ext codes each reader must actively pass as ext_handlers."""
        codes = {self.seg[s].tables[t].ext_code for s, t in inflight}
        required = {}
        for reader in self.ho.readers:
            need = set()
            for code in codes:
                if code in reader.registered:
                    continue
                need.add(code)
            required[reader.name] = frozenset(need)
        return required

    def problems(self, inflight):
        return validate_inflight(self.ho, inflight)

    def advance_pending(self, pending):
        """Await every outstanding sink; return (new_pending, awaits, cleared,
        returned_codes)."""
        awaits = []
        cleared = []
        returned = []
        new_pending = []
        for i, seg in enumerate(self.segs):
            sinks = set(self.seg[seg].sinks)
            kept = []
            for table_id, got in pending[i]:
                missing = sinks - set(got)
                awaits.extend((seg, s) for s in sorted(missing))
                got = frozenset(set(got) | sinks)
                if got >= sinks:
                    cleared.append((seg, table_id))
                    table = self.seg[seg].tables[table_id]
                    if table.borrowed:
                        returned.append(table.ext_code)
                else:
                    kept.append((table_id, got))
            new_pending.append(tuple(kept))
        return tuple(new_pending), tuple(awaits), tuple(cleared), \
            tuple(sorted(returned))

    # -- moves ---------------------------------------------------------------

    def moves(self, state):
        """Yield (new_state, step_info, problems).

        ``step_info`` is None when the move is invalid; ``problems``
        then carries the reasons.
        """
        # switch moves
        for i, seg in enumerate(self.segs):
            current = state.currents[i]
            if current == self.seg[seg].final:
                continue
            targets = [self.seg[seg].final] + list(self.seg[seg].borrowable)
            for target in targets:
                if target == current:
                    continue
                yield self._try_switch(state, i, target)
        # drain-only move
        if any(state.pending[i] for i in range(len(self.segs))):
            new_pending, awaits, cleared, returned = \
                self.advance_pending(state.pending)
            info = {
                "switch": None,
                "pending": new_pending,
                "currents": state.currents,
                "awaits": awaits,
                "cleared": cleared,
                "returned": returned,
            }
            yield (info, [])

    def _try_switch(self, state, i, target):
        seg = self.segs[i]
        currents = list(state.currents)
        retiring = currents[i]
        currents[i] = target
        currents = tuple(currents)
        # in-flight right after the switch, before any receipt arrives:
        # previously pending versions + the just-retired table + currents
        pending_mid = [list(p) for p in state.pending]
        pending_mid[i] = pending_mid[i] + [(retiring, frozenset())]
        inflight = self.inflight(currents,
                                 [tuple(p) for p in pending_mid])
        problems = self.problems(inflight)
        if problems:
            return ({"switch": (seg, retiring, target)}, problems)
        new_pending, awaits, cleared, returned = \
            self.advance_pending(state.pending)
        # the table retired by this switch still needs all its sinks
        new_pending = [list(p) for p in new_pending]
        new_pending[i] = new_pending[i] + [(retiring, frozenset())]
        used = set(state.used)
        table = self.seg[seg].tables[target]
        if table.borrowed:
            used.add(table.ext_code)
        info = {
            "switch": (seg, retiring, target),
            "pending": tuple(tuple(p) for p in new_pending),
            "currents": currents,
            "awaits": awaits,
            "cleared": cleared,
            "returned": returned,
            "used": frozenset(used),
        }
        return (info, [])

    # -- search --------------------------------------------------------------

    def is_goal(self, state):
        for i, seg in enumerate(self.segs):
            if state.currents[i] != self.seg[seg].final:
                return False
            if state.pending[i]:
                return False
        return True

    def state_desc(self, state):
        parts = []
        for i, seg in enumerate(self.segs):
            pend = ",".join(t for t, _ in state.pending[i])
            parts.append("%s=%s%s" % (seg, state.currents[i],
                                      " (pending: %s)" % pend if pend else ""))
        return "; ".join(parts)

    def run(self):
        start = _State(
            currents=tuple(self.seg[s].old for s in self.segs),
            pending=tuple(() for _ in self.segs),
            used=frozenset(),
        )
        initial = self.inflight(start.currents, start.pending)
        initial_problems = self.problems(initial)
        if initial_problems:
            return PlanResult(ok=False, blocking=({
                "depth": 0,
                "state": self.state_desc(start),
                "reasons": tuple(p.describe() for p in initial_problems),
            },))

        counter = 0
        queue = [((0, 0), 0, start, ())]
        best = {start.key(): (0, 0)}
        visited = {}
        while queue:
            cost, _, state, path = heapq.heappop(queue)
            if best.get(state.key()) != cost:
                continue
            visited[state.key()] = (cost, state)
            if self.is_goal(state):
                return PlanResult(ok=True, steps=path,
                                  borrowed=tuple(sorted(state.used)))
            for info, problems in self.moves(state):
                if problems:
                    continue
                used = info.get("used", state.used)
                new_state = _State(info["currents"], info["pending"], used)
                new_cost = (len(used), cost[1] + 1)
                if new_cost < best.get(new_state.key(),
                                       (float("inf"),) * 2):
                    best[new_state.key()] = new_cost
                    counter += 1
                    step = self._make_step(state, info, len(path) + 1)
                    heapq.heappush(
                        queue, (new_cost, counter, new_state, path + (step,)))
        return PlanResult(ok=False, blocking=self._blocking(visited))

    def _make_step(self, state, info, index):
        switch = info.get("switch")
        seg, retiring, target = switch if switch else ("", "", "")
        ext_code = self.seg[seg].tables[target].ext_code if seg else 0
        before = self.required_handlers(
            self.inflight(state.currents, state.pending))
        after = self.required_handlers(
            self.inflight(info["currents"], info["pending"]))
        enables = []
        disables = []
        for reader in self.ho.readers:
            name = reader.name
            for code in sorted(after[name] - before[name]):
                if code not in reader.eh_initial:
                    enables.append((name, code))
            for code in sorted(before[name] - after[name]):
                disables.append((name, code))
        return Step(index=index, segment=seg, from_table=retiring,
                    to_table=target, ext_code=ext_code,
                    enables=tuple(enables), disables=tuple(disables),
                    awaits=info["awaits"], cleared=info["cleared"],
                    returned=info["returned"])

    def _blocking(self, visited):
        """Explain why the search cannot reach the final tables."""
        dead = []
        for cost, state in visited.values():
            reasons = []
            has_move = False
            for info, problems in self.moves(state):
                if problems:
                    reasons.extend(p.describe() for p in problems)
                else:
                    has_move = True
            if not has_move:
                dead.append((cost[1], self.state_desc(state),
                             tuple(sorted(set(reasons)))))
        if not dead:
            # every reachable state has some valid move, yet no goal was
            # found: report the states closest to the goal instead
            def remaining(item):
                cost, state = item
                left = sum(1 for i, s in enumerate(self.segs)
                           if state.currents[i] != self.seg[s].final)
                return (left, cost[1])
            cost, state = min(visited.values(), key=remaining)
            return ({"depth": cost[1], "state": self.state_desc(state),
                     "reasons": ("search space exhausted without reaching "
                                 "the final tables",)},)
        dead.sort(key=lambda d: d[0])
        earliest = dead[0][0]
        return tuple({"depth": depth, "state": desc, "reasons": reasons}
                     for depth, desc, reasons in dead if depth == earliest)


def plan(handover):
    """Compute an executable switch sequence for a handover sheet."""
    return _Planner(handover).run()
