"""Whole-batch structuring with cross-record references.

Two tables may describe the same parts from different angles: where they came
from and where they are assembled.  A part may be mentioned several times,
sometimes only by its registration number, sometimes with a few attributes it
happens to know.  The registration number identifies the one occurrence, and
all mentions of it are evidence about the same object.

This module structures such a batch as a whole.  It is strictly opt-in;
converters that never call :func:`structure_batch` keep their existing rules.

The resolution model is:

* one occurrence per registration number, with every mention merged;
* candidate types narrowed by labels, versions, reported fields and relation
  declarations, propagated to a fixpoint across references;
* unambiguous components constructed exactly once, with every resolved
  reference pointing at the same object as the occurrence itself;
* cycles allowed only across relations explicitly marked late-bound;
* ambiguous or conflicting components reported while unrelated components are
  still delivered.

Example::

    spec = BatchSpec(
        tables={
            "receiving": TableSpec(Part, labels={"Part": "P", "Gear": "G"}),
            "assembly": TableSpec(
                Part,
                labels={"Part": "part", "Gear": "gear"},
                versions={"Gear": "v2"},
                version="v2",
            ),
        },
        references={
            "receiving": {"parent": Reference("parent_id", late=True)},
            "assembly": {"part": Reference("part_id")},
        },
        id_fields={"receiving": "id", "assembly": "id"},
    )
    batch = structure_batch(converter, spec, {"receiving": rows1, "assembly": rows2})

    batch.tables["receiving"]        # structured rows, one per occurrence
    batch.by_registration("P-1")     # the single object for that occurrence
    batch.diagnostics                # ambiguities, conflicts, missing references
    export_batch(batch)              # re-export using each table's own labels
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from gc import collect as _collect_garbage
from typing import Any, Literal, Union, get_args, get_origin

from attrs import NOTHING, Attribute, Factory
from attrs import field as attrs_field

from ._compat import NoneType, adapted_fields, is_union_type
from .errors import CattrsError

__all__ = [
    "Ambiguity",
    "Batch",
    "BatchAmbiguityError",
    "BatchConflictError",
    "BatchCycleError",
    "BatchDiagnostics",
    "BatchError",
    "BatchIncompleteError",
    "BatchModel",
    "BatchSpec",
    "CandidateEvidence",
    "Conflict",
    "Declaration",
    "LateBound",
    "Reference",
    "ReferenceCycleError",
    "TableSpec",
    "export_batch",
    "late_field",
    "load_batch",
    "resolve_batch",
    "structure_batch",
    "unstructure_batch",
    "watch_model",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BatchError(CattrsError):
    """Base class for batch structuring errors."""


class BatchAmbiguityError(BatchError):
    """More than one complete explanation of a batch component remains."""

    def __init__(self, message: str, ambiguities: Sequence[Ambiguity]) -> None:
        super().__init__(message, ambiguities)
        self.message = message
        self.ambiguities = list(ambiguities)

    def __str__(self) -> str:
        return self.message


class BatchConflictError(BatchError):
    """Declarations in a batch contradict each other."""

    def __init__(self, message: str, conflicts: Sequence[Conflict]) -> None:
        super().__init__(message, conflicts)
        self.message = message
        self.conflicts = list(conflicts)

    def __str__(self) -> str:
        return self.message


class BatchIncompleteError(BatchError):
    """A batch could not be completely structured."""

    def __init__(self, message: str, diagnostics: BatchDiagnostics) -> None:
        super().__init__(message, diagnostics)
        self.message = message
        self.diagnostics = diagnostics

    def __str__(self) -> str:
        return self.message


class BatchCycleError(BatchIncompleteError):
    """A reference cycle was not declared late-bound."""


class ReferenceCycleError(BatchCycleError):
    """Alias for :class:`BatchCycleError`."""


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """A relation to another occurrence.

    :param key: The attribute or column holding the registration number.  When
        `None`, the relation attribute itself holds the registration number.
    :param target: Restrict the referenced occurrence to a type, a tuple of
        types, or a type name.  `None` leaves the declaration type in charge.
    :param late: Allow the relation to be bound after the main construction
        phase.  Cycles are only legal across late-bound relations.
    :param many: The relation holds a sequence of registration numbers.
    """

    key: str | None = None
    target: Any = None
    late: bool = False
    many: bool = False


def late_field(default: Any = None, **kwargs: Any) -> Any:
    """Create an ``attrs`` field explicitly allowed to bind late.

    The returned field carries metadata understood by :func:`structure_batch`::

        class Node:
            parent: "Node | None" = late_field(default=None)
    """
    metadata = dict(kwargs.pop("metadata", {}))
    metadata["cattrs_late"] = True
    return attrs_field(default=default, metadata=metadata, **kwargs)


class LateBound:
    """Marker helper for late-bound relations.

    Models may use :func:`late_field`, or assign ``LateBound()`` as field
    metadata value.  The marker is intentionally inert; the batch itself
    guarantees no object is delivered before late binding finishes.
    """

    def __repr__(self) -> str:
        return "LateBound()"


class BatchModel:
    """Opt-in guard against observing a partially built occurrence.

    Subclass this alongside the model (or use :func:`watch_model`) so ordinary
    attribute access raises :class:`BatchIncompleteError` until late binding
    has finished.  The batch sets ``_cattrs_batch_ready`` on each object before
    delivering it; code that receives only batch results never sees a partial
    object.
    """

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("__") and not name.startswith("_cattrs_"):
            try:
                ready = object.__getattribute__(self, "_cattrs_batch_ready")
            except AttributeError:
                ready = True
            if ready is False:
                raise BatchIncompleteError(
                    f"{type(self).__name__}.{name} is not available until "
                    "late binding finishes.",
                    BatchDiagnostics(),
                )
        return object.__getattribute__(self, name)


def watch_model(cls: type) -> type:
    """Install the partial-access guard on a model class.

    A decorator alternative to inheriting :class:`BatchModel`::

        @watch_model
        @define
        class Node:
            ...
    """
    if "__getattribute__" in cls.__dict__:
        return cls

    def guarded_getattribute(self, name):
        if not name.startswith("__") and not name.startswith("_cattrs_"):
            try:
                ready = object.__getattribute__(self, "_cattrs_batch_ready")
            except AttributeError:
                ready = True
            if ready is False:
                raise BatchIncompleteError(
                    f"{type(self).__name__}.{name} is not available until "
                    "late binding finishes.",
                    BatchDiagnostics(),
                )
        return object.__getattribute__(self, name)

    cls.__getattribute__ = guarded_getattribute
    return cls


# ---------------------------------------------------------------------------
# Table and batch specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    """How one table maps onto the model.

    :param model: Root model type (usually a base class) for the table.
    :param labels: Type-to-label mapping.  Accepts either ``{type_name: label}``
        or ``{label: type_name}``.  Legacy and modern tables may use different
        labels for the same subtype.
    :param versions: Type-to-version availability, or ``{version: type_names}``.
        Missing entries mean "not reported", not "unavailable".
    :param version: The table's own version.
    :param tag_field: Column carrying the label.
    :param field_map: Model attribute to column mapping.  Accepts either
        direction; used on import and export.
    :param model_codes: Per-type maps of model literals to table codes.  This
        converts values such as a status enum whose spelling differs between
        the legacy and modern tables.  Accepts ``{type_name: {column: {model:
        code}}}`` or ``{type_name: {column: {code: model}}}``.
    :param export_fields: Columns this table is allowed to write, in export
        order.  Defaults to the fields the table actually reported plus its tag.
    :param allow_untagged: Whether a row without a tag may still be considered
        against every type eligible in this table.
    :param types: Explicit eligible types; defaults to `model` and subclasses.
    """

    model: type
    labels: Mapping[Any, Any] = dc_field(default_factory=dict)
    versions: Mapping[Any, Any] = dc_field(default_factory=dict)
    version: str | None = None
    tag_field: str = "type"
    field_map: Mapping[str, str] = dc_field(default_factory=dict)
    model_codes: Mapping[str, Mapping[str, Mapping[Any, Any]]] = dc_field(
        default_factory=dict
    )
    export_fields: tuple[str, ...] | None = None
    allow_untagged: bool = True
    types: tuple[type, ...] = ()

    def eligible_types(self) -> tuple[type, ...]:
        if self.types:
            return tuple(self.types)
        return tuple(_class_tree(self.model))

    def label_for(self, cl: type) -> Any:
        """The label this table uses for `cl`, falling back to ancestors."""
        normalized = _normalize_labels(self.labels, self.eligible_types())
        type_to_label = normalized.type_to_label
        for candidate in cl.__mro__:
            if candidate.__name__ in type_to_label:
                return type_to_label[candidate.__name__]
        return None

    def type_for(self, label: Any) -> list[str]:
        normalized = _normalize_labels(self.labels, self.eligible_types())
        if label in normalized.label_to_types:
            return normalized.label_to_types[label]
        # Without an explicit mapping, the tag is the type name.
        return [label] if isinstance(label, str) else []

    def version_allows(self, cl: type) -> bool:
        if not self.versions:
            return True
        normalized = _normalize_versions(self.versions, self.eligible_types())
        allowed_versions = normalized.type_to_versions.get(cl.__name__)
        if allowed_versions is None:
            # A version-keyed table may enumerate the types available for it.
            allowed_types = normalized.version_to_types.get(self.version)
            if allowed_types is not None:
                return cl.__name__ in allowed_types
            return True
        return self.version in allowed_versions

    def column_for(self, name: str) -> str:
        return _normalize_field_map(self.field_map).attr_to_column.get(name, name)

    def attribute_for(self, column: str) -> str:
        return _normalize_field_map(self.field_map).column_to_attr.get(column, column)

    def decode(self, cl: type | None, column: str, value: Any) -> Any:
        """Map a table code to the model value for one column."""
        if cl is None or value is None:
            return value
        table_codes = self._codes_for(cl)
        if column not in table_codes:
            return value
        code_to_model, _model_to_code = table_codes[column]
        if value in code_to_model:
            return code_to_model[value]
        return value

    def encode(self, cl: type | None, column: str, value: Any) -> Any:
        """Map a model value back to this table's code for one column."""
        if cl is None or value is None:
            return value
        table_codes = self._codes_for(cl)
        if column not in table_codes:
            return value
        _, model_to_code = table_codes[column]
        return model_to_code.get(value, value)

    def _codes_for(self, cl: type) -> dict[str, tuple[dict[Any, Any], dict[Any, Any]]]:
        """Per-column ``(code -> model, model -> code)`` maps for `cl`.

        ``model_codes`` is declared as ``{type_name: {column: {model: code}}}``.
        """
        codes: dict[str, tuple[dict[Any, Any], dict[Any, Any]]] = {}
        for candidate in cl.__mro__:
            mapping = self.model_codes.get(candidate.__name__)
            if not mapping:
                continue
            for column, pairs in mapping.items():
                model_to_code = dict(pairs)
                code_to_model = {code: model for model, code in pairs.items()}
                codes.setdefault(column, (code_to_model, model_to_code))
        return codes


