"""
pint.facets.calibration
~~~~~~~~~~~~~~~~~~~~~~~

Calibration-aware conversions between instrument readings.

The facet is optional and does not alter ordinary unit or context
conversion.  It adds piecewise monotone polynomial calibrations, interval
coverage analysis with ambiguity detection, and signed read-only archives.

:copyright: 2026 by Pint Authors, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import annotations

from .definitions import (
    Affine,
    CalibrationArchive,
    CalibrationEdge,
    CalibrationSegment,
    CalibrationTable,
    Interval,
    PiecewisePolynomial,
    Polynomial,
    UnitDefinitionVersion,
)
from .objects import (
    CalibrationGraph,
    CalibrationPath,
    CalibrationResult,
    CoverageReport,
    CoverageSegment,
    PathCoverage,
    RevalidationReport,
    classify,
    revalidate_coverages,
)
from .registry import (
    GenericCalibrationRegistry,
    calibration_archive_from_dict,
    calibration_archive_from_json,
    calibration_archive_to_dict,
    calibration_archive_to_json,
    interval_from_quantities,
)

__all__ = [
    "Affine",
    "CalibrationArchive",
    "CalibrationEdge",
    "CalibrationGraph",
    "CalibrationPath",
    "CalibrationResult",
    "CalibrationSegment",
    "CalibrationTable",
    "CoverageReport",
    "CoverageSegment",
    "GenericCalibrationRegistry",
    "Interval",
    "PathCoverage",
    "PiecewisePolynomial",
    "Polynomial",
    "RevalidationReport",
    "UnitDefinitionVersion",
    "calibration_archive_from_dict",
    "calibration_archive_from_json",
    "calibration_archive_to_dict",
    "calibration_archive_to_json",
    "classify",
    "interval_from_quantities",
    "revalidate_coverages",
]
