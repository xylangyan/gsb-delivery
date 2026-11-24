"""Recovery of merged HyperLogLog registers from archived summaries.

A collector switch can leave shards (fixed sets of events) archived at
different HyperLogLog precisions. An :class:`ArchivedSummary` is one archived
register vector over a known set of shards, computed with a known hash
version. The register of a summary is the *exact* merged register of the
union of its shards. Summaries may cover several shards at once and different
summaries may overlap on shards; the underlying events are gone, so only the
register observations remain.

This module answers: for a requested set of shards, what is the register of
the union sketch at the highest precision the observations determine
uniquely? Two archive platters built from the same shard set impose the same
constraints, so the answer never depends on summary ordering or grouping.

Only precisions 4 and 5 (the precisions left by the collector switch) are
supported. Summaries using different hash versions are never mixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Hashable, List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "ArchiveConflict",
    "ArchiveConflictError",
    "ArchivedSummary",
    "RecoveryResult",
    "detect_conflicts",
    "downgrade_p5_to_p4",
    "lift_p4_to_p5",
    "realizing_hashes",
    "recover_merged_registers",
]

# Supported precisions and the rank range each register can take. A rank of 0
# means the bucket never fired; the maximum rank is max_rank + 1 and only
# occurs when no bits remain after the index bits (e.g. hash value 0).
_P4 = 4
_P5 = 5
_M4 = 1 << _P4
_M5 = 1 << _P5
_MAX_RANK4 = 32 - _P4  # 28; p=4 register values span 0..29.
_MAX_RANK5 = 32 - _P5  # 27; p=5 register values span 0..28.
_MAX_REG4 = _MAX_RANK4 + 1
_MAX_REG5 = _MAX_RANK5 + 1


def _as_reg(reg: Sequence[int], p: int) -> np.ndarray:
    """Validate a register vector and return an int8 copy."""
    size = 1 << p
    arr = np.asarray(list(reg), dtype=np.int8)
    if arr.shape != (size,):
        raise ValueError("p=%d summary needs %d registers, got %d" % (p, size, arr.size))
    max_rank = _MAX_REG4 if p == _P4 else _MAX_REG5
    if np.any(arr < 0) or np.any(arr > max_rank):
        raise ValueError("p=%d register ranks must be in range [0, %d]" % (p, max_rank))
    return arr


def _low_half_downgrade(rank5: int) -> int:
    """Downgrade rank of a p=5 low-half bucket to its p=4 rank."""
    return _MAX_REG4 if rank5 == _MAX_REG5 else rank5


def _extreme_pairs(pairs: Sequence[Tuple[int, int]], low_first: bool) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Pick two feasible pairs minimizing/maximizing one half's rank."""
    if low_first:
        first = min(pairs, key=lambda pair: pair[0])
        second = max(pairs, key=lambda pair: pair[0])
    else:
        first = min(pairs, key=lambda pair: pair[1])
        second = max(pairs, key=lambda pair: pair[1])
    return first, second


def downgrade_p5_to_p4(reg5: np.ndarray) -> np.ndarray:
    """Downgrade a p=5 register vector to the p=4 vector of the same data.

    A p=4 bucket ``j`` is the union of p=5 buckets ``j`` (low half, index bit
    0) and ``j + 16`` (high half, index bit 1). Ranks are unchanged by the
    extra index bit except when the p=5 low-half bucket used every remaining
    hash bit (rank 28): the same hash has zero remaining bits at p=4 and thus
    rank 29. The high half never hits that corner.
    """
    reg5 = np.asarray(reg5, dtype=np.int8)
    if reg5.shape != (_M5,):
        raise ValueError("expected a p=5 register vector of length %d" % _M5)
    low = reg5[:_M4].copy()
    low[low == _MAX_REG5] = _MAX_REG4
    return np.maximum(low, reg5[_M4:]).astype(np.int8)


