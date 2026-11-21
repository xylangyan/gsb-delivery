"""
Single-pass, batch-oriented reading of CLDF data with foreign-key resolution.

The regular ORM access via :meth:`pycldf.Dataset.objects` always reads a complete
table into memory before any foreign-key relation can be resolved. This module
provides a streaming alternative for exporting large wordlists (or similar
components) in batches:

* each table file is opened and read **exactly once**, advancing a single
  sequential iterator only as far as needed to locate the references of the
  current batch,
* references handed out before a target table has been read to the end are
  marked as :attr:`RefStatus.UNIQUE` ("unique so far, pending verification")
  rather than as definite results,
* a duplicate key encountered later invalidates *all* references obtained via
  that key (scoped per target table and key domain - CLDF id vs. local primary
  key), while references using other keys or the other key domain stay valid,
* references explicitly pointing at a missing key are reported as
  :attr:`RefStatus.NOT_FOUND` once the target table is exhausted, and empty
  references resolve to :attr:`RefStatus.EMPTY` without touching the target
  file at all.

Typical usage:

.. code-block:: python

    exporter = BatchExporter(dataset)
    first = exporter.export_batch('FormTable', ['f1', 'f2'])
    for record in first:
        for ref in record.references.values():
            assert ref.status in (RefStatus.UNIQUE, RefStatus.EMPTY)
    ...
    result = exporter.finalize()
    for report in result.conflict_reports:
        ...
"""
import collections
import enum
import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Any, Iterator

from pycldf import orm
from pycldf.terms import term_uri

if TYPE_CHECKING:  # pragma: no cover
    from pycldf import Dataset
    from pycldf.orm import Object


class RefStatus(enum.Enum):
    """Lifecycle state of a :class:`ResolvedReference`."""

    UNIQUE = 'unique'
    """A matching target row has been seen, but the target file is not exhausted yet."""

    CONFIRMED = 'confirmed'
    """The target file is exhausted and the key resolves to exactly one row."""

    CONFLICTED = 'conflicted'
    """At least two target rows carry the referenced key, so no target is definite."""

    NOT_FOUND = 'not_found'
    """The target file is exhausted and no row carries the referenced key."""

    EMPTY = 'empty'
    """The reference property carried no value; the target file was never read."""


@dataclass
class ResolvedReference:
    """
    A foreign-key reference issued by the :class:`BatchExporter`.

    :ivar source_component: CLDF component holding the referencing row.
    :ivar source_id: CLDF id of the referencing row.
    :ivar relation: CLDF reference property name (e.g. ``languageReference``).
    :ivar target_component: CLDF component the foreign key points at.
    :ivar key_domain: ``'id'`` for a CLDF id target column or ``'pk'`` for a
        local primary-key target column.
    :ivar key_value: the raw value found in the referencing cell.
    :ivar status: current :class:`RefStatus`.
    :ivar target: the resolved ORM object, or `None` for empty, not-found or
        conflicted references.
    :ivar candidates: all ORM objects sharing a conflicted key (in row order).
    """

    source_component: str
    source_id: str
    relation: str
    target_component: str
    key_domain: str
    key_value: Any
    status: RefStatus
    target: Optional['Object'] = None
    candidates: list['Object'] = field(default_factory=list)

    @property
    def path(self) -> str:
        """A stable, human-readable description of the reference path."""
        return f'{self.source_component}/{self.source_id}#{self.relation}'

    @classmethod
    def empty(cls, source: 'Object', relation: str) -> 'ResolvedReference':
        return cls(
            source_component=source.component_name(),
            source_id=source.id,
            relation=relation,
            target_component='',
            key_domain='',
            key_value=None,
            status=RefStatus.EMPTY,
        )


@dataclass
class ConflictReport:
    """
    A duplicate key discovered in a target table, together with every reference
    affected by it.
    """

    component: str
    key_domain: str
    key: Any
    candidates: list['Object']
    references: list[ResolvedReference] = field(default_factory=list)

    @property
    def source_paths(self) -> list[str]:
        """Reference paths (source record and relation) of all affected references."""
        return [ref.path for ref in self.references]


@dataclass
class ExportedRecord:
    """A source row delivered as part of a :class:`Batch`."""

    source: 'Object'
    references: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:  # pylint: disable=invalid-name
        return self.source.id


class Batch:
    """A checkable slice of a source component, in requested id order."""

    def __init__(self, component: str, records: list[ExportedRecord]):
        self.component = component
        self.records = records

    def __iter__(self) -> Iterator[ExportedRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, item) -> ExportedRecord:
        return self.records[item]


@dataclass
class FinalResult:
    """Aggregated state after every touched table has been read to the end."""

    references: list[ResolvedReference]
    conflict_reports: list[ConflictReport]


