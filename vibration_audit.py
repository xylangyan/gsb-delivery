"""Differential audit reports for pysimdjson ``Array.as_buffer`` windows.

The input job is JSON.  Archives may either be existing JSON files/text or
compact segment descriptions::

    {
      "archives": {
        "old": {
          "segments": [
            {
              "id": "near-n",
              "count": 8,
              "start": "9007199254740992",
              "step": "1",
              "literal": "integer"
            }
          ],
          "shape": [[8, []]]
        }
      },
      "checks": [
        {
          "id": "offset-one-d",
          "type": "d",
          "count": 8,
          "left": {"archive": "old", "offset": 0,
                   "start": "9007199254740992", "step": "1"},
          "right": {"archive": "new", "offset": 0,
                    "start": "9007199254740993", "step": "1"}
        }
      ]
    }

A shape integer is a run of reading cells in the enclosing array; a list is a
nested array.  ``[]`` therefore preserves an empty array.  JSON Pointer paths
are reconstructed for the first example in every non-empty result bucket.
"""

from __future__ import annotations

import ctypes
import html
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any

import simdjson

try:
    import numpy as _np
except Exception:
    _np = None


DTYPE_CODES = {"d", "i", "u"}
U64_MASK = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
U64_MAX = (1 << 64) - 1
SIGN_WORD = 1 << 63

_INTEGER_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")


class RawNumber(str):
    """String subclass carrying the original JSON number literal."""

    def __new__(cls, literal: str) -> "RawNumber":
        obj = str.__new__(cls, literal)
        obj.literal = literal
        return obj


def fraction_text(value: Fraction) -> str:
    decimal_value = Decimal(value.numerator) / Decimal(value.denominator)
    return format(decimal_value, "f")


def ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def floor_fraction(value: Fraction) -> int:
    return value.numerator // value.denominator


def linear_interval_within(
    start: Fraction,
    step: Fraction,
    count: int,
    lower: Fraction,
    upper: Fraction,
) -> tuple[int, int] | None:
    """Return the integer k interval for ``lower <= start+k*step <= upper``."""
    if count <= 0:
        return None
    if step == 0:
        if lower <= start <= upper:
            return 0, count
        return None

    if step > 0:
        first = ceil_fraction((lower - start) / step)
        last = floor_fraction((upper - start) / step)
    else:
        first = ceil_fraction((upper - start) / step)
        last = floor_fraction((lower - start) / step)

    first = max(0, first)
    last = min(count - 1, last)
    if first > last:
        return None
    return first, last + 1


def original_equality(start_a: Fraction, step_a: Fraction,
                      start_b: Fraction, step_b: Fraction,
                      count: int) -> tuple[str, int | None]:
    """Classify exact numeric equality in two paired arithmetic sequences."""
    intercept = start_a - start_b
    slope = step_a - step_b
    if slope == 0:
        return ("all", None) if intercept == 0 else ("none", None)

    candidate = -intercept / slope
    if candidate.denominator == 1 and 0 <= candidate.numerator < count:
        return "one", candidate.numerator
    return "none", None


def integer_double_word(value: int) -> int:
    return struct.unpack("=Q", struct.pack("=d", float(value)))[0]


def round_positive_integer_to_ulp(value: int, ulp: int) -> int:
    quotient, remainder = divmod(value, ulp)
    half = ulp // 2
    if remainder < half:
        return quotient * ulp
    if remainder > half:
        return (quotient + 1) * ulp
    if quotient % 2:
        return (quotient + 1) * ulp
    return quotient * ulp


