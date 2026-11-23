"""Versioned pipeline migrations on top of Dramatiq's pipeline primitive.

This module is intentionally opt-in.  Pipelines that are not registered with a
:class:`MigrationPipeline` keep using :class:`dramatiq.middleware.Pipelines`
and its ``pipe_target`` semantics unchanged.

The pieces fit together as follows:

* :class:`Stage` describes a logical step (``parse``, ``validate``, ...),
  the versions of that step that exist, and which queue each version runs on.
* :class:`MigrationRegistry` records stages and the workers that are actually
  online.  Workers register the queue they consume and the versions they
  support.
* :class:`MigrationPipeline` starts runs, routes stages to compatible workers,
  and stores one :class:`Receipt` per ``(run_id, stage)`` pair.
* :class:`InMemoryReceiptStore` implements conditional commits.  A receipt can
  only be replaced by a receipt from a newer stage version (or explicitly with
  ``force=True``), so a result arriving late from an old worker cannot
  overwrite a result that a new worker already committed.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Union

from .middleware import Middleware

__all__ = [
    # Payload helpers
    "MISSING",
    "is_missing",
    "has_field",
    "get_field",
    "resolve_default",
    "resolve_currency",
    "with_defaults",
    "missing_fields",
    "null_fields",
    "get_pipe_target",
    "pipe_target_payload",
    "yuan_to_cents",
    "cents_to_yuan",
    # Versions
    "canonical_version",
    "Stage",
    "StageStatus",
    "RunStatus",
    # Registration
    "WorkerRegistration",
    "RouteMatch",
    "MigrationRegistry",
    # Receipts
    "Receipt",
    "CommitResult",
    "ReceiptStore",
    "InMemoryReceiptStore",
    # Running
    "RunState",
    "StageState",
    "Dispatch",
    "Submission",
    "PendingMigration",
    "MigrationPipeline",
    "VersionedPipeline",
    "MigrationCoordinator",
    "MigrationMiddleware",
    "MigrationError",
]


class _MissingType:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "MISSING"

    def __reduce__(self):
        return (_get_missing, ())


def _get_missing() -> "_MissingType":
    return MISSING


#: Sentinel used to distinguish an absent payload field from a field whose
#: value is ``None``.  This distinction matters when a new validator consumes
#: results produced by an older parse implementation.
MISSING = _MissingType()

VersionLike = Union[str, int]


def is_missing(value: Any) -> bool:
    """Return True only for the :data:`MISSING` sentinel."""

    return value is MISSING


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def has_field(payload: Any, name: str) -> bool:
    """Return whether *name* is present in *payload*.

    Unlike ``bool(payload.get(name))`` this returns True for keys whose value
    is explicitly ``None`` (or any other falsy value).
    """

    if _is_mapping(payload):
        return name in payload
    return getattr(payload, name, MISSING) is not MISSING


def get_field(payload: Any, name: str, default: Any = MISSING) -> Any:
    """Read a field without conflating ``None`` with an absent field."""

    if _is_mapping(payload):
        return payload[name] if name in payload else default
    return getattr(payload, name, default)


def resolve_default(
    payload: Any,
    name: str,
    *,
    run: Optional["RunState"] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    default: Any = MISSING,
) -> Any:
    """Resolve a payload field, falling back to a run-scoped default.

    An explicit ``None`` in the payload is a value and is returned as-is.  The
    default is only used when the field is absent.
    """

    value = get_field(payload, name, MISSING)
    if not is_missing(value):
        return value

    source: Optional[Mapping[str, Any]] = defaults if defaults is not None else getattr(run, "defaults", None)
    if source is not None and name in source:
        return source[name]
    return default


def resolve_currency(
    payload: Any,
    *,
    run: Optional["RunState"] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    default: Any = MISSING,
) -> Any:
    """Return ``payload['currency']`` or the run's configured default."""

    return resolve_default(payload, "currency", run=run, defaults=defaults, default=default)


def missing_fields(payload: Any, names: Iterable[str]) -> list[str]:
    """Return the names that are absent from *payload*.

    Explicit ``None`` values are present and are not returned here.
    """

    return [name for name in names if not has_field(payload, name)]


def null_fields(payload: Any, names: Iterable[str]) -> list[str]:
    """Return the names that are present in *payload* with a ``None`` value."""

    return [name for name in names if has_field(payload, name) and get_field(payload, name) is None]


def with_defaults(
    payload: Any,
    defaults: Mapping[str, Any],
    *,
    keys: Optional[Iterable[str]] = None,
) -> Any:
    """Return a copy of *payload* with absent fields filled from *defaults*.

    Existing fields are never changed, including fields whose value is None.
    """

    if not _is_mapping(payload):
        raise TypeError("with_defaults() expects a mapping payload")

    result = dict(payload)
    for name in keys if keys is not None else defaults:
        if name not in result and name in defaults:
            result[name] = defaults[name]
    return result


def _options_of(message_or_options: Any) -> Mapping[str, Any]:
    if _is_mapping(message_or_options):
        return message_or_options
    return getattr(message_or_options, "options", {})


def get_pipe_target(message_or_options: Any) -> Optional[dict[str, Any]]:
    """Return the successor message dict hidden in ``pipe_target``.

    Older pipelines store the entire next message in the current message's
    options.  Reading it lets a migration adopt an in-flight chain without
    recomputing the predecessor.
    """

    target = _options_of(message_or_options).get("pipe_target")
    if target is None:
        return None
    if _is_mapping(target):
        return dict(target)
    return target.asdict()


def pipe_target_payload(message_or_options: Any) -> Any:
    """Return the arguments of the successor message, or :data:`MISSING`.

    The successor's ``args`` are what a legacy pipeline would have extended
    with the predecessor's result, so they are the payload the next stage is
    waiting for.
    """

    target = get_pipe_target(message_or_options)
    if target is None:
        return MISSING
    args = target.get("args", ())
    return tuple(args)


def yuan_to_cents(amount: Any, *, rounding: str = ROUND_HALF_UP) -> int:
    """Convert a yuan amount to integer cents without binary-float drift.

    ``yuan_to_cents("10.01")`` and ``yuan_to_cents(10.01)`` both return
    ``1001``.  Sub-cent precision is rounded only at the final step.
    """

    if isinstance(amount, Decimal):
        decimal_amount = amount
    elif isinstance(amount, bool):
        raise TypeError("boolean amounts are not valid currency values")
    else:
        decimal_amount = Decimal(str(amount))

    return int((decimal_amount * 100).quantize(Decimal("1"), rounding=rounding))