def lift_p4_to_p5(reg4: np.ndarray) -> np.ndarray:
    """Pointwise lower bound on any p=5 vector consistent with a p=4 vector.

    A p=4 rank could have come from either p=5 half, so both halves inherit
    the rank as a lower bound. The p=4-only corner rank 29 maps to the
    maximum feasible p=5 rank, 28, in the low half (the only half that can
    produce it); ranks are otherwise copied. Note this is only a lower bound:
    for p=4 rank 28 the low half is in fact capped at 27 and the rank must
    come from the high half.
    """
    reg4 = np.asarray(reg4, dtype=np.int8)
    if reg4.shape != (_M4,):
        raise ValueError("expected a p=4 register vector of length %d" % _M4)
    low = reg4.copy()
    low[low == _MAX_REG4] = _MAX_REG5
    high = np.minimum(reg4, _MAX_REG5)
    return np.concatenate([low, high]).astype(np.int8)


def _low_half_cap(rank4: int) -> int:
    """Largest p=5 low-half rank allowed by a p=4 bucket rank."""
    if rank4 == _MAX_REG4:
        return _MAX_REG5
    if rank4 == _MAX_RANK4:
        return _MAX_RANK4 - 1
    return rank4


def _high_half_cap(rank4: int) -> int:
    """Largest p=5 high-half rank allowed by a p=4 bucket rank."""
    return min(rank4, _MAX_REG5)


def realizing_hashes(reg: np.ndarray, p: int) -> List[int]:
    """Return 32-bit hash values whose sketch has exactly ``reg``.

    Every feasible register vector is realizable: one hash per non-zero
    bucket, placed in that bucket with the recorded rank. This lets callers
    turn a witness register vector into a concrete input interpretation and
    double-check it with :class:`datasketch.HyperLogLog`.
    """
    reg = _as_reg(reg, p)
    max_rank = 32 - p
    hashes: List[int] = []
    for index_value, rank_value in enumerate(reg):
        rank = int(rank_value)
        index = int(index_value)
        if rank == 0:
            continue
        # No bits remain after the index bits: the hash equals the index.
        remaining = 0 if rank > max_rank else 1 << (max_rank - rank)
        hashes.append((remaining << p) | index)
    return hashes


@dataclass(frozen=True)
class ArchivedSummary:
    """One archived HyperLogLog register vector.

    Args:
        summary_id: A stable identifier used in conflict reports.
        shards: The shards covered by this summary; order is irrelevant.
        p: The precision, either 4 or 5.
        reg: The exact merged register vector (16 values for p=4, 32 for
            p=5) over the union of ``shards``.
        hash_version: Tag of the hash function version used to build it.

    """

    summary_id: Hashable
    shards: FrozenSet[Hashable]
    p: int
    reg: np.ndarray
    hash_version: Hashable

    def __init__(
        self,
        summary_id: Hashable,
        shards: Sequence[Hashable] | FrozenSet[Hashable],
        p: int,
        reg: Sequence[int],
        hash_version: Hashable,
    ) -> None:
        if p not in (_P4, _P5):
            raise ValueError("only precisions 4 and 5 are supported, got p=%d" % p)
        object.__setattr__(self, "summary_id", summary_id)
        object.__setattr__(self, "shards", frozenset(shards))
        object.__setattr__(self, "p", p)
        object.__setattr__(self, "reg", _as_reg(reg, p))
        object.__setattr__(self, "hash_version", hash_version)

    def __repr__(self) -> str:
        return "ArchivedSummary(id=%r, p=%d, shards=%r, hash_version=%r)" % (
            self.summary_id,
            self.p,
            set(self.shards),
            self.hash_version,
        )


@dataclass(frozen=True)
class ArchiveConflict:
    """An observation that no single input can satisfy.

    Attributes:
        summary: The summary whose rank cannot be attained under the bounds
            implied by the other observations.
        register_index: Offending register index in ``summary.reg`` (p=4
            bucket index for a p=4 summary).
        observed_rank: Rank recorded by ``summary``.
        attainable_rank: Largest rank the other observations allow there.
        reason: Human-readable explanation of the conflict.

    """

    summary: ArchivedSummary
    register_index: int
    observed_rank: int
    attainable_rank: int
    reason: str


class ArchiveConflictError(ValueError):
    """Raised when archived observations contradict each other."""

    def __init__(self, conflicts: Sequence[ArchiveConflict]):
        self.conflicts = list(conflicts)
        ids = sorted({str(c.summary.summary_id) for c in conflicts})
        super().__init__("conflicting archived observations in summaries: %s" % ", ".join(ids))


