from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from bigtree.node import node
from bigtree.tree import search
from bigtree.utils import exceptions

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    from unittest.mock import MagicMock

    pd = MagicMock()

__all__ = [
    "STATUS_BLOCKED",
    "STATUS_FROZEN",
    "STATUS_PUBLISHED",
    "DayResult",
    "Decision",
    "Dispute",
    "Observation",
    "ProductHierarchy",
    "Publication",
    "WarehouseRow",
]
STATUS_PUBLISHED = "published"
STATUS_FROZEN = "frozen"
STATUS_BLOCKED = "blocked"

SELECTION_CONSENSUS = "consensus"
SELECTION_DECISION = "decision"

T = TypeVar("T")


def _as_date(value: dt.date | str, parameter: str = "business_date") -> dt.date:
    """Normalize date input.

    Args:
        value: date or ISO date string
        parameter: parameter name used in the error message

    Returns:
        Date
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"Invalid date for `{parameter}`: {value!r}")


def _as_datetime(
    value: dt.datetime | str | None, parameter: str = "arrived_at"
) -> dt.datetime | None:
    """Normalize datetime input.

    Args:
        value: datetime, ISO datetime string, or None
        parameter: parameter name used in the error message

    Returns:
        Datetime or None
    """
    if value is None or isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime for `{parameter}`: {value!r}")


def _order(
    timestamp: dt.datetime | None, sequence: int
) -> tuple[dt.datetime | None, int]:
    """Sort key for an observation or decision.

    Args:
        timestamp: delivery or decision timestamp
        sequence: recording sequence

    Returns:
        Normalized sort key
    """
    if timestamp is not None and timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return timestamp, sequence


def _sorted_by_arrival(
    records: Iterable[T],
    timestamp: Callable[[T], dt.datetime | None],
) -> list[T]:
    """Sort records by arrival order.

    When every record has a timestamp, timestamps decide the order so callers
    can replay late-arriving files out of recording order. Otherwise recording
    sequence decides the order. Applying this per entity avoids a mixed set
    where some records have timestamps and others do not.

    Args:
        records: observations or decisions
        timestamp: accessor returning the delivery or decision timestamp

    Returns:
        Sorted records
    """
    record_list = list(records)
    all_timestamped = all(timestamp(record) is not None for record in record_list)
    if all_timestamped:
        return sorted(
            record_list,
            key=lambda record: (
                timestamp(record),
                _sequence_of(record),
            ),
        )
    return sorted(record_list, key=_sequence_of)


def _sequence_of(record: Any) -> int:
    """Get recording sequence.

    Args:
        record: observation or decision

    Returns:
        Recording sequence
    """
    sequence: int = record.sequence
    return sequence


def _decision_supported(evaluation: _Evaluation) -> bool | None:
    """Whether an active decision's selected value has any support.

    Args:
        evaluation: evaluation

    Returns:
        None without an active decision, otherwise whether the decision's
        selected parent has an observation
    """
    if evaluation.active_decision is None:
        return None
    return evaluation.active_decision.parent in evaluation.candidates


@dataclass(frozen=True)
class Observation:
    """A department observation for one SKU on one business date.

    Attributes:
        department: department that delivered the observation
        business_date: business date the observation applies to
        sku: product ID, stable across dates
        parent: parent location/store written by the department
        name: product name written by the department
        arrived_at: delivery timestamp, optional
        sequence: recording sequence, used as the final tie-breaker
        complete: whether the row is a complete observation
    """

    department: str
    business_date: dt.date
    sku: str
    parent: str | None
    name: str | None
    arrived_at: dt.datetime | None
    sequence: int
    complete: bool = True

    @property
    def is_effective(self) -> bool:
        """Whether the observation can be used as a complete value."""
        return bool(self.complete and self.parent and self.name)


@dataclass(frozen=True)
class Decision:
    """Manual decision selecting a parent for conflicting observations.

    Attributes:
        sku: product ID
        parent: selected parent
        effective_from: first business date covered, inclusive
        effective_to: last business date covered, inclusive
        decided_at: decision timestamp, optional
        sequence: recording sequence, used as the final tie-breaker
        note: free-form note
    """

    sku: str
    parent: str
    effective_from: dt.date
    effective_to: dt.date
    decided_at: dt.datetime | None
    sequence: int
    note: str = ""

    def covers(self, business_date: dt.date) -> bool:
        """Whether the decision covers a business date.

        Args:
            business_date: business date

        Returns:
            Indicator
        """
        return self.effective_from <= business_date <= self.effective_to


@dataclass(frozen=True)
class Publication:
    """Value that has already been published for a SKU on a business date.

    Attributes:
        business_date: business date
        sku: product ID
        parent: published parent
        name: published product name
        warehouse_key: stable warehouse key
        path: tree path corresponding to the warehouse key
        decision_sequence: sequence of the decision used, if any
        decision_parent: parent selected by the decision, if any
        last_decision_sequence: latest decision sequence known at publication time
    """

    business_date: dt.date
    sku: str
    parent: str
    name: str | None
    warehouse_key: str
    path: str
    decision_sequence: int | None
    decision_parent: str | None
    last_decision_sequence: int | None


@dataclass(frozen=True)
class WarehouseRow:
    """One row of a day export.

    Attributes:
        business_date: business date
        sku: product ID
        name: product name, if published
        parent: parent location/store, if selected
        path: tree path, None if the row cannot be loaded
        warehouse_key: warehouse key, None if the row cannot be loaded
        status: published, frozen, or blocked
        supported_by: departments supporting the selected parent
        candidates: distinct parent values observed for the day
        decision_parent: parent selected by the active decision, if any
        decision_supported: whether the active decision's parent has support;
            None when no decision is active
        frozen: whether a previously published value is being retained
        reason: explanation for a frozen or blocked row
    """

    business_date: dt.date
    sku: str
    name: str | None
    parent: str | None
    path: str | None
    warehouse_key: str | None
    status: str
    supported_by: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    decision_parent: str | None = None
    decision_supported: bool | None = None
    frozen: bool = False
    reason: str | None = None

    @property
    def loadable(self) -> bool:
        """Whether the row can be loaded into the warehouse."""
        return self.path is not None and self.status in (
            STATUS_PUBLISHED,
            STATUS_FROZEN,
        )

    def as_dict(self) -> dict[str, Any]:
        """Convert row to a flat dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "business_date": self.business_date,
            "sku": self.sku,
            "name": self.name,
            "parent": self.parent,
            "path": self.path,
            "warehouse_key": self.warehouse_key,
            "status": self.status,
            "supported_by": list(self.supported_by),
            "candidates": list(self.candidates),
            "decision_parent": self.decision_parent,
            "decision_supported": self.decision_supported,
            "frozen": self.frozen,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Dispute:
    """Evidence retained for a disputed SKU on a business date.

    The dispute keeps the full observation history for the day, including
    observations superseded by a correction, so the original conflict remains
    auditable after an export.

    Attributes:
        business_date: business date
        sku: product ID
        kind: dispute kind
        reason: human-readable explanation
        observations: all observations for the day, in arrival order
        effective_observations: latest complete observation per department
        candidates: distinct parent values among effective observations
        decision: active decision, if any
        selected_parent: parent selected by the export, if any
        published_parent: previously published parent, if any
    """

    business_date: dt.date
    sku: str
    kind: str
    reason: str
    observations: tuple[Observation, ...]
    effective_observations: tuple[Observation, ...]
    candidates: tuple[str, ...]
    decision: Decision | None
    selected_parent: str | None
    published_parent: str | None


