"""
pint.facets.calibration.definitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Immutable definitions used by the calibration facet.

Calibrations are described as piecewise, continuous, strictly monotone
polynomials.  A calibration edge may additionally carry a multiplicative
scale and an additive offset, which is useful when the same measurement was
historically expressed with different unit definitions (for example a
legacy temperature scale).

:copyright: 2026 by Pint Authors, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ...errors import (
    ArchiveReadOnlyError,
    NotContinuousError,
    NotMonotonicError,
    OutsideDomainError,
)

Number = int | float

__all__ = [
    "Affine",
    "CalibrationArchive",
    "CalibrationEdge",
    "CalibrationSegment",
    "CalibrationTable",
    "Interval",
    "PiecewisePolynomial",
    "Polynomial",
    "UnitDefinitionVersion",
]

_MONOTONE_SAMPLES = 257
_TOL = 1e-12


def _as_float(value: Number) -> float:
    return float(value)


def _close(a: float, b: float, rel: float = 1e-10, abs_: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


@dataclass(frozen=True)
class Interval:
    """A (possibly empty) closed interval.

    Empty intervals are represented by ``lo > hi`` so that interval algebra
    remains total.  ``math.inf`` can be used for unbounded ends.
    """

    lo: float
    hi: float

    def __post_init__(self):
        object.__setattr__(self, "lo", _as_float(self.lo))
        object.__setattr__(self, "hi", _as_float(self.hi))

    @classmethod
    def empty(cls) -> Interval:
        return cls(1.0, 0.0)

    @classmethod
    def from_value(cls, value: Number) -> Interval:
        value = _as_float(value)
        return cls(value, value)

    @property
    def is_empty(self) -> bool:
        return self.lo > self.hi

    @property
    def width(self) -> float:
        return 0.0 if self.is_empty else self.hi - self.lo

    @property
    def midpoint(self) -> float:
        if self.is_empty:
            raise ValueError("An empty interval has no midpoint.")
        if math.isinf(self.lo) or math.isinf(self.hi):
            return self.lo if math.isinf(self.hi) else self.hi
        return self.lo + (self.hi - self.lo) / 2.0

    def contains(self, value: Number, tol: float = 0.0) -> bool:
        if self.is_empty:
            return False
        value = _as_float(value)
        return self.lo - tol <= value <= self.hi + tol

    def intersection(self, other: Interval) -> Interval:
        if self.is_empty or other.is_empty:
            return Interval.empty()
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo > hi:
            return Interval.empty()
        return Interval(lo, hi)

    def overlaps(self, other: Interval, tol: float = 0.0) -> bool:
        return (
            not self.intersection(other).is_empty
            or _close(self.hi, other.lo, abs_=tol)
            or _close(self.lo, other.hi, abs_=tol)
        )

    def hull(self, other: Interval) -> Interval:
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return Interval(min(self.lo, other.lo), max(self.hi, other.hi))

    def split(self, points: Iterable[Number], tol: float = 1e-12) -> list[Interval]:
        """Split this interval at the given interior points."""
        if self.is_empty:
            return []
        points = sorted(
            p for p in (_as_float(p) for p in points) if self.lo < p < self.hi
        )
        out: list[Interval] = []
        lo = self.lo
        for point in points:
            if point - lo <= tol:
                continue
            out.append(Interval(lo, point))
            lo = point
        out.append(Interval(lo, self.hi))
        return out

    def __repr__(self) -> str:
        return f"Interval({self.lo!r}, {self.hi!r})"


@dataclass(frozen=True)
class Polynomial:
    """A real polynomial stored low-order coefficient first."""

    coefficients: tuple[float, ...] = (0.0,)

    def __post_init__(self):
        coefficients = tuple(_as_float(c) for c in self.coefficients)
        if not coefficients:
            coefficients = (0.0,)
        object.__setattr__(self, "coefficients", coefficients)

    @classmethod
    def from_coefficients(cls, *coefficients: Number) -> Polynomial:
        if len(coefficients) == 1 and not isinstance(coefficients[0], (int, float)):
            coefficients = tuple(coefficients[0])  # type: ignore[assignment]
        return cls(tuple(float(c) for c in coefficients))

    @classmethod
    def constant(cls, value: Number) -> Polynomial:
        return cls((float(value),))

    @classmethod
    def identity(cls) -> Polynomial:
        return cls((0.0, 1.0))

    @classmethod
    def linear(cls, slope: Number, intercept: Number = 0.0) -> Polynomial:
        return cls((float(intercept), float(slope)))

    @property
    def degree(self) -> int:
        return len(self.coefficients) - 1

    @property
    def is_constant(self) -> bool:
        return all(abs(c) <= _TOL for c in self.coefficients[1:])

    @property
    def is_zero(self) -> bool:
        return all(abs(c) <= _TOL for c in self.coefficients)

    def __call__(self, x: Number) -> float:
        x = _as_float(x)
        result = 0.0
        for coefficient in reversed(self.coefficients):
            result = result * x + coefficient
        return result

    def derivative(self) -> Polynomial:
        if self.degree == 0:
            return Polynomial.constant(0.0)
        return Polynomial(
            tuple(i * c for i, c in enumerate(self.coefficients) if i > 0)
        )

    def compose(self, other: Polynomial | Affine) -> Polynomial:
        """Return ``self(other(x))``."""
        if isinstance(other, Affine):
            other = other.as_polynomial()
        result = Polynomial.constant(0.0)
        power = Polynomial.constant(1.0)
        for coefficient in self.coefficients:
            result = Polynomial(
                tuple(
                    a + coefficient * b
                    for a, b in _zip_longest(result.coefficients, power.coefficients)
                )
            )
            power = _poly_mul(power, other)
        return result

    def scaled(self, scale: float, offset: float = 0.0) -> Polynomial:
        coefficients = list(self.coefficients)
        coefficients[0] = coefficients[0] * scale + offset
        for index in range(1, len(coefficients)):
            coefficients[index] *= scale
        return Polynomial(tuple(coefficients))

    def derivative_sign(self, lo: float, hi: float, samples: int = _MONOTONE_SAMPLES):
        """Return (min, max) of the derivative over an interval.

        Degrees up to three are checked exactly by including any real
        derivative extrema in the sample set; higher degrees fall back to
        dense sampling.
        """
        derivative = self.derivative()
        if derivative.is_constant:
            value = derivative(lo)
            return value, value
        if derivative.degree <= 2:
            # A derivative of degree <= 2 has at most one interior extremum;
            # including the roots of its own derivative makes the bounds exact.
            points = [lo, hi]
            second = derivative.derivative()
            points.extend(second.roots_in(lo, hi, samples=max(samples, 65)))
            values = [derivative(point) for point in points]
            return min(values), max(values)
        values = [
            derivative(lo + (hi - lo) * i / (samples - 1)) for i in range(samples)
        ]
        return min(values), max(values)

    def roots_in(self, lo: float, hi: float, samples: int = _MONOTONE_SAMPLES):
        """Find real roots by sign changes and exact sample hits."""
        if hi < lo:
            return []
        if hi == lo:
            return [lo] if abs(self(lo)) <= _TOL else []
        roots: list[float] = []
        prev_x = lo
        prev_y = self(prev_x)
        if abs(prev_y) <= _TOL:
            roots.append(prev_x)
        for index in range(1, samples):
            x = lo + (hi - lo) * index / (samples - 1)
            y = self(x)
            if abs(y) <= _TOL:
                roots.append(x)
            elif prev_y * y < 0:
                roots.append(_bisect(self, prev_x, x, prev_y, y))
            prev_x, prev_y = x, y
        return _dedupe_sorted(roots)

    def __repr__(self) -> str:
        return f"Polynomial({self.coefficients!r})"


@dataclass(frozen=True)
class Affine:
    """A non-singular affine transform ``scale * x + offset``."""

    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "scale", _as_float(self.scale))
        object.__setattr__(self, "offset", _as_float(self.offset))
        if self.scale == 0:
            raise ValueError("An affine transform must have a non-zero scale.")

    def __call__(self, x: Number) -> float:
        return self.scale * _as_float(x) + self.offset

    def inverse(self, y: Number) -> float:
        return (_as_float(y) - self.offset) / self.scale

    def as_polynomial(self) -> Polynomial:
        return Polynomial.linear(self.scale, self.offset)

    def composition(self, other: Affine) -> Affine:
        # self(other(x))
        return Affine(self.scale * other.scale, self.scale * other.offset + self.offset)

    def __repr__(self) -> str:
        return f"Affine({self.scale!r}, {self.offset!r})"


@dataclass(frozen=True)
class CalibrationSegment:
    """One polynomial segment of a calibration curve.

    Parameters
    ----------
    domain:
        The input interval on which this segment is valid.
    polynomial:
        The polynomial mapping an input magnitude to an output magnitude.
    direction:
        ``1`` for increasing, ``-1`` for decreasing and ``0`` to infer it.
    """

    domain: Interval
    polynomial: Polynomial
    direction: int = 0

    def __post_init__(self):
        domain = (
            self.domain if isinstance(self.domain, Interval) else Interval(*self.domain)
        )
        polynomial = (
            self.polynomial
            if isinstance(self.polynomial, Polynomial)
            else Polynomial.from_coefficients(*self.polynomial)
        )
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "polynomial", polynomial)
        if domain.is_empty:
            raise ValueError("A calibration segment cannot have an empty domain.")
        if self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0 or 1.")
        inferred = self._infer_direction()
        if self.direction == 0:
            object.__setattr__(self, "direction", inferred)
        elif self.direction != inferred:
            raise NotMonotonicError(domain)

    @classmethod
    def from_coefficients(
        cls, lo: Number, hi: Number, *coefficients: Number, direction: int = 0
    ) -> CalibrationSegment:
        return cls(
            Interval(lo, hi), Polynomial.from_coefficients(*coefficients), direction
        )

    def _infer_direction(self) -> int:
        lo, hi = self.domain.lo, self.domain.hi
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError("Calibration segment domains must be finite.")
        min_derivative, max_derivative = self.polynomial.derivative_sign(lo, hi)
        if min_derivative >= -_TOL and max_derivative > _TOL:
            return 1
        if max_derivative <= _TOL and min_derivative < -_TOL:
            return -1
        raise NotMonotonicError(self.domain)

    def __call__(self, x: Number) -> float:
        if not self.domain.contains(x):
            raise OutsideDomainError(x, self.domain)
        return self.polynomial(x)

    def inverse(self, y: Number, tol: float = 1e-12) -> float:
        return _bisect_monotone(
            self.polynomial,
            _as_float(y),
            self.domain.lo,
            self.domain.hi,
            self.direction,
            tol,
        )

    @property
    def range(self) -> Interval:
        lo = self.polynomial(self.domain.lo)
        hi = self.polynomial(self.domain.hi)
        return Interval(min(lo, hi), max(lo, hi))

    def __repr__(self) -> str:
        return f"CalibrationSegment({self.domain!r}, {self.polynomial!r})"


@dataclass(frozen=True)
class PiecewisePolynomial:
    """A piecewise-continuous, strictly monotone polynomial calibration."""

    segments: tuple[CalibrationSegment, ...]

    def __post_init__(self):
        segments = tuple(self.segments)
        if not segments:
            raise ValueError("A piecewise calibration needs at least one segment.")
        if not all(isinstance(segment, CalibrationSegment) for segment in segments):
            raise TypeError("segments must be CalibrationSegment instances.")
        object.__setattr__(self, "segments", segments)
        for left, right in zip(segments, segments[1:]):
            if not _close(left.domain.hi, right.domain.lo, rel=1e-10, abs_=1e-9):
                raise NotContinuousError(left.domain.hi, right.domain.lo)
            left_value = left.polynomial(left.domain.hi)
            right_value = right.polynomial(right.domain.lo)
            if not _close(left_value, right_value, rel=1e-10, abs_=1e-9):
                raise NotContinuousError(
                    left.domain.hi, right.domain.lo, left_value - right_value
                )
        directions = {segment.direction for segment in segments}
        if len(directions) != 1:
            raise NotMonotonicError(self.domain)

    @classmethod
    def from_segments(
        cls, segments: Iterable[CalibrationSegment | Sequence[Any]]
    ) -> PiecewisePolynomial:
        normalized = []
        for segment in segments:
            if isinstance(segment, CalibrationSegment):
                normalized.append(segment)
            elif len(segment) == 2:
                normalized.append(
                    CalibrationSegment(
                        segment[0], Polynomial.from_coefficients(*segment[1])
                    )
                )
            elif len(segment) == 3:
                normalized.append(CalibrationSegment.from_coefficients(*segment))
            else:
                raise TypeError(f"Cannot build a calibration segment from {segment!r}")
        return cls(tuple(normalized))

    @classmethod
    def polynomial(
        cls, domain: Interval | tuple[Number, Number], *coefficients: Number
    ) -> PiecewisePolynomial:
        if not isinstance(domain, Interval):
            domain = Interval(*domain)
        return cls(
            (CalibrationSegment(domain, Polynomial.from_coefficients(*coefficients)),)
        )

    @classmethod
    def linear(
        cls,
        domain: Interval | tuple[Number, Number],
        slope: Number,
        intercept: Number = 0.0,
    ) -> PiecewisePolynomial:
        return cls.polynomial(domain, intercept, slope)

    @property
    def domain(self) -> Interval:
        return Interval(self.segments[0].domain.lo, self.segments[-1].domain.hi)

    @property
    def direction(self) -> int:
        return self.segments[0].direction

    @property
    def breakpoints(self) -> tuple[float, ...]:
        return tuple(segment.domain.hi for segment in self.segments[:-1])

    @property
    def range(self) -> Interval:
        if self.direction > 0:
            return Interval(self(self.domain.lo), self(self.domain.hi))
        return Interval(self(self.domain.hi), self(self.domain.lo))

    def segment_for(self, x: Number) -> CalibrationSegment:
        x = _as_float(x)
        if not self.domain.contains(x):
            raise OutsideDomainError(x, self.domain)
        for index, segment in enumerate(self.segments):
            if segment.domain.contains(x):
                if index + 1 < len(self.segments) and _close(x, segment.domain.hi):
                    return self.segments[index + 1]
                return segment
        raise OutsideDomainError(x, self.domain)

    def __call__(self, x: Number) -> float:
        return self.segment_for(x)(x)

    def inverse(self, y: Number, tol: float = 1e-12) -> float:
        y = _as_float(y)
        for segment in self.segments:
            segment_range = segment.range
            if segment_range.contains(y, tol=tol):
                return segment.inverse(y, tol=tol)
        raise OutsideDomainError(y, self.range)

    def scaled(self, scale: float, offset: float = 0.0) -> PiecewisePolynomial:
        return PiecewisePolynomial(
            tuple(
                CalibrationSegment(
                    segment.domain,
                    segment.polynomial.scaled(scale, offset),
                    segment.direction if scale > 0 else -segment.direction,
                )
                for segment in self.segments
            )
        )

    def __repr__(self) -> str:
        return f"PiecewisePolynomial({self.segments!r})"


def _bisect(
    func,
    lo: float,
    hi: float,
    flo: float | None = None,
    fhi: float | None = None,
    tol: float = 1e-12,
) -> float:
    if flo is None:
        flo = func(lo)
    if fhi is None:
        fhi = func(hi)
    if abs(flo) <= tol:
        return lo
    if abs(fhi) <= tol:
        return hi
    for _ in range(200):
        mid = lo + (hi - lo) / 2.0
        fmid = func(mid)
        if abs(fmid) <= tol or (hi - lo) <= tol:
            return mid
        if (flo < 0) != (fmid < 0):
            hi, fhi = mid, fmid
        else:
            lo, flo = mid, fmid
    return lo + (hi - lo) / 2.0


def _bisect_monotone(
    func,
    target: float,
    lo: float,
    hi: float,
    direction: int,
    tol: float = 1e-12,
) -> float:
    flo = func(lo)
    fhi = func(hi)
    if direction > 0:
        if target < min(flo, fhi) - tol or target > max(flo, fhi) + tol:
            raise OutsideDomainError(target, Interval(min(flo, fhi), max(flo, fhi)))
    else:
        if target < min(flo, fhi) - tol or target > max(flo, fhi) + tol:
            raise OutsideDomainError(target, Interval(min(flo, fhi), max(flo, fhi)))
    for _ in range(200):
        mid = lo + (hi - lo) / 2.0
        value = func(mid)
        if abs(value - target) <= tol or (hi - lo) <= tol:
            return mid
        if (value < target) == (direction > 0):
            lo = mid
        else:
            hi = mid
    return lo + (hi - lo) / 2.0


def _dedupe_sorted(values: Iterable[float], tol: float = 1e-10) -> list[float]:
    out: list[float] = []
    for value in sorted(values):
        if not out or abs(value - out[-1]) > tol:
            out.append(value)
    return out


def _zip_longest(
    a: Sequence[float], b: Sequence[float]
) -> Iterator[tuple[float, float]]:
    length = max(len(a), len(b))
    for index in range(length):
        yield (
            a[index] if index < len(a) else 0.0,
            b[index] if index < len(b) else 0.0,
        )


def _poly_mul(a: Polynomial, b: Polynomial) -> Polynomial:
    coefficients = [0.0] * (len(a.coefficients) + len(b.coefficients) - 1)
    for i, ca in enumerate(a.coefficients):
        if ca == 0:
            continue
        for j, cb in enumerate(b.coefficients):
            coefficients[i + j] += ca * cb
    return Polynomial(tuple(coefficients))


@dataclass(frozen=True)
class UnitDefinitionVersion:
    """A pinned historical unit definition used by calibration edges."""

    name: str
    version: str
    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "scale", _as_float(self.scale))
        object.__setattr__(self, "offset", _as_float(self.offset))
        if self.scale == 0:
            raise ValueError("A unit definition scale must be non-zero.")

    @property
    def affine(self) -> Affine:
        return Affine(self.scale, self.offset)

    def fingerprint(self) -> tuple[Any, ...]:
        return ("unit", self.name, self.version, self.scale, self.offset)


@dataclass(frozen=True)
class CalibrationEdge:
    """A directed calibration relation between two nodes.

    Parameters
    ----------
    source, target:
        Node labels.  They may be unit names (for use with a registry) or
        arbitrary labels when the graph is used on plain magnitudes.
    calibration:
        A piecewise polynomial, polynomial or affine transform.
    domain:
        Optional input domain.  When omitted the calibration's own domain is
        used.  The domain is expressed in the source node's magnitude units.
    scale, offset:
        Optional historical unit scale and offset applied *after* the
        calibration polynomial.  This is equivalent to composing the curve
        with an affine unit definition.
    unit_versions:
        Pinned ``(unit, version)`` pairs.  A path may not use two different
        versions of the same unit.
    """

    source: Any
    target: Any
    calibration: PiecewisePolynomial | Polynomial | Affine
    domain: Interval | tuple[Number, Number] | None = None
    name: str | None = None
    scale: float = 1.0
    offset: float = 0.0
    unit_versions: tuple[tuple[str, str], ...] = ()
    metadata: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self):
        calibration = _normalize_transform(self.calibration)
        object.__setattr__(self, "calibration", calibration)
        if isinstance(self.domain, dict):
            object.__setattr__(
                self, "domain", Interval(self.domain["lo"], self.domain["hi"])
            )
        elif self.domain is not None and not isinstance(self.domain, Interval):
            object.__setattr__(self, "domain", Interval(*self.domain))
        object.__setattr__(self, "scale", _as_float(self.scale))
        object.__setattr__(self, "offset", _as_float(self.offset))
        if self.scale == 0:
            raise ValueError("A calibration edge scale must be non-zero.")
        versions = _normalize_pairs(self.unit_versions, "unit_versions")
        object.__setattr__(self, "unit_versions", versions)
        metadata = _normalize_pairs(self.metadata, "metadata")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(
            self, "name", _normalize_name(self.name, self.source, self.target)
        )
        domain = self.input_domain
        if domain.is_empty:
            raise ValueError("A calibration edge cannot have an empty domain.")
        if not (math.isfinite(domain.lo) and math.isfinite(domain.hi)):
            raise ValueError(
                "A calibration edge must have a finite input domain;"
                " provide `domain=` for polynomial or affine transforms."
            )
        if not isinstance(calibration, _CallableTransform):
            # Build the piecewise form eagerly so a non-monotone polynomial is
            # rejected at construction time, not on first conversion.
            self.piecewise

    @property
    def input_domain(self) -> Interval:
        if self.domain is not None:
            return self.domain
        calibration = self.calibration
        if isinstance(calibration, PiecewisePolynomial):
            return calibration.domain
        if isinstance(calibration, _CallableTransform):
            return Interval(-math.inf, math.inf)
        return Interval(-math.inf, math.inf)

    @property
    def output_range(self) -> Interval:
        domain = self.input_domain
        low = self(domain.lo)
        high = self(domain.hi)
        return Interval(min(low, high), max(low, high))

    @property
    def piecewise(self) -> PiecewisePolynomial:
        calibration = self.calibration
        if isinstance(calibration, PiecewisePolynomial):
            piecewise = _restrict_piecewise(calibration, self.input_domain)
            if self.scale != 1 or self.offset != 0:
                return piecewise.scaled(self.scale, self.offset)
            return piecewise
        domain = self.input_domain
        if isinstance(calibration, Affine):
            polynomial = calibration.as_polynomial()
        elif isinstance(calibration, Polynomial):
            polynomial = calibration
        else:
            raise TypeError(
                "Callable calibration transforms cannot be analysed over an"
                " interval; use a polynomial, affine or piecewise transform."
            )
        polynomial = polynomial.scaled(self.scale, self.offset)
        return PiecewisePolynomial((CalibrationSegment(domain, polynomial),))

    @property
    def unit_versions_dict(self) -> dict[str, str]:
        return dict(self.unit_versions)

    def __call__(self, x: Number) -> float:
        x = _as_float(x)
        if not self.input_domain.contains(x):
            raise OutsideDomainError(x, self.input_domain)
        value = self.calibration(x)
        return self.scale * value + self.offset

    def inverse(self, y: Number, tol: float = 1e-12) -> float:
        y = (_as_float(y) - self.offset) / self.scale
        x = self.calibration.inverse(y, tol=tol)
        if not self.input_domain.contains(x, tol=tol):
            raise OutsideDomainError(x, self.input_domain)
        return x

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            "edge",
            self.name,
            _freeze(self.source),
            _freeze(self.target),
            _transform_fingerprint(self.calibration),
            None if self.domain is None else (self.domain.lo, self.domain.hi),
            self.scale,
            self.offset,
            self.unit_versions,
            self.metadata,
        )

    def __repr__(self) -> str:
        return f"CalibrationEdge({self.name!r}, {self.source!r} -> {self.target!r})"


def _normalize_transform(transform) -> PiecewisePolynomial | Polynomial | Affine:
    if isinstance(transform, (PiecewisePolynomial, Polynomial, Affine)):
        return transform
    if callable(transform):
        # A callable is accepted for symmetry with contexts, but it cannot be
        # inverted or stamped into a signed archive.
        return _CallableTransform(transform)  # type: ignore[return-value]
    raise TypeError(f"Unsupported calibration transform: {transform!r}")


def _restrict_piecewise(
    piecewise: PiecewisePolynomial, domain: Interval
) -> PiecewisePolynomial:
    """Restrict a piecewise curve to ``domain``, keeping its own breakpoints."""
    overlap = piecewise.domain.intersection(domain)
    if overlap.is_empty:
        raise ValueError("The calibration domain does not overlap its edge domain.")
    split = {overlap.lo, overlap.hi}
    split.update(piecewise.breakpoints)
    segments: list[CalibrationSegment] = []
    for piece in overlap.split(split, tol=1e-12):
        midpoint = piece.midpoint
        segment = piecewise.segment_for(midpoint)
        segments.append(
            CalibrationSegment(piece, segment.polynomial, segment.direction)
        )
    return PiecewisePolynomial(tuple(segments))


@dataclass(frozen=True)
class _CallableTransform:
    func: Any

    def __call__(self, x):
        return self.func(x)


def _freeze(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    return value


def _normalize_pairs(value: Any, field_name: str) -> tuple[tuple[str, Any], ...]:
    """Accept mappings and iterables of pairs, and sort for stable hashing."""
    if value is None:
        return ()
    if isinstance(value, dict):
        pairs = value.items()
    else:
        pairs = value
    try:
        normalized = tuple(sorted(((str(k), v) for k, v in pairs), key=lambda p: p[0]))
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{field_name} must be a mapping or an iterable of pairs."
        ) from exc
    return normalized


def _normalize_name(name: str | None, source: Any, target: Any) -> str:
    if name is not None:
        return str(name)
    return f"{source}->{target}"


def _transform_fingerprint(transform: Any) -> tuple[Any, ...]:
    if isinstance(transform, PiecewisePolynomial):
        return (
            "piecewise",
            tuple(
                (segment.domain.lo, segment.domain.hi, segment.polynomial.coefficients)
                for segment in transform.segments
            ),
        )
    if isinstance(transform, Polynomial):
        return ("polynomial", transform.coefficients)
    if isinstance(transform, Affine):
        return ("affine", transform.scale, transform.offset)
    if isinstance(transform, _CallableTransform):
        return ("callable", id(transform.func))
    return (repr(transform),)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _signature(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class _Sealed:
    """Small mixin enforcing read-only semantics after construction."""

    _sealed: bool

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise ArchiveReadOnlyError(getattr(self, "name", None))
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            raise ArchiveReadOnlyError(getattr(self, "name", None))
        object.__delattr__(self, name)


@dataclass
class CalibrationTable(_Sealed):
    """A named, immutable collection of calibration edges."""

    name: str
    edges: tuple[CalibrationEdge, ...] = ()
    version: str = "1"
    unit_definitions: tuple[UnitDefinitionVersion, ...] = ()
    metadata: tuple[tuple[str, Any], ...] = ()
    signature: str = field(default="", init=False)
    _sealed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        edges = []
        for edge in self.edges:
            if isinstance(edge, CalibrationEdge):
                edges.append(edge)
            elif isinstance(edge, dict):
                edges.append(edge_from_dict(edge))
            else:
                raise TypeError(
                    "CalibrationTable edges must be CalibrationEdge instances"
                    f" or mappings, not {type(edge).__name__}."
                )
        object.__setattr__(self, "edges", tuple(edges))
        unit_definitions = []
        for definition in self.unit_definitions:
            if isinstance(definition, UnitDefinitionVersion):
                unit_definitions.append(definition)
            elif isinstance(definition, dict):
                unit_definitions.append(
                    UnitDefinitionVersion(
                        definition["name"],
                        definition.get("version", "1"),
                        definition.get("scale", 1.0),
                        definition.get("offset", 0.0),
                    )
                )
            else:
                raise TypeError(
                    "unit_definitions must contain UnitDefinitionVersion objects"
                    f" or mappings, not {type(definition).__name__}."
                )
        object.__setattr__(
            self,
            "unit_definitions",
            tuple(sorted(unit_definitions, key=lambda d: (d.name, d.version))),
        )
        object.__setattr__(
            self,
            "metadata",
            _normalize_pairs(self.metadata, "metadata"),
        )
        object.__setattr__(self, "signature", _signature(self._payload()))
        self._seal()

    def _payload(self) -> dict[str, Any]:
        return {
            "kind": "calibration-table",
            "name": self.name,
            "version": self.version,
            "edges": [edge.fingerprint() for edge in self.edges],
            "unit_definitions": [u.fingerprint() for u in self.unit_definitions],
            "metadata": self.metadata,
        }

    def add_edge(self, edge: CalibrationEdge) -> CalibrationTable:
        return CalibrationTable(
            self.name,
            self.edges + (edge,),
            self.version,
            self.unit_definitions,
            self.metadata,
        )

    def merge(
        self, other: CalibrationTable, name: str | None = None
    ) -> CalibrationTable:
        return CalibrationTable(
            name or self.name,
            self.edges + other.edges,
            self.version,
            self.unit_definitions + other.unit_definitions,
            self.metadata + other.metadata,
        )

    @property
    def read_only(self) -> bool:
        return True

    def verify(self) -> bool:
        return self.signature == _signature(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "edges": [edge_to_dict(edge) for edge in self.edges],
            "unit_definitions": [
                {
                    "name": u.name,
                    "version": u.version,
                    "scale": u.scale,
                    "offset": u.offset,
                }
                for u in self.unit_definitions
            ],
            "metadata": self.metadata,
            "signature": self.signature,
        }

    def __iter__(self) -> Iterator[CalibrationEdge]:
        return iter(self.edges)

    def __len__(self) -> int:
        return len(self.edges)


@dataclass
class CalibrationArchive(_Sealed):
    """A signed, read-only archive of calibration tables.

    Adding a table never mutates an existing archive; :meth:`add_table`
    returns a new revision whose signature covers all tables.
    """

    name: str
    tables: tuple[CalibrationTable, ...] = ()
    revision: int = 0
    metadata: tuple[tuple[str, Any], ...] = ()
    signature: str = field(default="", init=False)
    _sealed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        tables = []
        for table in self.tables:
            if not isinstance(table, CalibrationTable):
                raise TypeError(
                    "CalibrationArchive tables must be CalibrationTable instances,"
                    f" not {type(table).__name__}."
                )
            tables.append(table)
        object.__setattr__(self, "tables", tuple(tables))
        object.__setattr__(
            self,
            "metadata",
            _normalize_pairs(self.metadata, "metadata"),
        )
        object.__setattr__(self, "signature", _signature(self._payload()))
        self._seal()

    def _payload(self) -> dict[str, Any]:
        return {
            "kind": "calibration-archive",
            "name": self.name,
            "revision": self.revision,
            "tables": [table._payload() for table in self.tables],
            "metadata": self.metadata,
        }

    @classmethod
    def seal(
        cls,
        name: str,
        tables: Iterable[CalibrationTable] = (),
        *,
        revision: int = 0,
        metadata: Any = (),
    ) -> CalibrationArchive:
        return cls(name, tuple(tables), revision, metadata)

    def add_table(
        self,
        table: CalibrationTable,
        name: str | None = None,
        *,
        revision: int | None = None,
    ) -> CalibrationArchive:
        """Return a new signed revision containing ``table``."""
        return CalibrationArchive(
            name or self.name,
            self.tables + (table,),
            self.revision + 1 if revision is None else revision,
            self.metadata,
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, default=str
        )

    @classmethod
    def from_json(cls, text: str) -> CalibrationArchive:
        from .registry import calibration_archive_from_dict

        return calibration_archive_from_dict(json.loads(text))

    def save(self, path) -> None:
        from pathlib import Path

        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path) -> CalibrationArchive:
        from pathlib import Path

        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def with_table(
        self, table: CalibrationTable, name: str | None = None
    ) -> CalibrationArchive:
        return self.add_table(table, name)

    @property
    def read_only(self) -> bool:
        return True

    def verify(self) -> bool:
        if self.signature != _signature(self._payload()):
            return False
        return all(table.verify() for table in self.tables)

    def table(self, name: str) -> CalibrationTable:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    @property
    def edges(self) -> tuple[CalibrationEdge, ...]:
        return tuple(edge for table in self.tables for edge in table.edges)

    def graph(self, registry=None):
        from .objects import CalibrationGraph

        return CalibrationGraph(self.edges, registry=registry, archive=self)

    def revalidate(
        self,
        previous: CalibrationArchive,
        source: Any,
        target: Any,
        interval: Interval | tuple[Number, Number],
        *,
        tolerance: float = 1e-9,
        registry=None,
    ):
        """Compare this archive with ``previous`` over an input interval.

        Only the input ranges whose classification, value, or set of legal
        interpretations changed are reported as affected.
        """
        from .objects import revalidate_coverages

        old_coverage = previous.graph(registry=registry).classify(
            interval, source, target, tolerance=tolerance
        )
        new_coverage = self.graph(registry=registry).classify(
            interval, source, target, tolerance=tolerance
        )
        return revalidate_coverages(
            old_coverage,
            new_coverage,
            previous_revision=previous.revision,
            current_revision=self.revision,
            tolerance=tolerance,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "revision": self.revision,
            "tables": [table.to_dict() for table in self.tables],
            "metadata": self.metadata,
            "signature": self.signature,
        }


def edge_to_dict(edge: CalibrationEdge) -> dict[str, Any]:
    return {
        "name": edge.name,
        "source": _jsonable(edge.source),
        "target": _jsonable(edge.target),
        "calibration": _transform_to_dict(edge.calibration),
        "domain": None
        if edge.domain is None
        else {"lo": edge.domain.lo, "hi": edge.domain.hi},
        "scale": edge.scale,
        "offset": edge.offset,
        "unit_versions": list(edge.unit_versions),
        "metadata": list(edge.metadata),
    }


def edge_from_dict(data: Mapping[str, Any]) -> CalibrationEdge:
    calibration = _transform_from_dict(data["calibration"])
    domain = data.get("domain")
    if isinstance(domain, Mapping):
        domain = Interval(domain["lo"], domain["hi"])
    return CalibrationEdge(
        source=data["source"],
        target=data["target"],
        calibration=calibration,
        domain=domain,
        name=data.get("name"),
        scale=data.get("scale", 1.0),
        offset=data.get("offset", 0.0),
        unit_versions=data.get("unit_versions", ()),
        metadata=data.get("metadata", ()),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _transform_to_dict(transform: Any) -> dict[str, Any]:
    if isinstance(transform, PiecewisePolynomial):
        return {
            "kind": "piecewise",
            "segments": [
                {
                    "domain": {"lo": s.domain.lo, "hi": s.domain.hi},
                    "coefficients": list(s.polynomial.coefficients),
                }
                for s in transform.segments
            ],
        }
    if isinstance(transform, Polynomial):
        return {"kind": "polynomial", "coefficients": list(transform.coefficients)}
    if isinstance(transform, Affine):
        return {"kind": "affine", "scale": transform.scale, "offset": transform.offset}
    raise TypeError(f"Cannot serialize calibration transform {transform!r}")


def _transform_from_dict(data: Mapping[str, Any]) -> Any:
    kind = data["kind"]
    if kind == "piecewise":
        return PiecewisePolynomial.from_segments(
            [
                (
                    Interval(segment["domain"]["lo"], segment["domain"]["hi"]),
                    tuple(segment["coefficients"]),
                )
                for segment in data["segments"]
            ]
        )
    if kind == "polynomial":
        return Polynomial(tuple(data["coefficients"]))
    if kind == "affine":
        return Affine(data["scale"], data["offset"])
    raise ValueError(f"Unknown calibration transform kind {kind!r}")
