from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class PassBand:
    modulus: int
    half: int
    intervals: tuple[tuple[int, int], ...]

    @classmethod
    def signed_threshold(cls, threshold: int, bits: int) -> "PassBand":
        modulus = 1 << bits
        half = 1 << (bits - 1)
        if threshold < -half:
            intervals = ((0, modulus - 1),)
        elif threshold >= half:
            intervals = ()
        elif threshold >= 0:
            intervals = ((threshold, half - 1),) if threshold < half else ()
        else:
            intervals = ((0, half - 1), (modulus + threshold, modulus - 1))
        return cls(modulus, half, tuple((lo, hi) for lo, hi in intervals if lo <= hi))


def floor_sum(n: int, m: int, a: int, b: int) -> int:
    ans = 0
    while True:
        if a >= m:
            ans += (n - 1) * n * (a // m) // 2
            a %= m
        if b >= m:
            ans += n * (b // m)
            b %= m
        y_max = a * n + b
        if y_max < m:
            return ans
        n = y_max // m
        b = y_max % m
        m, a = a, m


def count_mod_lt(n: int, modulus: int, step: int, base: int, cutoff: int) -> int:
    if n <= 0:
        return 0
    if cutoff <= 0:
        return 0
    if cutoff >= modulus:
        return n
    a = step % modulus
    b = base % modulus
    at_or_above_cutoff = (
        floor_sum(n, modulus, a, b + modulus - cutoff)
        - floor_sum(n, modulus, a, b)
    )
    return n - at_or_above_cutoff


def count_mod_intervals(
    n: int,
    modulus: int,
    step: int,
    base: int,
    intervals: Sequence[tuple[int, int]],
) -> int:
    return sum(
        count_mod_lt(n, modulus, step, base, hi + 1)
        - count_mod_lt(n, modulus, step, base, lo)
        for lo, hi in intervals
    )


def first_mod_hit(
    n: int,
    modulus: int,
    step: int,
    base: int,
    intervals: Sequence[tuple[int, int]],
) -> int | None:
    if n <= 0 or count_mod_intervals(n, modulus, step, base, intervals) == 0:
        return None
    lo = 0
    hi = n
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if count_mod_intervals(mid, modulus, step, base, intervals) > 0:
            hi = mid
        else:
            lo = mid
    return hi - 1


def ceil_div(value: int, divisor: int) -> int:
    return -((-value) // divisor)


def crt(residue_a: int, mod_a: int, residue_b: int, mod_b: int) -> tuple[int, int] | None:
    gcd = math.gcd(mod_a, mod_b)
    difference = residue_b - residue_a
    if difference % gcd:
        return None
    reduced_a = mod_a // gcd
    reduced_b = mod_b // gcd
    if reduced_b == 1:
        multiplier = 0
    else:
        multiplier = (difference // gcd * pow(reduced_a, -1, reduced_b)) % reduced_b
    modulus = mod_a * reduced_b
    residue = (residue_a + mod_a * multiplier) % modulus
    return residue, modulus


@dataclass(frozen=True)
class Correction:
    index: int
    shift: int
    end: int | None = None
    correction_id: str | None = None


@dataclass
class Formula:
    lo: int
    hi: int
    length: int
    first_n: tuple[int, ...]
    value: tuple[int, ...]
    step: int
    modulus: int
    source: dict[str, Any] = field(default_factory=dict)

    def residue_at(self, index: int, offset: int) -> int:
        repeat_index = (index - self.first_n[offset]) // self.length
        return (self.value[offset] + self.step * repeat_index) % self.modulus


def absolute_formula(
    block_start: int,
    absolute_value: int,
    modulus: int,
    source: dict[str, Any],
) -> Formula:
    return Formula(
        lo=block_start,
        hi=block_start + 1,
        length=1,
        first_n=(block_start,),
        value=(absolute_value % modulus,),
        step=0,
        modulus=modulus,
        source=source,
    )


def fragment_formula(
    run_lo: int,
    repeat: int,
    fragment: Sequence[int],
    anchor_value: int,
    modulus: int,
    source: dict[str, Any],
) -> Formula:
    length = len(fragment)
    if length == 0:
        raise ValueError("fragment must contain at least one delta")
    values = []
    first_n = []
    prefix = 0
    for offset, delta in enumerate(fragment):
        prefix += int(delta)
        first_n.append(run_lo + offset)
        values.append((anchor_value + prefix) % modulus)
    return Formula(
        lo=run_lo,
        hi=run_lo + repeat * length,
        length=length,
        first_n=tuple(first_n),
        value=tuple(values),
        step=sum(int(delta) for delta in fragment) % modulus,
        modulus=modulus,
        source=dict(source, repeat=repeat),
    )


@dataclass
class Channel:
    name: str
    bits: int
    formulas: list[Formula]
    corrections: list[Correction] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def modulus(self) -> int:
        return 1 << self.bits

    @property
    def half(self) -> int:
        return 1 << (self.bits - 1)

    def signed(self, residue: int) -> int:
        residue %= self.modulus
        return residue if residue < self.half else residue - self.modulus


def read_encoded_values(path: str | Path, config: dict[str, Any]) -> list[int]:
    import numpy as np

    astype = np.dtype(config["astype"])
    data = Path(path).read_bytes()
    return [int(value) for value in np.frombuffer(data, dtype=astype)]


def bits_from_config(config: dict[str, Any]) -> int:
    import numpy as np

    return np.dtype(config["dtype"]).itemsize * 8


def _overlaps(
    channel_a: Channel,
    channel_b: Channel,
    start: int,
    end: int,
):
    formulas_a = channel_a.formulas
    formulas_b = channel_b.formulas
    index_a = 0
    index_b = 0
    while index_a < len(formulas_a) and index_b < len(formulas_b):
        formula_a = formulas_a[index_a]
        formula_b = formulas_b[index_b]
        lo = max(formula_a.lo, formula_b.lo, start)
        hi = min(formula_a.hi, formula_b.hi, end)
        if lo < hi:
            yield formula_a, formula_b, lo, hi
        if formula_a.hi < formula_b.hi:
            index_a += 1
        elif formula_a.hi > formula_b.hi:
            index_b += 1
        else:
            index_a += 1
            index_b += 1


def _active_corrections(
    corrections: Sequence[Correction],
    lo: int,
) -> tuple[int, tuple[str, ...]]:
    shift = 0
    ids = []
    for correction in corrections:
        if correction.index <= lo and (
            correction.end is None or lo < correction.end
        ):
            shift += correction.shift
            if correction.correction_id is not None:
                ids.append(correction.correction_id)
    return shift, tuple(ids)


def _atomic_intervals(
    formula_a: Formula,
    formula_b: Formula,
    channel_a: Channel,
    channel_b: Channel,
    lo: int,
    hi: int,
):
    cuts = {lo, hi}
    for correction in (*channel_a.corrections, *channel_b.corrections):
        for coordinate in (correction.index, correction.end):
            if coordinate is not None and lo < coordinate < hi:
                cuts.add(coordinate)
    ordered = sorted(cuts)
    for left, right in zip(ordered, ordered[1:]):
        shift_a, ids_a = _active_corrections(channel_a.corrections, left)
        shift_b, ids_b = _active_corrections(channel_b.corrections, left)
        yield left, right, shift_a % channel_a.modulus, shift_b % channel_b.modulus, ids_a, ids_b


def _pair_affine(
    formula: Formula,
    offset: int,
    shift: int,
    residue: int,
    lcm: int,
) -> tuple[int, int]:
    first_n = formula.first_n[offset]
    t0 = (residue - first_n) // formula.length
    base = (formula.value[offset] + formula.step * t0 + shift) % formula.modulus
    step = formula.step * (lcm // formula.length) % formula.modulus
    return base, step


def _count_pair(
    k_lo: int,
    k_hi: int,
    affine_a: tuple[int, int],
    band_a: PassBand,
    affine_b: tuple[int, int],
    band_b: PassBand,
) -> int:
    base_a, step_a = affine_a
    base_b, step_b = affine_b
    period_a = band_a.modulus // math.gcd(step_a, band_a.modulus)
    period_b = band_b.modulus // math.gcd(step_b, band_b.modulus)
    if period_a <= period_b:
        enum_base, enum_step, enum_band = base_a, step_a, band_a
        other_base, other_step, other_band = base_b, step_b, band_b
        period = period_a
    else:
        enum_base, enum_step, enum_band = base_b, step_b, band_b
        other_base, other_step, other_band = base_a, step_a, band_a
        period = period_b

    total = 0
    last_delta = min(period - 1, k_hi - k_lo)
    for delta in range(last_delta + 1):
        k0 = k_lo + delta
        if not count_mod_intervals(
            1,
            enum_band.modulus,
            enum_step,
            enum_base + enum_step * k0,
            enum_band.intervals,
        ):
            continue
        count = (k_hi - k0) // period + 1
        total += count_mod_intervals(
            count,
            other_band.modulus,
            other_step * period % other_band.modulus,
            other_base + other_step * k0,
            other_band.intervals,
        )
    return total


def _first_pair(
    k_lo: int,
    k_hi: int,
    affine_a: tuple[int, int],
    band_a: PassBand,
    affine_b: tuple[int, int],
    band_b: PassBand,
    upper_bound_index: int | None = None,
    residue: int = 0,
    lcm: int = 1,
) -> int | None:
    base_a, step_a = affine_a
    base_b, step_b = affine_b
    period_a = band_a.modulus // math.gcd(step_a, band_a.modulus)
    period_b = band_b.modulus // math.gcd(step_b, band_b.modulus)
    if period_a <= period_b:
        enum_base, enum_step, enum_band = base_a, step_a, band_a
        other_base, other_step, other_band = base_b, step_b, band_b
        period = period_a
    else:
        enum_base, enum_step, enum_band = base_b, step_b, band_b
        other_base, other_step, other_band = base_a, step_a, band_a
        period = period_b

    best_k = None
    last_delta = min(period - 1, k_hi - k_lo)
    for delta in range(last_delta + 1):
        k0 = k_lo + delta
        earliest_index = residue + lcm * k0
        if upper_bound_index is not None and earliest_index >= upper_bound_index:
            break
        if best_k is not None and k0 >= best_k:
            break
        if not count_mod_intervals(
            1,
            enum_band.modulus,
            enum_step,
            enum_base + enum_step * k0,
            enum_band.intervals,
        ):
            continue
        count = (k_hi - k0) // period + 1
        hit = first_mod_hit(
            count,
            other_band.modulus,
            other_step * period % other_band.modulus,
            other_base + other_step * k0,
            other_band.intervals,
        )
        if hit is None:
            continue
        candidate_k = k0 + hit * period
        if best_k is None or candidate_k < best_k:
            best_k = candidate_k
    return best_k


def _provenance(
    formula: Formula,
    offset: int,
    index: int,
    correction_ids: tuple[str, ...],
) -> dict[str, Any]:
    repeat_index = (index - formula.first_n[offset]) // formula.length
    result = dict(formula.source)
    result.update(
        fragment_offset=offset,
        repeat_index=repeat_index,
        correction_ids=list(correction_ids),
    )
    element_base = result.get("encoded_element_base")
    if element_base is not None:
        encoded_element = int(element_base) + repeat_index * formula.length + offset
        result["encoded_element"] = encoded_element
        data_offset = result.get("data_byte_offset", 0)
        itemsize = result.get("astype_itemsize")
        if itemsize is not None:
            result["byte_offset"] = int(data_offset) + encoded_element * int(itemsize)
    return result


@dataclass
class JointResult:
    count: int
    first_index: int | None = None
    first_values: tuple[int, int] | None = None
    first_residues: tuple[int, int] | None = None
    provenance_a: dict[str, Any] | None = None
    provenance_b: dict[str, Any] | None = None


def joint_audit(
    channel_a: Channel,
    channel_b: Channel,
    start: int,
    end: int,
    threshold_a: int,
    threshold_b: int,
) -> JointResult:
    if end < start:
        raise ValueError("end must be greater than or equal to start")
    band_a = PassBand.signed_threshold(threshold_a, channel_a.bits)
    band_b = PassBand.signed_threshold(threshold_b, channel_b.bits)
    result = JointResult(count=0)

    for formula_a, formula_b, overlap_lo, overlap_hi in _overlaps(
        channel_a, channel_b, start, end + 1
    ):
        for lo, hi, shift_a, shift_b, ids_a, ids_b in _atomic_intervals(
            formula_a,
            formula_b,
            channel_a,
            channel_b,
            overlap_lo,
            overlap_hi,
        ):
            for offset_a in range(formula_a.length):
                residue_a = formula_a.first_n[offset_a] % formula_a.length
                for offset_b in range(formula_b.length):
                    residue_b = formula_b.first_n[offset_b] % formula_b.length
                    chinese_remainder = crt(
                        residue_a,
                        formula_a.length,
                        residue_b,
                        formula_b.length,
                    )
                    if chinese_remainder is None:
                        continue
                    residue, lcm = chinese_remainder
                    k_lo = ceil_div(lo - residue, lcm)
                    k_hi = (hi - 1 - residue) // lcm
                    if k_lo > k_hi:
                        continue
                    affine_a = _pair_affine(
                        formula_a, offset_a, shift_a, residue, lcm
                    )
                    affine_b = _pair_affine(
                        formula_b, offset_b, shift_b, residue, lcm
                    )
                    pair_count = _count_pair(
                        k_lo,
                        k_hi,
                        affine_a,
                        band_a,
                        affine_b,
                        band_b,
                    )
                    result.count += pair_count
                    if pair_count == 0:
                        continue
                    lower_index = residue + lcm * k_lo
                    if (
                        result.first_index is not None
                        and lower_index >= result.first_index
                    ):
                        continue
                    best_k = _first_pair(
                        k_lo,
                        k_hi,
                        affine_a,
                        band_a,
                        affine_b,
                        band_b,
                        result.first_index,
                        residue,
                        lcm,
                    )
                    if best_k is None:
                        continue
                    index = residue + lcm * best_k
                    if (
                        result.first_index is not None
                        and index >= result.first_index
                    ):
                        continue
                    residue_value_a = (
                        formula_a.residue_at(index, offset_a) + shift_a
                    ) % channel_a.modulus
                    residue_value_b = (
                        formula_b.residue_at(index, offset_b) + shift_b
                    ) % channel_b.modulus
                    result.first_index = index
                    result.first_residues = (residue_value_a, residue_value_b)
                    result.first_values = (
                        channel_a.signed(residue_value_a),
                        channel_b.signed(residue_value_b),
                    )
                    result.provenance_a = _provenance(
                        formula_a, offset_a, index, ids_a
                    )
                    result.provenance_b = _provenance(
                        formula_b, offset_b, index, ids_b
                    )
    return result


def brute_force_values(channel: Channel, start: int, end: int) -> list[int]:
    values = []
    for index in range(start, end + 1):
        formula = next(
            formula
            for formula in channel.formulas
            if formula.lo <= index < formula.hi
        )
        candidate_offset = next(
            offset
            for offset, first_n in enumerate(formula.first_n)
            if index % formula.length == first_n % formula.length
        )
        shift, _ = _active_corrections(channel.corrections, index)
        residue = (
            formula.residue_at(index, candidate_offset) + shift
        ) % channel.modulus
        values.append(channel.signed(residue))
    return values


def _selftest(seed: int = 1109) -> None:
    rng = random.Random(seed)
    for case in range(300):
        bits_a = 16
        bits_b = 32
        modulus_a = 1 << bits_a
        modulus_b = 1 << bits_b
        length_a = rng.randint(1, 7)
        length_b = rng.randint(1, 9)
        fragment_a = [rng.randint(-4000, 4000) for _ in range(length_a)]
        fragment_b = [rng.randint(-2_000_000, 2_000_000) for _ in range(length_b)]
        repeat_a = rng.randint(1, 12)
        repeat_b = rng.randint(1, 12)
        absolute_a = rng.randint(-30000, 30000) % modulus_a
        absolute_b = rng.randint(-1_000_000_000, 1_000_000_000) % modulus_b

        source_a = {"path": "instrument_a.delta.dat", "block": 0}
        source_b = {"path": "instrument_b.delta.dat", "block": 0}
        formulas_a = [absolute_formula(0, absolute_a, modulus_a, source_a)]
        formulas_b = [absolute_formula(0, absolute_b, modulus_b, source_b)]
        formulas_a.append(
            fragment_formula(
                1, repeat_a, fragment_a, absolute_a, modulus_a, source_a
            )
        )
        formulas_b.append(
            fragment_formula(
                1, repeat_b, fragment_b, absolute_b, modulus_b, source_b
            )
        )
        total_length_a = 1 + repeat_a * length_a
        total_length_b = 1 + repeat_b * length_b
        total_length = min(total_length_a, total_length_b)
        channel_a = Channel("a", bits_a, formulas_a)
        channel_b = Channel("b", bits_b, formulas_b)

        correction_index = rng.randint(1, total_length - 1)
        formula = formulas_a[1]
        relative = correction_index - 1
        correction_repeat, correction_offset = divmod(relative, length_a)
        old_delta = fragment_a[correction_offset]
        new_delta = old_delta + rng.choice([-777, -1, 1, 911])
        corrected_a = Channel(
            "a",
            bits_a,
            formulas_a,
            [
                Correction(
                    correction_index,
                    new_delta - old_delta,
                    total_length_a,
                    "CORR-1",
                )
            ],
        )

        start = rng.randint(0, max(0, total_length - 8))
        end = rng.randint(start, total_length - 1)
        threshold_a = rng.choice([-30000, -1, 0, 1000, 20000, 32767])
        threshold_b = rng.choice(
            [-1_000_000_000, -1, 0, 10_000_000, 2_000_000_000]
        )

        for use_correction in (False, True):
            active_a = corrected_a if use_correction else channel_a
            result = joint_audit(
                active_a,
                channel_b,
                start,
                end,
                threshold_a,
                threshold_b,
            )
            values_a = brute_force_values(active_a, start, end)
            values_b = brute_force_values(channel_b, start, end)
            expected_indices = [
                start + position
                for position, (value_a, value_b) in enumerate(
                    zip(values_a, values_b)
                )
                if value_a >= threshold_a and value_b >= threshold_b
            ]
            assert result.count == len(expected_indices), (
                case,
                use_correction,
                result.count,
                len(expected_indices),
            )
            if expected_indices:
                expected_first = expected_indices[0]
                assert result.first_index == expected_first
                assert result.first_values == (
                    values_a[expected_first - start],
                    values_b[expected_first - start],
                )
            else:
                assert result.first_index is None
    print("joint_delta_audit selftest passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