def integer_d_equal_stats(
    start_a: int,
    step_a: int,
    start_b: int,
    step_b: int,
    count: int,
) -> dict[str, Any] | None:
    """Count equal double words for paired integer arithmetic sequences.

    The implementation splits at binary exponent boundaries and enumerates one
    modular period inside each constant-ULP band.  It therefore does not iterate
    over every cell of million-length windows.
    """
    if count <= 0:
        return {"equal": 0, "first_equal": None, "first_different": None}

    if step_a != step_b:
        difference_start = Fraction(start_a - start_b)
        difference_step = Fraction(step_a - step_b)
        nearby = linear_interval_within(
            difference_start,
            difference_step,
            count,
            Fraction(-4096),
            Fraction(4096),
        )
        if nearby is None:
            return {"equal": 0, "first_equal": None, "first_different": 0}

        near_lo, near_hi = nearby
        equal_count = 0
        first_equal = None
        first_different = 0 if near_lo > 0 else None
        for index in range(near_lo, near_hi):
            same = integer_double_word(start_a + step_a * index) == integer_double_word(
                start_b + step_b * index
            )
            if same:
                equal_count += 1
                if first_equal is None:
                    first_equal = index
            elif first_different is None:
                first_different = index

        if first_different is None and near_hi < count:
            first_different = near_hi
        return {
            "equal": equal_count,
            "first_equal": first_equal,
            "first_different": first_different,
        }

    if step_a == 0:
        same = integer_double_word(start_a) == integer_double_word(start_b)
        return {
            "equal": count if same else 0,
            "first_equal": 0 if same else None,
            "first_different": None if same else 0,
        }

    min_a = min(start_a, start_a + step_a * (count - 1))
    max_a = max(start_a, start_a + step_a * (count - 1))
    min_b = min(start_b, start_b + step_b * (count - 1))
    max_b = max(start_b, start_b + step_b * (count - 1))

    if min_a < 0 or min_b < 0:
        if max_a <= 0 and max_b <= 0:
            start_a, step_a = -start_a, -step_a
            start_b, step_b = -start_b, -step_b
        else:
            return None

    max_value = max(max_a, max_b)
    if max_value <= 1 << 53:
        mode, candidate = original_equality(
            Fraction(start_a), Fraction(step_a),
            Fraction(start_b), Fraction(step_b), count,
        )
        if mode == "all":
            return {"equal": count, "first_equal": 0, "first_different": None}
        if mode == "none":
            return {"equal": 0, "first_equal": None, "first_different": 0}
        assert candidate is not None
        first_different = 0 if candidate != 0 else (1 if count > 1 else None)
        return {
            "equal": 1,
            "first_equal": candidate,
            "first_different": first_different,
        }

    cuts = {0, count}
    for exponent in range(53, 66):
        power = 1 << exponent
        for start, step in ((start_a, step_a), (start_b, step_b)):
            if step > 0:
                crossing = ceil_fraction(Fraction(power - start, step))
                if 0 < crossing < count:
                    cuts.add(crossing)
            else:
                crossing = floor_fraction(Fraction(start - power, -step)) + 1
                if 0 < crossing < count:
                    cuts.add(crossing)

    cut_list = sorted(cuts)
    equal_total = 0
    first_equal: int | None = None
    first_different: int | None = None

    def record_range(lo: int, hi: int, equal_count: int,
                     range_first_equal: int | None,
                     range_first_different: int | None) -> None:
        nonlocal equal_total, first_equal, first_different
        equal_total += equal_count
        if range_first_equal is not None and first_equal is None:
            first_equal = lo + range_first_equal
        if range_first_different is not None and first_different is None:
            first_different = lo + range_first_different

    for lo, hi in zip(cut_list, cut_list[1:]):
        length = hi - lo
        value_a = start_a + step_a * lo
        value_b = start_b + step_b * lo
        exponent_a = value_a.bit_length() - 1
        exponent_b = value_b.bit_length() - 1

        if exponent_a != exponent_b:
            margin = 8192
            brute = length if length <= margin * 2 else 0
            if brute:
                equal_count = 0
                rfe = None
                rfd = None
                for index in range(length):
                    equal = integer_double_word(
                        start_a + step_a * (lo + index)
                    ) == integer_double_word(
                        start_b + step_b * (lo + index)
                    )
                    if equal:
                        equal_count += 1
                        if rfe is None:
                            rfe = index
                    elif rfd is None:
                        rfd = index
                record_range(lo, hi, equal_count, rfe, rfd)
                continue

            edge_length = min(margin, length // 2)
            for edge_lo, edge_hi in (
                (0, edge_length),
                (length - edge_length, length),
            ):
                if edge_hi <= edge_lo:
                    continue
                stats = integer_d_equal_stats(
                    start_a + step_a * (lo + edge_lo), step_a,
                    start_b + step_b * (lo + edge_lo), step_b,
                    edge_hi - edge_lo,
                )
                if stats is None:
                    return None
                record_range(
                    lo + edge_lo,
                    lo + edge_hi,
                    stats["equal"],
                    stats["first_equal"],
                    stats["first_different"],
                )
            middle_lo = edge_length
            middle_hi = length - edge_length
            if middle_hi > middle_lo and first_different is None:
                first_different = lo + middle_lo
            continue

        exponent = exponent_a
        if exponent <= 52:
            mode, candidate = original_equality(
                Fraction(start_a + step_a * lo), Fraction(step_a),
                Fraction(start_b + step_b * lo), Fraction(step_b),
                length,
            )
            if mode == "all":
                record_range(lo, hi, length, 0, None)
            elif mode == "none":
                record_range(lo, hi, 0, None, 0)
            else:
                assert candidate is not None
                rfd = 0 if candidate != 0 else (1 if length > 1 else None)
                record_range(lo, hi, 1, candidate, rfd)
            continue

        ulp = 1 << (exponent - 52)
        modulus = ulp * 2
        period_a = modulus // math.gcd(modulus, abs(step_a))
        period_b = modulus // math.gcd(modulus, abs(step_b))
        period = math.lcm(period_a, period_b)

        pattern: list[bool] = []
        prefix = [0]
        for phase in range(period):
            global_index = lo + phase
            rounded_a = round_positive_integer_to_ulp(
                start_a + step_a * global_index, ulp
            )
            rounded_b = round_positive_integer_to_ulp(
                start_b + step_b * global_index, ulp
            )
            same = rounded_a == rounded_b
            pattern.append(same)
            prefix.append(prefix[-1] + int(same))

        equal_count = (length // period) * prefix[period]
        remainder = length % period
        equal_count += prefix[remainder]

        rfe = None
        for phase in range(min(period, length)):
            if pattern[phase]:
                rfe = phase
                break

        rfd = None
        if equal_count < length:
            for phase in range(min(period, length)):
                if not pattern[phase]:
                    rfd = phase
                    break
            if rfd is None:
                rfd = period
        record_range(lo, hi, equal_count, rfe, rfd)

    return {
        "equal": equal_total,
        "first_equal": first_equal,
        "first_different": first_different,
    }


class _WordCounts(ctypes.Structure):
    _fields_ = [
        ("equal", ctypes.c_uint64),
        ("different", ctypes.c_uint64),
        ("first_equal", ctypes.c_size_t),
        ("first_different", ctypes.c_size_t),
    ]


class WordOps:
    """Small cached native vector scanner used only when NumPy is absent."""

    _instance: "WordOps | None" = None

    def __init__(self) -> None:
        source = r"""
#include <stdint.h>
#include <stddef.h>
typedef struct {
  uint64_t equal;
  uint64_t different;
  size_t first_equal;
  size_t first_different;
} vibration_word_counts;
void vibration_count_words(const uint64_t *a, const uint64_t *b,
                          size_t n, vibration_word_counts *out) {
  out->equal = 0;
  out->different = 0;
  out->first_equal = n;
  out->first_different = n;
  for (size_t i = 0; i < n; i++) {
    if (a[i] == b[i]) {
      out->equal++;
      if (out->first_equal == n) out->first_equal = i;
    } else {
      out->different++;
      if (out->first_different == n) out->first_different = i;
    }
  }
}
"""
        suffix = ".dylib" if sys.platform == "darwin" else (
            ".dll" if sys.platform.startswith("win") else ".so"
        )
        directory = Path(tempfile.gettempdir()) / "pysimdjson-vibration-audit"
        directory.mkdir(parents=True, exist_ok=True)
        library_path = directory / f"wordops-{ctypes.sizeof(ctypes.c_void_p)}{suffix}"
        source_path = library_path.with_suffix(".c")
        if not library_path.exists():
            source_path.write_text(source)
            compiler = "clang" if sys.platform == "darwin" else "cc"
            subprocess.run(
                [compiler, "-O3", "-shared", "-fPIC",
                 str(source_path), "-o", str(library_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        self.lib = ctypes.CDLL(str(library_path))
        self.lib.vibration_count_words.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_size_t,
            ctypes.POINTER(_WordCounts),
        ]

    @classmethod
    def get(cls) -> "WordOps | None":
        if cls._instance is None:
            try:
                cls._instance = cls()
            except Exception:
                cls._instance = None
        return cls._instance

    def count_buffers(
        self,
        buffer_a: Any,
        count_a: int,
        offset_a: int,
        buffer_b: Any,
        count_b: int,
        offset_b: int,
        length: int,
    ) -> dict[str, Any]:
        array_type = ctypes.c_uint64 * count_a
        array_a = array_type.from_buffer(buffer_a)
        pointer_a = ctypes.cast(
            ctypes.byref(array_a, offset_a * 8),
            ctypes.POINTER(ctypes.c_uint64),
        )

        array_type_b = ctypes.c_uint64 * count_b
        array_b = array_type_b.from_buffer(buffer_b)
        pointer_b = ctypes.cast(
            ctypes.byref(array_b, offset_b * 8),
            ctypes.POINTER(ctypes.c_uint64),
        )

        result = _WordCounts()
        self.lib.vibration_count_words(
            pointer_a, pointer_b, length, ctypes.byref(result)
        )
        sentinel = length
        return {
            "equal": int(result.equal),
            "first_equal": (
                None if result.first_equal == sentinel
                else int(result.first_equal)
            ),
            "first_different": (
                None if result.first_different == sentinel
                else int(result.first_different)
            ),
        }


def word_stats_from_source(
    buffer_a: Any,
    elements_a: int,
    offset_a: int,
    buffer_b: Any,
    elements_b: int,
    offset_b: int,
    length: int,
) -> dict[str, Any]:
    if length == 0:
        return {"equal": 0, "first_equal": None, "first_different": None}

    if _np is not None:
        words_a = _np.frombuffer(
            buffer_a, dtype=_np.uint64, count=length, offset=offset_a * 8
        )
        words_b = _np.frombuffer(
            buffer_b, dtype=_np.uint64, count=length, offset=offset_b * 8
        )
        equal_mask = words_a == words_b
        equal = int(equal_mask.sum())
        first_equal = None
        first_different = None
        if equal:
            first_equal = int(_np.argmax(equal_mask))
        if equal < length:
            first_different = int(_np.argmin(equal_mask))
        return {
            "equal": equal,
            "first_equal": first_equal,
            "first_different": first_different,
        }

    ops = WordOps.get()
    if ops is not None:
        return ops.count_buffers(
            buffer_a, elements_a, offset_a,
            buffer_b, elements_b, offset_b, length,
        )

    view_a = memoryview(buffer_a).cast("Q")
    view_b = memoryview(buffer_b).cast("Q")
    equal = 0
    first_equal = None
    first_different = None
    for index in range(length):
        same = view_a[offset_a + index] == view_b[offset_b + index]
        if same:
            equal += 1
            if first_equal is None:
                first_equal = index
        elif first_different is None:
            first_different = index
    return {
        "equal": equal,
        "first_equal": first_equal,
        "first_different": first_different,
    }


@dataclass
class Segment:
    index: int
    spec: dict[str, Any]
    count: int
    start: Fraction
    step: Fraction
    start_decimal: Decimal
    step_decimal: Decimal
    integer_style: bool
    format_spec: str | None
    negative_zero_ranges: list[tuple[int, int]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, index: int, spec: dict[str, Any]) -> "Segment":
        count = int(spec["count"])
        start_raw = str(spec.get("start", "0"))
        step_raw = str(spec.get("step", "0"))
        literal = spec.get("literal", "auto")
        format_spec = None
        if isinstance(literal, dict):
            kind = literal.get("kind", "auto")
            format_spec = literal.get("format")
            integer_style = kind == "integer"
        elif literal == "integer":
            integer_style = True
        elif literal in ("decimal", "fixed"):
            integer_style = False
            digits = int(spec.get("decimal_places", 1))
            format_spec = f".{digits}f"
        else:
            integer_style = bool(_INTEGER_RE.fullmatch(start_raw))

        ranges: list[tuple[int, int]] = []
        for cell in spec.get("negative_zero", []):
            if isinstance(cell, int):
                ranges.append((cell, cell + 1))
            else:
                low, high = cell
                ranges.append((int(low), int(high)))
        for low, high in spec.get("negative_zero_ranges", []):
            ranges.append((int(low), int(high)))
        ranges.sort()

        return cls(
            index=index,
            spec=spec,
            count=count,
            start=Fraction(Decimal(start_raw)),
            step=Fraction(Decimal(step_raw)),
            start_decimal=Decimal(start_raw),
            step_decimal=Decimal(step_raw),
            integer_style=integer_style,
            format_spec=format_spec,
            negative_zero_ranges=ranges,
        )

    def is_negative_zero(self, index: int) -> bool:
        for low, high in self.negative_zero_ranges:
            if low <= index < high:
                return True
            if low > index:
                break
        return False

    def token_at(self, index: int) -> str:
        if self.is_negative_zero(index):
            negative_zero = Decimal("-0")
            if self.format_spec:
                return format(negative_zero, self.format_spec)
            return "-0" if self.integer_style else "-0.0"

        if self.integer_style:
            value = self.start.numerator // self.start.denominator
            step = self.step.numerator // self.step.denominator
            return str(value + step * index)

        precision = max(
            60,
            len(self.start_decimal.as_tuple().digits)
            + len(self.step_decimal.as_tuple().digits)
            + len(str(max(1, self.count)))
            + 20,
        )
        with localcontext() as context:
            context.prec = precision
            value = self.start_decimal + self.step_decimal * index
            if self.format_spec:
                return format(value, self.format_spec)
            return format(value, "f")

    def negative_zero_intersects(self, offset: int, count: int) -> bool:
        end = offset + count
        for low, high in self.negative_zero_ranges:
            if high <= offset:
                continue
            if low >= end:
                break
            return True
        return False

    def accepted_interval(self, dtype: str) -> tuple[int, int] | None:
        if dtype in ("i", "u") and not self.integer_style:
            return None

        if self.integer_style:
            start = self.start
            step = self.step
            if dtype == "i":
                lower, upper = Fraction(I64_MIN), Fraction(I64_MAX)
            elif dtype == "u":
                lower, upper = Fraction(0), Fraction(U64_MAX)
            else:
                lower, upper = Fraction(I64_MIN), Fraction(U64_MAX)
            return linear_interval_within(
                start, step, self.count, lower, upper
            )

        if dtype in ("i", "u"):
            return None

        try:
            first_ok = math.isfinite(float(self.token_at(0)))
            last_ok = math.isfinite(float(self.token_at(self.count - 1)))
        except OverflowError:
            first_ok = False
            last_ok = False
        if first_ok and last_ok:
            return 0, self.count
        return None


def count_shape(node: Any) -> int:
    if isinstance(node, int) and not isinstance(node, bool):
        return node
    if isinstance(node, list):
        return sum(count_shape(child) for child in node)
    if isinstance(node, dict):
        if "array" in node:
            return count_shape(node["array"])
        if "run" in node:
            return int(node["run"])
    raise TypeError(f"Unsupported shape node: {node!r}")


def emit_shape(node: Any, tokens: Any) -> str:
    if isinstance(node, int) and not isinstance(node, bool):
        values = [next(tokens) for _ in range(node)]
        return ",".join(values)
    if isinstance(node, list):
        return "[" + ",".join(emit_shape(child, tokens) for child in node) + "]"
    if isinstance(node, dict):
        if "array" in node:
            return emit_shape(node["array"], tokens)
        if "run" in node:
            return emit_shape(int(node["run"]), tokens)
    raise TypeError(f"Unsupported shape node: {node!r}")


def resolve_shape_path(node: Any, index: int, pointer: str = "") -> str | None:
    element_index = 0
    if not isinstance(node, list):
        node = [node]

    for child in node:
        if isinstance(child, int) and not isinstance(child, bool):
            run = child
            if index < run:
                return f"{pointer}/{element_index + index}"
            index -= run
            element_index += run
            continue

        child_node = child["array"] if isinstance(child, dict) and "array" in child else child
        child_count = count_shape(child_node)
        if index < child_count:
            child_pointer = f"{pointer}/{element_index}"
            return resolve_shape_path(child_node, index, child_pointer)
        index -= child_count
        element_index += 1

    return None


def json_pointer_escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def decode_raw_json(data: bytes) -> tuple[Any, list[RawNumber]]:
    tokens: list[RawNumber] = []

    def remember(literal: str) -> RawNumber:
        number = RawNumber(literal)
        tokens.append(number)
        return number

    root = json.loads(
        data.decode("utf-8"),
        parse_int=remember,
        parse_float=remember,
    )
    return root, tokens


def resolve_raw_path_and_token(
    node: Any, index: int, pointer: str = ""
) -> tuple[str, str] | None:
    if isinstance(node, list):
        for position, child in enumerate(node):
            child_pointer = f"{pointer}/{position}"
            if isinstance(child, list):
                count = count_raw_leaves(child)
                if index < count:
                    return resolve_raw_path_and_token(child, index, child_pointer)
                index -= count
            elif isinstance(child, dict):
                count = count_raw_leaves(child)
                if index < count:
                    return resolve_raw_path_and_token(child, index, child_pointer)
                index -= count
            else:
                if index == 0:
                    token = raw_scalar_token(child)
                    return child_pointer, token
                index -= 1
        return None

    if isinstance(node, dict):
        if index == 0:
            return pointer or "", raw_scalar_token(node)
        return None

    if index == 0:
        return pointer or "", raw_scalar_token(node)
    return None


def count_raw_leaves(node: Any) -> int:
    if isinstance(node, list):
        return sum(count_raw_leaves(child) for child in node)
    return 1


def raw_scalar_token(node: Any) -> str:
    if isinstance(node, RawNumber):
        return node.literal
    if node is None:
        return "null"
    if node is True:
        return "true"
    if node is False:
        return "false"
    if isinstance(node, str):
        return json.dumps(node)
    if isinstance(node, (dict, list)):
        return json.dumps(node, separators=(",", ":"))
    return str(node)


@dataclass
class WordRun:
    gstart: int
    gend: int
    ok: bool
    buffer: Any = None
    elements: int = 0
    error: str | None = None


class Archive:
    def __init__(self, name: str, spec: dict[str, Any], base_dir: Path):
        self.name = name
        self.spec = spec
        self.base_dir = base_dir
        self.parser = simdjson.Parser()
        self.segments: list[Segment] = []
        self.segment_ends: list[int] = []
        self.shape: Any = None
        self.raw_root: Any = None
        self.raw_tokens: list[RawNumber] = []
        self.data = self._load_data()
        self.flat_count = count_raw_leaves(self.raw_root) if self.raw_root is not None else sum(
            segment.count for segment in self.segments
        )
        self.parse_error: str | None = None
        self.doc: Any = None
        try:
            self.doc = self.parser.parse(self.data)
        except Exception as exc:
            self.parse_error = f"{type(exc).__name__}: {exc}"
        self._sources: dict[str, tuple[Any, int] | None] = {}
        self._source_errors: dict[str, str] = {}
        self._segment_buffers: dict[tuple[int, str, int, int], tuple[Any, int]] = {}
        self._raw_runs: dict[str, list[WordRun]] = {}

    def _load_data(self) -> bytes:
        if "json_path" in self.spec:
            path = Path(self.spec["json_path"])
            if not path.is_absolute():
                path = self.base_dir / path
            data = path.read_bytes()
            self.raw_root, self.raw_tokens = decode_raw_json(data)
            return data

        if "json_text" in self.spec:
            text = self.spec["json_text"]
            data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
            self.raw_root, self.raw_tokens = decode_raw_json(data)
            return data

        segment_specs = self.spec.get("segments", [])
        cumulative = 0
        for index, segment_spec in enumerate(segment_specs):
            segment = Segment.from_dict(index, segment_spec)
            self.segments.append(segment)
            cumulative += segment.count
            self.segment_ends.append(cumulative)

        self.shape = self.spec.get("shape")
        total = cumulative
        if self.shape is None:
            root_shape: Any = [total]
        elif isinstance(self.shape, list):
            root_shape = self.shape
        else:
            root_shape = [self.shape]

        shape_total = count_shape(root_shape)
        if shape_total != total:
            raise ValueError(
                f"Archive {self.name!r} shape has {shape_total} cells, "
                f"segments have {total}"
            )

        def token_stream() -> Any:
            for segment in self.segments:
                for index in range(segment.count):
                    yield segment.token_at(index)

        text = emit_shape(root_shape, token_stream())
        return text.encode("utf-8")

    def segment_at(self, flat_index: int) -> tuple[Segment, int, int] | None:
        if not self.segments:
            return None
        segment_index = bisect_right(self.segment_ends, flat_index)
        if segment_index >= len(self.segments):
            return None
        base = 0 if segment_index == 0 else self.segment_ends[segment_index - 1]
        segment = self.segments[segment_index]
        return segment, segment_index, flat_index - base

    def path_and_token(self, flat_index: int) -> tuple[str, str]:
        if flat_index < 0 or flat_index >= self.flat_count:
            raise IndexError(flat_index)

        if self.segments:
            located = self.segment_at(flat_index)
            assert located is not None
            segment, _, local_index = located
            root_shape = self.shape if isinstance(self.shape, list) else [
                self.shape if self.shape is not None else self.flat_count
            ]
            path = resolve_shape_path(root_shape, flat_index) or ""
            return path, segment.token_at(local_index)

        resolved = resolve_raw_path_and_token(self.raw_root, flat_index)
        if resolved is None:
            raise IndexError(flat_index)
        return resolved

    def root_source(self, dtype: str) -> tuple[Any, int] | None:
        if dtype in self._sources:
            return self._sources[dtype]
        if dtype in self._source_errors:
            return None

        if self.parse_error is not None:
            self._source_errors[dtype] = self.parse_error
            self._sources[dtype] = None
            return None

        try:
            buffer = self.doc.as_buffer(of_type=dtype)
            elements = memoryview(buffer).nbytes // 8
            source = (buffer, elements)
            self._sources[dtype] = source
            return source
        except Exception as exc:
            self._source_errors[dtype] = f"{type(exc).__name__}: {exc}"
            self._sources[dtype] = None
            return None

    def root_error(self, dtype: str) -> str | None:
        self.root_source(dtype)
        return self._source_errors.get(dtype)

    def native_cell(self, flat_index: int, dtype: str) -> dict[str, Any]:
        source = self.root_source(dtype)
        if source is not None:
            buffer, _ = source
            word = int(memoryview(buffer).cast("Q")[flat_index])
            return {"ok": True, "word": word, "error": None}

        _, token = self.path_and_token(flat_index)
        return convert_literal_native(token, dtype)

    def buffer_for_tokens(
        self, tokens: list[str], dtype: str
    ) -> tuple[Any, int]:
        data = b"[" + b",".join(token.encode("utf-8") for token in tokens) + b"]"
        doc = simdjson.Parser().parse(data)
        buffer = doc.as_buffer(of_type=dtype)
        return buffer, memoryview(buffer).nbytes // 8

    def segment_word_run(
        self,
        segment_index: int,
        dtype: str,
        local_start: int,
        local_end: int,
    ) -> WordRun:
        key = (segment_index, dtype, local_start, local_end)
        cached = self._segment_buffers.get(key)
        segment = self.segments[segment_index]
        base = 0 if segment_index == 0 else self.segment_ends[segment_index - 1]
        gstart = base + local_start
        gend = base + local_end
        if cached is not None:
            buffer, elements = cached
            return WordRun(gstart, gend, True, buffer, elements)

        tokens = [segment.token_at(i) for i in range(local_start, local_end)]
        try:
            buffer, elements = self.buffer_for_tokens(tokens, dtype)
            self._segment_buffers[key] = (buffer, elements)
            return WordRun(gstart, gend, True, buffer, elements)
        except Exception as exc:
            return WordRun(
                gstart, gend, False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def resolve_runs(self, dtype: str, glo: int, ghi: int) -> list[WordRun]:
        source = self.root_source(dtype)
        if source is not None:
            buffer, elements = source
            return [WordRun(glo, ghi, True, buffer, elements)]

        if self.segments:
            return self._resolve_segment_runs(dtype, glo, ghi)
        return self._resolve_raw_runs(dtype, glo, ghi)

    def _resolve_segment_runs(
        self, dtype: str, glo: int, ghi: int
    ) -> list[WordRun]:
        runs: list[WordRun] = []
        first_segment = bisect_right(self.segment_ends, glo)
        last_segment = bisect_right(self.segment_ends, ghi - 1)
        for segment_index in range(first_segment, last_segment + 1):
            if segment_index >= len(self.segments):
                break
            segment = self.segments[segment_index]
            base = 0 if segment_index == 0 else self.segment_ends[segment_index - 1]
            end = self.segment_ends[segment_index]
            request_lo = max(glo, base)
            request_hi = min(ghi, end)
            if request_lo >= request_hi:
                continue

            accepted = segment.accepted_interval(dtype)
            if accepted is None:
                error = self.native_cell(request_lo, dtype)["error"]
                runs.append(WordRun(
                    request_lo, request_hi, False, error=error
                ))
                continue

            accept_lo, accept_hi = accepted
            global_accept_lo = base + accept_lo
            global_accept_hi = base + accept_hi

            if request_lo < global_accept_lo:
                error = self.native_cell(request_lo, dtype)["error"]
                runs.append(WordRun(
                    request_lo, global_accept_lo, False, error=error
                ))

            overlap_lo = max(request_lo, global_accept_lo)
            overlap_hi = min(request_hi, global_accept_hi)
            if overlap_lo < overlap_hi:
                word_run = self.segment_word_run(
                    segment_index, dtype, accept_lo, accept_hi
                )
                if word_run.ok:
                    runs.append(WordRun(
                        global_accept_lo, global_accept_hi, True,
                        word_run.buffer, word_run.elements,
                    ))
                else:
                    for global_index in range(overlap_lo, overlap_hi):
                        result = self.native_cell(global_index, dtype)
                        runs.append(WordRun(
                            global_index, global_index + 1, result["ok"],
                            error=result["error"],
                        ))

            if global_accept_hi < request_hi:
                reject_lo = max(request_lo, global_accept_hi)
                error = self.native_cell(reject_lo, dtype)["error"]
                runs.append(WordRun(
                    global_accept_hi, request_hi, False, error=error
                ))

        runs.sort(key=lambda run: run.gstart)
        return runs

    def _resolve_raw_runs(
        self, dtype: str, glo: int, ghi: int
    ) -> list[WordRun]:
        if dtype not in self._raw_runs:
            runs: list[WordRun] = []
            start: int | None = None
            accepted: list[bool] = []

            def flush(end: int) -> None:
                nonlocal start, accepted
                if start is None:
                    return
                if all(accepted):
                    tokens = [
                        self.path_and_token(index)[1]
                        for index in range(start, end)
                    ]
                    try:
                        buffer, elements = self.buffer_for_tokens(tokens, dtype)
                        runs.append(WordRun(start, end, True, buffer, elements))
                    except Exception:
                        for index in range(start, end):
                            result = self.native_cell(index, dtype)
                            runs.append(WordRun(
                                index, index + 1, result["ok"],
                                error=result["error"],
                            ))
                else:
                    error = next((
                        self.native_cell(index, dtype)["error"]
                        for index in range(start, end)
                        if not self._predicted_accept(index, dtype)
                    ), None)
                    runs.append(WordRun(start, end, False, error=error))
                start = None
                accepted = []

            previous_ok: bool | None = None
            for index in range(self.flat_count):
                ok = self._predicted_accept(index, dtype)
                if previous_ok is None:
                    start = 0
                elif ok != previous_ok:
                    flush(index)
                    start = index
                accepted.append(ok)
                previous_ok = ok
            flush(self.flat_count)
            self._raw_runs[dtype] = runs

        return [
            run for run in self._raw_runs[dtype]
            if run.gend > glo and run.gstart < ghi
        ]

    def _predicted_accept(self, index: int, dtype: str) -> bool:
        _, token = self.path_and_token(index)
        integer_token = bool(_INTEGER_RE.fullmatch(token))
        if dtype in ("i", "u"):
            if not integer_token:
                return False
            value = int(token)
            if dtype == "i":
                return I64_MIN <= value <= I64_MAX
            return 0 <= value <= U64_MAX
        if integer_token:
            value = int(token)
            return I64_MIN <= value <= U64_MAX
        try:
            return math.isfinite(float(token))
        except (OverflowError, ValueError):
            return False


def convert_literal_native(token: str, dtype: str) -> dict[str, Any]:
    data = b"[" + token.encode("utf-8") + b"]"
    try:
        doc = simdjson.Parser().parse(data)
        buffer = doc.as_buffer(of_type=dtype)
        word = int(memoryview(buffer).cast("Q")[0])
        return {"ok": True, "word": word, "error": None}
    except Exception as exc:
        return {
            "ok": False,
            "word": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


@dataclass
class SideCheck:
    archive_name: str
    offset: int
    start: Fraction
    step: Fraction

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SideCheck":
        return cls(
            archive_name=data["archive"],
            offset=int(data.get("offset", 0)),
            start=Fraction(Decimal(str(data.get("start", 0)))),
            step=Fraction(Decimal(str(data.get("step", 0)))),
        )


def example_for_cell(
    check_id: str,
    category: str,
    index: int,
    left_archive: Archive,
    left_check: SideCheck,
    right_archive: Archive,
    right_check: SideCheck,
    dtype: str,
) -> dict[str, Any]:
    left_global = left_check.offset + index
    right_global = right_check.offset + index
    left_path, left_token = left_archive.path_and_token(left_global)
    right_path, right_token = right_archive.path_and_token(right_global)
    left_result = left_archive.native_cell(left_global, dtype)
    right_result = right_archive.native_cell(right_global, dtype)

    return {
        "check": check_id,
        "category": category,
        "cell": index,
        "left": {
            "archive": left_archive.name,
            "global_cell": left_global,
            "path": left_path,
            "literal": left_token,
            "original_value": fraction_text(
                left_check.start + left_check.step * index
            ),
            "word": (
                None if left_result["word"] is None
                else f"0x{int(left_result['word']):016x}"
            ),
            "error": left_result["error"],
        },
        "right": {
            "archive": right_archive.name,
            "global_cell": right_global,
            "path": right_path,
            "literal": right_token,
            "original_value": fraction_text(
                right_check.start + right_check.step * index
            ),
            "word": (
                None if right_result["word"] is None
                else f"0x{int(right_result['word']):016x}"
            ),
            "error": right_result["error"],
        },
    }


def _word_stats_for_runs(
    left_run: WordRun,
    left_global: int,
    right_run: WordRun,
    right_global: int,
    length: int,
) -> dict[str, Any]:
    left_offset = left_global - left_run.gstart
    right_offset = right_global - right_run.gstart
    return word_stats_from_source(
        left_run.buffer, left_run.elements, left_offset,
        right_run.buffer, right_run.elements, right_offset,
        length,
    )


def audit_check(
    spec: dict[str, Any],
    archives: dict[str, Archive],
    cell_limit: int = 4096,
) -> dict[str, Any]:
    check_id = str(spec.get("id", "<check>"))
    dtype = str(spec["type"])
    if dtype not in DTYPE_CODES:
        raise ValueError(f"Unsupported type {dtype!r}; expected d, i, or u")
    count = int(spec["count"])
    left = SideCheck.from_dict(spec["left"])
    right = SideCheck.from_dict(spec["right"])
    left_archive = archives[left.archive_name]
    right_archive = archives[right.archive_name]

    equal_words = 0
    first_equal: int | None = None
    first_different: int | None = None
    rejected_left = 0
    rejected_right = 0
    rejected_both = 0
    first_rejected_left: int | None = None
    first_rejected_right: int | None = None

    left_source = left_archive.root_source(dtype)
    right_source = right_archive.root_source(dtype)

    if left_source is not None and right_source is not None:
        segment_stats = None
        if dtype in ("i", "u"):
            equality_mode, unique_cell = original_equality(
                left.start, left.step, right.start, right.step, count
            )
            if equality_mode == "all":
                segment_stats = {
                    "equal": count,
                    "first_equal": 0,
                    "first_different": None,
                }
            elif equality_mode == "none":
                segment_stats = {
                    "equal": 0,
                    "first_equal": None,
                    "first_different": 0,
                }
            else:
                assert unique_cell is not None
                first_different = 0 if unique_cell != 0 else (
                    1 if count > 1 else None
                )
                segment_stats = {
                    "equal": 1,
                    "first_equal": unique_cell,
                    "first_different": first_different,
                }
        elif dtype == "d":
            no_negative_zero = True

            def window_has_negative_zero(
                side_check: SideCheck, archive: Archive
            ) -> bool:
                if not archive.segments:
                    return True
                end_index = bisect_right(
                    archive.segment_ends, side_check.offset + count - 1
                )
                start_index = bisect_right(archive.segment_ends, side_check.offset)
                for segment_index in range(start_index, end_index + 1):
                    if segment_index >= len(archive.segments):
                        break
                    segment = archive.segments[segment_index]
                    base = (
                        0 if segment_index == 0
                        else archive.segment_ends[segment_index - 1]
                    )
                    segment_end = archive.segment_ends[segment_index]
                    local_start = max(side_check.offset, base) - base
                    local_end = min(
                        side_check.offset + count, segment_end
                    ) - base
                    if segment.negative_zero_intersects(
                        local_start, local_end - local_start
                    ):
                        return True
                return False

            for side_check, archive in (
                (left, left_archive),
                (right, right_archive),
            ):
                if not archive.segments:
                    no_negative_zero = False
                elif window_has_negative_zero(side_check, archive):
                    no_negative_zero = False

            if (
                no_negative_zero
                and left.start.denominator == 1
                and left.step.denominator == 1
                and right.start.denominator == 1
                and right.step.denominator == 1
            ):
                segment_stats = integer_d_equal_stats(
                    left.start.numerator, left.step.numerator,
                    right.start.numerator, right.step.numerator,
                    count,
                )

        left_buffer, left_elements = left_source
        right_buffer, right_elements = right_source

        if dtype == "d" and segment_stats is not None:
            left_words = memoryview(left_buffer).cast("Q")
            right_words = memoryview(right_buffer).cast("Q")
            sample_candidates = {0, 1, count - 3, count - 2, count - 1}
            for boundary in (
                segment_stats["first_equal"],
                segment_stats["first_different"],
            ):
                if boundary is not None:
                    sample_candidates.update((boundary - 1, boundary, boundary + 1))

            for sample in sample_candidates:
                if not 0 <= sample < count:
                    continue
                formula_sample = integer_d_equal_stats(
                    left.start.numerator + left.step.numerator * sample,
                    left.step.numerator,
                    right.start.numerator + right.step.numerator * sample,
                    right.step.numerator,
                    1,
                )
                formula_equal = formula_sample["equal"] == 1
                native_equal = (
                    left_words[left.offset + sample]
                    == right_words[right.offset + sample]
                )
                if formula_equal != native_equal:
                    segment_stats = None
                    break

        if segment_stats is not None:
            stats = segment_stats
        else:
            stats = word_stats_from_source(
                left_buffer, left_elements, left.offset,
                right_buffer, right_elements, right.offset,
                count,
            )
        equal_words = stats["equal"]
        first_equal = stats["first_equal"]
        first_different = stats["first_different"]
    else:
        left_runs = left_archive.resolve_runs(
            dtype, left.offset, left.offset + count
        )
        right_runs = right_archive.resolve_runs(
            dtype, right.offset, right.offset + count
        )
        cuts = {0, count}
        for run in left_runs:
            local_start = max(0, run.gstart - left.offset)
            local_end = min(count, run.gend - left.offset)
            if local_start < local_end:
                cuts.add(local_start)
                cuts.add(local_end)
        for run in right_runs:
            local_start = max(0, run.gstart - right.offset)
            local_end = min(count, run.gend - right.offset)
            if local_start < local_end:
                cuts.add(local_start)
                cuts.add(local_end)
        cut_values = sorted(cuts)

        for local_lo, local_hi in zip(cut_values, cut_values[1:]):
            length = local_hi - local_lo
            left_global = left.offset + local_lo
            right_global = right.offset + local_lo
            left_index = bisect_right(
                [run.gstart for run in left_runs], left_global
            ) - 1
            right_index = bisect_right(
                [run.gstart for run in right_runs], right_global
            ) - 1
            left_run = left_runs[max(0, left_index)]
            right_run = right_runs[max(0, right_index)]

            if not left_run.ok or not right_run.ok:
                left_bad = not left_run.ok
                right_bad = not right_run.ok
                if left_bad:
                    rejected_left += length
                    if first_rejected_left is None:
                        first_rejected_left = local_lo
                if right_bad:
                    rejected_right += length
                    if first_rejected_right is None:
                        first_rejected_right = local_lo
                if left_bad and right_bad:
                    rejected_both += length
                continue

            stats = _word_stats_for_runs(
                left_run, left_global,
                right_run, right_global,
                length,
            )
            equal_words += stats["equal"]
            if stats["first_equal"] is not None and first_equal is None:
                first_equal = local_lo + stats["first_equal"]
            if stats["first_different"] is not None and first_different is None:
                first_different = local_lo + stats["first_different"]

    equality_mode, unique_cell = original_equality(
        left.start, left.step, right.start, right.step, count
    )

    same_value_and_word = 0
    first_same: int | None = None
    if equality_mode == "all":
        same_value_and_word = equal_words
        first_same = first_equal
    elif equality_mode == "one" and unique_cell is not None:
        left_result = left_archive.native_cell(left.offset + unique_cell, dtype)
        right_result = right_archive.native_cell(right.offset + unique_cell, dtype)
        if (
            left_result["ok"]
            and right_result["ok"]
            and left_result["word"] == right_result["word"]
        ):
            same_value_and_word = 1
            first_same = unique_cell

    same_word_different_value = equal_words - same_value_and_word
    different_word = count - equal_words - rejected_left - rejected_right + rejected_both

    examples: dict[str, Any] = {
        "same_value_and_word": None,
        "same_word_different_value": None,
        "different_word": None,
        "rejected_left": None,
        "rejected_right": None,
    }
    if first_same is not None:
        examples["same_value_and_word"] = example_for_cell(
            check_id, "same_value_and_word", first_same,
            left_archive, left, right_archive, right, dtype,
        )
    if same_word_different_value:
        collision_cell = first_equal
        if collision_cell == first_same and equality_mode == "one":
            before = audit_check(
                {**spec, "count": unique_cell},
                archives,
                cell_limit,
            ) if unique_cell else None
            after_spec = {
                **spec,
                "count": count - unique_cell - 1,
                "left": {
                    **spec["left"],
                    "offset": left.offset + unique_cell + 1,
                    "start": fraction_text(left.start + left.step * (unique_cell + 1)),
                },
                "right": {
                    **spec["right"],
                    "offset": right.offset + unique_cell + 1,
                    "start": fraction_text(right.start + right.step * (unique_cell + 1)),
                },
            }
            after = audit_check(after_spec, archives, cell_limit)
            if before and before["counts"]["same_word_different_value"]:
                collision_cell = before["first"]["same_word_different_value"]["cell"]
            elif after["counts"]["same_word_different_value"]:
                collision_cell = unique_cell + 1 + after["first"][
                    "same_word_different_value"
                ]["cell"]
        if collision_cell is not None and collision_cell != first_same:
            examples["same_word_different_value"] = example_for_cell(
                check_id, "same_word_different_value", collision_cell,
                left_archive, left, right_archive, right, dtype,
            )
    if different_word > 0 and first_different is not None:
        examples["different_word"] = example_for_cell(
            check_id, "different_word", first_different,
            left_archive, left, right_archive, right, dtype,
        )
    if first_rejected_left is not None:
        examples["rejected_left"] = example_for_cell(
            check_id, "rejected_left", first_rejected_left,
            left_archive, left, right_archive, right, dtype,
        )
    if first_rejected_right is not None:
        examples["rejected_right"] = example_for_cell(
            check_id, "rejected_right", first_rejected_right,
            left_archive, left, right_archive, right, dtype,
        )

    rejected_either = rejected_left + rejected_right - rejected_both
    return {
        "id": check_id,
        "type": dtype,
        "count": count,
        "counts": {
            "same_value_and_word": same_value_and_word,
            "same_word_different_value": same_word_different_value,
            "different_word": different_word,
            "rejected_left": rejected_left,
            "rejected_right": rejected_right,
            "rejected_both": rejected_both,
            "rejected_either": rejected_either,
        },
        "first": examples,
    }


def cell_by_cell_audit(
    spec: dict[str, Any], archives: dict[str, Archive]
) -> dict[str, Any]:
    check_id = str(spec.get("id", "<check>"))
    dtype = str(spec["type"])
    count = int(spec["count"])
    left = SideCheck.from_dict(spec["left"])
    right = SideCheck.from_dict(spec["right"])
    left_archive = archives[left.archive_name]
    right_archive = archives[right.archive_name]

    counts = {
        "same_value_and_word": 0,
        "same_word_different_value": 0,
        "different_word": 0,
        "rejected_left": 0,
        "rejected_right": 0,
        "rejected_both": 0,
        "rejected_either": 0,
    }
    first_cells: dict[str, int | None] = {
        "same_value_and_word": None,
        "same_word_different_value": None,
        "different_word": None,
        "rejected_left": None,
        "rejected_right": None,
    }
    equality_mode, unique_cell = original_equality(
        left.start, left.step, right.start, right.step, count
    )

    for cell in range(count):
        left_global = left.offset + cell
        right_global = right.offset + cell
        _, left_token = left_archive.path_and_token(left_global)
        _, right_token = right_archive.path_and_token(right_global)
        left_result = convert_literal_native(left_token, dtype)
        right_result = convert_literal_native(right_token, dtype)

        if not left_result["ok"]:
            counts["rejected_left"] += 1
            if first_cells["rejected_left"] is None:
                first_cells["rejected_left"] = cell
        if not right_result["ok"]:
            counts["rejected_right"] += 1
            if first_cells["rejected_right"] is None:
                first_cells["rejected_right"] = cell
        if not left_result["ok"] or not right_result["ok"]:
            if not left_result["ok"] and not right_result["ok"]:
                counts["rejected_both"] += 1
            continue

        original_same = equality_mode == "all" or (
            equality_mode == "one" and unique_cell == cell
        )
        word_same = left_result["word"] == right_result["word"]
        if word_same and original_same:
            category = "same_value_and_word"
        elif word_same:
            category = "same_word_different_value"
        else:
            category = "different_word"
        counts[category] += 1
        if first_cells[category] is None:
            first_cells[category] = cell

    counts["rejected_either"] = (
        counts["rejected_left"]
        + counts["rejected_right"]
        - counts["rejected_both"]
    )
    first_examples = {
        key: (
            example_for_cell(
                check_id,
                key,
                cell,
                left_archive,
                left,
                right_archive,
                right,
                dtype,
            )
            if cell is not None
            else None
        )
        for key, cell in first_cells.items()
    }
    return {
        "id": check_id,
        "type": dtype,
        "count": count,
        "counts": counts,
        "first": first_examples,
    }


def run_job(job: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    base_dir = base_dir or Path.cwd()
    archives = {
        name: Archive(name, spec, base_dir)
        for name, spec in job.get("archives", {}).items()
    }
    reports = [
        audit_check(spec, archives)
        for spec in job.get("checks", [])
    ]
    totals = {
        key: sum(report["counts"][key] for report in reports)
        for key in (
            "same_value_and_word",
            "same_word_different_value",
            "different_word",
            "rejected_left",
            "rejected_right",
            "rejected_both",
            "rejected_either",
        )
    }
    return {"checks": reports, "totals": totals}


def _example_html(example: dict[str, Any] | None) -> str:
    if not example:
        return ""
    left = example["left"]
    right = example["right"]
    return f"""
<details class="example"><summary>first cell #{example['cell']}</summary>
<dl>
<dt>left <code>{html.escape(left['archive'])}{html.escape(left['path'])}</code></dt>
<dd><code>{html.escape(left['literal'])}</code> value={html.escape(left['original_value'])} word={html.escape(str(left['word']))}</dd>
{f'<dd class="error">{html.escape(left["error"])}</dd>' if left['error'] else ''}
<dt>right <code>{html.escape(right['archive'])}{html.escape(right['path'])}</code></dt>
<dd><code>{html.escape(right['literal'])}</code> value={html.escape(right['original_value'])} word={html.escape(str(right['word']))}</dd>
{f'<dd class="error">{html.escape(right["error"])}</dd>' if right['error'] else ''}
</dl></details>
"""


def render_html(report: dict[str, Any]) -> str:
    rows = []
    for check in report["checks"]:
        counts = check["counts"]
        first = check["first"]

        def cell(key: str, label: str) -> str:
            value = counts[key]
            example_key = key if key in first else None
            example = first.get(example_key) if example_key else None
            if example:
                return (
                    f"<td>{value}{_example_html(example)}</td>"
                )
            return f"<td>{value}</td>"

        rows.append(f"""
<tr>
  <th scope="row">{html.escape(check['id'])}</th>
  <td><code>{check['type']}</code></td>
  <td>{check['count']}</td>
  {cell('same_value_and_word', 'same')}
  {cell('same_word_different_value', 'collision')}
  {cell('different_word', 'different')}
  {cell('rejected_left', 'left')}
  {cell('rejected_right', 'right')}
  {cell('rejected_both', 'both')}
</tr>""")

    totals = report["totals"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>pysimdjson vibration audit</title>
<style>
body {{ font: 14px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; color: #17202a; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #d5dbdb; padding: .45rem .6rem; vertical-align: top; text-align: right; }}
th[scope=row], td:nth-child(2), td:nth-child(3) {{ text-align: left; }}
thead th {{ background: #f4f6f7; }}
.example {{ margin-top: .35rem; text-align: left; }}
.example summary {{ cursor: pointer; color: #1f618d; }}
dl {{ margin: .4rem 0 0; }}
dt {{ font-weight: 600; margin-top: .35rem; }}
dd {{ margin-left: 0; }}
.error {{ color: #922b21; }}
code {{ background: #f4f6f7; padding: .1rem .25rem; border-radius: 3px; }}
</style></head><body>
<h1>振动计数两代存档抽查</h1>
<p>same={totals['same_value_and_word']}, hidden-different={totals['same_word_different_value']},
different-word={totals['different_word']}, rejected-either={totals['rejected_either']}.</p>
<table>
<thead><tr><th>check</th><th>type</th><th>cells</th>
<th>same value + word</th><th>same word, different value</th><th>different word</th>
<th>left rejected</th><th>right rejected</th><th>both rejected</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>
"""


def example_job() -> dict[str, Any]:
    near = 1 << 53
    return {
        "archives": {
            "old": {
                "segments": [
                    {"id": "near", "count": 8, "start": str(near), "step": "1",
                     "literal": "integer"},
                    {"id": "zero-int", "count": 1, "start": "0", "step": "0",
                     "literal": "integer", "negative_zero": [0]},
                    {"id": "u64", "count": 2, "start": str(1 << 63),
                     "step": str(U64_MAX - (1 << 63)), "literal": "integer"},
                ],
                "shape": [[8, [1], []], 2],
            },
            "new": {
                "segments": [
                    {"id": "near-offset", "count": 8, "start": str(near + 1),
                     "step": "1", "literal": "integer"},
                    {"id": "zero-decimal", "count": 1, "start": "0.0", "step": "0",
                     "literal": {"kind": "fixed", "format": ".1f"},
                     "negative_zero": [0]},
                    {"id": "u-decimal", "count": 2, "start": f"{1 << 63}.0",
                     "step": "0", "literal": {"kind": "fixed", "format": ".1f"}},
                ],
                "shape": [[[8], 1, []], [2]],
            },
        },
        "checks": [
            {
                "id": "near-2p53-d", "type": "d", "count": 8,
                "left": {"archive": "old", "offset": 0,
                         "start": str(near), "step": "1"},
                "right": {"archive": "new", "offset": 0,
                          "start": str(near + 1), "step": "1"},
            },
            {
                "id": "negative-zero-d", "type": "d", "count": 1,
                "left": {"archive": "old", "offset": 8, "start": "0", "step": "0"},
                "right": {"archive": "new", "offset": 8, "start": "0", "step": "0"},
            },
            {
                "id": "unsigned-with-decimal-u", "type": "u", "count": 2,
                "left": {"archive": "old", "offset": 9,
                         "start": str(1 << 63),
                         "step": str(U64_MAX - (1 << 63))},
                "right": {"archive": "new", "offset": 9,
                          "start": f"{1 << 63}.0", "step": "0"},
            },
        ],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit paired pysimdjson as_buffer windows."
    )
    parser.add_argument("job", nargs="?", type=Path, help="JSON job file")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--html-output", type=Path)
    parser.add_argument("--print-example", action="store_true")
    args = parser.parse_args(argv)

    if args.print_example:
        print(json.dumps(example_job(), ensure_ascii=False, indent=2))
        return 0
    if args.job is None:
        parser.error("job is required unless --print-example is used")

    job_path = args.job.resolve()
    job = json.loads(job_path.read_text("utf-8"))
    report = run_job(job, job_path.parent)
    rendered_json = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.write_text(rendered_json + "\n", "utf-8")
    if args.html_output:
        args.html_output.write_text(render_html(report), "utf-8")
    print(rendered_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
