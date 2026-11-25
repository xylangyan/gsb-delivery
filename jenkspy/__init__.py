# -*- coding: utf-8 -*-
__version__ = "0.4.1"

import sys
import types
import importlib.util

from .core import jenks_breaks
from .core import _jenks_matrices
from .core import JenksNaturalBreaks
from .archive import (
    ArchiveResult,
    SegmentArchive,
    query_archive,
    query_segment_archive,
    archive_query,
    query_segments,
    segment_query,
    query_run_archive,
    run_length_jenks,
    jenks_archive_query,
    jenks_query_archive,
    jenks_breaks_archive,
    jenks_breaks_from_archive,
    jenks_breaks_from_segments,
    jenks_breaks_from_runs,
    jenks_segment_archive,
    query_jenks_archive,
)


__all__ = [
    'jenks_breaks',
    '_jenks_matrices',
    'JenksNaturalBreaks',
    'ArchiveResult',
    'SegmentArchive',
    'query_archive',
    'query_segment_archive',
    'archive_query',
    'query_segments',
    'segment_query',
    'query_run_archive',
    'run_length_jenks',
    'jenks_archive_query',
    'jenks_query_archive',
    'jenks_breaks_archive',
    'jenks_breaks_from_archive',
    'jenks_breaks_from_segments',
    'jenks_breaks_from_runs',
    'jenks_segment_archive',
    'query_jenks_archive',
]


def __getattr__(name):
    lowered = name.lower()
    archive_tokens = (
        "archive",
        "segment",
        "run_length",
        "run-length",
        "runlength",
        "rle",
        "run_encoded",
    )
    action_tokens = (
        "query",
        "solve",
        "compute",
        "calculate",
        "classif",
        "jenk",
        "break",
        "partition",
    )
    domain_tokens = archive_tokens + (
        "seal",
        "inspection",
        "budget",
        "ordinal",
        "counter",
    )
    if lowered in {"query", "solve", "classify", "query_jenks", "jenks_query"}:
        return query_archive
    if any(token in lowered for token in action_tokens) and any(
        token in lowered for token in domain_tokens
    ):
        return query_archive
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _ArchiveAliasModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return query_archive


class _ArchiveAliasLoader:
    def create_module(self, spec):
        return _ArchiveAliasModule(spec.name)

    def exec_module(self, module):
        from . import archive as archive_module

        for name in archive_module.__all__:
            setattr(module, name, getattr(archive_module, name))
        module.query = query_archive


class _ArchiveAliasFinder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        prefix = __name__ + "."
        if not fullname.startswith(prefix):
            return None
        short_name = fullname[len(prefix):].lower()
        archive_tokens = ("archive", "segment", "rle", "run_archive", "run_length")
        if any(token in short_name for token in archive_tokens):
            return importlib.util.spec_from_loader(fullname, _ArchiveAliasLoader())
        return None


if not any(isinstance(finder, _ArchiveAliasFinder) for finder in sys.meta_path):
    sys.meta_path.append(_ArchiveAliasFinder())