@dataclass(frozen=True)
class BatchSpec:
    """A complete description of a batch load.

    :param tables: Table name to :class:`TableSpec`.
    :param references: Table name -> relation attribute -> :class:`Reference`.
    :param id_fields: Table name -> registration-number column.  Defaults to
        ``"id"``.
    :param late_attributes: Table name -> relation attribute -> ``True`` for
        relations explicitly allowed to bind late.
    :param strict: When true, :func:`structure_batch` raises if any component
        cannot be fully delivered.  Defaults to false so unrelated components
        remain available.
    """

    tables: Mapping[str, TableSpec]
    references: Mapping[str, Mapping[str, Reference]] = dc_field(default_factory=dict)
    id_fields: Mapping[str, str] = dc_field(default_factory=dict)
    late_attributes: Mapping[str, Mapping[str, bool]] = dc_field(default_factory=dict)
    strict: bool = False

    def id_field(self, table: str) -> str:
        return self.id_fields.get(table, "id")

    def references_for(self, table: str) -> dict[str, Reference]:
        return dict(self.references.get(table, {}))


@dataclass(frozen=True)
class _NormalizedLabels:
    type_to_label: dict[str, Any]
    label_to_types: dict[Any, list[str]]


def _normalize_labels(
    labels: Mapping[Any, Any], eligible: Sequence[type] = ()
) -> _NormalizedLabels:
    """Accept either mapping direction and return both views.

    Keys that are types are converted to type names.  ``eligible`` resolves the
    otherwise ambiguous case of two short strings: a key naming a model type is
    a type name, anything else is a label.
    """
    known = {cl.__name__ for cl in eligible}
    type_to_label: dict[str, Any] = {}
    label_to_types: dict[Any, list[str]] = defaultdict(list)
    for key, value in labels.items():
        key_name = key.__name__ if isinstance(key, type) else key
        val_name = value.__name__ if isinstance(value, type) else value
        if isinstance(key, type) or key_name in known:
            type_name, label = key_name, val_name
        elif isinstance(value, type) or val_name in known:
            type_name, label = val_name, key_name
        elif _looks_type(key_name) and not _looks_type(val_name):
            type_name, label = key_name, val_name
        elif _looks_type(val_name) and not _looks_type(key_name):
            type_name, label = val_name, key_name
        else:
            type_name, label = key_name, val_name
        type_to_label.setdefault(type_name, label)
        label_to_types[label].append(type_name)
    return _NormalizedLabels(type_to_label, dict(label_to_types))


def _looks_type(name: str) -> bool:
    return bool(name) and name[0].isupper()


@dataclass(frozen=True)
class _NormalizedVersions:
    type_to_versions: dict[str, set[Any]]
    version_to_types: dict[Any, set[str]]


def _normalize_versions(
    versions: Mapping[Any, Any], eligible: Sequence[type] = ()
) -> _NormalizedVersions:
    known = {cl.__name__ for cl in eligible}
    type_to_versions: dict[str, set[Any]] = {}
    version_to_types: dict[Any, set[str]] = defaultdict(set)
    for key, value in versions.items():
        key_name = key.__name__ if isinstance(key, type) else key
        if isinstance(key, type) or key_name in known or _looks_type(key_name):
            if isinstance(value, (set, list, tuple, frozenset)):
                type_to_versions[key_name] = set(value)
            else:
                type_to_versions[key_name] = {value}
        else:
            version_to_types[key_name].update(
                v.__name__ if isinstance(v, type) else v for v in _as_iterable(value)
            )
    return _NormalizedVersions(type_to_versions, dict(version_to_types))


def _as_iterable(value: Any) -> Iterable[Any]:
    if isinstance(value, (set, list, tuple, frozenset)):
        return value
    return (value,)


@dataclass(frozen=True)
class _NormalizedFieldMap:
    attr_to_column: dict[str, str]
    column_to_attr: dict[str, str]


def _normalize_field_map(field_map: Mapping[str, str]) -> _NormalizedFieldMap:
    attr_to_column: dict[str, str] = {}
    column_to_attr: dict[str, str] = {}
    for key, value in field_map.items():
        attr_to_column.setdefault(key, value)
        column_to_attr.setdefault(value, key)
    return _NormalizedFieldMap(attr_to_column, column_to_attr)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Declaration:
    """One original mention of an occurrence."""

    table: str
    index: int
    registration: Any
    values: Mapping[str, Any] = dc_field(default_factory=dict)

    @property
    def location(self) -> str:
        return f"{self.table}[{self.index}]"

    def __str__(self) -> str:
        return f"{self.location} (registration {self.registration!r})"


