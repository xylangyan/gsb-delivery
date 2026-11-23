"""
pint.facets.calibration.registry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Registry support for interval-aware calibration graphs.

Existing context conversions are untouched: a context still follows a single
active rule set.  Calibration conversions are opt-in and can report every
legitimate path, which makes ambiguous instrument conversions visible instead
of silently choosing the shortest path.

:copyright: 2026 by Pint Authors, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ...errors import CalibrationError
from ..plain import GenericPlainRegistry
from . import objects
from .definitions import (
    Affine,
    CalibrationArchive,
    CalibrationEdge,
    CalibrationTable,
    Interval,
    PiecewisePolynomial,
    Polynomial,
    UnitDefinitionVersion,
    edge_from_dict,
)

__all__ = ["GenericCalibrationRegistry"]


class GenericCalibrationRegistry[QuantityT, UnitT](
    GenericPlainRegistry[QuantityT, UnitT]
):
    """Add calibration graphs and signed archives to a registry."""

    CalibrationGraph = objects.CalibrationGraph
    CalibrationPath = objects.CalibrationPath
    CalibrationEdge = CalibrationEdge
    CalibrationTable = CalibrationTable
    CalibrationArchive = CalibrationArchive
    CalibrationInterval = Interval
    CalibrationUnitDefinition = UnitDefinitionVersion

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._calibration_edges: list[CalibrationEdge] = []
        self._calibration_archive: CalibrationArchive | None = None
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Building graphs
    # ------------------------------------------------------------------
    def _normalize_edge(
        self,
        source: Any = None,
        target: Any = None,
        calibration: Any = None,
        *,
        domain: Any = None,
        scale: float = 1.0,
        offset: float = 0.0,
        unit_versions: Iterable[tuple[str, str]] = (),
        name: str | None = None,
        metadata: Iterable[tuple[str, Any]] = (),
        edge: CalibrationEdge | None = None,
    ) -> CalibrationEdge:
        if edge is not None:
            if (
                any(
                    value is not None for value in (source, target, calibration, domain)
                )
                or scale != 1.0
                or offset != 0.0
                or name is not None
                or tuple(unit_versions)
                or tuple(metadata)
            ):
                raise TypeError("Pass either `edge=` or edge parameters, not both.")
            return edge
        if source is None or target is None or calibration is None:
            raise TypeError("source, target and calibration are required.")
        return CalibrationEdge(
            source,
            target,
            calibration,
            domain=domain,
            name=name,
            scale=scale,
            offset=offset,
            unit_versions=tuple(unit_versions),
            metadata=tuple(metadata),
        )

    def add_calibration(
        self,
        source: Any = None,
        target: Any = None,
        calibration: Any = None,
        *,
        domain: Any = None,
        scale: float = 1.0,
        offset: float = 0.0,
        unit_versions: Iterable[tuple[str, str]] = (),
        name: str | None = None,
        metadata: Iterable[tuple[str, Any]] = (),
        edge: CalibrationEdge | None = None,
    ) -> CalibrationEdge:
        """Add a calibration edge to the registry graph."""
        normalized = self._normalize_edge(
            source,
            target,
            calibration,
            domain=domain,
            scale=scale,
            offset=offset,
            unit_versions=unit_versions,
            name=name,
            metadata=metadata,
            edge=edge,
        )
        self._calibration_edges.append(normalized)
        return normalized

    define_calibration = add_calibration
    register_calibration = add_calibration

    def add_calibration_segment(
        self,
        source: Any,
        target: Any,
        lo: float,
        hi: float,
        *coefficients: float,
        direction: int = 0,
        **kwargs: Any,
    ) -> CalibrationEdge:
        """Convenience helper for a single polynomial segment."""
        segment = CalibrationEdge(
            source,
            target,
            PiecewisePolynomial.polynomial((lo, hi), *coefficients),
            **kwargs,
        )
        self._calibration_edges.append(segment)
        return segment

    def remove_calibration(self, name: str) -> CalibrationEdge:
        """Remove a calibration edge by name."""
        for index, edge in enumerate(self._calibration_edges):
            if edge.name == name:
                return self._calibration_edges.pop(index)
        raise KeyError(name)

    def clear_calibrations(self) -> None:
        self._calibration_edges.clear()

    @property
    def calibration_edges(self) -> tuple[CalibrationEdge, ...]:
        """All edges visible to the current calibration graph."""
        edges: list[CalibrationEdge] = []
        if self._calibration_archive is not None:
            edges.extend(self._calibration_archive.edges)
        edges.extend(self._calibration_edges)
        return tuple(edges)

    # ------------------------------------------------------------------
    # Archives
    # ------------------------------------------------------------------
    @property
    def calibration_archive(self) -> CalibrationArchive | None:
        return self._calibration_archive

    def seal_calibration_archive(
        self,
        name: str = "calibration",
        *,
        revision: int | None = None,
        metadata: Iterable[tuple[str, Any]] = (),
        include_units: bool = False,
    ) -> CalibrationArchive:
        """Freeze the calibrations added since the last signed revision.

        The returned archive is read-only.  Calibrations already present in a
        previous archive are not copied into the new table; each revision
        contains only what was newly signed.  ``archive.edges`` still exposes
        the complete history.
        """
        new_edges = tuple(self._calibration_edges)
        previous = self._calibration_archive
        if revision is None:
            revision = 0 if previous is None else previous.revision + 1
        table = CalibrationTable(
            name,
            new_edges,
            version=str(revision),
            unit_definitions=self._unit_definition_versions() if include_units else (),
        )
        if previous is None:
            archive = CalibrationArchive(name, (table,), revision, tuple(metadata))
        else:
            archive = previous.add_table(table, revision=revision)
        self._calibration_archive = archive
        self._calibration_edges.clear()
        return archive

    def _unit_definition_versions(self) -> tuple[UnitDefinitionVersion, ...]:
        versions = []
        for name, definition in self._units.items():
            converter = getattr(definition, "converter", None)
            if converter is None:
                continue
            scale = getattr(converter, "scale", 1)
            offset = getattr(converter, "offset", 0)
            if not isinstance(scale, (int, float)):
                continue
            versions.append(UnitDefinitionVersion(name, "current", scale, offset))
        return tuple(versions)

    def load_calibration_archive(
        self, archive: CalibrationArchive | str | Path | dict[str, Any]
    ) -> CalibrationArchive:
        """Install a signed archive, optionally adding a newer table.

        Passing an archive whose name matches the current archive appends its
        tables and increments the revision.  A read-only error is raised if
        the caller tries to mutate the archive object in place.
        """
        if isinstance(archive, (str, Path)):
            archive = calibration_archive_from_json(
                Path(archive).read_text(encoding="utf-8")
            )
        elif isinstance(archive, dict):
            archive = calibration_archive_from_dict(archive)
        if not isinstance(archive, CalibrationArchive):
            raise TypeError("Expected a CalibrationArchive.")
        if self._calibration_archive is None:
            self._calibration_archive = archive
        elif archive is not self._calibration_archive:
            self._calibration_archive = self._calibration_archive.add_table(
                archive.tables[-1], name=self._calibration_archive.name
            )
        return self._calibration_archive

    def set_calibration_archive(self, archive: CalibrationArchive) -> None:
        if not isinstance(archive, CalibrationArchive):
            raise TypeError("Expected a CalibrationArchive.")
        self._calibration_archive = archive

    def calibration_table(self, name: str = "calibration") -> CalibrationTable:
        if self._calibration_archive is not None:
            return self._calibration_archive.table(name)
        return CalibrationTable(name, tuple(self._calibration_edges))

    # ------------------------------------------------------------------
    # Graph analysis
    # ------------------------------------------------------------------
    def calibration_graph(
        self,
        source: Any = None,
        target: Any = None,
        *,
        archive: CalibrationArchive | None = None,
        include_pending: bool = True,
    ) -> objects.CalibrationGraph:
        """Build a sealed calibration graph.

        By default this uses the current signed archive plus any calibrations
        added since the last seal.  Pass ``include_pending=False`` to analyse
        exactly one signed revision.
        """
        if archive is None:
            archive = self._calibration_archive
        edges: list[CalibrationEdge] = []
        if archive is not None:
            edges.extend(archive.edges)
        if include_pending:
            edges.extend(self._calibration_edges)
        return objects.CalibrationGraph(
            edges,
            registry=self,
            archive=archive if archive is not None else None,
            source=source,
            target=target,
        )

    def calibration_paths(
        self, source: Any, target: Any, *, archive: CalibrationArchive | None = None
    ) -> tuple[objects.CalibrationPath, ...]:
        return self.calibration_graph(source, target, archive=archive).paths()

    def calibration_path_names(
        self, source: Any, target: Any, *, archive: CalibrationArchive | None = None
    ) -> tuple[str, ...]:
        return tuple(
            path.name
            for path in self.calibration_paths(source, target, archive=archive)
        )

    def classify_calibration(
        self,
        interval: Interval | tuple[float, float],
        source: Any,
        target: Any,
        *,
        archive: CalibrationArchive | None = None,
        tolerance: float = 1e-9,
    ) -> objects.CoverageReport:
        return self.calibration_graph(source, target, archive=archive).classify(
            interval, source, target, tolerance=tolerance
        )

    calibration_coverage = classify_calibration
    calibration_coverages = classify_calibration

    def convert_calibration(
        self,
        value: float,
        source: Any,
        target: Any,
        *,
        archive: CalibrationArchive | None = None,
        tolerance: float = 1e-9,
    ) -> float:
        return self.calibration_graph(source, target, archive=archive).convert(
            value, source, target, tolerance=tolerance
        )

    def calibration_results(
        self,
        value: float,
        source: Any,
        target: Any,
        *,
        archive: CalibrationArchive | None = None,
        tolerance: float = 1e-9,
    ) -> tuple[objects.CalibrationResult, ...]:
        return self.calibration_graph(source, target, archive=archive).results(
            value, source, target, tolerance=tolerance
        )

    def invert_calibration(
        self,
        value: float,
        source: Any,
        target: Any,
        *,
        archive: CalibrationArchive | None = None,
        tolerance: float = 1e-9,
    ) -> float:
        return self.calibration_graph(source, target, archive=archive).invert(
            value, source, target, tolerance=tolerance
        )

    invert_calibration_value = invert_calibration

    def revalidate_calibration(
        self,
        previous: CalibrationArchive,
        *args: Any,
        archive: CalibrationArchive | None = None,
        tolerance: float = 1e-9,
        interval: Interval | tuple[float, float] | None = None,
        source: Any = None,
        target: Any = None,
    ) -> objects.RevalidationReport:
        """Re-check only the changed input ranges against a newer archive.

        Accepts both ``(previous, source, target, interval)`` and the legacy
        ``(previous, interval, source, target)`` argument order.
        """
        if interval is None:
            interval, source, target = _parse_revalidation_args(*args)
        else:
            leftover = list(args)
            if source is None and leftover:
                source = leftover.pop(0)
            if target is None and leftover:
                target = leftover.pop(0)
        current = archive if archive is not None else self._calibration_archive
        if current is None:
            current = self.seal_calibration_archive("calibration")
        return current.revalidate(
            previous, source, target, interval, tolerance=tolerance, registry=self
        )

    def save_calibration_archive(
        self, path: str | Path, archive: CalibrationArchive | None = None
    ) -> Path:
        archive = archive if archive is not None else self._calibration_archive
        if archive is None:
            raise CalibrationError("There is no signed calibration archive to save.")
        destination = Path(path)
        destination.write_text(calibration_archive_to_json(archive), encoding="utf-8")
        return destination

    def load_calibration_file(
        self, path: str | Path, *, install: bool = True
    ) -> CalibrationArchive:
        archive = calibration_archive_from_json(Path(path).read_text(encoding="utf-8"))
        if install:
            self.load_calibration_archive(archive)
        return archive

    def classify_calibration_quantity(
        self,
        interval: Any,
        source: Any,
        target: Any,
        **kwargs: Any,
    ):
        """Classify a Quantity interval and return Quantity-valued segments.

        ``source`` and ``target`` are node labels, not units.  To use units,
        pass a ``unit=`` keyword: the bounds are converted to that unit and
        the returned values carry it.
        """
        unit = kwargs.pop("unit", None)
        low, high = interval
        if unit is not None:
            if hasattr(low, "to"):
                low = low.to(unit).magnitude
            if hasattr(high, "to"):
                high = high.to(unit).magnitude
        elif hasattr(low, "magnitude") and hasattr(high, "to"):
            high = high.to(low.units).magnitude
            low = low.magnitude
        else:
            low = getattr(low, "magnitude", low)
            high = getattr(high, "magnitude", high)
        report = self.classify_calibration((low, high), source, target, **kwargs)
        out = []
        for segment in report.segments:
            values = tuple(
                self.Quantity(v, unit) if unit is not None else v
                for v in segment.values
            )
            out.append(
                (
                    Interval(segment.interval.lo, segment.interval.hi),
                    segment.status,
                    values,
                )
            )
        return out

    def calibration_to_quantity(self, value: float, units: Any):
        return self.Quantity(value, units)


def calibration_archive_to_dict(archive: CalibrationArchive) -> dict[str, Any]:
    return archive.to_dict()


def _parse_revalidation_args(
    *args: Any,
) -> tuple[Interval | tuple[float, float], Any, Any]:
    """Accept both (source, target, interval) and (interval, source, target)."""
    if len(args) != 3:
        raise TypeError(
            "revalidate_calibration() needs previous, source, target and interval."
        )
    first, second, third = args
    if _looks_like_interval(first):
        interval, source, target = first, second, third
    elif _looks_like_interval(third):
        source, target, interval = first, second, third
    else:
        raise TypeError("Could not find the input interval in the arguments.")
    return interval, source, target


def _looks_like_interval(value: Any) -> bool:
    if isinstance(value, Interval):
        return True
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return all(isinstance(item, (int, float)) for item in value)
    return False


def calibration_archive_from_dict(data: dict[str, Any]) -> CalibrationArchive:
    tables = []
    for table_data in data["tables"]:
        tables.append(
            CalibrationTable(
                table_data["name"],
                tuple(edge_from_dict(edge) for edge in table_data["edges"]),
                table_data.get("version", "1"),
                tuple(
                    UnitDefinitionVersion(
                        unit["name"],
                        unit["version"],
                        unit.get("scale", 1.0),
                        unit.get("offset", 0.0),
                    )
                    for unit in table_data.get("unit_definitions", ())
                ),
                tuple(tuple(item) for item in table_data.get("metadata", ())),
            )
        )
    return CalibrationArchive(
        data["name"],
        tuple(tables),
        data.get("revision", 0),
        tuple(tuple(item) for item in data.get("metadata", ())),
    )


def calibration_archive_to_json(archive: CalibrationArchive, indent: int = 2) -> str:
    return json.dumps(archive.to_dict(), ensure_ascii=False, indent=indent, default=str)


def calibration_archive_from_json(text: str) -> CalibrationArchive:
    return calibration_archive_from_dict(json.loads(text))


def calibration_value_in_range(value: float, interval: Interval) -> bool:
    return interval.contains(value)


def intervals_overlap(a: Interval, b: Interval, tolerance: float = 1e-9) -> bool:
    return a.overlaps(b, tol=tolerance)


def interval_from_quantities(lo, hi, unit=None) -> Interval:
    if unit is not None:
        lo = lo.to(unit).magnitude
        hi = hi.to(unit).magnitude
    elif hasattr(lo, "to") and hasattr(hi, "to"):
        hi = hi.to(lo.units).magnitude
        lo = lo.magnitude
    else:
        lo, hi = getattr(lo, "magnitude", lo), getattr(hi, "magnitude", hi)
    return Interval(float(lo), float(hi))


def calibrate_value(value, graph: objects.CalibrationGraph, source=None, target=None):
    return graph.convert(value, source, target)


# Keep the low-level helpers importable from the registry module as well.
__all__ += [
    "calibration_archive_from_dict",
    "calibration_archive_from_json",
    "calibration_archive_to_dict",
    "calibration_archive_to_json",
    "calibration_value_in_range",
    "intervals_overlap",
    "interval_from_quantities",
    "calibrate_value",
    "Affine",
    "Polynomial",
    "PiecewisePolynomial",
    "Interval",
]