@dataclass
class DayResult:
    """Export result for one business date.

    Attributes:
        business_date: business date
        tree: warehouse-loadable tree
        rows: one row per SKU seen on the date
        disputes: conflicts and freeze evidence
        warnings: human-readable freeze and conflict messages
        root_name: root node name
    """

    business_date: dt.date
    tree: node.Node
    rows: list[WarehouseRow] = field(default_factory=list)
    disputes: list[Dispute] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    root_name: str = "Catalog"

    @property
    def frozen(self) -> bool:
        """Whether any row is frozen."""
        return any(row.frozen for row in self.rows)

    @property
    def status(self) -> str:
        """Overall export status."""
        if self.frozen:
            return STATUS_FROZEN
        if any(row.status == STATUS_BLOCKED for row in self.rows):
            return STATUS_BLOCKED
        if any(row.status == STATUS_PUBLISHED for row in self.rows):
            return STATUS_PUBLISHED
        return "empty"

    @property
    def loadable_rows(self) -> list[WarehouseRow]:
        """Rows that can be loaded into the warehouse."""
        return [row for row in self.rows if row.loadable]

    @property
    def blocked_rows(self) -> list[WarehouseRow]:
        """Rows that cannot be loaded."""
        return [row for row in self.rows if row.status == STATUS_BLOCKED]

    def row_by_sku(self, sku: str) -> WarehouseRow:
        """Get a row by SKU.

        Args:
            sku: product ID

        Returns:
            Row
        """
        for row in self.rows:
            if row.sku == sku:
                return row
        raise KeyError(f"SKU {sku} not found on {self.business_date}")

    def row_by_warehouse_key(self, warehouse_key: str) -> WarehouseRow:
        """Get a row by warehouse key.

        Args:
            warehouse_key: warehouse key

        Returns:
            Row
        """
        for row in self.rows:
            if row.warehouse_key == warehouse_key:
                return row
        raise KeyError(f"Warehouse key {warehouse_key} not found")

    def row_by_path(self, path: str) -> WarehouseRow:
        """Get a row by tree path.

        Args:
            path: tree path

        Returns:
            Row
        """
        for row in self.rows:
            if row.path == path:
                return row
        raise KeyError(f"Path {path} not found")

    @exceptions.optional_dependencies_pandas
    def to_dataframe(self) -> pd.DataFrame:
        """Export rows to a pandas DataFrame.

        Returns:
            DataFrame with one row per SKU
        """
        return pd.DataFrame([row.as_dict() for row in self.rows])

    def show(self) -> None:
        """Print the warehouse-loadable tree."""
        self.tree.show(
            attr_list=["sku", "product_name", "status"],
            attr_omit_null=True,
        )

    def verify(self) -> list[str]:
        """Check that tree paths and warehouse keys agree.

        Returns:
            List of mismatch messages, empty if the views agree
        """
        problems: list[str] = []
        seen_paths: set[str] = set()
        seen_keys: set[str] = set()
        for row in self.rows:
            if not row.loadable:
                continue
            assert row.path is not None and row.warehouse_key is not None
            if row.path in seen_paths:
                problems.append(f"Duplicate path {row.path}")
            seen_paths.add(row.path)
            if row.warehouse_key in seen_keys:
                problems.append(f"Duplicate warehouse key {row.warehouse_key}")
            seen_keys.add(row.warehouse_key)

            expected_path = f"/{self.root_name}/{row.warehouse_key}"
            if row.path != expected_path:
                problems.append(
                    f"Path {row.path} does not match warehouse key {row.warehouse_key}"
                )
            tree_node = search.find_full_path(self.tree, row.path)
            if tree_node is None:
                problems.append(f"Path {row.path} not found in tree")
                continue
            if tree_node.node_name != row.sku:
                problems.append(
                    f"Path {row.path} resolves to {tree_node.node_name}, "
                    f"expected SKU {row.sku}"
                )
            if tree_node.get_attr("warehouse_key") != row.warehouse_key:
                problems.append(
                    f"Path {row.path} has warehouse key "
                    f"{tree_node.get_attr('warehouse_key')}, expected "
                    f"{row.warehouse_key}"
                )
        return problems


