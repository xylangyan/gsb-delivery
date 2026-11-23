"""
pint.facets.calibration.objects
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Interval-aware analysis of calibration graphs.

The objects in this module answer three questions for a whole input interval:

* which parts have exactly one valid conversion;
* which parts have no valid conversion at all;
* which parts have two (or more) valid paths that disagree.

The analysis never picks an arbitrary path: :meth:`CalibrationGraph.convert`
raises instead of silently returning the shortest path when more than one
result exists.

:copyright: 2026 by Pint Authors, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from ...errors import (
    AmbiguousConversionError,
    ArchiveReadOnlyError,
    NoInverseError,
    NoPathError,
    NotContinuousError,
    OutsideDomainError,
)
from .definitions import (
    CalibrationArchive,
    CalibrationEdge,
    CalibrationSegment,
    CalibrationTable,
    Interval,
    PiecewisePolynomial,
    Polynomial,
)

Number = int | float

__all__ = [
    "CalibrationGraph",
    "CalibrationPath",
    "CalibrationResult",
    "CoverageReport",
    "CoverageSegment",
    "PathCoverage",
    "RevalidationReport",
    "classify",
    "revalidate_coverages",
]

_TOL = 1e-9


def _endpoint_tolerance(value: float) -> float:
    return max(1e-9, abs(value) * 1e-12)


@dataclass(frozen=True)
class PathCoverage:
    """The part of an input interval covered by one calibration path."""

    path: CalibrationPath
    interval: Interval
    transform: PiecewisePolynomial
    direction: int

    @property
    def output_range(self) -> Interval:
        low = self.transform(self.interval.lo)
        high = self.transform(self.interval.hi)
        return Interval(min(low, high), max(low, high))

    def __call__(self, value: Number) -> float:
        return self.transform(value)


@dataclass(frozen=True)
class CalibrationPath:
    """An ordered sequence of calibration edges.

    A path is valid only where every edge accepts the intermediate value.  The
    resulting transform is expressed directly in the source node's magnitude.
    """

    edges: tuple[CalibrationEdge, ...]
    source: Any = None
    target: Any = None

    def __post_init__(self):
        edges = tuple(self.edges)
        if not edges:
            raise ValueError("A calibration path needs at least one edge.")
        object.__setattr__(self, "edges", edges)
        if self.source is None:
            object.__setattr__(self, "source", edges[0].source)
        if self.target is None:
            object.__setattr__(self, "target", edges[-1].target)
        for left, right in zip(edges, edges[1:]):
            if left.target != right.source:
                raise ValueError(
                    f"Calibration path is disconnected: {left.target!r} != {right.source!r}"
                )

    @property
    def nodes(self) -> tuple[Any, ...]:
        return (self.source,) + tuple(edge.target for edge in self.edges)

    @property
    def name(self) -> str:
        return " -> ".join(str(node) for node in self.nodes)

    @property
    def key(self) -> str:
        """A stable identity that distinguishes parallel edges."""
        return " | ".join(str(edge.fingerprint()) for edge in self.edges)

    @property
    def label(self) -> str:
        """A human-readable path label including edge names."""
        parts = [str(self.source)]
        for edge in self.edges:
            parts.append(f"-[{edge.name}]->")
            parts.append(str(edge.target))
        return " ".join(parts)

    @property
    def edge_names(self) -> tuple[str, ...]:
        return tuple(edge.name for edge in self.edges)

    @property
    def unit_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for edge in self.edges:
            for unit, version in edge.unit_versions:
                previous = versions.get(unit)
                if previous is not None and previous != version:
                    raise ValueError(
                        f"Path uses two versions of {unit!r}: {previous!r} and {version!r}"
                    )
                versions[unit] = version
        return versions

    def _valid_interval(self, requested: Interval) -> Interval:
        # Walk backwards: the input of each edge must land inside the next
        # edge's input domain.  `allowed` is always expressed in the units of
        # the edge currently being examined.
        allowed = self.edges[-1].input_domain
        for edge in reversed(self.edges[:-1]):
            if allowed.is_empty:
                return Interval.empty()
            allowed = allowed.intersection(edge.output_range)
            if allowed.is_empty:
                # A single point can still be exactly on the edge of both
                # ranges; only a truly empty overlap is rejected.
                if edge.output_range.contains(allowed.lo) or edge.output_range.contains(
                    allowed.hi
                ):
                    allowed = Interval.from_value(allowed.lo)
                else:
                    return Interval.empty()
            low = edge.inverse(allowed.lo)
            high = edge.inverse(allowed.hi)
            allowed = Interval(min(low, high), max(low, high))
            if abs(allowed.lo - edge.input_domain.lo) <= _endpoint_tolerance(
                allowed.lo
            ):
                allowed = Interval(edge.input_domain.lo, allowed.hi)
            if abs(allowed.hi - edge.input_domain.hi) <= _endpoint_tolerance(
                allowed.hi
            ):
                allowed = Interval(allowed.lo, edge.input_domain.hi)
            allowed = allowed.intersection(edge.input_domain)
        return requested.intersection(allowed)

    def _map_breakpoint(self, edge_index: int, value: float) -> float | None:
        for edge in reversed(self.edges[:edge_index]):
            if not edge.output_range.contains(value):
                return None
            try:
                value = edge.inverse(value)
            except (OutsideDomainError, NoInverseError):
                return None
            if not edge.input_domain.contains(value):
                return None
        return value

    def coverage(
        self, requested: Interval | tuple[Number, Number] = (-math.inf, math.inf)
    ) -> PathCoverage | None:
        """Return the portion of ``requested`` where this path is valid."""
        if not isinstance(requested, Interval):
            if isinstance(requested, (int, float)):
                requested = Interval.from_value(requested)
            else:
                requested = Interval(*requested)
        valid = self._valid_interval(requested)
        if valid.is_empty or (
            valid.width != 0 and valid.hi - valid.lo <= _endpoint_tolerance(valid.lo)
        ):
            return None

        breakpoints: set[float] = set()
        # The domain of each edge can change the polynomial used by the path.
        candidates: set[float] = set()
        for index, edge in enumerate(self.edges):
            piecewise = edge.piecewise
            candidates.update(piecewise.breakpoints)
            candidates.update((piecewise.domain.lo, piecewise.domain.hi))
            candidates.update((edge.input_domain.lo, edge.input_domain.hi))
            for candidate in candidates:
                if not math.isfinite(candidate):
                    continue
                mapped = self._map_breakpoint(index, candidate)
                if mapped is not None and valid.lo < mapped < valid.hi:
                    breakpoints.add(mapped)

        # Snap analytic endpoints to nearby edge boundaries: inverse bisection
        # can leave a ~1e-13 seam that would otherwise surface as a sliver.
        for candidate in candidates:
            if not math.isfinite(candidate):
                continue
            if abs(candidate - valid.lo) <= _endpoint_tolerance(valid.lo):
                valid = Interval(candidate, valid.hi)
            if abs(candidate - valid.hi) <= _endpoint_tolerance(valid.hi):
                valid = Interval(valid.lo, candidate)
        for candidate in (requested.lo, requested.hi):
            if math.isfinite(candidate):
                if abs(candidate - valid.lo) <= _endpoint_tolerance(valid.lo):
                    valid = Interval(candidate, valid.hi)
                if abs(candidate - valid.hi) <= _endpoint_tolerance(valid.hi):
                    valid = Interval(valid.lo, candidate)
        if valid.is_empty:
            return None

        pieces: list[CalibrationSegment] = []
        for piece in valid.split(breakpoints):
            midpoint = piece.midpoint
            current_poly = Polynomial.identity()
            current_value = midpoint
            direction = 1
            ok = True
            for edge in self.edges:
                if not edge.input_domain.contains(current_value):
                    ok = False
                    break
                segment = edge.piecewise.segment_for(current_value)
                edge_poly = segment.polynomial
                current_poly = edge_poly.compose(current_poly)
                current_value = edge_poly(current_value)
                direction *= segment.direction
            if not ok:
                continue
            pieces.append(CalibrationSegment(piece, current_poly, direction))
        if not pieces:
            return None
        try:
            transform = PiecewisePolynomial(tuple(pieces))
        except (NotContinuousError, ValueError):
            # The pieces should meet exactly for continuous edge definitions.
            # If a numerical seam is slightly off, snap them together.
            snapped = []
            for index, segment in enumerate(pieces):
                polynomial = segment.polynomial
                if index:
                    previous = snapped[-1]
                    delta = previous.polynomial(previous.domain.hi) - polynomial(
                        segment.domain.lo
                    )
                    polynomial = Polynomial(
                        (polynomial.coefficients[0] + delta,)
                        + polynomial.coefficients[1:]
                    )
                snapped.append(
                    CalibrationSegment(segment.domain, polynomial, segment.direction)
                )
            transform = PiecewisePolynomial(tuple(snapped))
        return PathCoverage(self, valid, transform, transform.direction)

    def evaluate(self, value: Number) -> float:
        coverage = self.coverage(value)
        if coverage is None:
            raise NoPathError(value, self.source, self.target)
        return coverage.transform(value)

    def invert(self, value: Number) -> float:
        coverage = self.coverage(self._input_guess(value))
        if coverage is None:
            raise NoInverseError(value)
        return coverage.transform.inverse(value)

    def _input_guess(self, value: Number) -> Interval:
        # The path is monotone; the inverse can be found using the full source
        # domain and then checked against the output range.
        return Interval(-math.inf, math.inf)

    def fingerprint(self) -> tuple[Any, ...]:
        return tuple(edge.fingerprint() for edge in self.edges)

    def __len__(self) -> int:
        return len(self.edges)

    def __repr__(self) -> str:
        return f"CalibrationPath({self.name!r})"


@dataclass(frozen=True)
class CalibrationResult:
    """One value produced by one path."""

    value: float
    path: CalibrationPath

    @property
    def path_name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class CoverageSegment:
    """A contiguous input range with a single classification."""

    interval: Interval
    status: str
    results: tuple[CalibrationResult, ...] = ()
    paths: tuple[CalibrationPath, ...] = ()

    def __post_init__(self):
        if self.status not in ("unique", "ambiguous", "unreachable"):
            raise ValueError(f"Unknown coverage status {self.status!r}")

    @property
    def is_unique(self) -> bool:
        return self.status == "unique"

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def is_unreachable(self) -> bool:
        return self.status == "unreachable"

    @property
    def needs_review(self) -> bool:
        return self.status == "ambiguous"

    @property
    def value(self) -> float | None:
        if self.status != "unique" or not self.results:
            return None
        return self.results[0].value

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(result.value for result in self.results)

    @property
    def paths_by_value(self) -> tuple[tuple[float, tuple[CalibrationPath, ...]], ...]:
        """Group the valid paths by the value they produce."""
        if not self.values:
            return ()
        if self.status == "unique":
            return ((self.values[0], self.paths),)
        groups: dict[float, list[CalibrationPath]] = {}
        for result, path in zip(self.results, self.paths):
            for existing in groups:
                if math.isclose(result.value, existing, rel_tol=1e-9, abs_tol=1e-9):
                    groups[existing].append(path)
                    break
            else:
                groups[result.value] = [path]
        return tuple((value, tuple(paths)) for value, paths in sorted(groups.items()))

    def __repr__(self) -> str:
        return f"CoverageSegment({self.interval!r}, {self.status!r}, {self.values!r})"


@dataclass(frozen=True)
class CoverageReport:
    """Classification of an input interval against a calibration graph."""

    interval: Interval
    source: Any
    target: Any
    segments: tuple[CoverageSegment, ...]

    def __iter__(self) -> Iterator[CoverageSegment]:
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def _ranges(self, status: str) -> tuple[Interval, ...]:
        return tuple(
            segment.interval for segment in self.segments if segment.status == status
        )

    @property
    def unique(self) -> tuple[Interval, ...]:
        """Input ranges with exactly one conversion value."""
        return self._ranges("unique")

    @property
    def ambiguous(self) -> tuple[Interval, ...]:
        """Input ranges whose valid paths disagree and need review."""
        return self._ranges("ambiguous")

    @property
    def review(self) -> tuple[Interval, ...]:
        return self.ambiguous

    @property
    def unreachable(self) -> tuple[Interval, ...]:
        """Input ranges with no valid path."""
        return self._ranges("unreachable")

    @property
    def empty(self) -> tuple[Interval, ...]:
        return self.unreachable

    @property
    def status(self) -> str:
        if not self.segments:
            return "unreachable"
        statuses = {segment.status for segment in self.segments}
        if len(statuses) == 1:
            return statuses.pop()
        return "mixed"

    def segment_at(self, value: Number) -> CoverageSegment:
        for segment in self.segments:
            if segment.interval.contains(value):
                return segment
        raise OutsideDomainError(value, self.interval)

    def result_at(self, value: Number) -> CalibrationResult | None:
        segment = self.segment_at(value)
        if segment.status != "unique":
            return None
        return segment.results[0]

    def values_at(self, value: Number) -> tuple[float, ...]:
        return self.segment_at(value).values

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval": {"lo": self.interval.lo, "hi": self.interval.hi},
            "source": self.source,
            "target": self.target,
            "segments": [
                {
                    "interval": {"lo": segment.interval.lo, "hi": segment.interval.hi},
                    "status": segment.status,
                    "values": list(segment.values),
                    "paths": [path.label for path in segment.paths],
                }
                for segment in self.segments
            ],
        }

    def __repr__(self) -> str:
        return (
            f"CoverageReport({self.source!r} -> {self.target!r}, "
            f"{len(self.segments)} segments)"
        )


@dataclass(frozen=True)
class RevalidationReport:
    """The result of re-analysing a batch against a newer archive.

    ``affected`` lists only the input ranges whose classification or value
    changed.  ``unchanged`` lists the ranges that can reuse their previous
    result; the whole batch is never re-read.
    """

    previous_revision: int
    current_revision: int
    affected: tuple[Interval, ...]
    unchanged: tuple[Interval, ...]
    reason: tuple[tuple[Interval, str], ...] = ()
    new_paths: tuple[str, ...] = ()
    changed_values: tuple[
        tuple[Interval, tuple[float, ...], tuple[float, ...]], ...
    ] = ()
    review: tuple[Interval, ...] = ()

    @property
    def affected_ranges(self) -> tuple[Interval, ...]:
        return self.affected

    @property
    def unchanged_ranges(self) -> tuple[Interval, ...]:
        return self.unchanged

    @property
    def needs_quality_review(self) -> bool:
        return bool(self.review)

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_revision": self.previous_revision,
            "current_revision": self.current_revision,
            "affected": [
                {"lo": interval.lo, "hi": interval.hi} for interval in self.affected
            ],
            "unchanged": [
                {"lo": interval.lo, "hi": interval.hi} for interval in self.unchanged
            ],
            "review": [
                {"lo": interval.lo, "hi": interval.hi} for interval in self.review
            ],
            "reason": [
                {
                    "lo": interval.lo,
                    "hi": interval.hi,
                    "reason": reason,
                }
                for interval, reason in self.reason
            ],
            "new_paths": list(self.new_paths),
            "changed_values": [
                {
                    "lo": interval.lo,
                    "hi": interval.hi,
                    "before": list(before),
                    "after": list(after),
                }
                for interval, before, after in self.changed_values
            ],
        }


class CalibrationGraph:
    """A directed graph of piecewise calibration edges.

    The graph is deliberately conservative:

    * every simple path between source and target is considered;
    * a path with two different pinned versions of the same unit is discarded;
    * conversions with no path raise :class:`NoPathError`;
    * conversions with disagreeing valid paths raise
      :class:`AmbiguousConversionError`.

    Parameters
    ----------
    edges:
        Initial calibration edges.
    registry:
        Optional registry used by :meth:`to_quantity` for unit conversion.
    archive:
        When supplied, the graph is sealed to the archive and cannot be
        modified in place.
    """

    def __init__(
        self,
        edges: Iterable[CalibrationEdge] = (),
        *,
        registry: Any = None,
        archive: CalibrationArchive | None = None,
        source: Any = None,
        target: Any = None,
    ):
        self._edges: list[CalibrationEdge] = list(edges)
        self.registry = registry
        self.archive = archive
        self.source = source
        self.target = target
        self._sealed = archive is not None

    @classmethod
    def from_table(cls, table: CalibrationTable, **kwargs: Any) -> CalibrationGraph:
        return cls(table.edges, archive=None, **kwargs)

    @classmethod
    def from_archive(
        cls, archive: CalibrationArchive, **kwargs: Any
    ) -> CalibrationGraph:
        return cls(archive.edges, archive=archive, **kwargs)

    @property
    def edges(self) -> tuple[CalibrationEdge, ...]:
        return tuple(self._edges)

    @property
    def read_only(self) -> bool:
        return self._sealed

    def add_edge(self, edge: CalibrationEdge) -> CalibrationGraph:
        if self._sealed:
            raise ArchiveReadOnlyError("calibration graph")
        self._edges.append(edge)
        return self

    def with_edge(self, edge: CalibrationEdge) -> CalibrationGraph:
        if self._sealed:
            raise ArchiveReadOnlyError("calibration graph")
        return CalibrationGraph(
            self._edges + [edge],
            registry=self.registry,
            source=self.source,
            target=self.target,
        )

    def extend(self, edges: Iterable[CalibrationEdge]) -> CalibrationGraph:
        if self._sealed:
            raise ArchiveReadOnlyError("calibration graph")
        self._edges.extend(edges)
        return self

    def _adjacency(self) -> dict[Any, list[CalibrationEdge]]:
        adjacency: dict[Any, list[CalibrationEdge]] = {}
        for edge in self._edges:
            adjacency.setdefault(edge.source, []).append(edge)
        return adjacency

    def _simple_paths(
        self,
        source: Any | None = None,
        target: Any | None = None,
        *,
        include_conflicting: bool = False,
    ) -> list[CalibrationPath]:
        source = self.source if source is None else source
        target = self.target if target is None else target
        if source is None or target is None:
            if len(self._edges) == 1:
                return [CalibrationPath(self._edges)]
            raise ValueError("A source and target are required for a multi-edge graph.")
        if source == target:
            return []

        adjacency = self._adjacency()
        paths: list[CalibrationPath] = []

        def visit(node: Any, edges: list[CalibrationEdge], seen: set[Any]) -> None:
            for edge in adjacency.get(node, ()):
                if edge.target in seen:
                    continue
                next_edges = edges + [edge]
                if edge.target == target:
                    path = CalibrationPath(tuple(next_edges))
                    if include_conflicting or not self._has_conflicting_unit_versions(
                        path
                    ):
                        paths.append(path)
                    continue
                visit(edge.target, next_edges, seen | {edge.target})

        visit(source, [], {source})
        return paths

    def _all_simple_paths(self) -> list[CalibrationPath]:
        """Every simple path, including those rejected for version conflicts."""
        if self.source is None or self.target is None:
            if len(self._edges) == 1:
                return [CalibrationPath(self._edges)]
            return []
        return self._simple_paths(include_conflicting=True)

    @staticmethod
    def _has_conflicting_unit_versions(path: CalibrationPath) -> bool:
        versions: dict[str, str] = {}
        for edge in path.edges:
            for unit, version in edge.unit_versions:
                previous = versions.get(unit)
                if previous is not None and previous != version:
                    return True
                versions[unit] = version
        return False

    def paths(
        self, source: Any | None = None, target: Any | None = None
    ) -> tuple[CalibrationPath, ...]:
        """Return all simple valid paths between two nodes."""
        return tuple(self._simple_paths(source, target))

    def path_names(
        self, source: Any | None = None, target: Any | None = None
    ) -> tuple[str, ...]:
        return tuple(path.name for path in self.paths(source, target))

    def coverages(
        self,
        interval: Interval | tuple[Number, Number],
        source: Any | None = None,
        target: Any | None = None,
    ) -> tuple[PathCoverage, ...]:
        if not isinstance(interval, Interval):
            interval = Interval(*interval)
        out = []
        for path in self.paths(source, target):
            coverage = path.coverage(interval)
            if coverage is not None:
                out.append(coverage)
        return tuple(out)

    def classify(
        self,
        interval: Interval | tuple[Number, Number],
        source: Any | None = None,
        target: Any | None = None,
        *,
        tolerance: float = _TOL,
    ) -> CoverageReport:
        """Classify every part of ``interval`` as unique, ambiguous or unreachable."""
        if not isinstance(interval, Interval):
            interval = Interval(*interval)
        source = self.source if source is None else source
        target = self.target if target is None else target
        if interval.is_empty:
            return CoverageReport(interval, source, target, ())

        coverages = self.coverages(interval, source, target)
        if not coverages:
            return CoverageReport(
                interval,
                source,
                target,
                (CoverageSegment(interval, "unreachable"),),
            )

        boundaries: set[float] = {interval.lo, interval.hi}
        for coverage in coverages:
            boundaries.update((coverage.interval.lo, coverage.interval.hi))
            boundaries.update(coverage.transform.breakpoints)

        # Split where two valid paths meet, so a difference never hides in the
        # interior of a reported range.
        for index, left in enumerate(coverages):
            for right in coverages[index + 1 :]:
                overlap = left.interval.intersection(right.interval)
                if overlap.is_empty:
                    continue
                boundaries.update(
                    _crossing_points(left.transform, right.transform, overlap)
                )

        all_boundaries = sorted(
            point
            for point in boundaries
            if math.isfinite(point) and interval.lo <= point <= interval.hi
        )
        all_boundaries = _snap_boundaries(all_boundaries, interval)
        atomic = interval.split(all_boundaries)
        segments: list[CoverageSegment] = []
        for piece in atomic:
            midpoint = piece.midpoint
            active: list[tuple[CalibrationResult, CalibrationPath]] = []
            for coverage in coverages:
                if not coverage.interval.contains(midpoint):
                    continue
                try:
                    value = coverage.transform(midpoint)
                except (OutsideDomainError, ValueError):
                    continue
                active.append(
                    (
                        CalibrationResult(value, coverage.path),
                        coverage.path,
                    )
                )
            if not active:
                segments.append(CoverageSegment(piece, "unreachable"))
                continue
            values = [result.value for result, _ in active]
            if _all_close(values, tolerance):
                segments.append(
                    CoverageSegment(
                        piece,
                        "unique",
                        (active[0][0],),
                        tuple(path for _, path in active),
                    )
                )
            else:
                segments.append(
                    CoverageSegment(
                        piece,
                        "ambiguous",
                        tuple(result for result, _ in active),
                        tuple(path for _, path in active),
                    )
                )
        segments = _merge_segments(segments)
        return CoverageReport(interval, source, target, tuple(segments))

    def convert(
        self,
        value: Number,
        source: Any | None = None,
        target: Any | None = None,
        *,
        tolerance: float = _TOL,
    ) -> float:
        """Return a unique conversion or raise on no path / ambiguity."""
        report = self.classify(
            Interval.from_value(value), source, target, tolerance=tolerance
        )
        segment = report.segment_at(value)
        if segment.status == "unreachable":
            raise NoPathError(value, report.source, report.target)
        if segment.status == "ambiguous":
            raise AmbiguousConversionError(value, segment.values)
        return segment.results[0].value

    def results(
        self,
        value: Number,
        source: Any | None = None,
        target: Any | None = None,
        *,
        tolerance: float = _TOL,
    ) -> tuple[CalibrationResult, ...]:
        """Return every legitimate result for one input value."""
        report = self.classify(
            Interval.from_value(value), source, target, tolerance=tolerance
        )
        return report.segment_at(value).results

    def invert(
        self,
        value: Number,
        source: Any | None = None,
        target: Any | None = None,
        *,
        tolerance: float = _TOL,
    ) -> float:
        """Map a target value back to the source, requiring a unique answer."""
        paths = list(self.paths(source, target))
        candidates: list[float] = []
        for path in paths:
            coverage = path.coverage(Interval(-math.inf, math.inf))
            if coverage is None:
                continue
            if coverage.output_range.contains(value, tol=tolerance):
                try:
                    candidates.append(coverage.transform.inverse(value, tol=tolerance))
                except (OutsideDomainError, NoInverseError):
                    continue
        unique = _dedupe(candidates, tolerance)
        if not unique:
            raise NoInverseError(value)
        if len(unique) > 1:
            raise AmbiguousConversionError(value, tuple(unique))
        return unique[0]

    def trace(
        self,
        value: Number,
        source: Any | None = None,
        target: Any | None = None,
        *,
        tolerance: float = _TOL,
    ) -> tuple[CalibrationResult, ...]:
        """Return all path results without raising on disagreement."""
        return self.results(value, source, target, tolerance=tolerance)

    def validate(self) -> tuple[CalibrationPath, ...]:
        """Return paths that reuse two versions of the same unit.

        Such paths are never considered by :meth:`paths`, so this method is
        empty for graphs built normally.  It is useful as a self-check when
        constructing edges programmatically.
        """
        bad = []
        for path in self._all_simple_paths():
            if self._has_conflicting_unit_versions(path):
                bad.append(path)
        return tuple(bad)

    def to_quantity(self, value: Number, units: Any):
        if self.registry is None:
            raise ValueError("This calibration graph has no registry.")
        return self.registry.Quantity(value, units)

    def to_dict(self) -> dict[str, Any]:
        from .definitions import edge_to_dict

        return {
            "source": self.source,
            "target": self.target,
            "edges": [edge_to_dict(edge) for edge in self.edges],
        }

    def __iter__(self) -> Iterator[CalibrationEdge]:
        return iter(self._edges)

    def __len__(self) -> int:
        return len(self._edges)

    def __repr__(self) -> str:
        return f"CalibrationGraph({len(self._edges)} edges)"


def _crossing_points(
    left: PiecewisePolynomial, right: PiecewisePolynomial, overlap: Interval
) -> list[float]:
    """Return where two piecewise polynomials are equal on ``overlap``."""
    split = {overlap.lo, overlap.hi}
    split.update(left.breakpoints)
    split.update(right.breakpoints)
    points: list[float] = []
    for piece in overlap.split(split):
        midpoint = piece.midpoint
        try:
            left_poly = left.segment_for(midpoint).polynomial
            right_poly = right.segment_for(midpoint).polynomial
        except OutsideDomainError:
            continue
        difference = _poly_sub(left_poly, right_poly)
        if difference.is_constant:
            if abs(difference.coefficients[0]) <= 1e-12:
                continue
            # Parallel curves never cross.
            continue
        points.extend(difference.roots_in(piece.lo, piece.hi))
    return _dedupe(points, 1e-9)


def _poly_sub(a: Polynomial, b: Polynomial) -> Polynomial:
    length = max(len(a.coefficients), len(b.coefficients))
    coefficients = []
    for index in range(length):
        ca = a.coefficients[index] if index < len(a.coefficients) else 0.0
        cb = b.coefficients[index] if index < len(b.coefficients) else 0.0
        coefficients.append(ca - cb)
    return Polynomial(tuple(coefficients))


def _all_close(values: Sequence[float], tolerance: float) -> bool:
    if not values:
        return True
    first = values[0]
    return all(
        math.isclose(value, first, rel_tol=tolerance, abs_tol=tolerance)
        for value in values[1:]
    )


def _dedupe(values: Iterable[float], tolerance: float = 1e-9) -> list[float]:
    out: list[float] = []
    for value in sorted(values):
        if not out or abs(value - out[-1]) > tolerance:
            out.append(value)
    return out


def _snap_boundaries(points: Sequence[float], interval: Interval) -> list[float]:
    """Remove hairline slivers caused by inverse bisection tolerance.

    A span narrower than a millionth of the requested interval carries no
    information: it is either a numerical seam or a single endpoint.  Snap it
    to the nearer side and drop duplicate/outside points.
    """
    if not points:
        return []
    width = interval.width
    tolerance = max(1e-12, (width if math.isfinite(width) else 1.0) * 1e-6)
    snapped: list[float] = []
    for point in points:
        point = min(max(point, interval.lo), interval.hi)
        if snapped and point - snapped[-1] <= tolerance:
            # Keep the point closer to an actual edge of the requested range;
            # otherwise the first of the pair is representative.
            if abs(point - interval.hi) < abs(snapped[-1] - interval.hi):
                snapped[-1] = point
            continue
        snapped.append(point)
    if len(snapped) >= 2 and snapped[-1] - snapped[-2] <= tolerance:
        snapped.pop()
    return snapped


def _merge_segments(segments: Sequence[CoverageSegment]) -> list[CoverageSegment]:
    """Merge adjacent segments with identical status, values and paths."""
    merged: list[CoverageSegment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        same_status = previous.status == segment.status
        same_values = _values_equal(previous.values, segment.values, 1e-12)
        same_paths = {path.key for path in previous.paths} == {
            path.key for path in segment.paths
        }
        touches = math.isclose(
            previous.interval.hi, segment.interval.lo, rel_tol=1e-12, abs_tol=1e-9
        )
        if same_status and same_values and same_paths and touches:
            merged[-1] = CoverageSegment(
                Interval(previous.interval.lo, segment.interval.hi),
                previous.status,
                previous.results,
                previous.paths,
            )
        else:
            merged.append(segment)
    return merged


def classify(
    graph: CalibrationGraph,
    interval: Interval | tuple[Number, Number],
    source: Any | None = None,
    target: Any | None = None,
    **kwargs: Any,
) -> CoverageReport:
    """Module-level convenience wrapper for :meth:`CalibrationGraph.classify`."""
    return graph.classify(interval, source, target, **kwargs)


def revalidate_coverages(
    previous: CoverageReport,
    current: CoverageReport,
    *,
    previous_revision: int = 0,
    current_revision: int = 1,
    tolerance: float = 1e-9,
) -> RevalidationReport:
    """Compare two coverage reports and return only changed ranges."""
    boundaries: set[float] = {
        previous.interval.lo,
        previous.interval.hi,
        current.interval.lo,
        current.interval.hi,
    }
    for report in (previous, current):
        for segment in report.segments:
            boundaries.update((segment.interval.lo, segment.interval.hi))
    interval = previous.interval.hull(current.interval)
    points = sorted(
        point
        for point in boundaries
        if math.isfinite(point) and interval.lo <= point <= interval.hi
    )
    affected: list[Interval] = []
    unchanged: list[Interval] = []
    reasons: list[tuple[Interval, str]] = []
    changed: list[tuple[Interval, tuple[float, ...], tuple[float, ...]]] = []
    review: list[Interval] = []
    new_paths: set[str] = set()

    previous_paths = {
        path.key for segment in previous.segments for path in segment.paths
    }
    current_paths = {path.key for segment in current.segments for path in segment.paths}
    current_labels = {
        path.key: path.label for segment in current.segments for path in segment.paths
    }
    new_paths = current_paths - previous_paths

    for piece in interval.split(points):
        midpoint = piece.midpoint
        old_segment = _segment_at(previous, midpoint)
        new_segment = _segment_at(current, midpoint)
        old_status = None if old_segment is None else old_segment.status
        new_status = None if new_segment is None else new_segment.status
        old_values = _values_at(previous, midpoint)
        new_values = _values_at(current, midpoint)

        status_changed = old_status != new_status
        values_changed = not _values_equal(old_values, new_values, tolerance)
        added_path = bool(
            new_segment is not None
            and any(path.key not in previous_paths for path in new_segment.paths)
        )
        if status_changed or values_changed:
            affected.append(piece)
            if status_changed and new_status == "ambiguous":
                reason = "new ambiguity"
            elif status_changed and new_status == "unreachable":
                reason = "no longer reachable"
            elif status_changed and new_status == "unique":
                reason = "now uniquely reachable"
            elif values_changed:
                reason = "conversion value changed"
            else:
                reason = "classification changed"
            reasons.append((piece, reason))
            if new_status == "ambiguous":
                review.append(piece)
            if values_changed:
                changed.append((piece, old_values, new_values))
        elif added_path:
            # A new legal explanation exists even if it happens to agree with
            # the old result.  The affected range is reported for quality
            # review, but existing values can be reused.
            affected.append(piece)
            reasons.append((piece, "new legal interpretation"))
            review.append(piece)
        else:
            unchanged.append(piece)

    affected = _merge_reasoned(reasons)
    unchanged = _merge_intervals(unchanged)
    review = _merge_intervals(review)
    return RevalidationReport(
        previous_revision,
        current_revision,
        tuple(affected),
        tuple(unchanged),
        tuple(reasons),
        tuple(sorted(current_labels.get(key, key) for key in new_paths)),
        tuple(changed),
        tuple(review),
    )


def _segment_at(report: CoverageReport, value: Number) -> CoverageSegment | None:
    for segment in report.segments:
        if segment.interval.contains(value):
            return segment
    return None


def _values_at(report: CoverageReport, value: Number) -> tuple[float, ...]:
    # Evaluate at the sample point instead of reusing a segment's midpoint
    # value: revalidation splits the interval differently from the reports.
    values: list[float] = []
    for path in report_segment_paths(report).values():
        try:
            values.append(path.evaluate(value))
        except (NoPathError, OutsideDomainError):
            continue
    return tuple(sorted(values))


def report_segment_paths(report: CoverageReport) -> dict[str, CalibrationPath]:
    paths: dict[str, CalibrationPath] = {}
    for segment in report.segments:
        for path in segment.paths:
            paths[path.key] = path
    return paths


def _values_equal(a: Sequence[float], b: Sequence[float], tolerance: float) -> bool:
    """Compare value sets, ignoring duplicate paths that agree."""
    left = _dedupe(a, tolerance)
    right = _dedupe(b, tolerance)
    if len(left) != len(right):
        return False
    return all(
        math.isclose(x, y, rel_tol=tolerance, abs_tol=tolerance)
        for x, y in zip(left, right)
    )


def _merge_intervals(
    intervals: Iterable[Interval], tolerance: float = 1e-9
) -> list[Interval]:
    ordered = sorted(intervals, key=lambda interval: interval.lo)
    out: list[Interval] = []
    for interval in ordered:
        if interval.is_empty:
            continue
        if out and interval.lo <= out[-1].hi + tolerance:
            out[-1] = Interval(out[-1].lo, max(out[-1].hi, interval.hi))
        else:
            out.append(interval)
    return out


def _merge_reasoned(
    reasons: Iterable[tuple[Interval, str]], tolerance: float = 1e-9
) -> list[Interval]:
    """Merge ranges only when their review reason is identical."""
    ordered = sorted(reasons, key=lambda item: (item[0].lo, item[1]))
    out: list[Interval] = []
    out_reason: list[str] = []
    for interval, reason in ordered:
        if interval.is_empty:
            continue
        if out and out_reason[-1] == reason and interval.lo <= out[-1].hi + tolerance:
            out[-1] = Interval(out[-1].lo, max(out[-1].hi, interval.hi))
        else:
            out.append(interval)
            out_reason.append(reason)
    return out
