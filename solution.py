"""Compressed shift-report archive queries backed by ThresholdCounter semantics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from json import dumps
from math import ceil, gcd
from typing import Any, Mapping, Optional, Sequence, Union

from boltons.cacheutils import ThresholdCounter


Code = Any


def _codes(pattern: Union[str, Sequence[Code]]) -> tuple:
    if isinstance(pattern, (str, bytes)):
        return tuple(pattern.decode() if isinstance(pattern, bytes) else pattern)
    return tuple(pattern)


@dataclass(frozen=True)
class Clip:
    pattern: Sequence[Code]
    repeats: int = 1
    name: Optional[str] = None

    def __post_init__(self) -> None:
        codes = _codes(self.pattern)
        if not codes:
            raise ValueError("clip pattern must not be empty")
        if not isinstance(self.repeats, int) or self.repeats < 0:
            raise ValueError("repeats must be a non-negative integer")
        object.__setattr__(self, "pattern", codes)

    @property
    def length(self) -> int:
        return len(self.pattern) * self.repeats


@dataclass(frozen=True)
class Correction:
    value: Code
    receipt: Optional[int] = None
    clip: Optional[int] = None
    round: Optional[int] = None
    position: Optional[int] = None
    old: Optional[Code] = None

    def __post_init__(self) -> None:
        if self.receipt is None and None in (self.clip, self.round, self.position):
            raise ValueError("correction requires receipt or clip, round and position")
        if self.receipt is not None and self.receipt < 1:
            raise ValueError("receipt numbers start at 1")
        for field_name in ("clip", "round", "position"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, int) or value < (0 if field_name == "clip" else 1)):
                raise ValueError(f"invalid {field_name}")


@dataclass(frozen=True)
class Location:
    receipt: int
    clip: int
    clip_number: int
    round: int
    position: int
    clip_name: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "receipt": self.receipt,
            "clip": self.clip,
            "clip_number": self.clip_number,
            "round": self.round,
            "position": self.position,
            "clip_name": self.clip_name,
        }


@dataclass(frozen=True)
class ClearEvent:
    ordinal: int
    receipt: int
    location: Location

    def as_dict(self) -> dict:
        data = self.location.as_dict()
        data["ordinal"] = self.ordinal
        return data


class ClearReport:
    def __init__(self, code: Code, start: int, end: int, total: int, version: "ArchiveVersion"):
        self.code = code
        self.start = start
        self.end = end
        self.total = total
        self._version = version

    def __int__(self) -> int:
        return self.total

    def __len__(self) -> int:
        return self.total

    def nth(self, ordinal: int) -> ClearEvent:
        if not isinstance(ordinal, int) or ordinal < 1 or ordinal > self.total:
            raise IndexError(f"clear ordinal out of range: {ordinal}")
        before = self._version.clear_count(self.code, self.start - 1)
        receipt = self._version.receipt_for_clear_ordinal(self.code, before + ordinal)
        location = self._version.locate(receipt)
        return ClearEvent(ordinal, receipt, location)

    def first(self) -> Optional[ClearEvent]:
        return self.nth(1) if self.total else None

    def last(self) -> Optional[ClearEvent]:
        return self.nth(self.total) if self.total else None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "start": self.start,
            "end": self.end,
            "total": self.total,
        }


@dataclass
class _Segment:
    pattern: tuple
    repeats: int
    start: int

    @property
    def length(self) -> int:
        return len(self.pattern) * self.repeats

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class _BlockPhase:
    counts: Mapping[Code, int]
    first_offset: Mapping[Code, int]


@dataclass
class _BlockSpan:
    start_block: int
    block_count: int
    phases: tuple[_BlockPhase, ...]


def _step_margin(state: int, q: int) -> tuple[int, bool]:
    if state < 0:
        if q == 0:
            return -1, False
        if q == 1:
            return -1, True
        return q - 2, False
    if state == 0 and q == 0:
        return -1, True
    return state + q - 1, False


def _simulate_cycle(state: int, qs: Sequence[int]) -> tuple[int, int]:
    events = 0
    for q in qs:
        state, cleared = _step_margin(state, q)
        events += int(cleared)
    return state, events


def _advance_cycles(state: int, qs: Sequence[int], cycles: int) -> tuple[int, int]:
    if cycles <= 0:
        return state, 0
    period = len(qs)
    events = 0
    done = 0
    seen: dict[int, tuple[int, int]] = {}

    while done < cycles:
        if state in seen:
            previous_done, previous_events = seen[state]
            cycle_length = done - previous_done
            cycle_events = events - previous_events
            jumps = (cycles - done) // cycle_length
            if jumps:
                events += jumps * cycle_events
                done += jumps * cycle_length
            if done == cycles:
                return state, events
            for _ in range(cycles - done):
                state, extra = _simulate_cycle(state, qs)
                events += extra
            return state, events

        seen[state] = (done, events)
        initial_state = state
        state, extra = _simulate_cycle(state, qs)
        events += extra
        done += 1

        if extra == 0 and initial_state >= 0:
            delta = sum(qs) - period
            remaining = cycles - done
            if delta >= 0:
                state += remaining * delta
                done = cycles
            elif remaining:
                decrease = -delta
                safe_jump = max(0, (state - period) // decrease)
                safe_jump = min(safe_jump, remaining)
                state -= safe_jump * decrease
                done += safe_jump

    return state, events


def _advance_blocks(state: int, qs: Sequence[int], block_count: int) -> tuple[int, int]:
    if block_count <= 0:
        return state, 0
    full_cycles, remainder = divmod(block_count, len(qs))
    state, events = _advance_cycles(state, qs, full_cycles)
    for index in range(remainder):
        state, cleared = _step_margin(state, qs[index])
        events += int(cleared)
    return state, events


class ArchiveVersion:
    def __init__(
        self,
        clips: Sequence[Clip],
        width: int,
        segments: Sequence[_Segment],
        corrections: Sequence[Correction] = (),
    ):
        self.clips = tuple(clips)
        self.width = width
        self._segments = tuple(segments)
        self.corrections = tuple(corrections)
        self.total = sum(clip.length for clip in self.clips)
        self._spans: Optional[tuple[_BlockSpan, ...]] = None
        self._codes: Optional[tuple[Code, ...]] = None

    @property
    def codes(self) -> tuple[Code, ...]:
        if self._codes is None:
            values = set()
            for clip in self.clips:
                values.update(clip.pattern)
            for correction in self.corrections:
                values.add(correction.value)
            self._codes = tuple(values)
        return self._codes

    def locate(self, receipt: int) -> Location:
        if not isinstance(receipt, int) or not 1 <= receipt <= self.total:
            raise IndexError(f"receipt {receipt!r} is outside 1..{self.total}")
        offset = receipt - 1
        base = 0
        for index, clip in enumerate(self.clips):
            length = clip.length
            if offset < base + length:
                inside = offset - base
                pattern_length = len(clip.pattern)
                return Location(
                    receipt=receipt,
                    clip=index,
                    clip_number=index + 1,
                    round=inside // pattern_length + 1,
                    position=inside % pattern_length + 1,
                    clip_name=clip.name,
                )
            base += length
        raise AssertionError("receipt mapping failed")

    def resolve_correction(self, correction: Correction) -> Correction:
        if correction.receipt is not None:
            location = self.locate(correction.receipt)
            return Correction(
                value=correction.value,
                receipt=correction.receipt,
                clip=location.clip,
                round=location.round,
                position=location.position,
                old=correction.old,
            )
        assert correction.clip is not None
        clip = self.clips[correction.clip]
        if correction.round > clip.repeats or correction.position > len(clip.pattern):
            raise IndexError("correction coordinate is outside its clip")
        receipt = (
            sum(item.length for item in self.clips[: correction.clip])
            + (correction.round - 1) * len(clip.pattern)
            + correction.position
        )
        return Correction(
            value=correction.value,
            receipt=receipt,
            clip=correction.clip,
            round=correction.round,
            position=correction.position,
            old=correction.old,
        )

    def _segment_at(self, index: int) -> _Segment:
        for segment in self._segments:
            if segment.start <= index < segment.end:
                return segment
        if index == self.total and self._segments:
            return self._segments[-1]
        raise IndexError(f"logical index {index} is outside archive")

    def symbol_at(self, index: int) -> Code:
        segment = self._segment_at(index)
        offset = index - segment.start
        return segment.pattern[offset % len(segment.pattern)]

    def first_occurrence(self, code: Code, start: int = 0, end: Optional[int] = None) -> Optional[int]:
        end = self.total if end is None else end
        if start < 0 or end > self.total or start > end:
            raise IndexError("occurrence range is invalid")
        cursor = start
        for segment in self._segments:
            if cursor >= end:
                break
            overlap_start = max(cursor, segment.start)
            overlap_end = min(end, segment.end)
            if overlap_start >= overlap_end:
                continue
            pattern = segment.pattern
            size = len(pattern)
            relative_start = overlap_start - segment.start
            available = min(size, overlap_end - overlap_start)
            for offset in range(available):
                if pattern[(relative_start + offset) % size] == code:
                    return overlap_start + offset
        return None

    def count_occurrences(self, code: Code, start: int = 0, end: Optional[int] = None) -> int:
        end = self.total if end is None else end
        if start < 0 or end > self.total or start > end:
            raise IndexError("occurrence range is invalid")
        total = 0
        for segment in self._segments:
            overlap_start = max(start, segment.start)
            overlap_end = min(end, segment.end)
            if overlap_start >= overlap_end:
                continue
            size = len(segment.pattern)
            first = overlap_start - segment.start
            length = overlap_end - overlap_start
            full, remainder = divmod(length, size)
            total += full * segment.pattern.count(code)
            offset = first % size
            for index in range(remainder):
                if segment.pattern[(offset + index) % size] == code:
                    total += 1
        return total

    def _block_phase(self, pattern: tuple, offset: int, width: int) -> _BlockPhase:
        size = len(pattern)
        counts: Counter = Counter()
        first_offset: dict[Code, int] = {}
        phase_offset = offset % size
        for position in range(width):
            code = pattern[(phase_offset + position) % size]
            counts[code] += 1
            if code not in first_offset:
                first_offset[code] = position
        return _BlockPhase(dict(counts), first_offset)

    def _global_phase(self, block_start: int) -> _BlockPhase:
        counts: Counter = Counter()
        first_offset: dict[Code, int] = {}
        for position in range(self.width):
            code = self.symbol_at(block_start + position)
            counts[code] += 1
            if code not in first_offset:
                first_offset[code] = position
        return _BlockPhase(dict(counts), first_offset)

    def _build_spans(self) -> tuple[_BlockSpan, ...]:
        if self._spans is not None:
            return self._spans
        width = self.width
        total_blocks = self.total // width
        spans: list[_BlockSpan] = []
        block = 0

        while block < total_blocks:
            block_start = block * width
            block_end = block_start + width
            segment = self._segment_at(block_start)

            if block_end <= segment.end:
                end_block = segment.end // width
                block_count = end_block - block
                period = len(segment.pattern) // gcd(len(segment.pattern), width)
                phases = tuple(
                    self._block_phase(segment.pattern, block_start - segment.start + index * width, width)
                    for index in range(period)
                )
                spans.append(_BlockSpan(block, block_count, phases))
                block = end_block
            else:
                spans.append(_BlockSpan(block, 1, (self._global_phase(block_start),)))
                block += 1

        self._spans = tuple(spans)
        return self._spans

    def prefix_state(self, code: Code, block_count: Optional[int] = None) -> tuple[int, int]:
        spans = self._build_spans()
        total_blocks = self.total // self.width
        if block_count is None:
            block_count = total_blocks
        if not 0 <= block_count <= total_blocks:
            raise IndexError("block count is outside archive")

        state = -1
        events = 0
        used = 0
        for span in spans:
            if used >= block_count:
                break
            take = min(block_count - used, span.block_count)
            qs = tuple(phase.counts.get(code, 0) for phase in span.phases)
            if len(span.phases) == 1 and span.block_count == take:
                repeated = span.block_count
                state, extra = _advance_blocks(state, qs, repeated)
                events += extra
            else:
                state, extra = _advance_blocks(state, qs, take)
                events += extra
            used += take
        return events, state

    def clear_count(self, code: Code, end_receipt: Optional[int] = None) -> int:
        if end_receipt is None:
            end_receipt = self.total
        if end_receipt < 0 or end_receipt > self.total:
            raise IndexError("receipt prefix is outside archive")
        return self.prefix_state(code, end_receipt // self.width)[0]

    def receipt_for_clear_ordinal(self, code: Code, ordinal: int) -> int:
        total_blocks = self.total // self.width
        if ordinal < 1:
            raise IndexError("clear ordinal must be positive")
        low, high = 1, total_blocks
        while low < high:
            middle = (low + high) // 2
            count = self.prefix_state(code, middle)[0]
            if count >= ordinal:
                high = middle
            else:
                low = middle + 1
        if self.prefix_state(code, low)[0] < ordinal:
            raise IndexError("clear ordinal does not exist")
        return low * self.width

    def query_clears(self, code: Code, start: int, end: int) -> ClearReport:
        if start < 1 or end < start or end > self.total:
            raise IndexError("invalid inclusive receipt range")
        total = self.clear_count(code, end) - self.clear_count(code, start - 1)
        return ClearReport(code, start, end, total, self)

    def _boundary_counter(self, receipt: int) -> ThresholdCounter:
        if not isinstance(receipt, int) or not 0 <= receipt <= self.total:
            raise IndexError(f"receipt {receipt!r} is outside 0..{self.total}")
        width = self.width
        blocks, remainder = divmod(receipt, width)
        counter = ThresholdCounter(threshold=0.5)
        counter._thresh_count = width
        counter.total = blocks * width
        counter._cur_bucket = blocks + 1

        entries = []
        events: dict[Code, int] = {}
        states: dict[Code, int] = {}
        for code in self.codes:
            event_count, state = self.prefix_state(code, blocks)
            events[code] = event_count
            states[code] = state
            if state < 0:
                continue
            if event_count:
                clear_receipt = self.receipt_for_clear_ordinal(code, event_count)
                insertion = self.first_occurrence(code, clear_receipt, receipt)
            else:
                insertion = self.first_occurrence(code, 0, receipt)
            if insertion is None:
                continue
            count = self.count_occurrences(code, insertion, blocks * width)
            if count <= 0:
                continue
            birth = blocks + 1 + state - count
            entries.append((insertion, code, count, birth))

        entries.sort(key=lambda item: item[0])
        counter._count_map = {
            code: [count, birth]
            for _, code, count, birth in entries
        }

        block_start = blocks * width
        for offset in range(remainder):
            counter.add(self.symbol_at(block_start + offset))
        return counter

    def counter_after(self, receipt: int) -> ThresholdCounter:
        return self._boundary_counter(receipt)

    def leaderboard(self, receipt: int, number: Optional[int] = None) -> list[tuple[Code, int]]:
        return self._boundary_counter(receipt).most_common(number)

    def counts_after(self, receipt: int) -> dict[Code, int]:
        return dict(self.leaderboard(receipt))

    def query(self, receipt: int, code: Optional[Code] = None, clear_start: Optional[int] = None,
              clear_end: Optional[int] = None) -> dict:
        result = {
            "receipt": receipt,
            "leaderboard": self.leaderboard(receipt),
            "counts": self.counts_after(receipt),
        }
        if code is not None:
            clear_start = receipt if clear_start is None else clear_start
            clear_end = self.total if clear_end is None else clear_end
            report = self.query_clears(code, clear_start, clear_end)
            result["clears"] = report.as_dict()
            result["clear_report"] = report
        return result


class Archive:
    def __init__(self, clips: Optional[Sequence[Union[Clip, tuple, str]]] = None, width: int = 32,
                 threshold: Optional[float] = None):
        if isinstance(clips, int):
            requested_width = clips
            clips = None if isinstance(width, int) else width
            width = requested_width
        if threshold is not None:
            width = ceil(1 / threshold)
        if not isinstance(width, int) or width < 1:
            raise ValueError("width must be a positive integer")
        normalized: list[Clip] = []
        for clip in clips or ():
            if isinstance(clip, Clip):
                normalized.append(clip)
            elif isinstance(clip, (tuple, list)) and 2 <= len(clip) <= 3 and isinstance(clip[1], int):
                pattern = clip[0]
                repeats = clip[1]
                name = clip[2] if len(clip) > 2 else None
                normalized.append(Clip(pattern, repeats, name))
            elif isinstance(clip, (str, Sequence)):
                normalized.append(Clip(clip))
            else:
                raise ValueError(f"unsupported clip: {clip!r}")
        self.clips = tuple(normalized)
        self.width = width
        self.original = ArchiveVersion(self.clips, width, self._original_segments())

    def _original_segments(self) -> tuple[_Segment, ...]:
        segments = []
        start = 0
        for clip in self.clips:
            segments.append(_Segment(clip.pattern, clip.repeats, start))
            start += clip.length
        return tuple(segments)

    def publish(self, corrections: Optional[Sequence[Union[Correction, tuple, Mapping[str, Any]]]] = None) -> ArchiveVersion:
        correction_objects = [coerce_correction(item) for item in corrections or ()]
        resolved = [self.original.resolve_correction(item) for item in correction_objects]
        by_clip: dict[int, dict[int, dict[int, Code]]] = {}
        for correction in resolved:
            if correction.old is not None:
                actual = self.original.symbol_at(correction.receipt - 1)
                if actual != correction.old:
                    raise ValueError(f"old value mismatch at receipt {correction.receipt}")
            by_clip.setdefault(correction.clip, {}).setdefault(correction.round, {})[correction.position] = correction.value

        segments: list[_Segment] = []
        start = 0
        for clip_index, clip in enumerate(self.clips):
            size = len(clip.pattern)
            current_round = 1
            for round_number in sorted(by_clip.get(clip_index, {})):
                if round_number > current_round:
                    repeats = round_number - current_round
                    segments.append(_Segment(clip.pattern, repeats, start))
                    start += size * repeats
                modified = list(clip.pattern)
                for position, value in by_clip[clip_index][round_number].items():
                    modified[position - 1] = value
                segments.append(_Segment(tuple(modified), 1, start))
                start += size
                current_round = round_number + 1
            if current_round <= clip.repeats:
                repeats = clip.repeats - current_round + 1
                segments.append(_Segment(clip.pattern, repeats, start))
                start += size * repeats

        version = ArchiveVersion(self.clips, self.width, segments, resolved)
        return version


def coerce_correction(value: Union[Correction, tuple, Mapping[str, Any]]) -> Correction:
    if isinstance(value, Correction):
        return value
    if isinstance(value, Mapping):
        return Correction(**value)
    if isinstance(value, tuple):
        if len(value) == 2 and isinstance(value[0], int):
            return Correction(receipt=value[0], value=value[1])
        if len(value) == 4:
            clip, round_number, position, new_value = value
            return Correction(value=new_value, clip=clip, round=round_number, position=position)
    raise ValueError(f"unsupported correction: {value!r}")


def build_archive(clips: Sequence[Any], width: int = 32) -> Archive:
    return Archive(clips, width)


def solve_demo() -> dict:
    archive = Archive([Clip("aaab", 1_000_000_001), Clip("abcc", 5_000_000_006)], width=3)
    revised = archive.publish([Correction(receipt=1, value="b")])
    receipt = 24_000_000_012
    end = 24_000_000_024
    old_report = archive.original.query_clears("a", receipt, end)
    new_report = revised.query_clears("a", receipt, end)
    return {
        "old_leaderboard": archive.original.leaderboard(receipt),
        "new_leaderboard": revised.leaderboard(receipt),
        "old_clear_total": old_report.total,
        "old_clear_events": [old_report.nth(index).as_dict() for index in range(1, old_report.total + 1)],
        "new_clear_total": new_report.total,
        "new_clear_events": [new_report.nth(index).as_dict() for index in range(1, new_report.total + 1)],
        "new_third_clear_at": new_report.nth(3).receipt if new_report.total >= 3 else None,
    }


if __name__ == "__main__":
    print(dumps(solve_demo(), ensure_ascii=False, indent=2))
