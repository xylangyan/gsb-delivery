"""Minimum-tagging solver for patience-diff reconciliation.

Comparison model
----------------
Each ledger row keeps its original text untouched.  A row may carry at most
one tag, chosen from the field values it is allowed to use.  Its comparison
key is ``(text, tag)`` (``tag=None`` when untagged), so two rows compare
equal iff their texts are equal and, whenever either side carries a tag,
both tags are equal.  A row's tag is global: the same tag is seen by every
sign-off view that contains the row.

Every view is a pair of ordered subsequences of original rows and is diffed
with the repository's own ``patiencediff.PatienceSequenceMatcher``.  A
relation (old row, new row) counts as matched exactly when the native
matching blocks pair those two rows inside that view.

The solver searches for the global tagging that

* contains every confirmed pair in the native matching blocks of each view
  in which the two rows occur together,
* keeps every forbidden pair out of those blocks,
* tags the smallest possible number of original rows.

Per-view match results are memoised on the tagged-row configuration of that
view, so shared rows across many views never trigger a full re-diff per
global tagging combination.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from patiencediff import PatienceSequenceMatcher

Pair = Tuple[str, str]
ViewName = str
Tag = str
# A row assignment maps row id -> tag; absent key means untagged.
Tagging = Dict[str, Tag]


@dataclass(frozen=True)
class View:
    name: ViewName
    old_rows: Tuple[str, ...]
    new_rows: Tuple[str, ...]


@dataclass
class Instance:
    texts: Dict[str, str]
    allowed_tags: Dict[str, FrozenSet[Tag]]
    views: Tuple[View, ...]
    confirmed: FrozenSet[Pair]
    forbidden: FrozenSet[Pair]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Instance":
        texts: Dict[str, str] = {}
        allowed: Dict[str, FrozenSet[Tag]] = {}
        for side in ("old_rows", "new_rows"):
            for row in data[side]:
                rid = row["id"]
                texts[rid] = row["text"]
                allowed[rid] = frozenset(row.get("allowed_tags", ()))
        views = tuple(
            View(
                name=v["name"],
                old_rows=tuple(v["old"]),
                new_rows=tuple(v["new"]),
            )
            for v in data["views"]
        )
        confirmed = frozenset(
            (pair[0], pair[1]) for pair in data.get("confirmed", ())
        )
        forbidden = frozenset(
            (pair[0], pair[1]) for pair in data.get("forbidden", ())
        )
        return cls(texts, allowed, views, confirmed, forbidden)


def _view_config_key(
    view: View, tagging: Tagging, members: FrozenSet[str]
) -> FrozenSet[Tuple[str, Tag]]:
    return frozenset(
        (rid, tagging[rid]) for rid in members if rid in tagging
    )


def native_pairs(
    instance: Instance, view: View, tagging: Tagging
) -> FrozenSet[Pair]:
    """Return the pairs produced by the native patience matcher."""

    def key(rid: str) -> Tuple[str, Optional[Tag]]:
        return (instance.texts[rid], tagging.get(rid))

    seq_old = [key(rid) for rid in view.old_rows]
    seq_new = [key(rid) for rid in view.new_rows]
    blocks = PatienceSequenceMatcher(
        None, seq_old, seq_new
    ).get_matching_blocks()
    pairs: Set[Pair] = set()
    for block in blocks:
        for offset in range(block.size):
            pairs.add(
                (
                    view.old_rows[block.a + offset],
                    view.new_rows[block.b + offset],
                )
            )
    return frozenset(pairs)


@dataclass
class SolveResult:
    status: str  # "optimal", "ambiguous" or "infeasible"
    minimum_tagged_rows: Optional[int]
    tags: Tagging
    view_results: Dict[ViewName, FrozenSet[Pair]]
    ambiguous_views: Dict[ViewName, List[Tuple[FrozenSet[Pair], Tagging]]]
    blockers: List[str] = field(default_factory=list)


class Solver:
    def __init__(self, instance: Instance) -> None:
        self.instance = instance
        self._view_members: Dict[ViewName, FrozenSet[str]] = {
            v.name: frozenset(v.old_rows + v.new_rows) for v in instance.views
        }
        # Relations that matter per view, restricted to rows it contains.
        self._view_must: Dict[ViewName, FrozenSet[Pair]] = {}
        self._view_ban: Dict[ViewName, FrozenSet[Pair]] = {}
        for view in instance.views:
            members = self._view_members[view.name]
            self._view_must[view.name] = frozenset(
                pair
                for pair in instance.confirmed
                if pair[0] in members and pair[1] in members
            )
            self._view_ban[view.name] = frozenset(
                pair
                for pair in instance.forbidden
                if pair[0] in members and pair[1] in members
            )
        # Every row seen by a view is a tagging candidate: tagging a row
        # that is itself outside all confirmed/forbidden relations can still
        # change patience matching elsewhere (a newly unique anchor unlocks
        # recursion, or an anchor losing uniqueness re-routes it).  Rows in
        # no view never affect anything, and rows without allowed tags are
        # fixed untagged.
        in_view: Set[str] = set()
        for view in instance.views:
            in_view.update(view.old_rows)
            in_view.update(view.new_rows)
        self._relevant_rows = tuple(
            sorted(
                row
                for row in in_view
                if self.instance.allowed_tags[row]
            )
        )
        positions = {row: i for i, row in enumerate(self._relevant_rows)}
        # Once the search has decided every taggable row of a view, that
        # view's local configuration is final and can be checked immediately
        # instead of waiting for the whole tree to reach a leaf.
        self._ready_views: Dict[int, List[View]] = {}
        for view in instance.views:
            decided = [
                positions[row]
                for row in self._view_members[view.name]
                if row in positions
            ]
            ready_index = max(decided) + 1 if decided else 0
            self._ready_views.setdefault(ready_index, []).append(view)
        # A confirmed relation is only enforceable when both rows co-occur
        # in at least one view.
        self._enforceable_confirmed = frozenset(
            pair
            for pair in instance.confirmed
            if any(
                pair[0] in view.old_rows and pair[1] in view.new_rows
                for view in instance.views
            )
        )
        # Cache of native matcher results per view and local configuration.
        self._pair_cache: Dict[
            ViewName, Dict[FrozenSet[Tuple[str, Tag]], FrozenSet[Pair]]
        ] = {v.name: {} for v in instance.views}
        # Signature (native pair set of every view) -> one example tagging.
        # Only distinct native outcomes are retained, because taggings that
        # produce identical matching blocks are equivalent for the archivist.
        self._accepted: Dict[Tuple[FrozenSet[Pair], ...], Tagging] = {}
        self._signature_cap = 1000
        self._stop = False

    def _pairs_cached(self, view: View, tagging: Tagging) -> FrozenSet[Pair]:
        members = self._view_members[view.name]
        config = _view_config_key(view, tagging, members)
        cache = self._pair_cache[view.name]
        cached = cache.get(config)
        if cached is None:
            cached = native_pairs(self.instance, view, tagging)
            cache[config] = cached
        return cached

    def _is_dead(self, tagging: Tagging) -> bool:
        """Cheap necessary-condition pruning.

        A matched pair needs equal keys ``(text, tag)``.  A row is fixed
        once it is tagged, or when it has no allowed tag at all.  Two fixed
        rows with unequal keys can never be paired, and different original
        text can never be bridged by a tag.
        """
        for old_id, new_id in self._enforceable_confirmed:
            if (
                self.instance.texts[old_id]
                != self.instance.texts[new_id]
            ):
                return True
            old_tag = tagging.get(old_id)
            new_tag = tagging.get(new_id)
            old_fixed = old_id in tagging or not self.instance.allowed_tags[
                old_id
            ]
            new_fixed = new_id in tagging or not self.instance.allowed_tags[
                new_id
            ]
            if old_fixed and new_fixed and old_tag != new_tag:
                return True
        return False

    def _accepted_tagging(self, tagging: Tagging) -> bool:
        for view in self.instance.views:
            pairs = self._pairs_cached(view, tagging)
            if not self._view_must[view.name] <= pairs:
                return False
            if pairs & self._view_ban[view.name]:
                return False
        return True

    def solve(self) -> SolveResult:
        blockers = self._hard_blockers()
        if blockers:
            return SolveResult(
                status="infeasible",
                minimum_tagged_rows=None,
                tags={},
                view_results={},
                ambiguous_views={},
                blockers=blockers,
            )

        # Iterative deepening on the number of tagged rows.  The first
        # budget that admits any feasible tagging is the minimum, so cheap
        # solutions never get buried behind a full exponential enumeration
        # of the untagged spine.
        for budget in range(len(self._relevant_rows) + 1):
            self._accepted: Dict[Tuple[FrozenSet[Pair], ...], Tagging] = {}
            self._stop = False
            self._search_budget(0, {}, budget)
            if self._accepted:
                optimal = list(self._accepted.values())
                representative = dict(sorted(optimal[0].items()))
                view_results = {
                    view.name: self._pairs_cached(view, representative)
                    for view in self.instance.views
                }
                ambiguous: Dict[
                    ViewName, List[Tuple[FrozenSet[Pair], Tagging]]
                ] = {}
                for view_index, view in enumerate(self.instance.views):
                    distinct: Dict[FrozenSet[Pair], Tagging] = {}
                    for signature, tagging in self._accepted.items():
                        pairs = signature[view_index]
                        distinct.setdefault(
                            pairs, dict(sorted(tagging.items()))
                        )
                    if len(distinct) > 1:
                        ambiguous[view.name] = [
                            (pairs, example)
                            for pairs, example in distinct.items()
                        ]
                return SolveResult(
                    status="ambiguous" if ambiguous else "optimal",
                    minimum_tagged_rows=budget,
                    tags=representative,
                    view_results=view_results,
                    ambiguous_views=ambiguous,
                )
        return SolveResult(
            status="infeasible",
            minimum_tagged_rows=None,
            tags={},
            view_results={},
            ambiguous_views={},
            blockers=self._diagnose(),
        )

    def _search_budget(
        self, index: int, tagging: Tagging, remaining: int
    ) -> None:
        if self._stop or self._is_dead(tagging):
            return
        for view in self._ready_views.get(index, ()):
            pairs = self._pairs_cached(view, tagging)
            if not self._view_must[view.name] <= pairs:
                return
            if pairs & self._view_ban[view.name]:
                return
        if index == len(self._relevant_rows):
            if self._accepted_tagging(tagging):
                signature = tuple(
                    self._pairs_cached(view, tagging)
                    for view in self.instance.views
                )
                self._accepted.setdefault(signature, dict(tagging))
                if len(self._accepted) >= self._signature_cap:
                    self._stop = True
            return
        row = self._relevant_rows[index]
        # First leave the row untagged, then spend budget on each allowed
        # field value.  Both branches share the per-view result cache.
        self._search_budget(index + 1, tagging, remaining)
        if remaining > 0:
            for option in self.instance.allowed_tags[row]:
                tagging[row] = option
                self._search_budget(index + 1, tagging, remaining - 1)
                del tagging[row]

    def _hard_blockers(self) -> List[str]:
        """Relations that no tagging can ever make match."""
        blockers: List[str] = []
        for old_id, new_id in sorted(self._enforceable_confirmed):
            if (
                self.instance.texts[old_id]
                != self.instance.texts[new_id]
            ):
                blockers.append(
                    f"confirmed pair {(old_id, new_id)} has different "
                    "original text; tags never bridge different text"
                )
        # Patience matching pairs form a strictly increasing one-to-one
        # alignment inside every view.  Confirmed pairs that cross, or that
        # give one row two partners, can never all be matched together.
        for view in self.instance.views:
            old_pos = {row: i for i, row in enumerate(view.old_rows)}
            new_pos = {row: i for i, row in enumerate(view.new_rows)}
            inside = sorted(
                (old_pos[pair[0]], new_pos[pair[1]], pair)
                for pair in self._view_must[view.name]
            )
            seen_old: Set[int] = set()
            seen_new: Set[int] = set()
            last_new = -1
            for old_index, new_index, pair in inside:
                if old_index in seen_old or new_index in seen_new:
                    blockers.append(
                        f"view {view.name!r}: confirmed pair {pair} shares "
                        "a row with another confirmed pair; matching is "
                        "one-to-one"
                    )
                seen_old.add(old_index)
                seen_new.add(new_index)
                if new_index <= last_new:
                    blockers.append(
                        f"view {view.name!r}: confirmed pair {pair} crosses "
                        "another confirmed pair; patience matching is "
                        "strictly monotonic"
                    )
                last_new = new_index
        return blockers

    def _exhaustively_feasible(self) -> bool:
        """Whether some tagging (any number of rows) satisfies this view."""
        for budget in range(len(self._relevant_rows) + 1):
            self._accepted = {}
            self._stop = False
            self._search_budget(0, {}, budget)
            if self._accepted:
                return True
        return False

    def _diagnose(self) -> List[str]:
        """Best-effort per-view diagnosis when no global tagging works."""
        blockers: List[str] = []
        for view in self.instance.views:
            sub = Instance(
                texts=self.instance.texts,
                allowed_tags=self.instance.allowed_tags,
                views=(view,),
                confirmed=self._view_must[view.name],
                forbidden=self._view_ban[view.name],
            )
            sub_solver = Solver(sub)
            hard = sub_solver._hard_blockers()
            if hard:
                blockers.extend(
                    f"view {view.name!r}: {detail}" for detail in hard
                )
                continue
            if not sub_solver._exhaustively_feasible():
                blockers.append(
                    f"view {view.name!r} admits no tagging satisfying its "
                    "confirmed/forbidden relations"
                )
        if not blockers:
            blockers.append(
                "each view is feasible alone, so the conflict is between "
                "views sharing rows: a single global tag per row cannot "
                "satisfy all views at once"
            )
        return blockers


def report(result: SolveResult, instance: Instance) -> Dict[str, Any]:
    views_payload = []
    for view in instance.views:
        matched = result.view_results.get(view.name, frozenset())
        members = set(view.old_rows) | set(view.new_rows)
        views_payload.append(
            {
                "name": view.name,
                "matching_pairs": [
                    list(pair) for pair in sorted(matched)
                ],
                "confirmed_pairs_inside": sorted(
                    list(pair) for pair in matched & instance.confirmed
                ),
                "forbidden_pairs_outside": sorted(
                    list(pair)
                    for pair in instance.forbidden
                    if pair[0] in members
                    and pair[1] in members
                    and pair not in matched
                ),
            }
        )
    payload: Dict[str, Any] = {
        "status": result.status,
        "minimum_tagged_rows": result.minimum_tagged_rows,
        "tags_by_original_row": [
            {"row": row, "tag": tag}
            for row, tag in sorted(result.tags.items())
        ],
        "views": views_payload,
        "blockers": result.blockers,
    }
    if result.ambiguous_views:
        payload["ambiguous_views"] = [
            {
                "name": view_name,
                "distinct_native_results": [
                    {
                        "matching_pairs": [list(pair) for pair in sorted(pairs)],
                        "example_tagging": [
                            {"row": row, "tag": tag}
                            for row, tag in sorted(example.items())
                        ],
                    }
                    for pairs, example in variants
                ],
            }
            for view_name, variants in sorted(result.ambiguous_views.items())
        ]
    return payload


def solve_dict(data: Dict[str, Any]) -> Tuple[SolveResult, Instance]:
    instance = Instance.from_dict(data)
    return Solver(instance).solve(), instance


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", help="path to a JSON instance file")
    parser.add_argument(
        "-o", "--output", help="optional path for the JSON report"
    )
    args = parser.parse_args(argv)
    with open(args.instance, encoding="utf-8") as handle:
        data = json.load(handle)
    result, instance = solve_dict(data)
    payload = report(result, instance)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0 if result.status != "infeasible" else 2


if __name__ == "__main__":
    raise SystemExit(main())