@dataclass(frozen=True)
class _FreezeState:
    """Internal freeze state for a published row."""

    publication: Publication
    kind: str
    reason: str
    baseline_decision_sequence: int | None


@dataclass(frozen=True)
class _Evaluation:
    """Resolved observations for one SKU on one date."""

    business_date: dt.date
    sku: str
    observations: tuple[Observation, ...]
    effective: dict[str, Observation]
    candidates: dict[str, list[str]]
    active_decision: Decision | None
    selected_parent: str | None
    selection_kind: str | None
    name: str | None
    conflict: bool
    unsupported_decision: bool
    unresolved: bool
    reason: str | None
    all_decisions: tuple[Decision, ...] = ()

    @property
    def candidate_parents(self) -> tuple[str, ...]:
        """Distinct candidate parents."""
        return tuple(sorted(self.candidates))

    @property
    def effective_observations(self) -> tuple[Observation, ...]:
        """Effective observations in arrival order."""
        return tuple(
            _sorted_by_arrival(
                self.effective.values(), lambda record: record.arrived_at
            )
        )

    @property
    def supported_by(self) -> tuple[str, ...]:
        """Departments supporting the selected parent."""
        if self.selected_parent is None:
            return ()
        return tuple(sorted(self.candidates.get(self.selected_parent, [])))

    @property
    def latest_decision_sequence(self) -> int | None:
        """Latest decision sequence known for the date and SKU."""
        sequences = [
            decision.sequence
            for decision in self.all_decisions
            if decision.covers(self.business_date)
        ]
        return max(sequences) if sequences else None


