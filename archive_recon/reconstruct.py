"""Reconstruct a target Whisper archive from legacy archive evidence.

Every archive level of every legacy file and every target level live in one
joint model over the original seconds.  A target reading is written only when
all feasible original series agree on both the reading and whether the second
was observed; otherwise the cell stays empty and the report says what evidence
would change it.

Evidence handling
-----------------
* Only cells whose manifest ``origin`` is ``aggregate`` constrain the original
  series.  A value written straight into an old coarse cell is reported as
  provenance and never used as proof of sampling.
* An empty surviving cell constrains the model only when the handover material
  says it came from the presence threshold.  A cell whose slot was reused by a
  later rollover is not evidence of sparsity: it becomes an unknown cell whose
  hypothetical value still has to come from the same original seconds, so the
  reused slot's current timestamp/value is never borrowed.
* Aggregation follows Whisper's real chain: each archive is built from the
  next finer archive (``higherPoints = lower_step / higher_step``), and a cell
  is stored only when at least ``ceil(xFilesFactor * children)`` children are
  present.  A present zero is a reading, never a missing marker.
"""

from . import model as entails


class Conflict(Exception):
    """Contradictory evidence; ``cells`` lists claims that cannot coexist."""

    def __init__(self, message, cells=(), detail=None):
        super().__init__(message)
        self.cells = list(cells)
        self.detail = detail


def _key(leg_id, claim):
    return (claim.level, claim.start)


class Reconstruct:
    def __init__(self, spec, selected=None):
        self.spec = spec
        self.series = spec.series
        self.selected = selected
        self.model = entails.Model()
        self.seconds = []
        self.legacy_levels = {}
        self.legacy_cells = []
        self.target_levels = []
        self.provenance = []
        self.ignored = []
        self._build()

    # -- geometry ---------------------------------------------------------
    def block_bounds(self, start, step, aggregation):
        lows, highs = [], []
        for t in range(start, start + step):
            low, high = self.series.bounds(t - self.series.start)
            lows.append(low)
            highs.append(high)
        if aggregation == "sum":
            return sum(lows), sum(highs)
        return min(lows), max(highs)

    # -- build ------------------------------------------------------------
    def _build(self):
        self._build_seconds()
        for leg in self.spec.legacy:
            self._build_legacy(leg)
        self._build_target()

    def _build_seconds(self):
        for t in range(self.series.seconds):
            low, high = self.series.bounds(t)
            second = entails.Second(self.model, "s%d" % t, low, high)
            if t in self.series.known:
                second.pin(self.series.known[t])
            elif t in self.series.missing:
                second.force_absent()
            elif t in self.series.present:
                second.force_present()
            self.seconds.append(second)

    def _selected(self, leg, claim):
        if self.selected is None:
            return True
        return _key(leg.id, claim) in self.selected.get(leg.id, ())

    def _make_levels(self, id_prefix, archives, aggregation, xff, claims, evidence):
        steps = [step for step, _ in archives]
        cells_by_level = []
        for index, step in enumerate(steps):
            low, high = self.block_bounds(self.series.start, step, aggregation)
            count = self.series.seconds // step
            level = {}
            for j in range(count):
                start = self.series.start + j * step
                claim = claims.get((step, start))
                status, problem = self._status(claim, low, high)
                if problem:
                    raise Conflict(problem, cells=[(claim, "range")])
                if index == 0:
                    children = []
                    for t in range(start, start + step):
                        second = self.seconds[t - self.series.start]
                        children.append((second.presence, second.moment))
                else:
                    child_step = steps[index - 1]
                    ratio = step // child_step
                    children = []
                    for k in range(ratio):
                        child = cells_by_level[index - 1].get(start + k * child_step)
                        if child is None:
                            raise Conflict(
                                "internal gap at level %d start %d"
                                % (child_step, start)
                            )
                        children.append((child.presence, child.moment))
                threshold = entails.presence_threshold(xff, len(children))
                cell = entails.Cell(
                    self.model,
                    "%s_%d_%d" % (id_prefix, step, start),
                    children,
                    threshold,
                    low,
                    high,
                    status=status,
                    aggregation=aggregation,
                )
                level[start] = cell
                if evidence and claim is not None:
                    self.legacy_cells.append((cell, claim, step, start))
            cells_by_level.append(level)
        return cells_by_level

    def _build_legacy(self, leg):
        claims = {}
        for claim in leg.cells:
            if self._selected(leg, claim):
                claims[(claim.level, claim.start)] = claim
        levels = self._make_levels(
            "L" + leg.id.replace(" ", "_"),
            leg.archives,
            leg.aggregationMethod,
            leg.xFilesFactor,
            claims,
            evidence=True,
        )
        self.legacy_levels[leg.id] = (leg, [s for s, _ in leg.archives], levels)
        for claim in leg.cells:
            if claim.state == "value" and claim.origin == "direct":
                self.provenance.append(
                    {
                        "legacy": leg.id,
                        "level": claim.level,
                        "start": claim.start,
                        "value": claim.value,
                        "publication": claim.publication,
                        "note": "direct write; excluded from reconstruction evidence",
                    }
                )
            elif claim.state == "lost":
                self.ignored.append(
                    {
                        "legacy": leg.id,
                        "level": claim.level,
                        "start": claim.start,
                        "reason": "rollover hole; slot not used",
                    }
                )

    def _build_target(self):
        levels = self._make_levels(
            "T",
            self.spec.target.archives,
            self.spec.target.aggregationMethod,
            self.spec.target.xFilesFactor,
            {},
            evidence=False,
        )
        self.target_levels = list(zip([s for s, _ in self.spec.target.archives], levels))

    def _status(self, claim, low, high):
        if claim is None or claim.state == "lost":
            return "free", None
        if claim.state == "empty":
            return "absent", None
        if claim.origin == "direct":
            return "free", None
        if not (low - 1e-9 <= claim.value <= high + 1e-9):
            return None, (
                "legacy cell %s has value %.12g outside the readable aggregation "
                "range [%.12g, %.12g]"
                % ((claim.level, claim.start), claim.value, low, high)
            )
        return ("value", claim.value), None

    # -- solving ----------------------------------------------------------
    def solve(self):
        result = self.model.solve()
        if not result.success:
            raise Conflict(
                "the surviving legacy evidence is jointly infeasible: %s"
                % result.message
            )
        return result

    def feasible(self):
        return self.model.feasible()

    def bounds_of(self, variable, conditional=None):
        """Return ``(low, high)`` of ``variable`` over all feasible series.

        When ``conditional`` is given the bounds are taken only over solutions
        where that binary is one, which is how value bounds for a cell that may
        or may not be present are reported.
        """
        rows = []
        if conditional is not None:
            rows.append(({conditional: 1.0}, 1.0, 1.0))
        try:
            low_result = self.model.optimise({variable: 1.0}, extra_rows=rows)
            high_result = self.model.optimise({variable: -1.0}, extra_rows=rows)
        except entails.Infeasible:
            return None
        low = self.model.value(low_result, variable)
        high = self.model.value(high_result, variable)
        return (min(low, high), max(low, high))

    def cell_for(self, level, start):
        for step, cells in self.target_levels:
            if step == level:
                return cells.get(start)
        return None