@dataclass(frozen=True)
class RecoveryResult:
    """The recoverable merged register for a set of shards.

    Attributes:
        precision: Precision of ``reg`` (the requested one).
        reg: Recovered register vector. At p=5 an ambiguous register holds
            the smallest rank shared by every consistent input; at p=4 it is
            exact wherever ``fixed_mask`` is True.
        fixed_mask: Boolean mask, True where the register is uniquely
            determined by the observations.
        shards: Requested shard set.
        hash_version: Hash version used for the recovery.
        conflicts: Conflicting observations when recovery was forced via
            ``on_conflict="report"``; empty otherwise.

    """

    precision: int
    reg: np.ndarray
    fixed_mask: np.ndarray
    shards: FrozenSet[Hashable]
    hash_version: Hashable
    conflicts: Tuple[ArchiveConflict, ...] = ()
    _model: Optional["_ConstraintModel"] = None
    _ordered_shards: Tuple[Hashable, ...] = ()

    @property
    def determinable_precision(self) -> Optional[int]:
        """Highest precision at which every register is determined.

        Returns 5 when all p=5 cells are fixed, 4 when all p=4 buckets are
        fixed (but some p=5 cell is ambiguous), and ``None`` when even the
        p=4 register leaves ambiguity.
        """
        if self.precision == _P5 and bool(np.all(self.fixed_mask)):
            return _P5
        if not self._p4_buckets_fixed():
            return None
        return _P4

    def _p4_buckets_fixed(self) -> bool:
        if self.precision == _P4:
            return bool(np.all(self.fixed_mask))
        if self._model is None:
            return False
        shards = frozenset(self._ordered_shards)
        for bucket in range(_M4):
            pairs = self._model.feasible_pairs(bucket, shards)
            if not pairs:
                return False
            downgraded = [max(_low_half_downgrade(low_rank), high_rank) for low_rank, high_rank in pairs]
            if min(downgraded) != max(downgraded):
                return False
        return True

    @property
    def ambiguous_registers(self) -> Tuple[int, ...]:
        """Register indices that more than one input can explain."""
        return tuple(int(i) for i in np.nonzero(~self.fixed_mask)[0])

    def is_register_fixed(self, index: int) -> bool:
        """Return whether register ``index`` has a unique value."""
        return bool(self.fixed_mask[index])

    def witnesses(self, index: int) -> Tuple[np.ndarray, np.ndarray]:
        """Two p=5 register vectors consistent with every observation.

        Both vectors satisfy all archived summaries and downgrade to the same
        p=4 register, but differ at ``index``. Raises when the register is
        uniquely determined or the result precision is 4.
        """
        if self.precision != _P5:
            raise ValueError("witnesses are only available for p=5 recovery results")
        if self.fixed_mask[index]:
            raise ValueError("register %d is uniquely determined" % index)
        bucket = index if index < _M4 else index - _M4
        low_first = index < _M4
        pairs = self._model.feasible_pairs(bucket, frozenset(self._ordered_shards))
        if not pairs:
            raise ValueError("register %d has no feasible input because the observations conflict" % index)
        first, second = _extreme_pairs(pairs, low_first)
        return self._register_from_pair(bucket, first), self._register_from_pair(bucket, second)

    def witness_inputs(self, index: int) -> Tuple[Dict[Hashable, List[int]], Dict[Hashable, List[int]]]:
        """Two concrete input interpretations (32-bit hash values per shard).

        Each interpretation realizes every archived summary exactly, and the
        two give different merged ranks at p=5 register ``index`` while
        sharing the same p=4 register.
        """
        if self.precision != _P5 or self._model is None:
            raise ValueError("witness inputs are only available for p=5 recovery results")
        bucket = index if index < _M4 else index - _M4
        low_first = index < _M4
        pairs = self._model.feasible_pairs(bucket, frozenset(self._ordered_shards))
        if not pairs:
            raise ValueError("register %d has no feasible input because the observations conflict" % index)
        first, second = _extreme_pairs(pairs, low_first)
        return self._build_inputs(bucket, first), self._build_inputs(bucket, second)

    def _register_from_pair(self, bucket: int, pair: Tuple[int, int]) -> np.ndarray:
        reg = self.reg.copy()
        reg[bucket] = pair[0]
        reg[bucket + _M4] = pair[1]
        return reg

    def _build_inputs(self, changed_bucket: int, changed_pair: Tuple[int, int]) -> Dict[Hashable, List[int]]:
        """Assign per-shard p=5 registers realizing all contained summaries."""
        model = self._model
        if model is None:
            raise ValueError("witness inputs are not available for conflicting observations")
        requested = frozenset(self._ordered_shards)
        shard_regs: Dict[Hashable, np.ndarray] = {shard: np.zeros(_M5, dtype=np.int8) for shard in requested}
        for bucket in range(_M4):
            if bucket == changed_bucket:
                target_low, target_high = changed_pair
            else:
                pairs = model.feasible_pairs(bucket, requested)
                target_low, target_high = pairs[0] if pairs else (0, 0)
            assignment = model.assign_bucket(bucket, target_low, target_high, requested)
            for shard, (low_rank, high_rank) in assignment.items():
                shard_regs[shard][bucket] = low_rank
                shard_regs[shard][bucket + _M4] = high_rank
        return {shard: realizing_hashes(reg, _P5) for shard, reg in shard_regs.items()}


