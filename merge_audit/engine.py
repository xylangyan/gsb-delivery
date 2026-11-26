from __future__ import annotations

# built-in
from dataclasses import dataclass, field
from typing import Callable, Sequence

# project
import textdistance


AliasFunc = Callable[[str, str], bool]


def case_insensitive_alias(left: str, right: str) -> bool:
    """Transcription alias table: case is ignored while comparing chars."""
    return left.lower() == right.lower()


def alternating_family(length: int) -> str:
    """The aA registration pattern truncated to its real length."""
    return ('aA' * (length // 2 + 1))[:length]


@dataclass(frozen=True)
class MergeRule:
    threshold: int
    length_drift: int = 1
    alias: AliasFunc = case_insensitive_alias

    def old_scorer(self) -> textdistance.DamerauLevenshtein:
        return textdistance.DamerauLevenshtein(
            external=False, restricted=True, test_func=self.alias,
        )

    def new_scorer(self) -> textdistance.DamerauLevenshtein:
        return textdistance.DamerauLevenshtein(
            external=False, restricted=False, test_func=self.alias,
        )


@dataclass
class Entry:
    """One registration line. Duplicated short codes stay as separate entries."""

    token: str
    code: str = ''
    phase: int = 0
    lengths: tuple[int, ...] = ()
    group: str = ''

    def at_length(self, length: int) -> str:
        if length <= 0 or length == len(self.token):
            return self.token
        return self.token[:length]


@dataclass
class PairResult:
    left_index: int
    right_index: int
    left_token: str
    right_token: str
    direction: str
    restricted: int
    unrestricted: int
    old_pass: bool
    new_pass: bool
    bucket: str


@dataclass
class BandReport:
    step: int
    lengths: dict[str, int]
    counts: dict[str, int]
    first: dict[str, PairResult | None]
    pairs: list[PairResult] = field(default_factory=list)


BOTH = 'both'
OLD_ONLY = 'old_only'
NEW_ONLY = 'new_only'
NEITHER = 'neither'
BUCKETS = (BOTH, OLD_ONLY, NEW_ONLY, NEITHER)


def _direction(left: str, right: str) -> str:
    if len(left) > len(right):
        return 'left-longer'
    if len(left) < len(right):
        return 'right-longer'
    return 'equal'


def _bucket(old_pass: bool, new_pass: bool) -> str:
    if old_pass and new_pass:
        return BOTH
    if old_pass:
        return OLD_ONLY
    if new_pass:
        return NEW_ONLY
    return NEITHER


def score_pair(left: str, right: str, rule: MergeRule) -> PairResult:
    restricted = rule.old_scorer()(left, right)
    unrestricted = rule.new_scorer()(left, right)
    old_pass = restricted <= rule.threshold
    new_pass = unrestricted <= rule.threshold
    return PairResult(
        left_index=-1,
        right_index=-1,
        left_token=left,
        right_token=right,
        direction=_direction(left, right),
        restricted=restricted,
        unrestricted=unrestricted,
        old_pass=old_pass,
        new_pass=new_pass,
        bucket=_bucket(old_pass, new_pass),
    )


def _length_schedule(entries: Sequence[Entry]) -> list[dict[str, int]]:
    """Coupled lengths move together; entries without a group use real length."""
    groups: dict[str, list[int]] = {}
    for entry in entries:
        if entry.group and entry.lengths:
            groups.setdefault(entry.group, []).extend(entry.lengths)
    schedules = {
        group: sorted(set(values))
        for group, values in groups.items()
    }
    steps = max((len(values) for values in schedules.values()), default=1)
    result: list[dict[str, int]] = []
    for step in range(steps):
        assignment: dict[str, int] = {}
        for group, values in schedules.items():
            assignment[group] = values[min(step, len(values) - 1)]
        result.append(assignment)
    return result


def audit_band(entries: Sequence[Entry], rule: MergeRule) -> list[BandReport]:
    """Audit one band in stored scan order, one report per coupled length step."""
    reports: list[BandReport] = []
    for step, assignment in enumerate(_length_schedule(entries)):
        rendered = [
            entry.at_length(assignment.get(entry.group, len(entry.token)))
            for entry in entries
        ]
        counts = {bucket: 0 for bucket in BUCKETS}
        first: dict[str, PairResult | None] = {bucket: None for bucket in BUCKETS}
        pairs: list[PairResult] = []
        for left_index, right_index in _pairs_in_scan_order(entries):
            left = rendered[left_index]
            right = rendered[right_index]
            if abs(len(left) - len(right)) > rule.length_drift:
                continue
            result = score_pair(left, right, rule)
            result.left_index = left_index
            result.right_index = right_index
            counts[result.bucket] += 1
            pairs.append(result)
            if first[result.bucket] is None:
                first[result.bucket] = result
        reports.append(BandReport(
            step=step,
            lengths=dict(assignment),
            counts=counts,
            first=first,
            pairs=pairs,
        ))
    return reports


def _pairs_in_scan_order(entries: Sequence[Entry]):
    for left_index in range(len(entries)):
        for right_index in range(left_index + 1, len(entries)):
            yield left_index, right_index


def lowercased_unrestricted(left: str, right: str) -> int:
    """The wrong recipe: lowercase first, then run the unrestricted scorer."""
    scorer = textdistance.DamerauLevenshtein(external=False, restricted=False)
    return scorer(left.lower(), right.lower())


def boundary_family_pair(length: int) -> tuple[str, str]:
    """Left head ends in b, right head puts b before the final A."""
    left = alternating_family(length - 1) + 'b'
    right = alternating_family(length - 2) + 'bA'
    return left, right


def _family_suffix_pair(length: int, window: int) -> tuple[str, str]:
    """Suffix fragments of the family pair, built by parity at any length.

    For huge legal lengths the full strings must never be materialized. Only
    the last ``window`` characters matter, and their case is fixed by parity.
    """
    if length <= window + 2:
        left, right = boundary_family_pair(length)
        return left[-window:], right[-window:]
    left_fragment = ''.join(
        'a' if (length - window + index) % 2 == 0 else 'A'
        for index in range(window - 1)
    ) + 'b'
    right_fragment = ''.join(
        'a' if (length - window + index) % 2 == 0 else 'A'
        for index in range(window - 2)
    ) + 'bA'
    return left_fragment, right_fragment


@dataclass
class RangeVerdict:
    low: int
    high: int
    boundary_pairs: list[PairResult]
    spot_checks: list[PairResult]
    stable_bucket: str
    even_length_bucket: str
    odd_length_bucket: str


def audit_long_band(low: int, high: int, rule: MergeRule) -> RangeVerdict:
    """Audit a band whose legal lengths span a huge range.

    Only the terminal threshold+2 characters can differ after the shared aA
    prefix, so the distance is fixed by that suffix. Boundary fragments and a
    constant number of interior spot checks are scored; never one DP per length,
    and the full strings are never materialized.
    """
    window = rule.threshold + 2
    probe_lengths = sorted({
        low,
        low + 1,
        (low + high) // 2,
        high - 1,
        high,
    })
    probe_lengths = [value for value in probe_lengths if low <= value <= high]
    boundary_pairs: list[PairResult] = []
    spot_checks: list[PairResult] = []
    for length in probe_lengths:
        left_fragment, right_fragment = _family_suffix_pair(length, window)
        result = score_pair(left_fragment, right_fragment, rule)
        result.left_token = f'...{left_fragment}'
        result.right_token = f'...{right_fragment}'
        result.direction = f'length-{length}'
        if length in (low, high):
            boundary_pairs.append(result)
        else:
            spot_checks.append(result)
    all_results = boundary_pairs + spot_checks
    buckets = {result.bucket for result in all_results}
    stable = next(iter(buckets)) if len(buckets) == 1 else NEITHER
    parity_buckets: dict[int, set[str]] = {0: set(), 1: set()}
    for result in all_results:
        length = int(result.direction.split('-', maxsplit=1)[1])
        parity_buckets[length % 2].add(result.bucket)
    even_bucket = next(iter(parity_buckets[0])) if len(parity_buckets[0]) == 1 else NEITHER
    odd_bucket = next(iter(parity_buckets[1])) if len(parity_buckets[1]) == 1 else NEITHER
    return RangeVerdict(
        low=low,
        high=high,
        boundary_pairs=boundary_pairs,
        spot_checks=spot_checks,
        stable_bucket=stable,
        even_length_bucket=even_bucket,
        odd_length_bucket=odd_bucket,
    )


def family_parity_table(limit: int) -> list[tuple[int, str, str, int, int, int]]:
    rows = []
    for length in range(2, limit + 1):
        left, right = boundary_family_pair(length)
        restricted = MergeRule(1).old_scorer()(left, right)
        unrestricted = MergeRule(1).new_scorer()(left, right)
        lowered = lowercased_unrestricted(left, right)
        rows.append((length, left, right, restricted, unrestricted, lowered))
    return rows
