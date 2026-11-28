"""
h5netcdf
========

A Python library for the netCDF4 file-format that directly reads and writes
HDF5 files via h5py, without using the Unidata netCDF library.
"""

try:
    from ._version import version as __version__
except Exception:
    # Local copy or not installed with setuptools.
    # Disable minimum version checks on downstream libraries.
    __version__ = "999"

from .core import CompatibilityError, Dimension, File, Group, Variable  # noqa
from .scan import (  # noqa
    ScanIndex,
    ScanPage,
    ScanRecord,
    ScanWindow,
    ScanWindowError,
    ScanWindowIndex,
    create_scan_index,
    iter_scan_window,
    open_scan_window,
    page_scan_window,
    publish_scan_release,
    read_scan_page,
    read_scan_window,
)