class _BucketFacts:
    """Precomputed, O(1)-queryable facts for one p=4 bucket of one shard."""

    __slots__ = (
        "high_cap",
        "high_floor",
        "low_cap",
        "low_floor",
        "pin",
        "pin_via_high",
        "pin_via_low",
    )

    def __init__(self, low_cap: int, high_cap: int):
        self.low_cap = low_cap
        self.high_cap = high_cap
        self.low_floor = 0
        self.high_floor = 0
        # p=4 rank this shard is uniquely forced to attain, and which halves
        # can realize that rank under the caps.
        self.pin = 0
        self.pin_via_low = False
        self.pin_via_high = False

    def set_pin(self, rank: int) -> None:
        self.pin = max(self.pin, rank)
        self.pin_via_low = any(
            _low_half_downgrade(value) >= self.pin for value in range(self.low_floor, self.low_cap + 1)
        )
        self.pin_via_high = self.high_cap >= self.pin

    def pin_ok(self, low_rank: int, high_rank: int) -> bool:
        if self.pin == 0:
            return True
        low_ok = self.pin_via_low and _low_half_downgrade(low_rank) >= self.pin
        high_ok = self.pin_via_high and high_rank >= self.pin
        return low_ok or high_ok

    def any_pin_ok(self, low_domain: range, high_domain: range) -> bool:
        if self.pin == 0:
            return True
        if self.pin_via_low and any(_low_half_downgrade(value) >= self.pin for value in low_domain):
            return True
        return self.pin_via_high and any(value >= self.pin for value in high_domain)


