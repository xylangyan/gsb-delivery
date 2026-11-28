"""Shift reconciliation for SQLite work-order scripts.

This package adds a reconciliation entry point on top of the sqlparse
parser.  Given two almost-identical work orders (a delivered previous
shift and the current one) it tracks the physical lineage of every
temporary-table generation and delivered report page, then decides which
previously delivered pages can be reused and which must be redone using
the current shift's raw material.

The supported SQL subset is documented in
:mod:`sqlparse.reconcile.analyser`.
"""

from sqlparse.reconcile.analyser import (
    LineageAnalyser,
    ReconcileError,
)
from sqlparse.reconcile.manifest import WorkOrder, load_work_order
from sqlparse.reconcile.model import (
    AllowedSeal,
    ColumnRef,
    ExternalVersion,
    Failure,
    ReadRef,
    Report,
    ReportVerdict,
    RowMeta,
    TableGeneration,
)
from sqlparse.reconcile.report import render_json, render_text
from sqlparse.reconcile.verifier import (
    Alternative,
    Reconciler,
    ReconciliationResult,
)

__all__ = [
    'AllowedSeal',
    'Alternative',
    'ColumnRef',
    'ExternalVersion',
    'Failure',
    'LineageAnalyser',
    'ReadRef',
    'ReconcileError',
    'Reconciler',
    'ReconciliationResult',
    'Report',
    'ReportVerdict',
    'RowMeta',
    'TableGeneration',
    'WorkOrder',
    'load_work_order',
    'render_json',
    'render_text',
]