def cents_to_yuan(cents: int) -> Decimal:
    """Convert integer cents to a :class:`~decimal.Decimal` yuan amount."""

    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError("cents must be an integer")
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def canonical_version(version: VersionLike) -> str:
    """Normalize a version so ``1``, ``"1"`` and ``"v1"`` are equivalent."""

    if isinstance(version, bool):
        raise TypeError("boolean values are not valid versions")
    if isinstance(version, int):
        return f"v{version}"

    text = str(version).strip()
    if not text:
        raise ValueError("versions cannot be empty")

    lowered = text.lower()
    if lowered.startswith("v") and lowered[1:].isdigit():
        return f"v{int(lowered[1:])}"
    if text.isdigit():
        return f"v{int(text)}"
    return text


def _compare_versions(left: str, right: str) -> Optional[int]:
    """Compare canonical versions when they share a numeric form.

    Returns ``None`` when the versions are not comparable (for example
    ``"v1"`` versus ``"legacy"``).
    """

    left_canonical = canonical_version(left)
    right_canonical = canonical_version(right)
    if left_canonical == right_canonical:
        return 0

    left_digits = left_canonical[1:] if left_canonical.startswith("v") else ""
    right_digits = right_canonical[1:] if right_canonical.startswith("v") else ""
    if left_digits.isdigit() and right_digits.isdigit():
        left_number = int(left_digits)
        right_number = int(right_digits)
        return (left_number > right_number) - (left_number < right_number)
    return None


def _version_set(versions: Optional[Iterable[VersionLike]]) -> tuple[str, ...]:
    if versions is None:
        return ()
    if isinstance(versions, (str, int)):
        versions = (versions,)
    ordered: "OrderedDict[str, None]" = OrderedDict()
    for version in versions:
        ordered[canonical_version(version)] = None
    return tuple(ordered)


class StageStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PENDING_MIGRATION = "pending_migration"
    FAILED = "failed"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PENDING_MIGRATION = "pending_migration"
    FAILED = "failed"


class MigrationError(Exception):
    """Base error for the migration subsystem."""


@dataclasses.dataclass(frozen=True)
class Stage:
    """A logical stage of a versioned pipeline.

    Parameters:
      name(str): The stage name, e.g. ``"parse"``.
      versions(Iterable[str]): Known versions in increasing order.  The last
        version is considered the newest one.  If omitted, versions supplied
        by worker registrations are used.
      successor(str): The next stage name.  If omitted, the next stage in the
        pipeline definition is used.
      queues(Mapping[str, str]): An optional mapping of version to queue name.
        This lets ``v1`` run on ``parse-old`` while ``v2`` runs on
        ``parse-new``.
      queue_name(str): A fallback queue name shared by every version.
      accepts(Mapping[str, Iterable[str]]): Optional payload-compatibility
        overrides, keyed by implementation version.  By default a newer
        implementation accepts payloads produced by older versions, and an
        implementation accepts payloads from the same version.
      actors(Mapping[str, str]): An optional mapping of version to actor name.
        It is used by :class:`MigrationMiddleware` when it enqueues the next
        stage.
    """

    name: str
    versions: tuple[str, ...] = ()
    successor: Optional[str] = None
    queues: Mapping[str, str] = dataclasses.field(default_factory=dict)
    queue_name: Optional[str] = None
    accepts: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    actors: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "versions", _version_set(self.versions))
        object.__setattr__(
            self,
            "queues",
            {canonical_version(version): queue for version, queue in self.queues.items()},
        )
        object.__setattr__(
            self,
            "accepts",
            {
                canonical_version(version): _version_set(accepted)
                for version, accepted in self.accepts.items()
            },
        )
        object.__setattr__(
            self,
            "actors",
            {canonical_version(version): actor_name for version, actor_name in self.actors.items()},
        )
        if not self.name:
            raise ValueError("stages must have a name")
        if self.successor == self.name:
            raise ValueError("a stage cannot be its own successor")

    def actor_for(self, version: VersionLike) -> Optional[str]:
        return self.actors.get(canonical_version(version))

    def queue_for(self, version: VersionLike) -> Optional[str]:
        canonical = canonical_version(version)
        if canonical in self.queues:
            return self.queues[canonical]
        return self.queue_name

    def version_rank(self, version: VersionLike) -> Optional[int]:
        canonical = canonical_version(version)
        try:
            return self.versions.index(canonical)
        except ValueError:
            return None

    def newest_version(self) -> Optional[str]:
        return self.versions[-1] if self.versions else None

    def accepts_payload(self, implementation_version: VersionLike, payload_version: Optional[VersionLike]) -> bool:
        """Return whether an implementation can consume a payload version.

        Older implementations do not accept newer payloads.  Newer
        implementations are backward compatible unless ``accepts`` narrows
        that behaviour explicitly.
        """

        implementation = canonical_version(implementation_version)
        if payload_version is None:
            return True

        payload = canonical_version(payload_version)

        if implementation in self.accepts:
            return payload in self.accepts[implementation]

        implementation_rank = self.version_rank(implementation)
        payload_rank = self.version_rank(payload)
        if implementation_rank is None or payload_rank is None:
            return implementation == payload
        return implementation_rank >= payload_rank


@dataclasses.dataclass(frozen=True)
class WorkerRegistration:
    """A worker that is currently consuming a queue.

    Parameters:
      queue_name(str): The queue the worker consumes.
      versions(Iterable[str]): The stage versions this worker supports.
      stage(str): The logical stage this worker belongs to, if known.
      worker_id(str): Stable identity used in receipts and diagnostics.
      metadata(Mapping): Arbitrary operator metadata.
    """

    queue_name: str
    versions: frozenset[str]
    stage: Optional[str] = None
    worker_id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict, compare=False)

    def __post_init__(self):
        if not self.queue_name:
            raise ValueError("workers must declare a queue name")
        object.__setattr__(self, "versions", frozenset(_version_set(self.versions)))

    def supports(self, version: VersionLike) -> bool:
        return canonical_version(version) in self.versions

    @property
    def supported_versions(self) -> frozenset[str]:
        return self.versions