class ProductHierarchy:
    """Build a daily, warehouse-loadable product hierarchy from department data.

    Observations are recorded per department, business date, and SKU. For each
    business date, the latest complete observation per department is used. If
    departments agree, the agreed parent is selected. If they disagree, the
    latest effective manual decision is used when present.

    Once a SKU/date has been exported, a later export cannot silently rewrite a
    published parent that has lost all support. Such a row is frozen at the
    previously published value until a new decision selects a supported parent.
    Disputes, including superseded corrections, remain available on the result.

    Examples:
        >>> from bigtree import ProductHierarchy
        >>> hierarchy = ProductHierarchy()
        >>> _ = hierarchy.add_observation(
        ...     "North", "2026-03-01", "SKU7", "Outlet", "Old name"
        ... )
        >>> _ = hierarchy.add_observation(
        ...     "South", "2026-03-01", "SKU7", "Flagship", "Old name"
        ... )
        >>> _ = hierarchy.add_decision(
        ...     "SKU7", "Outlet", "2026-03-01", "2026-03-02"
        ... )
        >>> result = hierarchy.export_day("2026-03-01")
        >>> result.rows[0].parent
        'Outlet'
        >>> result.rows[0].status
        'published'
    """

    def __init__(
        self,
        name: str = "Catalog",
        departments: Sequence[str] | None = None,
    ):
        """Initialize product hierarchy.

        Args:
            name: root node name
            departments: optional expected departments
        """
        if not name or "/" in name:
            raise ValueError("`name` must be non-empty and cannot contain '/'")
        self.name = name
        self.departments = tuple(departments) if departments else ()
        self._observations: list[Observation] = []
        self._decisions: list[Decision] = []
        self._publications: dict[tuple[dt.date, str], Publication] = {}
        self._freezes: dict[tuple[dt.date, str], _FreezeState] = {}
        self._sequence = 0

    @property
    def observations(self) -> tuple[Observation, ...]:
        """All recorded observations in arrival order."""
        return tuple(self._observations)

    @property
    def decisions(self) -> tuple[Decision, ...]:
        """All recorded decisions in decision order."""
        return tuple(self._decisions)

    @property
    def publications(self) -> tuple[Publication, ...]:
        """All published values in business-date and SKU order."""
        return tuple(self._publications[key] for key in sorted(self._publications))

    def publication(self, business_date: dt.date | str, sku: str) -> Publication | None:
        """Get the published value for a SKU and business date.

        Args:
            business_date: business date
            sku: product ID

        Returns:
            Publication or None
        """
        return self._publications.get((_as_date(business_date), sku))

    def add_observation(
        self,
        department: str,
        business_date: dt.date | str,
        sku: str,
        parent: str | None,
        name: str | None,
        arrived_at: dt.datetime | str | None = None,
        complete: bool = True,
    ) -> Observation:
        """Record a department observation.

        A later observation for the same department, business date, and SKU does
        not delete the previous one; it supersedes it for value selection while
        remaining available as dispute evidence.

        Args:
            department: department name
            business_date: business date
            sku: product ID
            parent: parent location/store, may be None for an incomplete row
            name: product name, may be None for an incomplete row
            arrived_at: delivery timestamp, optional
            complete: whether the row is a complete observation

        Returns:
            Recorded observation
        """
        if not department:
            raise ValueError("`department` must be non-empty")
        if not sku:
            raise ValueError("`sku` must be non-empty")
        if parent is not None and not parent:
            raise ValueError("`parent` must be non-empty when provided")
        if parent is not None and any(not part for part in parent.split("/")):
            raise ValueError(f"Invalid parent path: {parent!r}")
        if name is not None and not name:
            raise ValueError("`name` must be non-empty when provided")

        self._sequence += 1
        observation = Observation(
            department=department,
            business_date=_as_date(business_date),
            sku=sku,
            parent=parent,
            name=name,
            arrived_at=_as_datetime(arrived_at),
            sequence=self._sequence,
            complete=complete,
        )
        self._observations.append(observation)
        return observation

    def add_observations(
        self, observations: Iterable[Mapping[str, Any]]
    ) -> list[Observation]:
        """Record multiple observations.

        Args:
            observations: mappings of arguments for `add_observation`

        Returns:
            Recorded observations
        """
        return [
            self.add_observation(**dict(observation)) for observation in observations
        ]

    def add_decision(
        self,
        sku: str,
        parent: str,
        effective_from: dt.date | str,
        effective_to: dt.date | str | None = None,
        decided_at: dt.datetime | str | None = None,
        note: str = "",
    ) -> Decision:
        """Record a manual decision selecting a parent.

        Args:
            sku: product ID
            parent: selected parent
            effective_from: first business date covered, inclusive
            effective_to: last business date covered, inclusive; defaults to
                `effective_from`
            decided_at: decision timestamp, optional
            note: free-form note

        Returns:
            Recorded decision
        """
        if not sku:
            raise ValueError("`sku` must be non-empty")
        if not parent:
            raise ValueError("`parent` must be non-empty")
        start = _as_date(effective_from, "effective_from")
        end = (
            _as_date(effective_to, "effective_to")
            if effective_to is not None
            else start
        )
        if end < start:
            raise ValueError("`effective_to` cannot be before `effective_from`")

        self._sequence += 1
        decision = Decision(
            sku=sku,
            parent=parent,
            effective_from=start,
            effective_to=end,
            decided_at=_as_datetime(decided_at, "decided_at"),
            sequence=self._sequence,
            note=note,
        )
        self._decisions.append(decision)
        return decision

    def export_day(self, business_date: dt.date | str) -> DayResult:
        """Export and publish one business date.

        Args:
            business_date: business date

        Returns:
            Day export result
        """
        return self._export_day(_as_date(business_date), publish=True)

    def preview_day(self, business_date: dt.date | str) -> DayResult:
        """Build one business date without updating publication state.

        Args:
            business_date: business date

        Returns:
            Day export result
        """
        return self._export_day(_as_date(business_date), publish=False)

    def export_days(self, business_dates: Iterable[dt.date | str]) -> list[DayResult]:
        """Export multiple business dates in order.

        Args:
            business_dates: business dates

        Returns:
            Day export results
        """
        return [self.export_day(business_date) for business_date in business_dates]

    def dispute_tree(self, business_date: dt.date | str) -> node.Node:
        """Build a tree retaining all conflict evidence for a date.

        The tree is for inspection only; the tree returned by `export_day` is the
        warehouse-loadable view.

        Args:
            business_date: business date

        Returns:
            Dispute tree
        """
        date = _as_date(business_date)
        root = node.Node(f"{self.name} Disputes")
        date_node = node.Node(date.isoformat(), parent=root)
        for sku in self._skus_for_date(date):
            evaluation = self._evaluate(date, sku)
            has_correction = len(evaluation.observations) > 1
            has_previous = (date, sku) in self._publications
            if not any(
                [
                    evaluation.conflict,
                    evaluation.unsupported_decision,
                    evaluation.unresolved,
                    has_correction,
                    has_previous and self._is_frozen(date, sku),
                ]
            ):
                continue
            sku_node = node.Node(sku, parent=date_node)
            candidates_node = node.Node("candidates", parent=sku_node)
            for parent in evaluation.candidate_parents:
                node.Node(
                    parent,
                    parent=candidates_node,
                    departments=evaluation.candidates[parent],
                )
            for observation in evaluation.observations:
                label = (
                    f"{observation.sequence}:{observation.department}"
                    f":{observation.parent or '-'}:{observation.name or '-'}"
                )
                node.Node(
                    label,
                    parent=sku_node,
                    department=observation.department,
                    parent_value=observation.parent,
                    product_name=observation.name,
                    complete=observation.complete,
                    effective=observation.is_effective,
                )
            if evaluation.active_decision:
                node.Node(
                    f"decision:{evaluation.active_decision.sequence}"
                    f":{evaluation.active_decision.parent}",
                    parent=sku_node,
                    effective_from=evaluation.active_decision.effective_from,
                    effective_to=evaluation.active_decision.effective_to,
                )
        return root

    def _skus_for_date(self, business_date: dt.date) -> list[str]:
        """Get SKUs observed or published on a date.

        Args:
            business_date: business date

        Returns:
            Sorted SKU list
        """
        skus = {
            observation.sku
            for observation in self._observations
            if observation.business_date == business_date
        }
        skus.update(
            sku
            for (date, sku), _publication in self._publications.items()
            if date == business_date
        )
        return sorted(skus)

    def _decisions_for(self, sku: str) -> tuple[Decision, ...]:
        """Get decisions for a SKU.

        Args:
            sku: product ID

        Returns:
            Decisions
        """
        return tuple(decision for decision in self._decisions if decision.sku == sku)

    def _active_decision(self, business_date: dt.date, sku: str) -> Decision | None:
        """Get the latest decision covering the date and SKU.

        Args:
            business_date: business date
            sku: product ID

        Returns:
            Decision or None
        """
        decisions = [
            decision
            for decision in self._decisions_for(sku)
            if decision.covers(business_date)
        ]
        if not decisions:
            return None
        return max(
            decisions,
            key=lambda decision: _order(decision.decided_at, decision.sequence),
        )

    def _evaluate(self, business_date: dt.date, sku: str) -> _Evaluation:
        """Evaluate latest complete observation and active decision.

        Args:
            business_date: business date
            sku: product ID

        Returns:
            Evaluation
        """
        observations = tuple(
            _sorted_by_arrival(
                (
                    observation
                    for observation in self._observations
                    if observation.business_date == business_date
                    and observation.sku == sku
                ),
                lambda record: record.arrived_at,
            )
        )

        effective: dict[str, Observation] = {}
        for observation in observations:
            if not observation.is_effective:
                continue
            effective[observation.department] = observation

        candidates: dict[str, list[str]] = {}
        for department, observation in effective.items():
            assert observation.parent is not None
            candidates.setdefault(observation.parent, []).append(department)

        active_decision = self._active_decision(business_date, sku)
        selected_parent: str | None = None
        selection_kind: str | None = None
        conflict = len(candidates) > 1
        unsupported_decision = False
        unresolved = False
        reason: str | None = None

        if not candidates:
            reason = f"No complete observation for {sku} on {business_date}"
            unresolved = True
        elif not conflict:
            selected_parent = next(iter(candidates))
            selection_kind = SELECTION_CONSENSUS
        elif active_decision is not None:
            selected_parent = active_decision.parent
            selection_kind = SELECTION_DECISION
            if selected_parent not in candidates:
                unsupported_decision = True
                reason = (
                    f"Decision-selected parent {selected_parent!r} for {sku} on "
                    f"{business_date} has no supporting observation"
                )
        else:
            unresolved = True
            reason = (
                f"Conflicting parent observations for {sku} on "
                f"{business_date} have no effective decision"
            )

        selected_observation: Observation | None = None
        if selected_parent is not None:
            matching = [
                observation
                for observation in effective.values()
                if observation.parent == selected_parent
            ]
            if matching:
                selected_observation = _sorted_by_arrival(
                    matching, lambda record: record.arrived_at
                )[-1]

        evaluation = _Evaluation(
            business_date=business_date,
            sku=sku,
            observations=observations,
            effective=effective,
            candidates=candidates,
            active_decision=active_decision,
            selected_parent=selected_parent,
            selection_kind=selection_kind,
            name=selected_observation.name
            if selected_observation is not None
            else None,
            conflict=conflict,
            unsupported_decision=unsupported_decision,
            unresolved=unresolved,
            reason=reason,
            all_decisions=self._decisions_for(sku),
        )
        return evaluation

    def _is_frozen(self, business_date: dt.date, sku: str) -> bool:
        """Whether a SKU/date is frozen.

        Args:
            business_date: business date
            sku: product ID

        Returns:
            Indicator
        """
        return (business_date, sku) in self._freezes

    def _warehouse_key(self, business_date: dt.date, parent: str, sku: str) -> str:
        """Build a stable warehouse key.

        Args:
            business_date: business date
            parent: parent path
            sku: product ID

        Returns:
            Warehouse key
        """
        return "/".join([business_date.isoformat(), parent, sku])

    def _path(self, warehouse_key: str) -> str:
        """Build a tree path from a warehouse key.

        Args:
            warehouse_key: warehouse key

        Returns:
            Tree path
        """
        return f"/{self.name}/{warehouse_key}"

    def _publication(
        self,
        business_date: dt.date,
        sku: str,
        parent: str,
        name: str | None,
        selection_kind: str | None,
        decision: Decision | None,
    ) -> Publication:
        """Create a publication value.

        Args:
            business_date: business date
            sku: product ID
            parent: selected parent
            name: selected name
            selection_kind: consensus or decision
            decision: active decision

        Returns:
            Publication
        """
        warehouse_key = self._warehouse_key(business_date, parent, sku)
        return Publication(
            business_date=business_date,
            sku=sku,
            parent=parent,
            name=name,
            warehouse_key=warehouse_key,
            path=self._path(warehouse_key),
            decision_sequence=decision.sequence
            if selection_kind == SELECTION_DECISION and decision is not None
            else None,
            decision_parent=decision.parent
            if selection_kind == SELECTION_DECISION and decision is not None
            else None,
            last_decision_sequence=self._latest_decision_sequence(business_date, sku),
        )

    def _latest_decision_sequence(self, business_date: dt.date, sku: str) -> int | None:
        """Get latest decision sequence for a date and SKU.

        Args:
            business_date: business date
            sku: product ID

        Returns:
            Sequence or None
        """
        sequences = [
            decision.sequence
            for decision in self._decisions_for(sku)
            if decision.covers(business_date)
        ]
        return max(sequences) if sequences else None

    def _row(
        self,
        publication: Publication,
        status: str,
        evaluation: _Evaluation,
        frozen: bool = False,
        reason: str | None = None,
    ) -> WarehouseRow:
        """Create a warehouse row from a publication.

        Args:
            publication: published value
            status: row status
            evaluation: evaluation
            frozen: whether the value is frozen
            reason: explanation

        Returns:
            Warehouse row
        """
        return WarehouseRow(
            business_date=publication.business_date,
            sku=publication.sku,
            name=publication.name,
            parent=publication.parent,
            path=publication.path,
            warehouse_key=publication.warehouse_key,
            status=status,
            supported_by=tuple(
                sorted(evaluation.candidates.get(publication.parent, []))
            ),
            candidates=evaluation.candidate_parents,
            decision_parent=evaluation.active_decision.parent
            if evaluation.active_decision is not None
            else None,
            decision_supported=_decision_supported(evaluation),
            frozen=frozen,
            reason=reason,
        )

    def _dispute(
        self,
        evaluation: _Evaluation,
        kind: str,
        reason: str,
        published_parent: str | None = None,
    ) -> Dispute:
        """Create dispute evidence from an evaluation.

        Args:
            evaluation: evaluation
            kind: dispute kind
            reason: explanation
            published_parent: previously published parent

        Returns:
            Dispute
        """
        return Dispute(
            business_date=evaluation.business_date,
            sku=evaluation.sku,
            kind=kind,
            reason=reason,
            observations=evaluation.observations,
            effective_observations=evaluation.effective_observations,
            candidates=evaluation.candidate_parents,
            decision=evaluation.active_decision,
            selected_parent=evaluation.selected_parent,
            published_parent=published_parent,
        )

    def _resolve(
        self,
        evaluation: _Evaluation,
        previous: Publication | None,
        freeze: _FreezeState | None,
    ) -> tuple[
        WarehouseRow,
        list[Dispute],
        list[str],
        Publication | None,
        _FreezeState | None,
    ]:
        """Resolve one SKU/date against publication state.

        Args:
            evaluation: evaluation
            previous: previously published value
            freeze: existing freeze state

        Returns:
            Row, disputes, warnings, publication to store, freeze to store
        """
        business_date = evaluation.business_date
        sku = evaluation.sku
        disputes: list[Dispute] = []
        warnings: list[str] = []

        if evaluation.conflict:
            disputes.append(
                self._dispute(
                    evaluation,
                    "parent_conflict",
                    f"Departments disagree on the parent for {sku} on "
                    f"{business_date}: {', '.join(evaluation.candidate_parents)}",
                    published_parent=previous.parent if previous else None,
                )
            )
        if evaluation.unsupported_decision and evaluation.reason:
            disputes.append(
                self._dispute(
                    evaluation,
                    "decision_unsupported",
                    evaluation.reason,
                    published_parent=previous.parent if previous else None,
                )
            )
        if evaluation.unresolved and evaluation.reason and not evaluation.conflict:
            disputes.append(
                self._dispute(
                    evaluation,
                    "unresolved",
                    evaluation.reason,
                    published_parent=previous.parent if previous else None,
                )
            )
        elif evaluation.unresolved and evaluation.reason:
            disputes.append(
                self._dispute(
                    evaluation,
                    "unresolved_conflict",
                    evaluation.reason,
                    published_parent=previous.parent if previous else None,
                )
            )

        # A later complete observation for the same department supersedes the
        # earlier value but does not erase it: keep the correction on record even
        # when the conflict it resolved no longer exists.
        if self._has_correction(evaluation):
            disputes.append(
                self._dispute(
                    evaluation,
                    "corrected_observation",
                    f"{sku} on {business_date} has multiple complete "
                    "observations for the same department; the latest one is "
                    "effective",
                    published_parent=previous.parent if previous else None,
                )
            )

        # An existing freeze is retained until a new decision selects a
        # supported parent, or until the frozen parent regains support.
        if freeze is not None:
            new_decision = self._new_supporting_decision(evaluation, freeze)
            if new_decision is not None:
                publication = self._publication(
                    business_date,
                    sku,
                    new_decision.parent,
                    evaluation.name
                    if new_decision.parent == evaluation.selected_parent
                    else self._name_under_parent(evaluation, new_decision.parent),
                    SELECTION_DECISION,
                    new_decision,
                )
                warnings.append(
                    f"{sku} {business_date}: freeze released by decision "
                    f"{new_decision.sequence} selecting {new_decision.parent!r}"
                )
                return (
                    self._row(publication, STATUS_PUBLISHED, evaluation),
                    disputes,
                    warnings,
                    publication,
                    None,
                )

            if freeze.publication.parent in evaluation.candidates:
                supporting_decision = (
                    evaluation.active_decision
                    if evaluation.active_decision is not None
                    and evaluation.active_decision.parent == freeze.publication.parent
                    else None
                )
                publication = self._publication(
                    business_date,
                    sku,
                    freeze.publication.parent,
                    self._name_under_parent(evaluation, freeze.publication.parent),
                    SELECTION_DECISION
                    if supporting_decision is not None
                    else SELECTION_CONSENSUS,
                    supporting_decision,
                )
                warnings.append(
                    f"{sku} {business_date}: freeze released because the "
                    f"published parent {freeze.publication.parent!r} regained "
                    "support"
                )
                return (
                    self._row(publication, STATUS_PUBLISHED, evaluation),
                    disputes,
                    warnings,
                    publication,
                    None,
                )

            reason = freeze.reason
            if evaluation.reason and evaluation.reason not in reason:
                reason = f"{reason}; {evaluation.reason}"
            warnings.append(
                f"{sku} {business_date}: {reason}; retaining frozen value "
                f"{freeze.publication.parent!r}"
            )
            disputes.append(
                self._dispute(
                    evaluation,
                    "frozen",
                    reason,
                    published_parent=freeze.publication.parent,
                )
            )
            return (
                self._row(
                    freeze.publication,
                    STATUS_FROZEN,
                    evaluation,
                    frozen=True,
                    reason=reason,
                ),
                disputes,
                warnings,
                None,
                freeze,
            )

        # A decision-selected value without supporting observations is not a
        # usable selection, even when departments otherwise agree.
        usable_selection = (
            None if evaluation.unsupported_decision else evaluation.selected_parent
        )

        # No usable selection. If the previously published value is still
        # supported, retain it rather than freezing: freezing is reserved for a
        # published value that has lost all support.
        if usable_selection is None and previous is not None:
            if previous.parent in evaluation.candidates:
                warnings.append(
                    f"{sku} {business_date}: no active decision selects a "
                    f"supported value; retaining published parent "
                    f"{previous.parent!r}, which remains supported"
                    + (
                        "; departments disagree and a decision is required"
                        if evaluation.conflict
                        else ""
                    )
                )
                return (
                    self._row(previous, STATUS_PUBLISHED, evaluation),
                    disputes,
                    warnings,
                    None,
                    None,
                )

        # Otherwise freeze the previous value if one exists, or block the row
        # and require a decision.
        if usable_selection is None:
            reason = evaluation.reason or (
                f"No selected parent for {sku} on {business_date}"
            )
            if previous is not None:
                decision_detail = (
                    f"manual decision {previous.decision_sequence} selected "
                    f"{previous.decision_parent!r}, but that value"
                    if previous.decision_sequence is not None
                    else f"previously published parent {previous.parent!r}"
                )
                freeze_reason = (
                    f"{reason}; {decision_detail} no longer has any supporting "
                    f"observation for {sku} on {business_date}; the published "
                    "value is frozen until a new decision selects a supported "
                    "value"
                )
                freeze_state = _FreezeState(
                    publication=previous,
                    kind="published_value_unsupported",
                    reason=freeze_reason,
                    baseline_decision_sequence=evaluation.latest_decision_sequence,
                )
                disputes.append(
                    self._dispute(
                        evaluation,
                        "published_value_unsupported",
                        freeze_reason,
                        published_parent=previous.parent,
                    )
                )
                warnings.append(f"{sku} {business_date}: {freeze_reason}")
                return (
                    self._row(
                        previous,
                        STATUS_FROZEN,
                        evaluation,
                        frozen=True,
                        reason=freeze_reason,
                    ),
                    disputes,
                    warnings,
                    None,
                    freeze_state,
                )

            row = WarehouseRow(
                business_date=business_date,
                sku=sku,
                name=evaluation.name,
                parent=None,
                path=None,
                warehouse_key=None,
                status=STATUS_BLOCKED,
                supported_by=evaluation.supported_by,
                candidates=evaluation.candidate_parents,
                decision_parent=evaluation.active_decision.parent
                if evaluation.active_decision is not None
                else None,
                decision_supported=_decision_supported(evaluation),
                frozen=False,
                reason=reason,
            )
            warnings.append(f"{sku} {business_date}: {reason}")
            return row, disputes, warnings, None, None

        selected_parent = usable_selection
        publication = self._publication(
            business_date,
            sku,
            selected_parent,
            evaluation.name,
            evaluation.selection_kind,
            evaluation.active_decision
            if evaluation.selection_kind == SELECTION_DECISION
            else None,
        )

        if previous is not None and previous.parent != selected_parent:
            previous_supported = previous.parent in evaluation.candidates
            new_decision_supports_selected = (
                evaluation.active_decision is not None
                and evaluation.active_decision.parent == selected_parent
                and evaluation.active_decision.sequence
                > (previous.last_decision_sequence or -1)
                and selected_parent in evaluation.candidates
            )
            if not previous_supported and not new_decision_supports_selected:
                decision_detail = (
                    f"manual decision {previous.decision_sequence} selected "
                    f"{previous.decision_parent!r}, but that value"
                    if previous.decision_sequence is not None
                    else f"previously published parent {previous.parent!r}"
                )
                reason = (
                    f"{decision_detail} no longer has any supporting observation "
                    f"for {sku} on {business_date}; the published value is frozen "
                    "until a new decision selects the current value"
                )
                freeze_state = _FreezeState(
                    publication=previous,
                    kind="published_value_unsupported",
                    reason=reason,
                    baseline_decision_sequence=evaluation.latest_decision_sequence,
                )
                disputes.append(
                    self._dispute(
                        evaluation,
                        "published_value_unsupported",
                        reason,
                        published_parent=previous.parent,
                    )
                )
                warnings.append(f"{sku} {business_date}: {reason}")
                return (
                    self._row(
                        previous,
                        STATUS_FROZEN,
                        evaluation,
                        frozen=True,
                        reason=reason,
                    ),
                    disputes,
                    warnings,
                    None,
                    freeze_state,
                )

        return (
            self._row(publication, STATUS_PUBLISHED, evaluation),
            disputes,
            warnings,
            publication,
            None,
        )

    @staticmethod
    def _has_correction(evaluation: _Evaluation) -> bool:
        """Whether a department delivered more than one complete observation.

        Args:
            evaluation: evaluation

        Returns:
            Indicator
        """
        counts: dict[str, int] = {}
        for observation in evaluation.observations:
            if not observation.is_effective:
                continue
            counts[observation.department] = counts.get(observation.department, 0) + 1
        return any(count > 1 for count in counts.values())

    @staticmethod
    def _name_under_parent(evaluation: _Evaluation, parent: str) -> str | None:
        """Get latest name for a parent.

        Args:
            evaluation: evaluation
            parent: parent value

        Returns:
            Product name or None
        """
        matching = [
            observation
            for observation in evaluation.effective.values()
            if observation.parent == parent
        ]
        if not matching:
            return None
        return _sorted_by_arrival(matching, lambda record: record.arrived_at)[-1].name

    def _new_supporting_decision(
        self, evaluation: _Evaluation, freeze: _FreezeState
    ) -> Decision | None:
        """Get a decision that can release a freeze.

        Only the latest decision covering the date is considered. A freeze is
        released only if that latest decision selects a parent that currently
        has supporting observations; older decisions cannot revive the row.

        Args:
            evaluation: evaluation
            freeze: existing freeze

        Returns:
            Decision or None
        """
        baseline = freeze.baseline_decision_sequence
        latest = evaluation.active_decision
        if latest is None:
            return None
        if latest.sequence <= (baseline if baseline is not None else -1):
            return None
        if latest.parent not in evaluation.candidates:
            return None
        return latest

    def _export_day(self, business_date: dt.date, publish: bool) -> DayResult:
        """Internal day export.

        Args:
            business_date: business date
            publish: whether to update publication state

        Returns:
            Day result
        """
        root = node.Node(self.name)
        date_node = node.Node(business_date.isoformat(), parent=root)
        rows: list[WarehouseRow] = []
        disputes: list[Dispute] = []
        warnings: list[str] = []

        for sku in self._skus_for_date(business_date):
            evaluation = self._evaluate(business_date, sku)
            key = (business_date, sku)
            previous = self._publications.get(key)
            freeze = self._freezes.get(key)

            row, row_disputes, row_warnings, new_publication, new_freeze = (
                self._resolve(evaluation, previous, freeze)
            )
            rows.append(row)
            disputes.extend(row_disputes)
            warnings.extend(row_warnings)

            if row.loadable and row.parent is not None:
                self._add_row_to_tree(date_node, row)

            if publish:
                if new_publication is not None:
                    self._publications[key] = new_publication
                    self._freezes.pop(key, None)
                elif new_freeze is not None:
                    self._publications[key] = new_freeze.publication
                    self._freezes[key] = new_freeze

        return DayResult(
            business_date=business_date,
            tree=root,
            rows=rows,
            disputes=disputes,
            warnings=warnings,
            root_name=self.name,
        )

    @staticmethod
    def _add_row_to_tree(date_node: node.Node, row: WarehouseRow) -> None:
        """Add a warehouse row to the date tree.

        Args:
            date_node: date node
            row: warehouse row
        """
        assert row.parent is not None
        parent_node = date_node
        for part in row.parent.split("/"):
            child = next(
                (child for child in parent_node.children if child.node_name == part),
                None,
            )
            if child is None:
                child = node.Node(part, parent=parent_node)
            parent_node = child

        node.Node(
            row.sku,
            parent=parent_node,
            sku=row.sku,
            product_name=row.name,
            business_date=row.business_date.isoformat(),
            parent_path=row.parent,
            warehouse_key=row.warehouse_key,
            status=row.status,
            frozen=row.frozen,
            supported_by=list(row.supported_by),
            candidates=list(row.candidates),
            decision_parent=row.decision_parent,
            decision_supported=row.decision_supported,
            reason=row.reason,
        )