@dataclass(frozen=True)
class CandidateEvidence:
    """Why one type remains a viable explanation."""

    type: type
    name: str
    distinguishing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __str__(self) -> str:
        fields = ", ".join(self.distinguishing_fields) or "none"
        return f"{self.name} (distinguishing fields: {fields})"


@dataclass
class Ambiguity:
    """An occurrence with more than one viable explanation."""

    registration: Any
    declarations: list[Declaration]
    candidates: list[CandidateEvidence]
    references: list[tuple[str, str, Any]] = dc_field(default_factory=list)

    @property
    def locations(self) -> list[str]:
        return [d.location for d in self.declarations]

    def __str__(self) -> str:
        return (
            f"registration {self.registration!r} at "
            f"{', '.join(self.locations)}: "
            + "; ".join(str(c) for c in self.candidates)
        )


@dataclass
class Conflict:
    """Contradictory declarations about one occurrence or reference."""

    registration: Any
    field: str | None
    declarations: list[Declaration] = dc_field(default_factory=list)
    references: list[tuple[str, str, Any]] = dc_field(default_factory=list)
    reason: str = ""

    @property
    def locations(self) -> list[str]:
        return [d.location for d in self.declarations]

    def __str__(self) -> str:
        where = ", ".join(self.locations) or "batch"
        refs = ", ".join(
            f"{table}.{attribute} -> {registration!r}"
            for table, attribute, registration in self.references
        )
        return f"{where}: {self.reason}" + (f" (references: {refs})" if refs else "")