@dataclasses.dataclass(frozen=True)
class RouteMatch:
    """The worker and queue selected for one stage invocation."""

    stage: str
    version: str
    queue_name: str
    worker: WorkerRegistration

    @property
    def worker_id(self) -> str:
        return self.worker.worker_id

    @property
    def supported_versions(self) -> frozenset[str]:
        return self.worker.versions


class MigrationRegistry:
    """Tracks stage definitions and live worker registrations."""

    def __init__(self) -> None:
        self._stages: "OrderedDict[str, Stage]" = OrderedDict()
        self._workers: "OrderedDict[str, WorkerRegistration]" = OrderedDict()
        self._lock = threading.RLock()

    def register_stage(
        self,
        stage: Optional[Stage] = None,
        *,
        name: Optional[str] = None,
        versions: Optional[Iterable[VersionLike]] = None,
        successor: Optional[str] = None,
        queues: Optional[Mapping[VersionLike, str]] = None,
        queue_name: Optional[str] = None,
        accepts: Optional[Mapping[VersionLike, Iterable[VersionLike]]] = None,
        actors: Optional[Mapping[VersionLike, str]] = None,
    ) -> Stage:
        """Register (or replace) a stage definition.

        A :class:`Stage` may be passed directly, or constructed from keyword
        arguments::

            registry.register_stage(
                "parse",
                versions=("v1", "v2"),
                queues={"v1": "parse-old", "v2": "parse-new"},
                successor="validate",
            )
        """

        if isinstance(stage, Stage):
            if name is not None:
                raise TypeError("pass either a Stage or stage= keyword arguments, not both")
        else:
            if stage is not None:
                name = str(stage)
            if name is None:
                raise TypeError("register_stage() requires a stage name or Stage")

            # A mapping passed as ``versions`` is shorthand for version -> queue.
            if isinstance(versions, Mapping):
                queue_map = {canonical_version(key): value for key, value in versions.items()}
                merged_queues = dict(queues or {})
                merged_queues.update(queue_map)
                stage_versions: Iterable[VersionLike] = tuple(queue_map)
                queues = merged_queues
            else:
                stage_versions = versions or ()

            stage = Stage(
                name=name,
                versions=tuple(stage_versions),  # type: ignore[arg-type]
                successor=successor,
                queues={canonical_version(key): value for key, value in (queues or {}).items()},
                queue_name=queue_name,
                accepts={
                    canonical_version(key): tuple(canonical_version(accepted) for accepted in values)
                    for key, values in (accepts or {}).items()
                },
                actors={canonical_version(key): value for key, value in (actors or {}).items()},
            )

        with self._lock:
            self._stages[stage.name] = stage
        return stage

    add_stage = register_stage

    def register_worker(
        self,
        queue_name: str,
        versions: Optional[Iterable[VersionLike]] = None,
        *,
        supported_versions: Optional[Iterable[VersionLike]] = None,
        stage: Optional[str] = None,
        worker_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorkerRegistration:
        """Register a worker and the stage versions it supports."""

        if versions is None:
            versions = supported_versions
        elif supported_versions is not None:
            raise TypeError("pass either versions or supported_versions, not both")
        if versions is None:
            raise TypeError("register_worker() requires versions")

        registration = WorkerRegistration(
            queue_name=queue_name,
            versions=frozenset(_version_set(versions)),
            stage=stage,
            worker_id=worker_id or uuid.uuid4().hex,
            metadata=metadata or {},
        )
        with self._lock:
            self._workers[registration.worker_id] = registration
        return registration

    register = register_worker
    add_worker = register_worker
    declare_worker = register_worker

    def register_workers(
        self,
        registrations: Mapping[str, Iterable[VersionLike]],
        *,
        stage: Optional[str] = None,
    ) -> tuple[WorkerRegistration, ...]:
        """Register several queues at once (``{queue_name: versions}``)."""

        return tuple(
            self.register_worker(queue_name, versions, stage=stage)
            for queue_name, versions in registrations.items()
        )

    register_queue = register_worker

    def unregister_worker(self, worker: Union[str, WorkerRegistration]) -> bool:
        """Remove a worker by id or registration.  Returns whether it existed."""

        worker_id = worker if isinstance(worker, str) else worker.worker_id
        with self._lock:
            return self._workers.pop(worker_id, None) is not None

    remove_worker = unregister_worker

    def stage(self, name: str) -> Stage:
        with self._lock:
            try:
                return self._stages[name]
            except KeyError:
                raise KeyError(f"unknown stage {name!r}") from None

    def get_stage(self, name: str) -> Optional[Stage]:
        with self._lock:
            return self._stages.get(name)

    @property
    def stages(self) -> tuple[Stage, ...]:
        with self._lock:
            return tuple(self._stages.values())

    @property
    def workers(self) -> tuple[WorkerRegistration, ...]:
        with self._lock:
            return tuple(self._workers.values())

    def workers_for(
        self,
        stage: Optional[str] = None,
        *,
        queue_name: Optional[str] = None,
    ) -> tuple[WorkerRegistration, ...]:
        with self._lock:
            workers = tuple(self._workers.values())

        result = []
        for worker in workers:
            if stage is not None and worker.stage is not None and worker.stage != stage:
                continue
            if queue_name is not None and worker.queue_name != queue_name:
                continue
            result.append(worker)
        return tuple(result)

    def version_rank(self, stage: str, version: VersionLike) -> Optional[int]:
        definition = self.get_stage(stage)
        return definition.version_rank(version) if definition is not None else None

    def stage_for_queue(self, queue_name: str) -> Optional[str]:
        """Return the stage that declares *queue_name*, if any."""

        for definition in self.stages:
            if definition.queue_name == queue_name:
                return definition.name
            if queue_name in definition.queues.values():
                return definition.name
        return None

    def route(
        self,
        stage: Union[str, Stage],
        *,
        payload_version: Optional[VersionLike] = None,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
        queue_name: Optional[str] = None,
    ) -> Optional[RouteMatch]:
        """Choose the best compatible worker for a stage invocation.

        Newer versions are preferred.  A worker is compatible only when it
        declares the selected version, consumes the queue registered for that
        version, and accepts ``payload_version``.
        """

        definition = stage if isinstance(stage, Stage) else self.get_stage(stage)
        stage_name = definition.name if definition is not None else str(stage)
        requested_version = canonical_version(version) if version is not None else None
        payload_version = canonical_version(payload_version) if payload_version is not None else None

        with self._lock:
            workers = tuple(self._workers.values())

        candidates: list[tuple[int, int, WorkerRegistration, str, str]] = []
        for worker in workers:
            if worker_id is not None and worker.worker_id != worker_id:
                continue
            if queue_name is not None and worker.queue_name != queue_name:
                continue
            if definition is not None and worker.stage is not None and worker.stage != definition.name:
                continue

            if requested_version is not None:
                possible_versions = [requested_version]
            elif definition is not None:
                possible_versions = list(definition.versions)
            else:
                possible_versions = []
            if not possible_versions:
                possible_versions = list(worker.versions)

            for candidate_version in possible_versions:
                if candidate_version is None or not worker.supports(candidate_version):
                    continue
                if definition is not None:
                    expected_queue = definition.queue_for(candidate_version)
                    if expected_queue is not None and expected_queue != worker.queue_name:
                        continue
                    accepts = definition.accepts_payload(candidate_version, payload_version)
                    if payload_version is not None and not accepts:
                        continue
                rank = definition.version_rank(candidate_version) if definition is not None else None
                candidates.append(
                    (
                        rank if rank is not None else -1,
                        len(worker.queue_name),
                        worker,
                        candidate_version,
                        worker.queue_name,
                    )
                )

        if not candidates:
            return None

        candidates.sort(key=lambda candidate: (candidate[0], -candidate[1]), reverse=True)
        _, _, worker, selected_version, selected_queue = candidates[0]
        return RouteMatch(
            stage=stage_name,
            version=selected_version,
            queue_name=selected_queue,
            worker=worker,
        )


@dataclasses.dataclass(frozen=True)
class Receipt:
    """A result committed for one run and stage."""

    run_id: str
    stage: str
    version: str
    payload: Any
    worker_id: Optional[str] = None
    rank: Optional[int] = None
    sequence: int = 0
    committed_at: float = dataclasses.field(default_factory=time.time)

    def __post_init__(self):
        object.__setattr__(self, "version", canonical_version(self.version))

    @property
    def payload_version(self) -> str:
        return self.version

    def copy(self, **changes: Any) -> "Receipt":
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(frozen=True)
class CommitResult:
    """Representation of a conditional receipt commit."""

    committed: bool
    receipt: Optional[Receipt] = None
    reason: Optional[str] = None
    duplicate: bool = False
    previous: Optional[Receipt] = None

    def __bool__(self) -> bool:
        return self.committed

    @property
    def stale(self) -> bool:
        return self.reason == "stale_version"


class ReceiptStore:
    """Abstract store for conditional ``(run_id, stage)`` receipts.

    External implementations (for example Redis-backed ones) only need to
    provide :meth:`get` and :meth:`commit` with compare-and-set semantics.
    """

    def get(self, run_id: str, stage: str) -> Optional[Receipt]:  # pragma: no cover - interface
        raise NotImplementedError

    def commit(
        self,
        receipt: Receipt,
        *,
        expected: Any = MISSING,
        force: bool = False,
    ) -> CommitResult:  # pragma: no cover - interface
        raise NotImplementedError

    def all_for_run(self, run_id: str) -> dict[str, Receipt]:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryReceiptStore(ReceiptStore):
    """Thread-safe conditional receipt store for tests and single processes."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], Receipt] = {}
        self._lock = threading.RLock()

    def get(self, run_id: str, stage: str) -> Optional[Receipt]:
        with self._lock:
            return self._receipts.get((run_id, stage))

    def all_for_run(self, run_id: str) -> dict[str, Receipt]:
        with self._lock:
            return {
                stage: receipt.copy()
                for (receipt_run_id, stage), receipt in self._receipts.items()
                if receipt_run_id == run_id
            }

    def commit(
        self,
        receipt: Receipt,
        *,
        expected: Any = MISSING,
        force: bool = False,
    ) -> CommitResult:
        key = (receipt.run_id, receipt.stage)
        with self._lock:
            current = self._receipts.get(key)

            if not is_missing(expected):
                if expected is None:
                    if current is not None:
                        return CommitResult(False, current, "precondition_failed", previous=current)
                elif isinstance(expected, Receipt):
                    if current is None or current.sequence != expected.sequence:
                        return CommitResult(False, current, "precondition_failed", previous=current)
                elif isinstance(expected, (str, int)):
                    if current is None or current.version != canonical_version(expected):
                        return CommitResult(False, current, "precondition_failed", previous=current)
                elif callable(expected):
                    if not expected(current):
                        return CommitResult(False, current, "precondition_failed", previous=current)
                elif current != expected:
                    return CommitResult(False, current, "precondition_failed", previous=current)

            if current is None:
                committed = dataclasses.replace(receipt, sequence=1)
                self._receipts[key] = committed
                return CommitResult(True, committed)

            if force:
                committed = dataclasses.replace(receipt, sequence=current.sequence + 1)
                self._receipts[key] = committed
                return CommitResult(True, committed, previous=current)

            if receipt.rank is not None and current.rank is not None:
                if receipt.rank < current.rank:
                    return CommitResult(False, current, "stale_version", previous=current)
                if receipt.rank == current.rank:
                    if receipt.payload == current.payload:
                        return CommitResult(False, current, "duplicate", duplicate=True, previous=current)
                    return CommitResult(False, current, "conflict", previous=current)
            else:
                order = _compare_versions(receipt.version, current.version)
                if order is not None and order < 0:
                    return CommitResult(False, current, "stale_version", previous=current)
                if order is None or order == 0:
                    if receipt.payload == current.payload:
                        return CommitResult(False, current, "duplicate", duplicate=True, previous=current)
                    return CommitResult(False, current, "conflict", previous=current)

            committed = dataclasses.replace(receipt, sequence=current.sequence + 1)
            self._receipts[key] = committed
            return CommitResult(True, committed, previous=current)


@dataclasses.dataclass
class StageState:
    """Public, mutable state for one stage of one run."""

    name: str
    status: StageStatus = StageStatus.PENDING
    version: Optional[str] = None
    payload_version: Optional[str] = None
    queue_name: Optional[str] = None
    worker_id: Optional[str] = None
    payload: Any = MISSING
    reason: Optional[str] = None

    @property
    def pending_migration(self) -> bool:
        return self.status == StageStatus.PENDING_MIGRATION

    def describe(self) -> str:
        if self.status == StageStatus.PENDING_MIGRATION:
            return f"{self.name}: pending migration"
        return f"{self.name}: {self.status.value}"

    def asdict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["status"] = self.status.value
        if is_missing(self.payload):
            result["payload"] = None
        return result


@dataclasses.dataclass
class RunState:
    """State for one logical run."""

    run_id: str
    defaults: dict[str, Any] = dataclasses.field(default_factory=dict)
    input: Any = None
    status: RunStatus = RunStatus.RUNNING
    current_stage: Optional[str] = None
    stages: "OrderedDict[str, StageState]" = dataclasses.field(default_factory=OrderedDict)
    pending: Optional["PendingMigration"] = None
    last_submission: Optional["Submission"] = None
    created_at: float = dataclasses.field(default_factory=time.time)

    def stage(self, name: str) -> StageState:
        try:
            return self.stages[name]
        except KeyError:
            state = self.stages[name] = StageState(name=name)
            return state

    @property
    def pending_stage(self) -> Optional[str]:
        if self.pending is not None:
            return self.pending.stage
        for name, state in self.stages.items():
            if state.status == StageStatus.PENDING_MIGRATION:
                return name
        return None

    @property
    def pending_migration(self) -> bool:
        return self.pending_stage is not None

    def asdict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "defaults": dict(self.defaults),
            "input": self.input,
            "status": self.status.value,
            "current_stage": self.current_stage,
            "stages": {name: state.asdict() for name, state in self.stages.items()},
            "pending_stage": self.pending_stage,
            "pending": str(self.pending) if self.pending is not None else None,
        }


@dataclasses.dataclass(frozen=True)
class Dispatch:
    """Instruction to enqueue a stage on a compatible worker."""

    run_id: str
    stage: str
    version: str
    queue_name: str
    worker: WorkerRegistration
    payload: Any
    payload_version: Optional[str]
    receipt: Optional[Receipt] = None
    actor_name: Optional[str] = None

    @property
    def worker_id(self) -> str:
        return self.worker.worker_id

    @property
    def options(self) -> dict[str, Any]:
        return {
            "migration_run_id": self.run_id,
            "migration_stage": self.stage,
            "migration_version": self.version,
            "migration_payload_version": self.payload_version,
        }


@dataclasses.dataclass(frozen=True)
class Submission:
    """Outcome of submitting or routing one stage."""

    run_id: str
    stage: str
    committed: bool = False
    duplicate: bool = False
    receipt: Optional[Receipt] = None
    dispatch: Optional[Dispatch] = None
    pending: Optional["PendingMigration"] = None
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.committed

    @property
    def stale(self) -> bool:
        return self.reason == "stale_version"

    def raise_if_pending(self) -> "Submission":
        """Raise :class:`PendingMigration` when this stage could not be routed."""

        if self.pending is not None:
            raise self.pending
        return self

    @property
    def outcome(self) -> str:
        if self.pending is not None:
            return "pending_migration"
        if self.committed and self.dispatch is not None:
            return "committed_and_dispatched"
        if self.committed:
            return "committed"
        if self.duplicate:
            return "duplicate"
        if self.dispatch is not None:
            return "dispatched"
        if self.reason is not None:
            return "rejected"
        return "noop"


class PendingMigration(Exception):
    """Raised or returned when a successor has no compatible receiver.

    The upstream receipt remains in the receipt store, so the run can be
    resumed later once a compatible worker registers.
    """

    def __init__(
        self,
        run_id: str,
        stage: str,
        *,
        payload_version: Optional[str] = None,
        payload: Any = MISSING,
        reason: str = "no compatible worker",
    ) -> None:
        self.run_id = run_id
        self.stage = stage
        self.stage_name = stage
        self.payload_version = payload_version
        self.payload = payload
        self.reason = reason
        super().__init__(str(self))

    @property
    def message(self) -> str:
        return f"stage {self.stage!r} for run {self.run_id!r} is pending migration: {self.reason}"

    def __str__(self) -> str:
        return self.message


class MigrationPipeline:
    """Run versioned stages with conditional, run-scoped receipts.

    The pipeline is broker-agnostic: :meth:`start` and :meth:`submit_result`
    return :class:`Dispatch` instructions that the caller can turn into
    messages.  When no successor can be routed, that stage is marked
    ``pending_migration`` and the existing receipt is retained for
    :meth:`resume`.
    """

    def __init__(
        self,
        stages: Optional[Iterable[Stage]] = None,
        *,
        registry: Optional[MigrationRegistry] = None,
        receipts: Optional[ReceiptStore] = None,
        first_stage: Optional[str] = None,
    ) -> None:
        self.registry = registry or MigrationRegistry()
        self.receipts = receipts or InMemoryReceiptStore()
        self.first_stage = first_stage
        self.runs: dict[str, RunState] = {}
        self._lock = threading.RLock()

        for stage in stages or ():
            self.registry.register_stage(stage)

        if self.first_stage is None:
            ordered = self.registry.stages
            if ordered:
                self.first_stage = ordered[0].name

    @property
    def stages(self) -> tuple[Stage, ...]:
        return self.registry.stages

    def successor_of(self, stage: str) -> Optional[str]:
        definition = self.registry.get_stage(stage)
        if definition is not None and definition.successor is not None:
            return definition.successor

        ordered = [item.name for item in self.registry.stages]
        try:
            index = ordered.index(stage)
        except ValueError:
            return None
        return ordered[index + 1] if index + 1 < len(ordered) else None

    def create_run(
        self,
        input: Any = None,
        *,
        run_id: Optional[str] = None,
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> RunState:
        """Create a run without scheduling its first stage."""

        run_id = run_id or uuid.uuid4().hex
        with self._lock:
            if run_id in self.runs:
                raise ValueError(f"run {run_id!r} already exists")

            run = RunState(
                run_id=run_id,
                defaults=dict(defaults or {}),
                input=input,
                current_stage=self.first_stage,
            )
            for stage in self.registry.stages:
                run.stages[stage.name] = StageState(name=stage.name)
            self.runs[run_id] = run
            return run

    def get_run(self, run: Union[str, RunState]) -> RunState:
        if isinstance(run, RunState):
            return run
        try:
            return self.runs[run]
        except KeyError:
            raise KeyError(f"unknown run {run!r}") from None

    def get_or_create_run(
        self,
        run: Union[str, RunState],
        *,
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> RunState:
        """Look a run up, creating a placeholder when it is unknown.

        A worker process that did not start the run can still commit the
        result it processed.  Such a placeholder has no input and only the
        defaults supplied by the caller.
        """

        if isinstance(run, RunState):
            return run
        existing = self.runs.get(run)
        if existing is not None:
            return existing
        return self.create_run(None, run_id=run, defaults=defaults)

    def start(
        self,
        input: Any = None,
        *,
        run_id: Optional[str] = None,
        defaults: Optional[Mapping[str, Any]] = None,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
        payload_version: Optional[VersionLike] = None,
    ) -> RunState:
        """Create a run and route its first stage.

        The returned :class:`RunState` exposes the routing outcome through
        ``last_submission`` and, when unroutable, ``pending``.
        """

        run = self.create_run(input, run_id=run_id, defaults=defaults)
        if self.first_stage is None:
            run.status = RunStatus.COMPLETED
            return run

        submission = self.enqueue(
            run,
            self.first_stage,
            input,
            payload_version=payload_version,
            version=version,
            worker_id=worker_id,
        )
        run.last_submission = submission
        return run

    start_run = start

    def enqueue(
        self,
        run: Union[str, RunState],
        stage: str,
        payload: Any,
        *,
        payload_version: Optional[VersionLike] = None,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
    ) -> Submission:
        """Route a stage invocation and retain its input payload.

        This does not create a receipt; receipts are created only when a
        worker submits a result.
        """

        run = self.get_run(run)
        state = run.stage(stage)
        payload_version = canonical_version(payload_version) if payload_version is not None else None

        route = self.registry.route(
            stage,
            payload_version=payload_version,
            version=version,
            worker_id=worker_id,
        )
        if route is None:
            pending = PendingMigration(
                run.run_id,
                stage,
                payload_version=payload_version,
                payload=payload,
                reason="no registered worker supports the required stage version",
            )
            state.status = StageStatus.PENDING_MIGRATION
            if version is not None:
                state.version = canonical_version(version)
            state.payload = payload
            state.payload_version = payload_version
            state.reason = pending.reason
            run.pending = pending
            run.current_stage = stage
            if run.status != RunStatus.COMPLETED:
                run.status = RunStatus.PENDING_MIGRATION
            submission = Submission(run.run_id, stage, pending=pending)
            run.last_submission = submission
            return submission

        state.status = StageStatus.QUEUED
        state.version = route.version
        state.queue_name = route.queue_name
        state.worker_id = route.worker_id
        state.payload = payload
        state.payload_version = payload_version
        state.reason = None

        definition = self.registry.get_stage(stage)
        dispatch = Dispatch(
            run_id=run.run_id,
            stage=stage,
            version=route.version,
            queue_name=route.queue_name,
            worker=route.worker,
            payload=payload,
            payload_version=payload_version,
            actor_name=definition.actor_for(route.version) if definition is not None else None,
        )
        run.pending = None
        run.current_stage = stage
        if run.status != RunStatus.COMPLETED:
            run.status = RunStatus.RUNNING
        submission = Submission(run.run_id, stage, dispatch=dispatch)
        run.last_submission = submission
        return submission

    def submit_result(
        self,
        run: Union[str, RunState],
        stage: str,
        version: VersionLike,
        payload: Any,
        *,
        worker_id: Optional[str] = None,
        expected: Any = MISSING,
        force: bool = False,
        advance: bool = True,
        create_missing: bool = False,
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> Submission:
        """Commit a result conditionally and route its successor.

        A stale result (an older version arriving after a newer one was
        committed) is rejected and never dispatches a successor.

        With ``create_missing=True`` an unknown run id is accepted (for
        example when a separate worker process reports a result); all other
        run-scoped data must then come from explicit ``defaults``.
        """

        if create_missing:
            run = self.get_or_create_run(run, defaults=defaults)
        else:
            run = self.get_run(run)
        version = canonical_version(version)
        rank = self.registry.version_rank(stage, version)

        # The selected route is authoritative.  Once a run was switched to a
        # newer version, a delayed result from the previous route is stale
        # even if the new version has not committed a receipt yet.
        state = run.stage(stage)
        if (
            rank is not None
            and state.version is not None
            and state.status in {StageStatus.QUEUED, StageStatus.RUNNING, StageStatus.PENDING_MIGRATION}
        ):
            selected_rank = self.registry.version_rank(stage, state.version)
            if selected_rank is not None and rank < selected_rank:
                submission = Submission(
                    run.run_id,
                    stage,
                    committed=False,
                    receipt=self.receipts.get(run.run_id, stage),
                    reason="stale_version",
                )
                run.last_submission = submission
                return submission

        receipt = Receipt(
            run_id=run.run_id,
            stage=stage,
            version=version,
            payload=payload,
            worker_id=worker_id,
            rank=rank,
        )
        commit = self.receipts.commit(receipt, expected=expected, force=force)

        if not commit.committed:
            if commit.receipt is not None:
                state.status = StageStatus.SUCCEEDED
                state.version = commit.receipt.version
                state.payload = commit.receipt.payload
                state.payload_version = commit.receipt.version
                state.worker_id = commit.receipt.worker_id
                state.reason = commit.reason
            submission = Submission(
                run.run_id,
                stage,
                committed=False,
                duplicate=commit.duplicate,
                receipt=commit.receipt,
                reason=commit.reason,
            )
            run.last_submission = submission
            return submission

        committed = commit.receipt
        assert committed is not None
        state.status = StageStatus.SUCCEEDED
        state.version = committed.version
        state.payload = committed.payload
        state.payload_version = committed.version
        state.worker_id = committed.worker_id
        state.reason = None

        submission = Submission(run.run_id, stage, committed=True, receipt=committed)
        if advance:
            next_submission = self._advance_after(run, stage, committed)
            if next_submission is not None:
                submission = dataclasses.replace(
                    submission,
                    dispatch=next_submission.dispatch,
                    pending=next_submission.pending,
                    reason=next_submission.reason,
                )

        run.last_submission = submission
        return submission

    commit_result = submit_result

    def submit(
        self,
        *args: Any,
        run_id: Optional[str] = None,
        run: Union[str, RunState, None] = None,
        stage: Optional[str] = None,
        version: Optional[VersionLike] = None,
        result: Any = MISSING,
        payload: Any = MISSING,
        worker_id: Optional[str] = None,
    ) -> Submission:
        """Call-style wrapper around :meth:`submit_result`.

        Accepted forms::

            submit(result, run_id="R1", stage="parse", version="v2")
            submit("R1", "parse", "v2", result)
        """

        if len(args) == 4:
            if run_id is not None or run is not None or stage is not None or version is not None:
                raise TypeError("submit() accepts positional or keyword arguments, not both")
            run_id, stage, version, result = args
        elif len(args) == 1:
            if result is not MISSING:
                raise TypeError("submit() received two result values")
            result = args[0]
        elif args:
            raise TypeError("submit() accepts one result value or (run_id, stage, version, result)")

        target = run if run is not None else run_id
        if target is None or stage is None or version is None:
            raise TypeError("submit() requires run_id, stage and version")

        committed_payload = result if result is not MISSING else payload
        if committed_payload is MISSING:
            raise TypeError("submit() requires a result payload")

        return self.submit_result(
            target,
            stage,
            version,
            committed_payload,
            worker_id=worker_id,
        )

    report_result = submit_result

    def _advance_after(self, run: RunState, stage: str, receipt: Receipt) -> Optional[Submission]:
        successor = self.successor_of(stage)
        if successor is None:
            run.status = RunStatus.COMPLETED
            run.current_stage = None
            run.pending = None
            return None

        existing = self.receipts.get(run.run_id, successor)
        if existing is not None:
            state = run.stage(successor)
            state.status = StageStatus.SUCCEEDED
            state.version = existing.version
            state.payload = existing.payload
            state.payload_version = existing.version
            state.worker_id = existing.worker_id
            run.pending = None
            return self._advance_after(run, successor, existing)

        return self.enqueue(
            run,
            successor,
            receipt.payload,
            payload_version=receipt.version,
        )

    def reroute(
        self,
        run: Union[str, RunState],
        stage: str,
        *,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
        payload_version: Optional[VersionLike] = None,
    ) -> Submission:
        """Route an existing stage without recomputing its predecessor.

        This is the operation used when an in-flight run is switched to a new
        route: the previously committed upstream receipt is reused verbatim,
        including explicit ``None`` values.
        """

        run = self.get_run(run)
        state = run.stage(stage)

        canonical_payload_version: Optional[str]
        if payload_version is not None:
            canonical_payload_version = canonical_version(payload_version)
            payload = state.payload
        else:
            source_stage = self._predecessor_of(stage)
            source_receipt = self.receipts.get(run.run_id, source_stage) if source_stage is not None else None
            if source_receipt is not None:
                payload = source_receipt.payload
                canonical_payload_version = source_receipt.version
            else:
                payload = state.payload
                canonical_payload_version = state.payload_version

        if is_missing(payload):
            raise ValueError(f"stage {stage!r} has no retained payload to reroute")

        return self.enqueue(
            run,
            stage,
            payload,
            payload_version=canonical_payload_version,
            version=version,
            worker_id=worker_id,
        )

    reroute_stage = reroute

    def _predecessor_of(self, stage: str) -> Optional[str]:
        for definition in self.registry.stages:
            if self.successor_of(definition.name) == stage:
                return definition.name
        return None

    def resume(
        self,
        run: Union[str, RunState],
        *,
        stage: Optional[str] = None,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
    ) -> Optional[Submission]:
        """Retry routing for a pending stage, retaining all prior results."""

        run = self.get_run(run)
        target = stage or run.current_stage
        if target is None:
            return None

        state = run.stage(target)
        # Resuming is only meaningful for a parked stage.  Queued or already
        # completed stages keep their in-flight dispatch instead of fanning
        # out a second copy.
        if state.status != StageStatus.PENDING_MIGRATION:
            return run.last_submission

        submission = self.reroute(run, target, version=version, worker_id=worker_id)
        run.last_submission = submission
        return submission

    resume_run = resume

    def receipt(self, run: Union[str, RunState], stage: str) -> Optional[Receipt]:
        """Alias for :meth:`get_receipt`."""

        return self.get_receipt(run, stage)

    def receipts_for(self, run: Union[str, RunState]) -> dict[str, Receipt]:
        """Return every committed receipt for *run* keyed by stage."""

        run = self.get_run(run)
        return self.receipts.all_for_run(run.run_id)

    def is_stage_done(self, run: Union[str, RunState], stage: str) -> bool:
        return self.receipts.get(self.get_run(run).run_id, stage) is not None

    def mark_pending(
        self,
        run: Union[str, RunState],
        stage: str,
        *,
        payload: Any = MISSING,
        payload_version: Optional[VersionLike] = None,
        reason: str = "pending migration",
    ) -> Submission:
        """Explicitly park a stage until a compatible receiver appears.

        All committed upstream receipts are left untouched.
        """

        run = self.get_run(run)
        state = run.stage(stage)
        if is_missing(payload):
            payload = state.payload
        selected_payload_version = state.payload_version
        if payload_version is not None:
            selected_payload_version = canonical_version(payload_version)
        pending = PendingMigration(
            run.run_id,
            stage,
            payload_version=selected_payload_version,
            payload=payload,
            reason=reason,
        )
        state.status = StageStatus.PENDING_MIGRATION
        state.payload = payload
        state.reason = reason
        run.pending = pending
        run.current_stage = stage
        if run.status != RunStatus.COMPLETED:
            run.status = RunStatus.PENDING_MIGRATION
        submission = Submission(run.run_id, stage, pending=pending)
        run.last_submission = submission
        return submission

    def complete_pending(
        self,
        run: Union[str, RunState],
        stage: str,
        version: VersionLike,
        payload: Any,
        *,
        worker_id: Optional[str] = None,
    ) -> Submission:
        """Commit a result for a previously pending stage."""

        return self.submit_result(run, stage, version, payload, worker_id=worker_id)

    def get_receipt(self, run: Union[str, RunState], stage: str) -> Optional[Receipt]:
        run = self.get_run(run)
        return self.receipts.get(run.run_id, stage)

    def adopt_result(
        self,
        run: Union[str, RunState],
        stage: str,
        version: VersionLike,
        payload: Any,
        *,
        worker_id: Optional[str] = None,
        advance: bool = True,
    ) -> Submission:
        """Adopt a result produced by an old worker during a rollout.

        This is the same conditional commit as :meth:`submit_result`; it is
        named separately because operators commonly seed a run with a result
        that was already stored by the previous version.
        """

        return self.submit_result(
            run,
            stage,
            version,
            payload,
            worker_id=worker_id,
            advance=advance,
        )

    adopt = adopt_result

    def switch_stage(
        self,
        run: Union[str, RunState],
        stage: str,
        *,
        version: Optional[VersionLike] = None,
        worker_id: Optional[str] = None,
    ) -> Submission:
        """Route a stage to a new version while preserving prior results."""

        return self.reroute(run, stage, version=version, worker_id=worker_id)

    switch = switch_stage

    def get_status(self, run: Union[str, RunState]) -> RunState:
        return self.get_run(run)

    def pending_stages(self, run: Union[str, RunState]) -> list[str]:
        run = self.get_run(run)
        return [name for name, state in run.stages.items() if state.status == StageStatus.PENDING_MIGRATION]

    def describe(self, run: Union[str, RunState]) -> str:
        """Return a one-line operator summary of a run."""

        run = self.get_run(run)
        parts = [f"run {run.run_id}: {run.status.value}"]
        for state in run.stages.values():
            parts.append(state.describe())
        return "; ".join(parts)

    def build_message(
        self,
        dispatch: Dispatch,
        *,
        actor_name: Optional[str] = None,
        args: Optional[tuple] = None,
        kwargs: Optional[Mapping[str, Any]] = None,
        options: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Build the Dramatiq message for a resolved dispatch.

        The actor name defaults to the stage's ``actors`` mapping for the
        selected version.  The resolved payload is passed as the sole
        positional argument unless explicit ``args`` are supplied.
        """

        from .message import Message

        resolved_actor = actor_name or dispatch.actor_name
        if resolved_actor is None:
            raise MigrationError(
                f"no actor configured for stage {dispatch.stage!r} version {dispatch.version!r}; "
                "pass actors={version: actor_name} to Stage or provide actor_name explicitly"
            )

        message_options = dict(dispatch.options)
        if options:
            message_options.update(options)

        return Message(
            queue_name=dispatch.queue_name,
            actor_name=resolved_actor,
            args=args if args is not None else (dispatch.payload,),
            kwargs=dict(kwargs or {}),
            options=message_options,
        )


#: More explicit aliases for the same coordinator.
VersionedPipeline = MigrationPipeline
MigrationCoordinator = MigrationPipeline


class MigrationMiddleware(Middleware):
    """Worker-side bridge between Dramatiq messages and a migration pipeline.

    The middleware ignores messages that don't carry migration options, so it
    can be added to a broker that also runs ordinary pipelines.  Results are
    committed through the run-scoped receipt store; only a successful,
    non-stale commit can enqueue the next stage.

    Parameters:
      pipeline(MigrationPipeline): The pipeline/coordinator to use.
      worker_id(str): This worker's identity.  Defaults to a generated id.
      registrations(Mapping[str, Iterable[str]]): A mapping of queue name to
        the stage versions this worker supports.  Each queue is registered on
        construction.
      stage(str): Optional stage name to attach to the registrations.
      broker(Broker): Optional broker used to enqueue successor messages.
        Defaults to ``get_broker()`` at enqueue time.
      auto_create_runs(bool): When True, results for run ids this process has
        not seen are accepted and a placeholder run is created.  This is
        useful for single-process setups; distributed workers should instead
        forward results to the process that owns the run, which is the
        default (False).
    """

    def __init__(
        self,
        pipeline: MigrationPipeline,
        *,
        worker_id: Optional[str] = None,
        registrations: Optional[Mapping[str, Iterable[VersionLike]]] = None,
        stage: Optional[str] = None,
        broker: Any = None,
        auto_create_runs: bool = False,
    ) -> None:
        self.pipeline = pipeline
        self.worker_id = worker_id or uuid.uuid4().hex
        self.broker = broker
        self.auto_create_runs = auto_create_runs
        self.stage = stage
        self.registrations = tuple(self._iter_registrations(registrations or {}, stage))
        self._registration_ids: list[str] = []

    @staticmethod
    def _iter_registrations(registrations: Mapping[str, Iterable[VersionLike]], stage: Optional[str]):
        for queue_name, versions in registrations.items():
            yield queue_name, tuple(versions)

    def after_worker_boot(self, broker: Any, worker: Any) -> None:
        for index, (queue_name, versions) in enumerate(self.registrations):
            stage = self.stage or self.pipeline.registry.stage_for_queue(queue_name)
            # One process commonly consumes several queues; registrations are
            # keyed by a per-queue id so each queue keeps its own versions.
            registration_id = f"{self.worker_id}:{index}"
            self.pipeline.registry.register_worker(
                queue_name,
                versions,
                stage=stage,
                worker_id=registration_id,
            )
            self._registration_ids.append(registration_id)

    def before_worker_shutdown(self, broker: Any, worker: Any) -> None:
        for registration_id in self._registration_ids:
            self.pipeline.registry.unregister_worker(registration_id)
        self._registration_ids.clear()

    @property
    def actor_options(self) -> set[str]:
        return {"migration_run_id", "migration_stage", "migration_version", "migration_payload_version"}

    def after_process_message(
        self,
        broker: Any,
        message: Any,
        *,
        result: Any = None,
        exception: Optional[BaseException] = None,
    ) -> None:
        options = message.options
        run_id = options.get("migration_run_id") or options.get("run_id")
        stage = options.get("migration_stage")
        version = options.get("migration_version")
        if run_id is None or stage is None or version is None:
            return
        if exception is not None or getattr(message, "failed", False):
            return

        submission = self.pipeline.submit_result(
            run_id,
            stage,
            version,
            result,
            worker_id=self.worker_id,
            create_missing=self.auto_create_runs,
        )
        if submission.pending is not None or submission.dispatch is None:
            return

        next_message = self.pipeline.build_message(submission.dispatch)
        target_broker = self.broker
        if target_broker is None:
            from .broker import get_broker

            target_broker = get_broker()
        target_broker.enqueue(next_message, delay=next_message.options.get("delay"))
