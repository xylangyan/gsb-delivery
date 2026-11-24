from . import archive as _archive
from .__version__ import __version__ as __version__
from .archive import (
    Archive,
    ArchiveIndex,
    ByteRange,
    ByteRangeArchive,
    ByteRangeReader,
    RangeArchive,
    RangeBackend,
    WindowResult,
    WindowResultCollection,
    XDFArchive,
    XDFIndex,
    XdfArchive,
    build_archive,
    index_archive,
    index_xdf,
    load_archive,
    load_xdf_window,
    open_archive,
    query_window,
    read_archived_window,
    read_sample_window,
    read_window,
    read_xdf_window,
    register_archive,
    window_read,
    windowed_load_xdf,
)
from .pyxdf import load_xdf, match_streaminfos, resolve_streams

__all__ = [
    "Archive",
    "ArchiveIndex",
    "ByteRange",
    "ByteRangeArchive",
    "ByteRangeReader",
    "RangeArchive",
    "RangeBackend",
    "WindowResult",
    "WindowResultCollection",
    "XDFArchive",
    "XDFIndex",
    "XdfArchive",
    "build_archive",
    "index_archive",
    "index_xdf",
    "load_archive",
    "load_xdf",
    "load_xdf_window",
    "match_streaminfos",
    "open_archive",
    "query_window",
    "read_archived_window",
    "read_sample_window",
    "read_window",
    "read_xdf_window",
    "register_archive",
    "resolve_streams",
    "window_read",
    "windowed_load_xdf",
]


def __getattr__(name):
    """Expose additional names added to the archive helper module."""
    if hasattr(_archive, name):
        return getattr(_archive, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