class _ConstraintModel:
    """Max-equality constraint system for one hash version."""

    def __init__(self, summaries: Sequence[ArchivedSummary]):
        self.summaries = list(summaries)
        self.shards: FrozenSet[Hashable] = frozenset(shard for summary in self.summaries for shard in summary.shards)
        self.facts: Dict[Hashable, List[_BucketFacts]] = {
            shard: [_BucketFacts(_MAX_REG5, _MAX_REG5) for _ in range(_M4)] for shard in self.shards
        }
        self.conflicts: List[ArchiveConflict] = []

    # -- Build the cap/floor/pin facts ------------------------------------

    def build(self) -> None:
        for summary in self.summaries:
            self._apply_caps(summary)
        for summary in self.summaries:
            if summary.p == _P5:
                self._attain_p5(summary)
            else:
                self._attain_p4(summary)

    def _apply_caps(self, summary: ArchivedSummary) -> None:
        if summary.p == _P5:
            for shard in summary.shards:
                facts = self.facts[shard]
                for bucket in range(_M4):
                    facts[bucket].low_cap = min(facts[bucket].low_cap, int(summary.reg[bucket]))
                    facts[bucket].high_cap = min(facts[bucket].high_cap, int(summary.reg[bucket + _M4]))
            return
        for shard in summary.shards:
            facts = self.facts[shard]
            for bucket, rank in enumerate(summary.reg):
                rank = int(rank)
                facts[bucket].low_cap = min(facts[bucket].low_cap, _low_half_cap(rank))
                facts[bucket].high_cap = min(facts[bucket].high_cap, _high_half_cap(rank))

    def _attain_p5(self, summary: ArchivedSummary) -> None:
        for bucket in range(_M4):
            self._attain_p5_half(summary, bucket, int(summary.reg[bucket]), low_half=True)
            self._attain_p5_half(summary, bucket, int(summary.reg[bucket + _M4]), low_half=False)

    def _attain_p5_half(self, summary: ArchivedSummary, bucket: int, observed: int, low_half: bool) -> None:
        if observed == 0:
            return
        cap_attr = "low_cap" if low_half else "high_cap"
        floor_attr = "low_floor" if low_half else "high_floor"
        attainers = [shard for shard in summary.shards if getattr(self.facts[shard][bucket], cap_attr) >= observed]
        if not attainers:
            best = max(getattr(self.facts[shard][bucket], cap_attr) for shard in summary.shards)
            index = bucket if low_half else bucket + _M4
            self.conflicts.append(
                ArchiveConflict(
                    summary,
                    index,
                    observed,
                    best,
                    "p=5 rank %d cannot be attained: every covered shard is capped at %d by "
                    "other observations" % (observed, best),
                )
            )
            return
        if len(attainers) == 1:
            facts = self.facts[attainers[0]][bucket]
            setattr(facts, floor_attr, max(getattr(facts, floor_attr), observed))

    def _attain_p4(self, summary: ArchivedSummary) -> None:
        for bucket in range(_M4):
            observed = int(summary.reg[bucket])
            if observed == 0:
                continue
            attainers = [shard for shard in summary.shards if self._shard_can_attain_p4(shard, bucket, observed)]
            if not attainers:
                best = max(self._shard_max_p4(shard, bucket) for shard in summary.shards)
                self.conflicts.append(
                    ArchiveConflict(
                        summary,
                        bucket,
                        observed,
                        best,
                        "p=4 rank %d cannot be attained: every covered shard is capped at %d by "
                        "other observations" % (observed, best),
                    )
                )
                continue
            if len(attainers) == 1:
                self.facts[attainers[0]][bucket].set_pin(observed)

    def _shard_can_attain_p4(self, shard: Hashable, bucket: int, rank: int) -> bool:
        facts = self.facts[shard][bucket]
        via_low = any(_low_half_downgrade(value) >= rank for value in range(facts.low_floor, facts.low_cap + 1))
        via_high = facts.high_cap >= rank and facts.high_floor <= rank
        return via_low or via_high

    def _shard_max_p4(self, shard: Hashable, bucket: int) -> int:
        facts = self.facts[shard][bucket]
        return max(_low_half_downgrade(facts.low_cap), facts.high_cap)

    # -- Feasibility of merged pairs over a requested shard set -----------

    def feasible_pairs(self, bucket: int, shards: FrozenSet[Hashable]) -> Tuple[Tuple[int, int], ...]:
        """All feasible merged (low-half, high-half) rank pairs for a bucket."""
        facts = {shard: self.facts[shard][bucket] for shard in shards}
        min_low = max(item.low_floor for item in facts.values())
        min_high = max(item.high_floor for item in facts.values())
        max_low = max(item.low_cap for item in facts.values())
        max_high = max(item.high_cap for item in facts.values())
        contained = [summary for summary in self.summaries if summary.shards and summary.shards.issubset(shards)]
        feasible: List[Tuple[int, int]] = []
        for target_low in range(min_low, max_low + 1):
            for target_high in range(min_high, max_high + 1):
                if self._pair_feasible(bucket, target_low, target_high, facts, contained):
                    feasible.append((target_low, target_high))
        return tuple(feasible)

    def _pair_feasible(
        self,
        bucket: int,
        target_low: int,
        target_high: int,
        facts: Dict[Hashable, _BucketFacts],
        contained: Sequence[ArchivedSummary],
    ) -> bool:
        domains = self._domains(target_low, target_high, facts)
        for low_domain, high_domain in domains.values():
            if not low_domain or not high_domain:
                return False
        for item, (low_domain, high_domain) in zip(facts.values(), domains.values()):
            if not item.any_pin_ok(low_domain, high_domain):
                return False
        for summary in contained:
            if summary.p == _P5:
                if not self._p5_summary_feasible(summary, bucket, target_low, target_high, facts, domains):
                    return False
            elif not self._p4_summary_feasible(summary, bucket, facts, domains):
                return False
        return True

    @staticmethod
    def _domains(
        target_low: int,
        target_high: int,
        facts: Dict[Hashable, _BucketFacts],
    ) -> Dict[Hashable, Tuple[range, range]]:
        return {
            shard: (
                range(item.low_floor, min(item.low_cap, target_low) + 1),
                range(item.high_floor, min(item.high_cap, target_high) + 1),
            )
            for shard, item in facts.items()
        }

    def _p5_summary_feasible(
        self,
        summary: ArchivedSummary,
        bucket: int,
        target_low: int,
        target_high: int,
        facts: Dict[Hashable, _BucketFacts],
        domains: Dict[Hashable, Tuple[range, range]],
    ) -> bool:
        low_rank = int(summary.reg[bucket])
        high_rank = int(summary.reg[bucket + _M4])
        if low_rank > target_low or high_rank > target_high:
            return False
        low_ok = low_rank == 0 or any(
            low_rank in domains[shard][0] and facts[shard].any_pin_ok(domains[shard][0], domains[shard][1])
            for shard in summary.shards
        )
        high_ok = high_rank == 0 or any(
            high_rank in domains[shard][1] and facts[shard].any_pin_ok(domains[shard][0], domains[shard][1])
            for shard in summary.shards
        )
        return low_ok and high_ok

    def _p4_summary_feasible(
        self,
        summary: ArchivedSummary,
        bucket: int,
        facts: Dict[Hashable, _BucketFacts],
        domains: Dict[Hashable, Tuple[range, range]],
    ) -> bool:
        rank = int(summary.reg[bucket])
        if rank == 0:
            return True
        for shard in summary.shards:
            item = facts[shard]
            low_domain, high_domain = domains[shard]
            if any(
                item.pin_ok(low_rank, high_rank) and (_low_half_downgrade(low_rank) == rank or high_rank == rank)
                for low_rank in low_domain
                for high_rank in high_domain
            ):
                return True
        return False

    # -- Concrete per-shard assignment for a feasible pair ----------------

    def assign_bucket(
        self,
        bucket: int,
        target_low: int,
        target_high: int,
        shards: FrozenSet[Hashable],
    ) -> Dict[Hashable, Tuple[int, int]]:
        """Build concrete per-shard (low, high) ranks for a feasible pair."""
        assignment: Dict[Hashable, Tuple[int, int]] = {}
        for shard in shards:
            item = self.facts[shard][bucket]
            if item.pin_ok(0, 0) and item.low_floor == 0 and item.high_floor == 0:
                assignment[shard] = (0, 0)
            else:
                assignment[shard] = self._initial_pair(item, target_low, target_high)
        for summary in self.summaries:
            if not summary.shards or not summary.shards.issubset(shards):
                continue
            if summary.p == _P5:
                self._attain_assignment_p5(summary, bucket, assignment, target_low, target_high)
            else:
                self._attain_assignment_p4(summary, bucket, assignment, target_low, target_high)
        self._hit_target(bucket, target_low, target_high, shards, assignment)
        return assignment

    @staticmethod
    def _initial_pair(item: "_BucketFacts", target_low: int, target_high: int) -> Tuple[int, int]:
        low_rank, high_rank = item.low_floor, item.high_floor
        if item.pin_ok(low_rank, high_rank):
            return low_rank, high_rank
        return _ConstraintModel._satisfy_pin(item, low_rank, high_rank, target_low, target_high)

    @staticmethod
    def _satisfy_pin(
        item: "_BucketFacts", low_rank: int, high_rank: int, target_low: int, target_high: int
    ) -> Tuple[int, int]:
        for candidate_low in range(low_rank, min(item.low_cap, target_low) + 1):
            if item.pin_via_low and _low_half_downgrade(candidate_low) >= item.pin:
                return candidate_low, high_rank
        candidate_high = max(high_rank, min(item.pin, target_high))
        if item.pin_via_high and candidate_high >= item.pin:
            return low_rank, candidate_high
        # The pair was declared feasible, so one route must exist.
        for candidate_low in range(low_rank, min(item.low_cap, target_low) + 1):
            for candidate_high in range(high_rank, min(item.high_cap, target_high) + 1):
                if item.pin_ok(candidate_low, candidate_high):
                    return candidate_low, candidate_high
        raise RuntimeError("feasible pair could not be realized")  # pragma: no cover

    def _attain_assignment_p5(
        self,
        summary: ArchivedSummary,
        bucket: int,
        assignment: Dict[Hashable, Tuple[int, int]],
        target_low: int,
        target_high: int,
    ) -> None:
        low_rank = int(summary.reg[bucket])
        high_rank = int(summary.reg[bucket + _M4])
        covered = list(summary.shards)
        if low_rank > 0 and all(assignment[shard][0] < low_rank for shard in covered):
            shard = next(shard for shard in covered if self.facts[shard][bucket].low_cap >= low_rank)
            _, high = assignment[shard]
            assignment[shard] = (low_rank, high)
            self._repair_pin(bucket, shard, target_low, target_high, assignment)
        if high_rank > 0 and all(assignment[shard][1] < high_rank for shard in covered):
            shard = next(shard for shard in covered if self.facts[shard][bucket].high_cap >= high_rank)
            low, _ = assignment[shard]
            assignment[shard] = (low, high_rank)
            self._repair_pin(bucket, shard, target_low, target_high, assignment)

    def _repair_pin(
        self,
        bucket: int,
        shard: Hashable,
        target_low: int,
        target_high: int,
        assignment: Dict[Hashable, Tuple[int, int]],
    ) -> None:
        item = self.facts[shard][bucket]
        low_rank, high_rank = assignment[shard]
        if item.pin_ok(low_rank, high_rank):
            return
        assignment[shard] = self._satisfy_pin(item, low_rank, high_rank, target_low, target_high)

    def _attain_assignment_p4(
        self,
        summary: ArchivedSummary,
        bucket: int,
        assignment: Dict[Hashable, Tuple[int, int]],
        target_low: int,
        target_high: int,
    ) -> None:
        rank = int(summary.reg[bucket])
        if rank == 0:
            return
        covered = list(summary.shards)
        attained = any(
            _low_half_downgrade(assignment[shard][0]) == rank or assignment[shard][1] == rank for shard in covered
        )
        if attained:
            return
        # Feasibility guarantees a route exists within the target domains.
        for shard in covered:
            item = self.facts[shard][bucket]
            low, high = assignment[shard]
            if rank <= target_high and item.high_cap >= rank and item.pin_ok(low, rank):
                assignment[shard] = (low, rank)
                self._repair_pin(bucket, shard, target_low, target_high, assignment)
                return
            ceiling = min(item.low_cap, target_low)
            candidates = [value for value in range(item.low_floor, ceiling + 1) if _low_half_downgrade(value) == rank]
            if candidates:
                assignment[shard] = (candidates[0], high)
                self._repair_pin(bucket, shard, target_low, target_high, assignment)
                return

    def _hit_target(
        self,
        bucket: int,
        target_low: int,
        target_high: int,
        shards: FrozenSet[Hashable],
        assignment: Dict[Hashable, Tuple[int, int]],
    ) -> None:
        if all(low < target_low for low, _ in assignment.values()):
            shard = next(shard for shard in shards if self.facts[shard][bucket].low_cap >= target_low)
            _, high = assignment[shard]
            assignment[shard] = (target_low, high)
            self._repair_pin(bucket, shard, target_low, target_high, assignment)
        if all(high < target_high for _, high in assignment.values()):
            shard = next(shard for shard in shards if self.facts[shard][bucket].high_cap >= target_high)
            low, _ = assignment[shard]
            assignment[shard] = (low, target_high)
            self._repair_pin(bucket, shard, target_low, target_high, assignment)


