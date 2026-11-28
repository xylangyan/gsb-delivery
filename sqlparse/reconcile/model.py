"""Data model for the shift reconciliation (``sqlparse.reconcile``).

The vocabulary used across the package mirrors the wording of the work
order:

* an *external* table is a physical source table with a fixed physical
  version per shift;
* a *temporary* table is rebuilt inside a work order from a
  ``CREATE TEMP TABLE`` / ``INSERT ... SELECT``;
* a *generation* is one immutable snapshot of a temporary table, or one
  immutable copy of an external read;
* a *report* is a delivered query page, pinned to the generations visible
  when it was handed over.

The classes are intentionally small and free of sqlparse imports so the
model can be reused by both the analyser and the verifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnRef:
    """One output column of a projection.

    ``display`` is the name shown in the report, ``sources`` are the
    physical ``(table generation, column)`` pairs that feed it.  A
    ``JOIN ... USING`` merge keeps both sources even though only one
    display value is produced.
    """

    display: str
    sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReadRef:
    """A single read of an external table inside one statement.

    Several reports may share one read; the fee is charged once per
    ``(table, instance)`` key.  ``variant`` records which permitted
    interpretation the receiving end chose for that physical table.
    """

    table: str
    instance: str
    variant: str
    byte_cost: int
    fee: float
    key: tuple[str, str]
    seq: int


@dataclass(frozen=True)
class RowMeta:
    """Read-piece metadata for one original row.

    ``line_no`` is the original row number in the physical read piece and
    ``pk`` links the row back to the external table's primary key, so the
    same original row can be recognised across two reads.
    """

    line_no: int
    pk: object


@dataclass
class TableGeneration:
    """One immutable generation of a temporary table."""

    name: str
    generation: str
    statement_index: int
    line_no: int
    sealed: bool
    columns: list[ColumnRef] = field(default_factory=list)
    reads: list[ReadRef] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)


@dataclass
class Report:
    """A delivered query page (a SELECT statement in a work order)."""

    name: str
    statement_index: int
    line_no: int
    reads: list[ReadRef]
    columns: list[ColumnRef]
    cte_names: frozenset[str]
    temp_refs: frozenset[str] = frozenset()
    sealed_snapshot: str | None = None


@dataclass
class ExternalVersion:
    """Physical metadata and finite data known for one external table.

    ``variants`` maps an allowed alternative label to the finite set of
    rows the receiving end may substitute.  Every variant contains the
    same original rows (matched through :class:`RowMeta`), only the values
    differ, which lets the verifier demand one consistent interpretation
    of an original row across all of its reads.
    """

    table: str
    instance: str
    columns: list[str]
    bytes: int
    fee: float
    rows: list[tuple] = field(default_factory=list)
    row_meta: list[RowMeta] = field(default_factory=list)
    variants: dict[str, list[tuple]] = field(default_factory=dict)
    # variant label -> (bytes read, fee) when it differs from the base cost
    variant_costs: dict[str, tuple[int, float]] = field(default_factory=dict)
    # Optional physical version used by the previous shift.
    previous_instance: str | None = None
    previous_rows: list[tuple] | None = None

    def cost_for(self, label: str) -> tuple[int, float]:
        if label in self.variant_costs:
            return self.variant_costs[label]
        return self.bytes, self.fee


@dataclass
class AllowedSeal:
    """A read-only temporary-table generation handed in with the order."""

    name: str
    generation: str


@dataclass
class ReportVerdict:
    """Whether one delivered report can be reused by the current shift."""

    name: str
    reusable: bool
    reason: str
    reads: list[ReadRef]
    line_no: int | None = None
    failures: list['Failure'] = field(default_factory=list)


@dataclass
class Failure:
    """Why an alternative cannot reproduce a delivered report."""

    variant_label: str
    table: str
    instance: str
    column: str
    expected: object
    observed: object
    row_ref: RowMeta | None