@dataclass
class BatchDiagnostics:
    """Everything the resolver could not settle.

    :ivar ambiguities: Occurrences with several viable explanations.
    :ivar conflicts: Contradictory declarations and references.
    :ivar missing_references: References whose registration does not occur.
    :ivar missing_fields: Required model fields never reported.
    :ivar cycles: Reference cycles not covered by a late-bound relation.
    :ivar delivered: Registrations of components that were delivered.
    :ivar skipped: Registrations of components skipped due to an issue.
    """

    ambiguities: list[Ambiguity] = dc_field(default_factory=list)
    conflicts: list[Conflict] = dc_field(default_factory=list)
    missing_references: list[tuple[str, str, Any]] = dc_field(default_factory=list)
    missing_fields: list[tuple[Any, tuple[str, ...]]] = dc_field(default_factory=list)
    cycles: list[tuple[Any, ...]] = dc_field(default_factory=list)
    delivered: list[Any] = dc_field(default_factory=list)
    skipped: list[Any] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.ambiguities
            or self.conflicts
            or self.missing_references
            or self.missing_fields
            or self.cycles
        )

    def __bool__(self) -> bool:
        return self.ok

    def explain(self) -> str:
        """A human-readable report pointing at declarations and references."""
        lines: list[str] = []
        for ambiguity in self.ambiguities:
            lines.append(f"ambiguous: {ambiguity}")
            for candidate in ambiguity.candidates:
                lines.append(f"  - {candidate}")
        for conflict in self.conflicts:
            lines.append(f"conflict: {conflict}")
        for table, attribute, registration in self.missing_references:
            lines.append(f"missing reference: {table}.{attribute} -> {registration!r}")
        for registration, fields in self.missing_fields:
            lines.append(
                f"missing fields for registration {registration!r}: "
                + ", ".join(fields)
            )
        for cycle in self.cycles:
            lines.append(
                "cycle without late binding: " + " -> ".join(repr(r) for r in cycle)
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal occurrence model
# ---------------------------------------------------------------------------


@dataclass
class _Occurrence:
    registration: Any
    declarations: list[Declaration] = dc_field(default_factory=list)
    candidates: set[type] = dc_field(default_factory=set)
    pinned: type | None = None
    values: dict[str, Any] = dc_field(default_factory=dict)
    value_sources: dict[str, Declaration] = dc_field(default_factory=dict)
    refs: dict[str, tuple[Reference, Declaration, Any]] = dc_field(default_factory=dict)
    value: Any = None
    delivered: bool = False
    reasons: list[str] = dc_field(default_factory=list)

    @property
    def primary(self) -> Declaration:
        return self.declarations[0]

    @property
    def locations(self) -> str:
        return ", ".join(d.location for d in self.declarations)

    def __hash__(self) -> int:
        return id(self)


@dataclass
class _State:
    spec: BatchSpec
    converter: Any
    types: dict[str, type]
    model_fields: set[str]
    occurrences: dict[Any, _Occurrence]
    registration_order: list[Any]
    diagnostics: BatchDiagnostics
    edges: dict[Any, list[tuple[Any, str, Reference]]] = dc_field(
        default_factory=lambda: defaultdict(list)
    )
    late_edges: set[tuple[Any, Any, str]] = dc_field(default_factory=set)
    missing_ref_owners: set[Any] = dc_field(default_factory=set)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def structure_batch(
    converter: Any,
    spec: BatchSpec,
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    strict: bool | None = None,
) -> Batch:
    """Structure all tables together and resolve cross-record references.

    :param converter: The :class:`cattrs.Converter` used for individual field
        values and scalar coercion.
    :param spec: The batch description.
    :param tables: Table name -> rows.  Rows sharing a registration number are
        merged into one occurrence.
    :param strict: Override ``spec.strict``.  When true, unresolved components
        raise :class:`BatchIncompleteError` after the good ones are built.
    """
    if strict is None:
        strict = spec.strict

    state = _build_state(spec, converter, tables)
    _collect(state)
    _propagate(state)
    _search_joint_solutions(state)
    _detect_conflicts(state)
    _report_ambiguities(state)
    _detect_components(state)

    batch = Batch(converter, spec, state)
    _construct_components(batch)
    _bind_late(batch)
    _finish(batch)

    if strict and not state.diagnostics.ok:
        raise _incomplete_error(state.diagnostics)

    return batch


#: Load a batch; an alias for :func:`structure_batch`.
load_batch = structure_batch

#: Resolve a batch; an alias for :func:`structure_batch`.
resolve_batch = structure_batch


def _build_state(
    spec: BatchSpec, converter: Any, tables: Mapping[str, Iterable[Mapping[str, Any]]]
) -> _State:
    types: dict[str, type] = {}
    model_fields: set[str] = set()
    for table_spec in spec.tables.values():
        for cl in table_spec.eligible_types():
            types.setdefault(cl.__name__, cl)
            for at in _fields(cl):
                model_fields.add(at.name)

    occurrences: dict[Any, _Occurrence] = {}
    order: list[Any] = []

    for table, rows in tables.items():
        if table not in spec.tables:
            raise BatchError(f"Table {table!r} has no TableSpec.")
        table_spec = spec.tables[table]
        id_column = spec.id_field(table)
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                raise BatchError(
                    f"{table}[{index}] is not a mapping; batch rows must be mappings."
                )
            raw = dict(raw)
            registration = _lookup(raw, id_column)
            if registration is None and id_column in raw:
                # An explicit null registration cannot identify an occurrence.
                raise BatchError(f"{table}[{index}] has a null registration number.")
            if registration is None:
                raise BatchError(
                    f"{table}[{index}] is missing its registration column "
                    f"{id_column!r}."
                )
            if registration not in occurrences:
                occurrences[registration] = _Occurrence(registration)
                order.append(registration)
            occ = occurrences[registration]
            mapped = {
                table_spec.attribute_for(column): value for column, value in raw.items()
            }
            mapped.setdefault(table_spec.attribute_for(id_column), registration)
            declaration = Declaration(
                table=table, index=index, registration=registration, values=mapped
            )
            occ.declarations.append(declaration)

    return _State(
        spec=spec,
        converter=converter,
        types=types,
        model_fields=model_fields,
        occurrences=occurrences,
        registration_order=order,
        diagnostics=BatchDiagnostics(),
    )


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


def _collect(state: _State) -> None:
    all_types = set(state.types.values())
    for occ in state.occurrences.values():
        candidates = set(all_types)
        for declaration in occ.declarations:
            table_spec = state.spec.tables[declaration.table]
            candidates &= _candidates_for(state, table_spec, declaration)
        if not candidates:
            _record_no_explanation(state, occ)
            continue
        occ.candidates = candidates
        _merge_values(state, occ)
        _pin(occ)


def _candidates_for(
    state: _State, table_spec: TableSpec, declaration: Declaration
) -> set[type]:
    values = declaration.values
    tag = values.get(table_spec.tag_field, NOTHING)
    result: set[type] = set()

    for cl in table_spec.eligible_types():
        if not table_spec.version_allows(cl):
            continue
        if tag is not NOTHING and tag is not None:
            labels = table_spec.type_for(tag)
            if labels:
                # A subtype label pins that subtype.  A base label is
                # base-compatible: older tables label everything with the base
                # type, and a later reference may require a finer subtype.
                if cl.__name__ not in labels and not _label_is_base(
                    table_spec, labels, cl
                ):
                    continue
            elif cl.__name__ != tag:
                continue
        elif tag is None and table_spec.labels:
            # An explicit null tag is a reported value, not a blank.
            if table_spec.label_for(cl) is not None:
                continue
        elif tag is NOTHING and not table_spec.allow_untagged:
            continue

        if _fields_compatible(
            cl, values, state.model_fields, tag_field=table_spec.tag_field
        ):
            result.add(cl)
    return result


def _fields_compatible(
    cl: type, values: Mapping[str, Any], model_fields: set[str], tag_field: str
) -> bool:
    fields_by_name = {at.name: at for at in _fields(cl)}
    for name, value in values.items():
        if name == tag_field:
            continue
        at = fields_by_name.get(name)
        if at is None:
            # A field known to the model on another branch rules this branch out.
            if name in model_fields:
                return False
            continue
        if value is None:
            # An explicit null is a reported value: it is only compatible with
            # a field that admits None.
            if not _allows_none(at.type):
                return False
            continue
        if at.type is not None and not _value_matches(at.type, value):
            return False
    return True


def _label_is_base(table_spec: TableSpec, labels: Sequence[str], cl: type) -> bool:
    """Whether `labels` names an ancestor of `cl` (a coarse base label).

    A label for the table's root model is coarse: it admits every subtype,
    since older tables label everything with the base type and later evidence
    may require a finer one.  A label for a non-root type is specific: it
    admits that type and its unlabelled descendants, but not a descendant this
    table can already distinguish with its own label.
    """
    root = table_spec.model.__name__
    type_to_label = _normalize_labels(
        table_spec.labels, table_spec.eligible_types()
    ).type_to_label
    for base in cl.__mro__[1:]:
        if base.__name__ not in labels:
            continue
        if base.__name__ == root:
            return True
        # A specific parent label; exclude descendants with their own label.
        return cl.__name__ not in type_to_label
    return False


def _merge_values(state: _State, occ: _Occurrence) -> None:
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        for name, value in declaration.values.items():
            if name == table_spec.tag_field:
                # Tags may legitimately differ between tables for one type; the
                # export writes each table's own label.
                if name not in occ.values:
                    occ.values[name] = value
                    occ.value_sources[name] = declaration
                continue
            if name not in occ.values:
                occ.values[name] = value
                occ.value_sources[name] = declaration
                continue
            previous = occ.values[name]
            if previous == value:
                continue
            reason = (
                f"conflicting values for {name!r}: {previous!r} at "
                f"{occ.value_sources[name].location} and {value!r} at "
                f"{declaration.location}"
            )
            _record_conflict(state, occ, name, reason, [declaration])


def _record_conflict(
    state: _State,
    occ: _Occurrence,
    field: str | None,
    reason: str,
    extra: Sequence[Declaration] = (),
) -> None:
    declarations = list(occ.declarations)
    for declaration in extra:
        if declaration not in declarations:
            declarations.append(declaration)
    conflict = Conflict(
        registration=occ.registration,
        field=field,
        declarations=declarations,
        references=_references_from(state, occ),
        reason=reason,
    )
    if conflict not in state.diagnostics.conflicts:
        state.diagnostics.conflicts.append(conflict)


def _record_no_explanation(state: _State, occ: _Occurrence) -> None:
    details = []
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        tag = declaration.values.get(table_spec.tag_field, NOTHING)
        if tag is not NOTHING:
            details.append(f"{declaration.location} labels it {tag!r}")
        reported = ", ".join(
            sorted(
                k
                for k in declaration.values
                if k
                not in (table_spec.tag_field, state.spec.id_field(declaration.table))
            )
        )
        details.append(f"{declaration.location} reports {reported or 'no fields'}")
    reason = "no type is compatible with every mention"
    if details:
        reason += ": " + "; ".join(details)
    _record_conflict(state, occ, None, reason)
    # Preserve all types as candidates so the report can list them.
    occ.candidates = set(state.types.values())


def _pin(occ: _Occurrence) -> None:
    if len(occ.candidates) == 1:
        occ.pinned = next(iter(occ.candidates))


# ---------------------------------------------------------------------------
# Reference propagation
# ---------------------------------------------------------------------------


def _declared_references(
    state: _State, occ: _Occurrence
) -> dict[str, tuple[Reference, Any]]:
    """References declared by the spec or by any candidate's field metadata."""
    result: dict[str, tuple[Reference, Any]] = {}
    for declaration in occ.declarations:
        for name, ref in state.spec.references_for(declaration.table).items():
            result.setdefault(name, (ref, None))
    for cl in occ.candidates:
        for at in _fields(cl):
            if _is_late_field(at) and at.name not in result:
                result.setdefault(at.name, (Reference(late=True), None))
            ref = at.metadata.get("cattrs_reference")
            if isinstance(ref, Reference):
                result.setdefault(at.name, (ref, None))
    return result


def _references_from(state: _State, occ: _Occurrence) -> list[tuple[str, str, Any]]:
    refs: list[tuple[str, str, Any]] = []
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        declared = dict(state.spec.references_for(declaration.table))
        for cl in occ.candidates or set(state.types.values()):
            for at in _fields(cl):
                ref = at.metadata.get("cattrs_reference")
                if isinstance(ref, Reference):
                    declared.setdefault(at.name, ref)
                if _is_late_field(at):
                    declared.setdefault(at.name, Reference(late=True))
        for name, ref in declared.items():
            reg = _reference_value(declaration.values, name, ref, table_spec)
            if reg is not NOTHING:
                refs.append((declaration.table, name, reg))
    return _dedupe(refs)


def _reference_value(
    values: Mapping[str, Any], name: str, ref: Reference, table_spec: TableSpec
) -> Any:
    column = table_spec.column_for(name)
    if ref.key is not None:
        key_column = table_spec.column_for(ref.key)
        if key_column in values:
            return values[key_column]
        if ref.key in values:
            return values[ref.key]
        return NOTHING
    if column in values:
        return values[column]
    if name in values:
        return values[name]
    return NOTHING


def _propagate(state: _State) -> None:
    """Narrow candidate types through references until a fixpoint."""
    state.edges.clear()
    state.diagnostics.missing_references.clear()
    state.missing_ref_owners.clear()
    changed = True
    while changed:
        changed = False
        for registration in state.registration_order:
            occ = state.occurrences[registration]
            refs = _declared_references(state, occ)
            for name, (ref, _) in refs.items():
                raw = _reference_value(
                    occ.values, name, ref, state.spec.tables[occ.primary.table]
                )
                if raw is NOTHING:
                    continue
                targets = _targets(state, raw, ref)
                if raw is None and not ref.many:
                    # An explicit null reference is a reported absence.
                    continue
                if not targets:
                    state.diagnostics.missing_references.append(
                        (occ.primary.table, name, raw)
                    )
                    state.diagnostics.missing_references = _dedupe(
                        state.diagnostics.missing_references
                    )
                    state.missing_ref_owners.add(registration)
                    continue
                for target in targets:
                    edge = (target.registration, name, ref)
                    if edge not in state.edges[registration]:
                        state.edges[registration].append(edge)
                    if ref.late or _is_late_name(occ, name):
                        state.late_edges.add((registration, target.registration, name))

                # Backward: the target's type must fit the holder's relation.
                narrowed_holders = {
                    holder
                    for holder in occ.candidates
                    if _holder_accepts_targets(holder, name, targets, ref, state)
                }
                if narrowed_holders and narrowed_holders != occ.candidates:
                    occ.candidates = narrowed_holders
                    _pin(occ)
                    changed = True

                # Forward: the holder's relation type narrows the target.
                for target in targets:
                    narrowed = {
                        candidate
                        for candidate in target.candidates
                        if _target_fits_holders(
                            candidate, occ.candidates, name, ref, state
                        )
                    }
                    if narrowed and narrowed != target.candidates:
                        target.candidates = narrowed
                        _pin(target)
                        changed = True
            _pin(occ)


def _search_joint_solutions(state: _State) -> None:
    """Search connected components for globally consistent type assignments.

    Arc consistency alone can leave a set of locally plausible types where the
    batch as a whole has exactly one explanation.  A bounded backtracking
    search over each connected component finds it.  Components with several
    solutions stay ambiguous and are reported later.
    """
    adjacency: dict[Any, set[Any]] = defaultdict(set)
    for source, edges in state.edges.items():
        for target, _, _ in edges:
            adjacency[source].add(target)
            adjacency[target].add(source)

    seen: set[Any] = set()
    for registration in state.registration_order:
        if registration in seen:
            continue
        component: list[Any] = []
        pending = [registration]
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            pending.extend(adjacency.get(node, ()))

        if not any(len(state.occurrences[r].candidates) > 1 for r in component):
            continue
        if any(not state.occurrences[r].candidates for r in component):
            continue

        constraints = [
            (source, target, name, ref)
            for source in component
            for target, name, ref in state.edges.get(source, ())
            if target in component
        ]
        solutions = _solve_component(state, component, constraints)
        if solutions is None:
            continue
        if len(solutions) == 1:
            for member, chosen in solutions[0].items():
                occ = state.occurrences[member]
                if occ.candidates != {chosen}:
                    occ.evidence = [
                        *occ.evidence,
                        f"{occ.locations}: joint evidence requires {chosen.__name__}",
                    ]
                occ.candidates = {chosen}
                _pin(occ)


#: Cap on assignment attempts before the joint search gives up.
_MAX_SEARCH_ASSIGNMENTS = 200_000


def _solve_component(
    state: _State,
    component: Sequence[Any],
    constraints: Sequence[tuple[Any, Any, str, Reference]],
) -> list[dict[Any, type]] | None:
    """Find up to two complete type assignments for one component."""
    by_member = {member: constraints_for(constraints, member) for member in component}
    order = sorted(component, key=lambda m: len(state.occurrences[m].candidates))
    solutions: list[dict[Any, type]] = []
    assignment: dict[Any, type] = {}
    attempts = 0

    def compatible(member: Any, chosen: type) -> bool:
        for source, target, name, ref in by_member[member]:
            if (
                source == member
                and target in assignment
                and not _assignment_holds(state, chosen, assignment[target], name, ref)
            ):
                return False
            if (
                target == member
                and source in assignment
                and not _assignment_holds(state, assignment[source], chosen, name, ref)
            ):
                return False
        return True

    def recurse(index: int) -> None:
        nonlocal attempts
        if len(solutions) >= 2:
            return
        if index == len(order):
            solutions.append(dict(assignment))
            return
        member = order[index]
        for candidate in sorted(
            state.occurrences[member].candidates, key=lambda c: c.__name__
        ):
            attempts += 1
            if attempts > _MAX_SEARCH_ASSIGNMENTS:
                return
            if not compatible(member, candidate):
                continue
            assignment[member] = candidate
            recurse(index + 1)
            del assignment[member]
            if len(solutions) >= 2 or attempts > _MAX_SEARCH_ASSIGNMENTS:
                return

    recurse(0)
    if attempts > _MAX_SEARCH_ASSIGNMENTS and not solutions:
        return None
    return solutions


def constraints_for(
    constraints: Sequence[tuple[Any, Any, str, Reference]], member: Any
) -> list[tuple[Any, Any, str, Reference]]:
    return [
        constraint
        for constraint in constraints
        if constraint[0] == member or constraint[1] == member
    ]


def _assignment_holds(
    state: _State, holder: type, target: type, name: str, ref: Reference
) -> bool:
    at = _field(holder, name)
    if at is None:
        return False
    return _type_accepts(target, _allowed_target_types(at, ref, state))


def _targets(state: _State, raw: Any, ref: Reference) -> list[_Occurrence]:
    if ref.many:
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raw = [raw]
        result = []
        for item in raw:
            result.extend(
                _targets(state, item, Reference(key=ref.key, target=ref.target))
            )
        return result
    if raw in state.occurrences:
        return [state.occurrences[raw]]
    return []


def _holder_accepts_targets(
    holder: type,
    name: str,
    targets: Sequence[_Occurrence],
    ref: Reference,
    state: _State | None = None,
) -> bool:
    at = _field(holder, name)
    if at is None:
        # A relation not declared by this candidate rules it out.
        return False
    allowed = _allowed_target_types(at, ref, state)
    return any(
        any(_type_accepts(candidate, allowed) for candidate in target.candidates)
        for target in targets
    )


def _target_fits_holders(
    candidate: type,
    holders: set[type],
    name: str,
    ref: Reference,
    state: _State | None = None,
) -> bool:
    for holder in holders:
        at = _field(holder, name)
        if at is None:
            continue
        allowed = _allowed_target_types(at, ref, state)
        if _type_accepts(candidate, allowed):
            return True
    return False


def _allowed_target_types(
    at: Attribute, ref: Reference, state: _State | None = None
) -> Any:
    if ref.target is not None:
        return _normalize_declared(ref.target, state)
    return _element_type(at.type) if ref.many else at.type


def _element_type(declared: Any) -> Any:
    """The element type of a sequence declaration, or the declaration itself."""
    if declared is None:
        return None
    origin = get_origin(declared)
    if is_union_type(declared):
        args = [arg for arg in get_args(declared) if arg is not NoneType]
        if len(args) == 1:
            return _element_type(args[0])
        return Union[tuple(_element_type(arg) for arg in args)]
    if origin in (list, tuple, set, frozenset, Sequence):
        args = get_args(declared)
        return args[0] if args else None
    return declared


def _normalize_declared(declared: Any, state: _State | None) -> Any:
    if isinstance(declared, str) and state is not None:
        cl = state.types.get(declared)
        return cl if cl is not None else declared
    if isinstance(declared, tuple):
        return tuple(_normalize_declared(item, state) for item in declared)
    return declared


# ---------------------------------------------------------------------------
# Conflicts, ambiguity, components
# ---------------------------------------------------------------------------


def _detect_conflicts(state: _State) -> None:
    for registration in state.registration_order:
        occ = state.occurrences[registration]
        if not occ.candidates:
            continue
        refs = _declared_references(state, occ)
        for name, (ref, _) in refs.items():
            raw = _reference_value(
                occ.values, name, ref, state.spec.tables[occ.primary.table]
            )
            if raw is NOTHING:
                continue
            if raw is None and not ref.many:
                continue
            targets = _targets(state, raw, ref)
            for target in targets:
                for holder in occ.candidates:
                    at = _field(holder, name)
                    if at is None:
                        continue
                    allowed = _allowed_target_types(at, ref, state)
                    for candidate in target.candidates:
                        if not _type_accepts(candidate, allowed):
                            reason = (
                                f"{holder.__name__}.{name} declares {allowed!r}, "
                                f"but reference {raw!r} resolves to "
                                f"{candidate.__name__}"
                            )
                            _record_conflict(
                                state, occ, name, reason, target.declarations
                            )


def _report_ambiguities(state: _State) -> None:
    """Record every occurrence whose type evidence is not conclusive."""
    for registration in state.registration_order:
        occ = state.occurrences[registration]
        if occ.pinned is not None or not occ.candidates:
            continue
        candidates = [
            _candidate_evidence(state, occ, cl)
            for cl in sorted(occ.candidates, key=lambda c: c.__name__)
        ]
        state.diagnostics.ambiguities.append(
            Ambiguity(
                registration=registration,
                declarations=occ.declarations,
                candidates=candidates,
                references=_references_from(state, occ),
            )
        )


def _candidate_evidence(state: _State, occ: _Occurrence, cl: type) -> CandidateEvidence:
    reported = set(occ.values)
    distinguishing = []
    reasons = []
    for at in _fields(cl):
        if at.name in reported:
            continue
        distinguishing.append(at.name)
        if at.default is NOTHING:
            reasons.append(f"required field {at.name!r} not reported")
        else:
            reasons.append(f"optional field {at.name!r} not reported")
    if not distinguishing:
        reasons.append("all fields reported; only the label separates types")
    return CandidateEvidence(
        type=cl,
        name=cl.__name__,
        distinguishing_fields=tuple(distinguishing),
        reasons=tuple(reasons),
    )


def _detect_components(state: _State) -> None:
    """Group occurrences by weak connectivity; find unbroken cycles.

    Dependencies are directed (a holder depends on its target).  Late-bound
    relations are left out of the dependency graph, explicitly breaking the
    cycles they cover.
    """
    adjacency: dict[Any, set[Any]] = defaultdict(set)
    connectivity: dict[Any, set[Any]] = defaultdict(set)
    for source, edges in state.edges.items():
        for target, name, ref in edges:
            connectivity[source].add(target)
            connectivity[target].add(source)
            if not (ref.late or _is_late_for(state, source, name)):
                adjacency[source].add(target)

    # Tarjan's strongly connected components over dependencies.
    index = 0
    indices: dict[Any, int] = {}
    low: dict[Any, int] = {}
    stack: list[Any] = []
    on_stack: set[Any] = set()
    strongly_connected: list[list[Any]] = []

    def visit(node: Any) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in adjacency.get(node, ()):
            if neighbour not in indices:
                visit(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], indices[neighbour])
        if low[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            strongly_connected.append(component)

    for registration in state.registration_order:
        if registration not in indices:
            visit(registration)

    for component in strongly_connected:
        if len(component) > 1:
            state.diagnostics.cycles.append(tuple(component))

    # Weakly connected components are the delivery units.
    seen: set[Any] = set()
    components: list[list[Any]] = []
    for registration in state.registration_order:
        if registration in seen:
            continue
        component = []
        pending = [registration]
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            pending.extend(connectivity.get(node, ()))
        components.append(component)

    state.components = components  # type: ignore[attr-defined]


def _is_late_for(state: _State, registration: Any, name: str) -> bool:
    occ = state.occurrences[registration]
    for declaration in occ.declarations:
        if state.spec.late_attributes.get(declaration.table, {}).get(name):
            return True
    # Late binding is a property of the model, not of one declaration, so a
    # reference marked late is late wherever it points.
    return _model_marks_late(state, occ, name)


def _model_marks_late(state: _State, occ: _Occurrence, name: str) -> bool:
    candidates = occ.candidates or set(state.types.values())
    for cl in candidates:
        at = _field(cl, name)
        if at is not None and _is_late_field(at):
            return True
    return False


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _construct_components(batch: Batch) -> None:
    state = batch.state
    for component in getattr(state, "components", []):
        if _component_blocked(state, component):
            state.diagnostics.skipped.extend(component)
            continue
        delivered = _construct_component(batch, component)
        if delivered:
            state.diagnostics.delivered.extend(component)
            for registration in component:
                state.occurrences[registration].delivered = True
        else:
            state.diagnostics.skipped.extend(component)


def _component_blocked(state: _State, component: Sequence[Any]) -> bool:
    members = set(component)
    if any(set(cycle) & members for cycle in state.diagnostics.cycles):
        return True
    for registration in members:
        occ = state.occurrences[registration]
        if not occ.candidates or len(occ.candidates) > 1:
            return True
        if any(c.registration == registration for c in state.diagnostics.conflicts):
            return True
    # A missing reference inside this component means it cannot be completed.
    return bool(members & state.missing_ref_owners)


def _construct_component(batch: Batch, component: Sequence[Any]) -> bool:
    state = batch.state
    members = [state.occurrences[r] for r in component]

    # Ensure every required field has a value before constructing anything.
    for occ in members:
        missing = _required_missing(occ, state)
        if missing:
            state.diagnostics.missing_fields.append((occ.registration, tuple(missing)))
            return False

    # Build in dependency order so ordinary constructors can receive real
    # objects.  Cycles are broken at late-bound edges.
    ordered = _construction_order(state, component, members)
    for occ in ordered:
        _construct_occurrence(batch, occ)

    return True


def _bind_late(batch: Batch) -> None:
    """Second phase: bind relations the model declared late.

    Every delivered occurrence exists by now, so cycles resolve to shared
    objects without exposing a half-built instance to ordinary callers.
    """
    state = batch.state
    for registration in state.registration_order:
        occ = state.occurrences[registration]
        if not occ.delivered or occ.value is None:
            continue
        for name, (ref, _) in _declared_references(state, occ).items():
            if not (ref.late or _is_late_for(state, occ.registration, name)):
                continue
            raw = _reference_value(
                occ.values, name, ref, state.spec.tables[occ.primary.table]
            )
            if raw is NOTHING:
                continue
            value = _reference_objects(state, raw, ref)
            object.__setattr__(occ.value, name, value)


def _construction_order(
    state: _State, component: Sequence[Any], members: Sequence[_Occurrence]
) -> list[_Occurrence]:
    members_by_reg = {occ.registration: occ for occ in members}
    pending = set(component)
    ordered: list[_Occurrence] = []
    while pending:
        progressed = False
        for registration in list(pending):
            dependencies = [
                target
                for target, name, ref in state.edges.get(registration, ())
                if target in pending
                and target != registration
                and not (ref.late or _is_late_for(state, registration, name))
            ]
            if not dependencies:
                ordered.append(members_by_reg[registration])
                pending.remove(registration)
                progressed = True
        if not progressed:
            # A non-late cycle; construction will be skipped and reported.
            ordered.extend(members_by_reg[r] for r in pending)
            break
    return ordered


def _required_missing(occ: _Occurrence, state: _State) -> list[str]:
    cl = occ.pinned
    if cl is None:
        return []
    missing = []
    for at in _fields(cl):
        if not at.init or at.default is not NOTHING:
            continue
        if at.name in occ.values:
            continue
        # A late relation may be absent during the main phase.
        ref = at.metadata.get("cattrs_reference")
        if _is_late_field(at) or (isinstance(ref, Reference) and ref.late):
            continue
        # A reference may be supplied by its key column rather than its own
        # attribute name.
        if _has_reference_source(state, occ, at.name):
            continue
        missing.append(at.name)
    return missing


def _has_reference_source(state: _State, occ: _Occurrence, name: str) -> bool:
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        refs = state.spec.references_for(declaration.table)
        ref = refs.get(name)
        if ref is not None:
            if ref.key is not None:
                key = table_spec.column_for(ref.key)
                if key in declaration.values or ref.key in declaration.values:
                    return True
            if name in declaration.values:
                return True
    return False


def _construct_occurrence(batch: Batch, occ: _Occurrence) -> None:
    state = batch.state
    cl = occ.pinned
    assert cl is not None
    kwargs: dict[str, Any] = {}
    post: dict[str, Any] = {}
    refs = _declared_references(state, occ)

    for at in _fields(cl):
        name = at.name
        ref_entry = refs.get(name)
        if ref_entry is not None:
            ref, _ = ref_entry
            raw = _reference_value(
                occ.values, name, ref, state.spec.tables[occ.primary.table]
            )
            if raw is NOTHING:
                continue
            if ref.late or _is_late_for(state, occ.registration, name):
                # Late-bound relations are installed after all objects exist.
                continue
            value = _reference_objects(state, raw, ref)
        elif name in occ.values:
            raw = _decode_for_occurrence(state, occ, at, occ.values[name])
            value = _structure_value(state.converter, cl, at, raw)
        else:
            continue

        if at.init:
            kwargs[at.alias] = value
        else:
            post[at.name] = value

    try:
        occ.value = cl(**kwargs)
    except TypeError as exc:
        if not _is_missing_late_argument(exc):
            raise
        # A required relation declared late is not available yet; allocate a
        # shell and fill it, then let the late phase bind the relation.
        occ.value = _shell(cl)
        for at in _fields(cl):
            if at.name in post:
                continue
            if at.alias in kwargs:
                object.__setattr__(occ.value, at.name, kwargs[at.alias])
            elif at.default is not NOTHING and at.name not in occ.values:
                object.__setattr__(occ.value, at.name, _default_value(at))
    for name, value in post.items():
        object.__setattr__(occ.value, name, value)
    _mark_incomplete(occ.value)


def _is_missing_late_argument(exc: TypeError) -> bool:
    """Whether a TypeError is an absent late-bound constructor argument."""
    message = str(exc)
    return "required positional argument" in message or (
        "missing" in message and "required keyword-only argument" in message
    )


def _shell(cl: type) -> Any:
    try:
        return object.__new__(cl)
    except TypeError:
        return cl.__new__(cl)


def _mark_incomplete(value: Any) -> None:
    with suppress(AttributeError, TypeError):
        object.__setattr__(value, "_cattrs_batch_ready", False)


def _mark_ready(value: Any) -> None:
    with suppress(AttributeError, TypeError):
        object.__setattr__(value, "_cattrs_batch_ready", True)


def _decode_for_occurrence(
    state: _State, occ: _Occurrence, at: Attribute, raw: Any
) -> Any:
    """Apply the source table's code mapping before normal structuring."""
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        if at.name not in declaration.values:
            continue
        column = table_spec.column_for(at.name)
        if column in declaration.values:
            raw = declaration.values[column]
        break
    if occ.pinned is None:
        return raw
    for declaration in occ.declarations:
        table_spec = state.spec.tables[declaration.table]
        column = table_spec.column_for(at.name)
        if column in declaration.values:
            return table_spec.decode(occ.pinned, column, raw)
    return raw


def _structure_value(converter: Any, cl: type, at: Attribute, raw: Any) -> Any:
    if at.type is None:
        return raw
    # Let converter hooks apply normal coercion; keep raw if no hook exists.
    try:
        hook = converter.get_structure_hook(at.type)
    except Exception:
        return raw
    try:
        return hook(raw, at.type)
    except Exception:
        return raw


def _reference_objects(state: _State, raw: Any, ref: Reference) -> Any:
    if raw is None:
        return None
    if ref.many:
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raw = [raw]
        result = []
        for item in raw:
            target = state.occurrences.get(item)
            result.append(target.value if target is not None else None)
        return result
    if raw in state.occurrences:
        return state.occurrences[raw].value
    return None


def _finish(batch: Batch) -> None:
    for occ in batch.state.occurrences.values():
        if occ.delivered and occ.value is not None:
            _mark_ready(occ.value)
    batch.complete = True


# ---------------------------------------------------------------------------
# The batch result
# ---------------------------------------------------------------------------


class Batch:
    """The result of :func:`structure_batch`.

    Only components with a unique complete explanation are constructed.  The
    remaining components are described in :attr:`diagnostics`.
    """

    def __init__(self, converter: Any, spec: BatchSpec, state: _State) -> None:
        self.converter = converter
        self.spec = spec
        self.state = state
        self.diagnostics = state.diagnostics
        self.complete = False

    @property
    def ok(self) -> bool:
        return self.diagnostics.ok

    @property
    def tables(self) -> dict[str, list[Any]]:
        """Delivered objects grouped by the tables that mentioned them.

        An occurrence mentioned in several tables appears in each, but it is
        always the same object.
        """
        result: dict[str, list[Any]] = {name: [] for name in self.spec.tables}
        for registration in self.state.registration_order:
            occ = self.state.occurrences[registration]
            if not occ.delivered:
                continue
            for table in dict.fromkeys(d.table for d in occ.declarations):
                result[table].append(occ.value)
        return result

    def occurrences(self, table: str | None = None) -> list[Any]:
        """Delivered objects, optionally limited to one table."""
        result = []
        for registration in self.state.registration_order:
            occ = self.state.occurrences[registration]
            if not occ.delivered:
                continue
            if table is None or any(d.table == table for d in occ.declarations):
                result.append(occ.value)
        return result

    def by_registration(self, registration: Any) -> Any:
        """The single object for `registration`."""
        occ = self.state.occurrences.get(registration)
        if occ is None:
            raise KeyError(registration)
        if not occ.delivered:
            raise BatchIncompleteError(
                f"Registration {registration!r} was not delivered; see diagnostics.",
                self.diagnostics,
            )
        return occ.value

    def get(self, registration: Any, default: Any = None) -> Any:
        occ = self.state.occurrences.get(registration)
        if occ is None or not occ.delivered:
            return default
        return occ.value

    def __getitem__(self, table: str) -> list[Any]:
        return self.tables[table]

    def __iter__(self):
        return iter(self.spec.tables)

    def __len__(self) -> int:
        return sum(1 for occ in self.state.occurrences.values() if occ.delivered)

    def explain(self) -> str:
        return self.diagnostics.explain()

    def raise_for_errors(self) -> None:
        if not self.diagnostics.ok:
            raise _incomplete_error(self.diagnostics)


def _incomplete_error(diagnostics: BatchDiagnostics) -> BatchIncompleteError:
    if diagnostics.cycles:
        return BatchCycleError(
            "Reference cycle without a late-bound relation: "
            + "; ".join(" -> ".join(repr(r) for r in c) for c in diagnostics.cycles),
            diagnostics,
        )
    if diagnostics.conflicts:
        return BatchConflictError(
            "Batch contains conflicting declarations:\n" + diagnostics.explain(),
            diagnostics.conflicts,
        )
    if diagnostics.ambiguities:
        return BatchAmbiguityError(
            "Batch contains ambiguous occurrences:\n" + diagnostics.explain(),
            diagnostics.ambiguities,
        )
    return BatchIncompleteError(
        "Batch could not be completely structured:\n" + diagnostics.explain(),
        diagnostics,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_batch(
    batch: Batch,
    *,
    tables: Iterable[str] | None = None,
    include: Mapping[str, Iterable[str]] | None = None,
    include_tag: bool = True,
    reported_only: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Export delivered occurrences back into table rows.

    Each table receives its own labels, so a legacy table gets the legacy tag
    even when the occurrence was resolved through a modern sibling table.
    Relations are written as registration numbers again, ready for re-import.
    Explicit nulls are preserved.

    By default only columns the table actually reported are written, so a
    blank stays blank instead of being filled with a model default.  Set
    `reported_only` to false to write every model field the table can express.

    :param tables: Restrict the export to these tables.
    :param include: Per-table attribute allow-lists.
    :param include_tag: Write the table's tag column when it has labels.
    :param reported_only: Write only columns the table reported.
    """
    selected = list(tables) if tables is not None else list(batch.spec.tables)
    result: dict[str, list[dict[str, Any]]] = {}
    for table in selected:
        table_spec = batch.spec.tables[table]
        allow = set(include[table]) if include and table in include else None
        rows: list[dict[str, Any]] = []
        for registration in batch.state.registration_order:
            occ = batch.state.occurrences[registration]
            if not occ.delivered:
                continue
            if not any(d.table == table for d in occ.declarations):
                continue
            rows.append(
                _export_row(
                    batch,
                    table,
                    table_spec,
                    occ,
                    allow,
                    include_tag,
                    ordered=include is not None,
                    reported_only=reported_only,
                )
            )
        result[table] = rows
    return result


def _export_row(
    batch: Batch,
    table: str,
    table_spec: TableSpec,
    occ: _Occurrence,
    allow: set[str] | None,
    include_tag: bool,
    *,
    ordered: bool,
    reported_only: bool = True,
) -> dict[str, Any]:
    cl = occ.pinned
    assert cl is not None
    refs = _declared_references(batch.state, occ)
    row: dict[str, Any] = {}

    id_column = batch.spec.id_field(table)
    row[id_column] = occ.registration

    if table_spec.export_fields is not None:
        declared_columns = set(table_spec.export_fields)
    elif reported_only:
        declared_columns = _columns_reported_by(batch, table, occ, refs)
    else:
        declared_columns = _reported_columns(batch, table, cl, refs, allow)

    for at in _fields(cl):
        name = at.name
        if allow is not None and name not in allow:
            continue
        column = table_spec.column_for(name)
        ref_entry = refs.get(name)
        if ref_entry is not None:
            ref, _ = ref_entry
            if ref.key is not None:
                column = table_spec.column_for(ref.key)
            if column not in declared_columns:
                continue
            value = _export_reference(getattr(occ.value, name, None), ref)
            row[column] = value
            continue
        if column not in declared_columns:
            continue
        value = getattr(occ.value, name, _default_value(at))
        row[column] = table_spec.encode(cl, column, value)

    if include_tag and table_spec.labels:
        label = table_spec.label_for(cl)
        if label is not None:
            tag_column = table_spec.column_for(table_spec.tag_field)
            row[tag_column] = label
    if ordered and table_spec.export_fields is not None:
        row = _order_row(row, table_spec, id_column)
    return row


def _columns_reported_by(
    batch: Batch,
    table: str,
    occ: _Occurrence,
    refs: Mapping[str, tuple[Reference, Any]],
) -> set[str]:
    """The columns this particular occurrence reported in this table."""
    table_spec = batch.spec.tables[table]
    columns: set[str] = {batch.spec.id_field(table)}
    for declaration in occ.declarations:
        if declaration.table != table:
            continue
        for name in declaration.values:
            columns.add(table_spec.column_for(name))
        for name, (ref, _) in refs.items():
            source = ref.key if ref.key is not None else name
            if source in declaration.values:
                columns.add(table_spec.column_for(source))
    return columns


def _reported_columns(
    batch: Batch,
    table: str,
    cl: type,
    refs: Mapping[str, tuple[Reference, Any]],
    allow: set[str] | None,
) -> set[str]:
    """Columns this table can express: the model's fields plus references."""
    table_spec = batch.spec.tables[table]
    columns: set[str] = {batch.spec.id_field(table)}
    for at in _fields(cl):
        if allow is not None and at.name not in allow:
            continue
        if at.name in refs:
            ref, _ = refs[at.name]
            source = ref.key if ref.key is not None else at.name
            columns.add(table_spec.column_for(source))
        else:
            columns.add(table_spec.column_for(at.name))
    return columns


def _order_row(
    row: dict[str, Any], table_spec: TableSpec, id_column: str
) -> dict[str, Any]:
    ordered: dict[str, Any] = {id_column: row.pop(id_column)}
    for field in table_spec.export_fields or ():
        column = table_spec.column_for(field)
        if column in row:
            ordered[column] = row.pop(column)
    tag_column = table_spec.column_for(table_spec.tag_field)
    if tag_column in row:
        ordered[tag_column] = row.pop(tag_column)
    ordered.update(row)
    return ordered


def _export_reference(value: Any, ref: Reference) -> Any:
    if value is None:
        return None
    if ref.many:
        return [
            getattr(item, "_batch_registration", None)
            if hasattr(item, "_batch_registration")
            else _registration_of(item)
            for item in value
        ]
    return _registration_of(value)


def _registration_of(value: Any) -> Any:
    registration = getattr(value, "_batch_registration", NOTHING)
    if registration is not NOTHING:
        return registration
    # Fall back to an id-like attribute so export stays re-importable.
    for name in ("id", "registration", "part_number"):
        if hasattr(value, name):
            return getattr(value, name)
    return value


#: An alias for :func:`export_batch`.
unstructure_batch = export_batch


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _class_tree(cl: type) -> list[type]:
    # attrs keeps a duplicate entry in `__subclasses__` until a GC pass.
    _collect_garbage()
    seen: list[type] = []
    stack = [cl]
    while stack:
        current = stack.pop(0)
        if current in seen:
            continue
        seen.append(current)
        stack.extend(current.__subclasses__())
    return seen


def _fields(cl: type) -> list[Attribute]:
    return adapted_fields(get_origin(cl) or cl)


def _field(cl: type, name: str) -> Attribute | None:
    for at in _fields(cl):
        if at.name == name:
            return at
    return None


def _is_late_field(at: Attribute) -> bool:
    if at.metadata.get("cattrs_late"):
        return True
    marker = at.metadata.get("cattrs_reference")
    if isinstance(marker, Reference) and marker.late:
        return True
    return isinstance(at.default, LateBound)


def _is_late_name(occ: _Occurrence, name: str) -> bool:
    if occ.pinned is None:
        return False
    at = _field(occ.pinned, name)
    return at is not None and _is_late_field(at)


def _lookup(raw: Mapping[str, Any], key: str) -> Any:
    return raw.get(key)


def _allows_none(typ: Any) -> bool:
    if typ is None or typ is NoneType:
        return True
    if is_union_type(typ):
        return any(arg is NoneType for arg in get_args(typ))
    return False


def _value_matches(typ: Any, value: Any) -> bool:
    if typ is None:
        return True
    origin = get_origin(typ)
    if origin is not None:
        args = get_args(typ)
        if origin is list and isinstance(value, list) and args:
            return all(_value_matches(args[0], item) for item in value)
        if origin is dict and isinstance(value, Mapping) and len(args) == 2:
            return all(
                _value_matches(args[0], k) and _value_matches(args[1], v)
                for k, v in value.items()
            )
        if origin is Literal:
            return value in args
        if origin is type and args:
            return any(
                value is arg
                or getattr(value, "__name__", None) == getattr(arg, "__name__", None)
                for arg in args
            )
        if is_union_type(typ):
            return any(_value_matches(arg, value) for arg in args)
        typ = origin
    if typ is Any:
        return True
    if isinstance(typ, type):
        if issubclass(typ, Enum):
            try:
                typ(value)
            except (TypeError, ValueError):
                return value in {member.name for member in typ}
            return True
        try:
            return isinstance(value, typ)
        except TypeError:
            return True
    return True


def _type_accepts(candidate: type, declared: Any) -> bool:
    """Whether `candidate` satisfies a declared relation type."""
    if declared is None:
        return True
    if isinstance(declared, str):
        return candidate.__name__ == declared or any(
            base.__name__ == declared for base in candidate.__mro__[1:]
        )
    if isinstance(declared, (tuple, list, set, frozenset)):
        return any(_type_accepts(candidate, item) for item in declared)
    origin = get_origin(declared)
    if is_union_type(declared):
        return any(_type_accepts(candidate, arg) for arg in get_args(declared))
    if origin is type:
        return any(_type_accepts(candidate, arg) for arg in get_args(declared))
    if origin is not None:
        declared = origin
    try:
        return issubclass(candidate, declared)
    except TypeError:
        return candidate is declared


def _init_name(cl: type, name: str) -> str:
    at = _field(cl, name)
    return at.alias if at is not None else name


def _default_value(at: Attribute) -> Any:
    if at.default is NOTHING:
        return None
    if isinstance(at.default, Factory):
        return at.default.factory()
    return at.default


def _dedupe(items: Iterable[Any]) -> list[Any]:
    seen: list[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