def detect_conflicts(summaries: Sequence[ArchivedSummary]) -> Dict[Hashable, Tuple[ArchiveConflict, ...]]:
    """Find observations that no single input can satisfy, per hash version.

    Summaries with different hash versions are checked independently and are
    never mixed. A summary rank conflicts when the other observations cap
    every covered shard below that rank, so the recorded max cannot be
    attained by any input.
    """
    report: Dict[Hashable, Tuple[ArchiveConflict, ...]] = {}
    for hash_version in {s.hash_version for s in summaries}:
        group = [s for s in summaries if s.hash_version == hash_version]
        model = _ConstraintModel(group)
        model.build()
        report[hash_version] = tuple(model.conflicts)
    return report


def recover_merged_registers(
    summaries: Sequence[ArchivedSummary],
    shards: Sequence[Hashable] | FrozenSet[Hashable],
    precision: int = _P5,
    hash_version: Optional[Hashable] = None,
    *,
    on_conflict: str = "raise",
) -> RecoveryResult:
    """Recover the merged HyperLogLog register of a union of shards.

    Args:
        summaries: All archived summaries that may cover the requested
            shards. Summaries of other hash versions are ignored.
        shards: The shard set whose merged register is requested.
        precision: Requested precision (4 or 5). At p=5 the call still
            succeeds when some registers are not uniquely determined; inspect
            :attr:`RecoveryResult.fixed_mask` and
            :meth:`RecoveryResult.witnesses`. At p=4 the register is exact
            wherever the mask is True.
        hash_version: Hash version to use. When omitted it is inferred from
            the relevant summaries; an error is raised if more than one
            version is present.
        on_conflict: ``"raise"`` (default) raises :class:`ArchiveConflictError`
            on contradictory observations; ``"report"`` returns a result whose
            ``conflicts`` field lists them.

    Returns:
        RecoveryResult: The recovered register and its determinism mask.

    """
    if precision not in (_P4, _P5):
        raise ValueError("only precisions 4 and 5 are supported, got p=%d" % precision)
    if on_conflict not in ("raise", "report"):
        raise ValueError("on_conflict must be 'raise' or 'report'")
    requested = frozenset(shards)

    relevant = [s for s in summaries if not s.shards.isdisjoint(requested)]
    versions = {s.hash_version for s in relevant}
    if hash_version is None:
        if not versions:
            raise ValueError("no archived summary covers the requested shards")
        if len(versions) > 1:
            raise ValueError(
                "summaries use multiple hash versions %s; pass hash_version explicitly" % sorted(versions, key=str)
            )
        hash_version = next(iter(versions))
    relevant = [s for s in relevant if s.hash_version == hash_version]
    if not relevant:
        raise ValueError("no archived summary with hash version %r covers the requested shards" % (hash_version,))

    model = _ConstraintModel(relevant)
    model.build()
    conflicts = tuple(model.conflicts)
    if conflicts and on_conflict == "raise":
        raise ArchiveConflictError(conflicts)

    covered = requested.intersection(model.shards)
    if covered != requested:
        raise ValueError(
            "requested shards %s are not covered by any archived summary" % sorted(requested - covered, key=str)
        )
    ordered = tuple(sorted(covered, key=str))
    shard_set = frozenset(ordered)

    bucket_pairs = tuple(model.feasible_pairs(bucket, shard_set) for bucket in range(_M4))
    if any(not pairs for pairs in bucket_pairs) and on_conflict == "raise":
        if conflicts:
            raise ArchiveConflictError(conflicts)
        raise ArchiveConflictError(
            (ArchiveConflict(relevant[0], 0, 0, 0, "no feasible register satisfies the observations"),)
        )

    if precision == _P4:
        reg4 = np.zeros(_M4, dtype=np.int8)
        mask4 = np.ones(_M4, dtype=bool)
        for bucket, pairs in enumerate(bucket_pairs):
            if not pairs:
                mask4[bucket] = False
                continue
            downgraded = [max(_low_half_downgrade(low_rank), high_rank) for low_rank, high_rank in pairs]
            reg4[bucket] = min(downgraded)
            mask4[bucket] = min(downgraded) == max(downgraded)
        return RecoveryResult(_P4, reg4, mask4, requested, hash_version, conflicts)

    reg5 = np.zeros(_M5, dtype=np.int8)
    mask5 = np.ones(_M5, dtype=bool)
    for bucket, pairs in enumerate(bucket_pairs):
        if not pairs:
            mask5[bucket] = False
            mask5[bucket + _M4] = False
            continue
        low_values = [pair[0] for pair in pairs]
        high_values = [pair[1] for pair in pairs]
        reg5[bucket] = min(low_values)
        reg5[bucket + _M4] = min(high_values)
        mask5[bucket] = min(low_values) == max(low_values)
        mask5[bucket + _M4] = min(high_values) == max(high_values)
    return RecoveryResult(_P5, reg5, mask5, requested, hash_version, conflicts, model, ordered)
