# -*- coding: UTF-8 -*-
"""
dtaidistance.rle
~~~~~~~~~~~~~~~~

Compatibility alias for :mod:`dtaidistance.dtw_rle`.

Run-length encoded DTW lives in :mod:`dtaidistance.dtw_rle`; this module
re-exports the same public functions for callers that prefer the shorter
``dtaidistance.rle`` name.
"""
from .dtw_rle import (  # noqa: F401
    RunLengthSeries,
    compress,
    expand,
    distance,
    distance_cost,
    distance_and_path,
    warping_path,
    warping_path_segments,
    warping_path_full,
    path_cost,
    distance_rle,
    warping_path_rle,
    distance_cost_rle,
    distance_and_path_rle,
)

__all__ = [
    "RunLengthSeries",
    "compress",
    "expand",
    "distance",
    "distance_cost",
    "distance_and_path",
    "warping_path",
    "warping_path_segments",
    "warping_path_full",
    "path_cost",
    "distance_rle",
    "warping_path_rle",
    "distance_cost_rle",
    "distance_and_path_rle",
]