def minimal_conflict(spec):
    """Deletion-filter minimal unsatisfiable subset of surviving claims.

    Returns ``None`` when the evidence is consistent, otherwise a dict naming
    the legacy cells that cannot all hold and the reason for the first one that
    is contradictory on its own.
    """
    for leg in spec.legacy:
        for claim in leg.cells:
            if claim.state != "value" or claim.origin != "aggregate":
                continue
            try:
                Reconstruct(spec, selected={leg.id: {_key(leg.id, claim)}}).solve()
            except Conflict as error:
                return {
                    "kind": "single-cell",
                    "cells": [_describe(leg, claim)],
                    "message": str(error),
                }

    keep = {}
    for leg in spec.legacy:
        keep[leg.id] = {
            _key(leg.id, claim)
            for claim in leg.cells
            if claim.state == "value" and claim.origin == "aggregate"
        }
    all_cells = [
        (leg, claim)
        for leg in spec.legacy
        for claim in leg.cells
        if claim.state == "value" and claim.origin == "aggregate"
    ]
    for leg, claim in all_cells:
        trial = {leg_id: set(keys) for leg_id, keys in keep.items()}
        trial[leg.id].discard(_key(leg, claim))
        try:
            Reconstruct(spec, selected=trial).solve()
        except Conflict:
            keep = trial  # dropping this claim keeps the contradiction
    core = [
        (leg, claim)
        for leg, claim in all_cells
        if _key(leg, claim) in keep.get(leg.id, ())
    ]
    return {
        "kind": "minimal-core",
        "cells": [_describe(leg, claim) for leg, claim in core],
        "message": "no single surviving aggregate cell is contradictory alone",
    }


def _describe(leg, claim):
    return {
        "legacy": leg.id,
        "level": claim.level,
        "start": claim.start,
        "value": claim.value,
        "publication": claim.publication,
    }