class _TableState:
    """Bookkeeping for one table, fed by a single sequential iterator."""

    def __init__(self, component: str):
        self.component = component
        self.gen: Optional[Iterator] = None
        self.exhausted = False
        self.rows_read = 0
        self.indexes = {'id': collections.OrderedDict(), 'pk': collections.OrderedDict()}
        self.dupes = {'id': set(), 'pk': set()}
        self.dupe_objects = {'id': collections.defaultdict(list),
                             'pk': collections.defaultdict(list)}
        self.reports: dict[tuple[str, Any], ConflictReport] = {}

    def located(self, domain: str, key: Any) -> bool:
        return key is not None and key in self.indexes[domain]


class BatchExporter:
    """
    Export CLDF rows in batches while resolving foreign keys with a single
    sequential read per table file.
    """

    def __init__(self, dataset: 'Dataset', strict: bool = False):
        """
        :param strict: Forwarded to csvw when reading table files. Defaults to
            `False` because data with duplicate or missing keys - the situations
            this exporter is meant to report on - would otherwise abort reading
            with a `ValueError`. The foreign-key constraints themselves stay in
            place and are still used to determine the reference target.
        """
        self.dataset = dataset
        self.strict = strict
        self.tables: dict[str, _TableState] = {}
        self.pending: dict[tuple[str, str, Any], list[ResolvedReference]] = \
            collections.defaultdict(list)
        self.refs_by_key: dict[tuple[str, str, Any], list[ResolvedReference]] = \
            collections.defaultdict(list)
        self.all_refs: list[ResolvedReference] = []

    # Introspection -------------------------------------------------------------------------------
    def rows_read(self, component: str) -> int:
        """Number of rows consumed from the component's file so far."""
        state = self.tables.get(component)
        return state.rows_read if state else 0

    def is_exhausted(self, component: str) -> bool:
        """Whether the component's single iterator has reached end of file."""
        state = self.tables.get(component)
        return bool(state and state.exhausted)

    def conflict_reports(self) -> list[ConflictReport]:
        """All duplicate-key reports known at this point."""
        reports = []
        for state in self.tables.values():
            reports.extend(state.reports.values())
        return reports

    # Batch export --------------------------------------------------------------------------------
    def export_batch(
            self,
            component: str,
            ids: list[str],
            relations: tuple[str, ...] = ('languageReference', 'parameterReference'),
    ) -> Batch:
        """
        Export the rows of `component` with the given CLDF `ids`, resolving the
        given reference properties.

        The source table is only read up to the last requested row; already
        consumed rows are reused for later batches.
        """
        ids = list(ids)
        self._advance(component, [('id', row_id) for row_id in ids])
        state = self._state(component)
        records = []
        for row_id in ids:
            obj = state.indexes['id'].get(row_id)
            if obj is None:
                raise KeyError(f'{component} has no row with id {row_id!r}')
            record = ExportedRecord(obj)
            for relation in relations:
                if hasattr(obj.cldf, relation):
                    record.references[relation] = self.reference(obj, relation)
            records.append(record)
        return Batch(component, records)

    def reference(self, source: 'Object', relation: str) -> Any:
        """
        Resolve one reference property of an ORM object.

        Returns a single :class:`ResolvedReference`, a list of them for
        list-valued properties, or an empty reference for blank cells.
        """
        value = getattr(source.cldf, relation, None)
        if value is None or value == '' or value == []:
            empty = ResolvedReference.empty(source, relation)
            self.all_refs.append(empty)
            return empty
        if isinstance(value, (list, tuple)):
            return [self._single_reference(source, relation, item) for item in value]
        return self._single_reference(source, relation, value)

    def finalize(self) -> FinalResult:
        """
        Read every touched table to its end and settle all reference states.

        Pending-unique references become confirmed (unless their key turned out
        duplicated), unresolved references become not-found.
        """
        for component in list(self.tables):
            self._advance(component, None)
        for refs in self.pending.values():
            for ref in refs:
                ref.status = RefStatus.NOT_FOUND
        self.pending.clear()
        for ref in self.all_refs:
            if ref.status == RefStatus.UNIQUE:
                ref.status = RefStatus.CONFIRMED
        return FinalResult(
            references=list(self.all_refs),
            conflict_reports=self.conflict_reports())

    # Internals -----------------------------------------------------------------------------------
    def _state(self, component: str) -> _TableState:
        if component not in self.tables:
            self.tables[component] = _TableState(component)
        return self.tables[component]

    def _fkey_domain(self, source_component: str, relation: str) -> tuple[str, str]:
        ref = self.dataset.get_foreign_key_reference(source_component, relation)
        if ref is None:
            raise ValueError(
                f'{source_component}.{relation} has no foreign key constraint to resolve')
        target_table, target_column = ref
        target_component = self.dataset.get_tabletype(target_table)
        if not target_component:
            raise ValueError(
                f'foreign key target of {source_component}.{relation} is not a CLDF component')
        if str(target_column.propertyUrl) == term_uri('id'):
            return target_component, 'id'
        if [target_column.name] == (target_table.tableSchema.primaryKey or []):
            return target_component, 'pk'
        raise NotImplementedError(
            'pycldf only supports foreign key constraints referencing the CLDF id or primary key')

    def _single_reference(
            self, source: 'Object', relation: str, value: Any) -> ResolvedReference:
        target_component, domain = self._fkey_domain(source.component_name(), relation)
        ref = ResolvedReference(
            source_component=source.component_name(),
            source_id=source.id,
            relation=relation,
            target_component=target_component,
            key_domain=domain,
            key_value=value,
            status=RefStatus.UNIQUE,
        )
        key = (target_component, domain, value)
        self.all_refs.append(ref)
        self.refs_by_key[key].append(ref)
        state = self.tables.get(target_component)
        if state is not None and state.located(domain, value):
            self._attach_known(ref, state)
        else:
            self.pending[key].append(ref)
            self._advance(target_component, [(domain, value)])
            if ref.target is None and ref.status == RefStatus.UNIQUE:
                # The target table was exhausted without a matching row.
                queued = self.pending.get(key)
                if queued and ref in queued:
                    queued.remove(ref)
                ref.status = RefStatus.NOT_FOUND
        report = state.reports.get((domain, value)) if state else None
        if report is not None and ref not in report.references:
            report.references.append(ref)
        return ref

    def _attach_known(self, ref: ResolvedReference, state: _TableState) -> None:
        if ref.key_value in state.dupes[ref.key_domain]:
            ref.status = RefStatus.CONFLICTED
            ref.candidates = list(state.dupe_objects[ref.key_domain][ref.key_value])
            state.reports[(ref.key_domain, ref.key_value)].references.append(ref)
            return
        ref.target = state.indexes[ref.key_domain][ref.key_value]
        ref.status = RefStatus.CONFIRMED if state.exhausted else RefStatus.UNIQUE

    def _advance(
            self, component: str,
            wanted: Optional[list[tuple[str, Any]]]) -> None:
        """Resume the component's single iterator until `wanted` is located or EOF."""
        state = self._state(component)
        if state.exhausted:
            return
        if state.gen is None:
            state.gen = self.dataset[component].iterdicts(strict=self.strict)
        while True:
            if wanted and all(state.located(domain, key) for domain, key in wanted):
                return
            try:
                row = next(state.gen)
            except StopIteration:
                self._mark_exhausted(state)
                return
            self._ingest(state, row)

    def _ingest(self, state: _TableState, row: dict) -> None:
        cls = _orm_class(state.component)
        obj = cls(self.dataset, row)
        state.rows_read += 1
        for domain, key in (('id', obj.id), ('pk', obj.pk)):
            if key is None:
                continue
            index = state.indexes[domain]
            if key in index:
                state.dupes[domain].add(key)
                bucket = state.dupe_objects[domain][key]
                if not bucket:
                    # Keep the original row alongside the later duplicate(s).
                    bucket.append(index[key])
                bucket.append(obj)
                self._flag_conflict(state, domain, key)
            else:
                index[key] = obj
                for ref in self.pending.pop((state.component, domain, key), []):
                    ref.target = obj
                    ref.status = RefStatus.CONFIRMED if state.exhausted else RefStatus.UNIQUE

    def _flag_conflict(self, state: _TableState, domain: str, key: Any) -> None:
        report_key = (domain, key)
        candidates = state.dupe_objects[domain][key]
        report = state.reports.get(report_key)
        if report is None:
            report = ConflictReport(
                component=state.component,
                key_domain=domain,
                key=key,
                candidates=list(candidates))
            state.reports[report_key] = report
        else:
            report.candidates = list(candidates)
        for ref in self.refs_by_key[(state.component, domain, key)]:
            ref.status = RefStatus.CONFLICTED
            ref.target = None
            ref.candidates = list(candidates)
            if ref not in report.references:
                report.references.append(ref)

    def _mark_exhausted(self, state: _TableState) -> None:
        state.exhausted = True
        state.gen = None
        for key, refs in list(self.pending.items()):
            if key[0] != state.component:
                continue
            for ref in refs:
                ref.status = RefStatus.NOT_FOUND
            del self.pending[key]
        for ref in self.all_refs:
            if ref.target_component == state.component \
                    and ref.status == RefStatus.UNIQUE:
                ref.status = RefStatus.CONFIRMED


@functools.lru_cache(maxsize=None)
def _orm_class(component: str):
    """ORM class for a component, falling back to a generic `Object` subclass."""
    return type(
        'Dynamic' + component,
        (orm.Object,),
        {'__component__': component})
