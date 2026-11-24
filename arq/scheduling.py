"""Versioned collection manifests, claims, and ingestion settlement ledgers.

The ledger keeps job execution reports separate from warehouse/ingestion
receipts.  Only receipts mark original sample intervals as settled.  Plan
versions and claim ids provide fencing/provenance while interval unions make
redelivered and overlapping reports idempotent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional, Sequence, Union


class SchedulingError(RuntimeError):
    """Base error for versioned collection scheduling."""


class InvalidInterval(SchedulingError, ValueError):
    """Raised when a sample interval is not a valid half-open range."""


class UnknownPlanVersion(SchedulingError, KeyError):
    """Raised when an operation references an unknown plan version."""


class StalePlanVersion(SchedulingError, PermissionError):
    """Raised when a superseded plan tries to claim new work."""


class UnknownAssignment(SchedulingError, KeyError):
    """Raised when an assignment is absent from the referenced plan."""


class ScheduleNotDue(SchedulingError):
    """Raised when an assignment's local execution time is still in the future."""


class ClaimState(str, Enum):
    active = 'active'
    completed = 'completed'
    released = 'released'
    expired = 'expired'
    revoked = 'revoked'
    superseded = 'superseded'


IntervalLike = Union['SampleRange', tuple[int, int], list[int], dict[str, Any], int]


@dataclass(frozen=True, order=True, init=False)
class SampleRange:
    """A half-open original sample interval ``[start, end)``."""

    start: int
    end: int

    def __init__(
        self,
        start: Any = None,
        end: Any = None,
        *,
        start_sample: Optional[int] = None,
        end_sample: Optional[int] = None,
        lo: Optional[int] = None,
        hi: Optional[int] = None,
    ) -> None:
        if isinstance(start, SampleRange):
            resolved_start, resolved_end = start.start, start.end
        elif isinstance(start, (tuple, list)) and end is None and len(start) == 2:
            resolved_start, resolved_end = start
        elif isinstance(start, dict) and end is None:
            parsed = self.coerce(start)
            resolved_start, resolved_end = parsed.start, parsed.end
        else:
            resolved_start = _first(
                {'value': start, 'start_sample': start_sample, 'lo': lo},
                'value',
                'start_sample',
                'lo',
            )
            resolved_end = _first(
                {'value': end, 'end_sample': end_sample, 'hi': hi},
                'value',
                'end_sample',
                'hi',
            )
        if resolved_start is None or resolved_end is None:
            raise InvalidInterval('SampleRange requires start and end')
        if not isinstance(resolved_start, int) or not isinstance(resolved_end, int):
            raise InvalidInterval('sample numbers must be integers')
        if resolved_start < 0 or resolved_end <= resolved_start:
            raise InvalidInterval(f'invalid half-open sample range: {resolved_start!r}:{resolved_end!r}')
        object.__setattr__(self, 'start', resolved_start)
        object.__setattr__(self, 'end', resolved_end)

    @classmethod
    def coerce(cls, value: IntervalLike, end: Optional[int] = None) -> 'SampleRange':
        if isinstance(value, SampleRange):
            return value
        if isinstance(value, dict):
            start = _first(value, 'start', 'start_sample', 'lo', 'from_sample', 'from_', default=None)
            finish = _first(value, 'end', 'end_sample', 'hi', 'to_sample', 'to', default=None)
            if start is None or finish is None:
                raise InvalidInterval(f'cannot parse interval from {value!r}')
            return cls(int(start), int(finish))
        if isinstance(value, (tuple, list)):
            if len(value) == 2:
                return cls(int(value[0]), int(value[1]))
            if len(value) == 1 and end is not None:
                return cls(int(value[0]), int(end))
            raise InvalidInterval(f'interval tuple must be [start, end): {value!r}')
        if end is not None:
            return cls(int(value), int(end))
        start = getattr(value, 'start', None)
        finish = getattr(value, 'end', None)
        if start is None:
            start = getattr(value, 'start_sample', None)
        if finish is None:
            finish = getattr(value, 'end_sample', None)
        if start is not None and finish is not None:
            return cls(int(start), int(finish))
        raise InvalidInterval(f'cannot parse sample interval from {value!r}')

    @property
    def start_sample(self) -> int:
        return self.start

    @property
    def end_sample(self) -> int:
        return self.end

    @property
    def lo(self) -> int:
        return self.start

    @property
    def hi(self) -> int:
        return self.end

    @property
    def a(self) -> int:
        return self.start

    @property
    def b(self) -> int:
        return self.end

    @property
    def from_sample(self) -> int:
        return self.start

    @property
    def to_sample(self) -> int:
        return self.end

    def __iter__(self):
        yield self.start
        yield self.end

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> int:
        return (self.start, self.end)[index]

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def size(self) -> int:
        return self.length

    def as_tuple(self) -> tuple[int, int]:
        return self.start, self.end

    def contains(self, other: Union['SampleRange', int]) -> bool:
        if isinstance(other, int):
            return self.start <= other < self.end
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: 'SampleRange') -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: 'SampleRange') -> Optional['SampleRange']:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return SampleRange(start, end) if start < end else None

    def subtract(self, covered: Iterable['SampleRange']) -> list['SampleRange']:
        pieces = [self]
        for raw_interval in covered:
            interval = SampleRange.coerce(raw_interval)
            next_pieces: list[SampleRange] = []
            for piece in pieces:
                overlap = piece.intersection(interval)
                if overlap is None:
                    next_pieces.append(piece)
                    continue
                if piece.start < overlap.start:
                    next_pieces.append(SampleRange(piece.start, overlap.start))
                if overlap.end < piece.end:
                    next_pieces.append(SampleRange(overlap.end, piece.end))
            pieces = next_pieces
        return merge_ranges(pieces)

    def to_dict(self) -> dict[str, int]:
        return {'start': self.start, 'end': self.end, 'start_sample': self.start, 'end_sample': self.end}


SegmentKey = tuple[str, str]


@dataclass(frozen=True, init=False)
class Segment:
    """A sample interval namespaced by site and source batch."""

    site: str
    source_batch: str
    range: SampleRange

    def __init__(
        self,
        site: Any = '_default',
        source_batch: Any = '_default',
        range: Any = None,
        *,
        batch: Optional[str] = None,
        interval: Any = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        start_sample: Optional[int] = None,
        end_sample: Optional[int] = None,
    ) -> None:
        if not isinstance(site, str) and source_batch == '_default' and range is None:
            value = site
            site = '_default'
        else:
            value = interval if interval is not None else range
        if (
            isinstance(value, SampleRange)
            and batch is None
            and start is None
            and end is None
            and start_sample is None
            and end_sample is None
        ):
            object.__setattr__(self, 'site', str(site or '_default'))
            object.__setattr__(self, 'source_batch', str(source_batch or '_default'))
            object.__setattr__(self, 'range', value)
            return
        resolved = self.coerce(
            value,
            site=site if isinstance(site, str) else None,
            source_batch=source_batch if isinstance(source_batch, str) else None,
            batch=batch,
            start=start,
            end=end,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        object.__setattr__(self, 'site', resolved.site)
        object.__setattr__(self, 'source_batch', resolved.source_batch)
        object.__setattr__(self, 'range', resolved.range)

    @classmethod
    def coerce(
        cls,
        value: Any = None,
        *,
        site: Optional[str] = None,
        source_batch: Optional[str] = None,
        batch: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        start_sample: Optional[int] = None,
        end_sample: Optional[int] = None,
    ) -> 'Segment':
        if isinstance(value, Segment):
            return value
        values: dict[str, Any] = {}
        interval_value: Any = None
        if isinstance(value, dict):
            values.update(value)
        elif value is not None:
            interval_value = value

        resolved_site = site or values.get('site') or values.get('site_id') or '_default'
        resolved_batch = (
            source_batch
            or batch
            or values.get('source_batch')
            or values.get('batch')
            or values.get('source_batch_id')
            or '_default'
        )
        resolved_start = _first(
            values, 'start', 'start_sample', 'lo', default=start if start is not None else start_sample
        )
        resolved_end = _first(values, 'end', 'end_sample', 'hi', default=end if end is not None else end_sample)
        if interval_value is not None and resolved_end is None:
            interval = SampleRange.coerce(interval_value)
        elif interval_value is not None and resolved_end is not None and resolved_start is None:
            interval = SampleRange.coerce(interval_value, resolved_end)  # type: ignore[arg-type]
        elif resolved_start is not None and resolved_end is not None:
            interval = SampleRange.coerce(int(resolved_start), int(resolved_end))
        elif isinstance(interval_value, (tuple, list, SampleRange, dict)):
            interval = SampleRange.coerce(interval_value)
        else:
            raise InvalidInterval('segment requires start and end samples')
        return cls(str(resolved_site), str(resolved_batch), interval)

    @property
    def key(self) -> SegmentKey:
        return self.site, self.source_batch

    @property
    def start(self) -> int:
        return self.range.start

    @property
    def end(self) -> int:
        return self.range.end

    @property
    def batch(self) -> str:
        return self.source_batch

    @property
    def start_sample(self) -> int:
        return self.range.start

    @property
    def end_sample(self) -> int:
        return self.range.end

    def as_tuple(self) -> tuple[int, int]:
        return self.range.as_tuple()

    def __iter__(self):
        yield self.start
        yield self.end

    def to_dict(self) -> dict[str, Any]:
        return {'site': self.site, 'source_batch': self.source_batch, **self.range.to_dict()}


@dataclass(frozen=True)
class Assignment:
    """One manifest line owned by an explicit plan version."""

    assignment_id: str
    plan_version: str
    segment: Segment
    scheduled_at: Optional[datetime] = None
    local_time: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scheduled_at is None and self.local_time is not None:
            object.__setattr__(self, 'scheduled_at', self.local_time)

    @property
    def site(self) -> str:
        return self.segment.site

    @property
    def source_batch(self) -> str:
        return self.segment.source_batch

    @property
    def range(self) -> SampleRange:
        return self.segment.range

    @property
    def interval(self) -> SampleRange:
        return self.segment.range

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if self.scheduled_at is None:
            return True
        if now is None:
            now = datetime.now(tz=self.scheduled_at.tzinfo) if self.scheduled_at.tzinfo else datetime.now()
        return now >= self.scheduled_at

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['segment'] = self.segment.to_dict()
        if self.scheduled_at is not None:
            data['scheduled_at'] = self.scheduled_at.isoformat()
        return data


@dataclass(frozen=True, init=False)
class PlanVersion:
    version: str
    assignments: tuple[Assignment, ...]
    published_at: Optional[datetime] = None
    supersedes: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        version: Union[str, int],
        assignments: Optional[Sequence[Any]] = None,
        *,
        windows: Optional[Sequence[Any]] = None,
        entries: Optional[Sequence[Any]] = None,
        manifest: Optional[Sequence[Any]] = None,
        published_at: Optional[datetime] = None,
        supersedes: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **extra_metadata: Any,
    ) -> None:
        raw_assignments = assignments if assignments is not None else windows
        raw_assignments = raw_assignments if raw_assignments is not None else entries
        raw_assignments = raw_assignments if raw_assignments is not None else manifest
        if raw_assignments is None:
            raw_assignments = []
        version_id = str(version)
        built = tuple(
            _coerce_assignment(raw, plan_version=version_id, index=index)
            for index, raw in enumerate(raw_assignments)
        )
        merged_metadata = dict(metadata or {})
        merged_metadata.update(extra_metadata)
        object.__setattr__(self, 'version', version_id)
        object.__setattr__(self, 'assignments', built)
        object.__setattr__(self, 'published_at', published_at)
        object.__setattr__(self, 'supersedes', supersedes)
        object.__setattr__(self, 'metadata', merged_metadata)

    @classmethod
    def build(
        cls,
        version: Union[str, int],
        assignments: Optional[Sequence[Any]] = None,
        **kwargs: Any,
    ) -> 'PlanVersion':
        return cls(version, assignments, **kwargs)

    def assignment(self, assignment_id: str) -> Assignment:
        for assignment in self.assignments:
            if assignment.assignment_id == assignment_id:
                return assignment
        raise UnknownAssignment(f'plan {self.version} has no assignment {assignment_id!r}')

    def to_dict(self) -> dict[str, Any]:
        return {
            'version': self.version,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'supersedes': self.supersedes,
            'metadata': dict(self.metadata),
            'assignments': [item.to_dict() for item in self.assignments],
        }


@dataclass(frozen=True)
class Claim:
    claim_id: str
    plan_version: str
    assignment_id: str
    segment: Segment
    worker_id: Optional[str] = None
    job_id: Optional[str] = None
    claimed_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    state: ClaimState = ClaimState.active
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state == ClaimState.active

    @property
    def site(self) -> str:
        return self.segment.site

    @property
    def source_batch(self) -> str:
        return self.segment.source_batch

    @property
    def range(self) -> SampleRange:
        return self.segment.range

    @property
    def interval(self) -> SampleRange:
        return self.segment.range

    @property
    def start(self) -> int:
        return self.segment.start

    @property
    def end(self) -> int:
        return self.segment.end

    @property
    def start_sample(self) -> int:
        return self.segment.start

    @property
    def end_sample(self) -> int:
        return self.segment.end

    @property
    def id(self) -> str:
        return self.claim_id

    @property
    def version(self) -> str:
        return self.plan_version

    def is_live(self, now: Optional[datetime] = None) -> bool:
        if self.state != ClaimState.active:
            return False
        if self.lease_expires_at is None:
            return True
        if now is None:
            now = datetime.now(tz=self.lease_expires_at.tzinfo) if self.lease_expires_at.tzinfo else datetime.now()
        return now < self.lease_expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            'claim_id': self.claim_id,
            'plan_version': self.plan_version,
            'assignment_id': self.assignment_id,
            'segment': self.segment.to_dict(),
            'worker_id': self.worker_id,
            'job_id': self.job_id,
            'claimed_at': self.claimed_at.isoformat() if self.claimed_at else None,
            'lease_expires_at': self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            'state': self.state.value if isinstance(self.state, ClaimState) else str(self.state),
            'metadata': dict(self.metadata),
        }


@dataclass(frozen=True)
class JobReport:
    report_id: str
    job_id: Optional[str]
    plan_version: Optional[str]
    assignment_id: Optional[str]
    success: bool
    segment: Optional[Segment] = None
    reported_at: Optional[datetime] = None
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    segment: Segment
    accepted_ranges: tuple[SampleRange, ...]
    plan_version: Optional[str] = None
    assignment_id: Optional[str] = None
    claim_id: Optional[str] = None
    received_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted_segment(self) -> Segment:
        if len(self.accepted_ranges) == 1:
            return Segment(self.segment.site, self.segment.source_batch, self.accepted_ranges[0])
        raise SchedulingError('receipt contains multiple accepted ranges')


@dataclass(frozen=True)
class ReceiptResult:
    receipt: Receipt
    new_ranges: tuple[SampleRange, ...]
    duplicate_ranges: tuple[SampleRange, ...]
    duplicate_delivery: bool = False

    @property
    def accepted_new(self) -> tuple[SampleRange, ...]:
        return self.new_ranges

    @property
    def newly_accepted(self) -> tuple[SampleRange, ...]:
        return self.new_ranges


@dataclass(frozen=True)
class ClaimResult:
    claim: Optional[Claim]
    requested: Segment
    work_ranges: tuple[SampleRange, ...]
    reason: Optional[str] = None

    @property
    def needed(self) -> bool:
        return bool(self.work_ranges)

    @property
    def empty(self) -> bool:
        return not self.work_ranges

    def __bool__(self) -> bool:
        return self.needed

    @property
    def residual(self) -> tuple[SampleRange, ...]:
        return self.work_ranges

    @property
    def id(self) -> Optional[str]:
        return None if self.claim is None else self.claim.claim_id

    @property
    def claim_id(self) -> Optional[str]:
        return None if self.claim is None else self.claim.claim_id

    @property
    def version(self) -> Optional[str]:
        return None if self.claim is None else self.claim.plan_version

    def __getattr__(self, name: str) -> Any:
        claim = object.__getattribute__(self, 'claim')
        if claim is None:
            raise AttributeError(name)
        return getattr(claim, name)


class ScheduleLedger:
    """In-memory source of truth for versioned collection and settlement state."""

    def __init__(self) -> None:
        self.plans: dict[str, PlanVersion] = {}
        self.current_version: Optional[str] = None
        self.superseded_versions: set[str] = set()
        self.invalidated_versions: set[str] = set()
        self.claims: dict[str, Claim] = {}
        self.claim_order: list[str] = []
        self.receipts: dict[str, Receipt] = {}
        self.receipt_order: list[str] = []
        self.job_reports: dict[str, JobReport] = {}
        self.job_report_order: list[str] = []
        self._settled: dict[SegmentKey, list[SampleRange]] = {}
        self._delivered: dict[SegmentKey, list[SampleRange]] = {}
        self._receipt_fingerprints: dict[str, str] = {}

    def publish_plan(
        self,
        plan: Union[PlanVersion, str, int],
        assignments: Optional[Sequence[Any]] = None,
        *,
        activate: bool = True,
        invalidate_previous: bool = True,
        **kwargs: Any,
    ) -> PlanVersion:
        if isinstance(plan, PlanVersion):
            plan_version = plan
        else:
            plan_version = PlanVersion.build(plan, assignments, **kwargs)
        if plan_version.version in self.plans:
            existing = self.plans[plan_version.version]
            if existing.to_dict() != plan_version.to_dict():
                raise SchedulingError(f'plan version {plan_version.version} already exists with different manifest')
            return existing
        previous_version = self.current_version
        if activate and previous_version is not None and plan_version.supersedes is None:
            object.__setattr__(plan_version, 'supersedes', previous_version)
        self.plans[plan_version.version] = plan_version
        if activate:
            self.current_version = plan_version.version
            if previous_version is not None and previous_version != plan_version.version:
                if invalidate_previous:
                    self.invalidate_version(previous_version)
                else:
                    self.superseded_versions.add(previous_version)
        return plan_version

    activate_plan = publish_plan
    add_plan = publish_plan
    create_plan = publish_plan
    update_plan = publish_plan
    publish = publish_plan

    def current_plan(self) -> PlanVersion:
        if self.current_version is None:
            raise UnknownPlanVersion('no active plan version')
        return self.plans[self.current_version]

    def claim(
        self,
        plan_version_or_claim: Any = None,
        assignment_id: Optional[str] = None,
        *,
        segment: Any = None,
        site: Optional[str] = None,
        source_batch: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        worker_id: Optional[str] = None,
        job_id: Optional[str] = None,
        claim_id: Optional[str] = None,
        now: Optional[datetime] = None,
        lease_expires_at: Optional[datetime] = None,
        force: bool = False,
        **metadata: Any,
    ) -> ClaimResult:
        plan_version, assignment = self._resolve_assignment(
            plan_version_or_claim,
            assignment_id,
            segment=segment,
            site=site,
            source_batch=source_batch,
            start=start,
            end=end,
        )

        if not force and self.current_version is not None and plan_version != self.current_version:
            raise StalePlanVersion(
                f'plan {plan_version} is not active; it cannot claim new segments after {self.current_version}'
            )
        if not assignment.is_due(now):
            raise ScheduleNotDue(f'assignment {assignment.assignment_id} is not due at {now!r}')

        existing_claim = self._find_claim(plan_version, assignment.assignment_id, worker_id, job_id)
        if existing_claim is not None:
            return self._claim_result(existing_claim)

        claim_identifier = claim_id or job_id or f'{plan_version}:{assignment.assignment_id}'
        if claim_identifier in self.claims:
            claim_identifier = f'{claim_identifier}:{len(self.claim_order) + 1}'
        claim = Claim(
            claim_id=claim_identifier,
            plan_version=plan_version,
            assignment_id=assignment.assignment_id,
            segment=assignment.segment,
            worker_id=worker_id,
            job_id=job_id,
            claimed_at=now,
            lease_expires_at=lease_expires_at,
            metadata=metadata,
        )
        self.claims[claim.claim_id] = claim
        self.claim_order.append(claim.claim_id)
        return self._claim_result(claim)

    claim_assignment = claim
    acquire = claim

    def claim_lease(self, *args: Any, **kwargs: Any) -> Optional[Claim]:
        return self.claim(*args, **kwargs).claim

    def claim_or_none(self, *args: Any, **kwargs: Any) -> Optional[ClaimResult]:
        try:
            result = self.claim(*args, **kwargs)
        except (StalePlanVersion, ScheduleNotDue):
            return None
        return result if result.needed else None

    try_claim = claim_or_none

    def claim_next_window(self, *args: Any, max_length: Optional[int] = None, **kwargs: Any) -> Optional[SampleRange]:
        result = self.claim(*args, **kwargs)
        if not result.work_ranges:
            return None
        window = result.work_ranges[0]
        if max_length is not None and window.length > max_length:
            return SampleRange(window.start, window.start + int(max_length))
        return window

    def claim_due(self, *, now: Optional[datetime] = None, **kwargs: Any) -> list[ClaimResult]:
        plan = self.current_plan()
        results = []
        for assignment in plan.assignments:
            if assignment.is_due(now):
                try:
                    results.append(self.claim(plan.version, assignment.assignment_id, now=now, **kwargs))
                except StalePlanVersion:
                    continue
        return results

    def record_job_report(
        self,
        report_id: Optional[str] = None,
        *,
        job_id: Optional[str] = None,
        plan_version: Optional[Union[str, int]] = None,
        assignment_id: Optional[str] = None,
        success: bool = True,
        segment: Any = None,
        site: Optional[str] = None,
        source_batch: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        reported_at: Optional[datetime] = None,
        result: Any = None,
        **metadata: Any,
    ) -> JobReport:
        resolved_segment = _optional_segment(
            segment, site=site, source_batch=source_batch, start=start, end=end
        )
        identifier = report_id or job_id or f'job-report:{len(self.job_report_order) + 1}'
        if identifier in self.job_reports:
            return self.job_reports[identifier]
        report = JobReport(
            report_id=identifier,
            job_id=job_id,
            plan_version=None if plan_version is None else str(plan_version),
            assignment_id=assignment_id,
            success=success,
            segment=resolved_segment,
            reported_at=reported_at,
            result=result,
            metadata=metadata,
        )
        self.job_reports[identifier] = report
        self.job_report_order.append(identifier)
        if success and resolved_segment is not None:
            self._delivered[resolved_segment.key] = merge_ranges(
                [*self._delivered.get(resolved_segment.key, []), resolved_segment.range]
            )
        return report

    report_job = record_job_report
    job_succeeded = lambda self, job_id=None, **kwargs: self.record_job_report(  # noqa: E731
        job_id=job_id, success=True, **kwargs
    )
    mark_job_succeeded = job_succeeded

    def record_delivery(
        self,
        delivery_id: Optional[str] = None,
        segment: Any = None,
        *args: Any,
        ranges: Optional[Sequence[Any]] = None,
        site: Optional[str] = None,
        source_batch: Optional[str] = None,
        batch: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        **metadata: Any,
    ) -> JobReport:
        resolved_segment = _optional_segment(
            segment, site=site, source_batch=source_batch or batch, start=start, end=end
        )
        report = self.record_job_report(
            delivery_id,
            success=True,
            segment=resolved_segment,
            site=site,
            source_batch=source_batch or batch,
            start=start,
            end=end,
            **metadata,
        )
        if resolved_segment is not None and ranges:
            key = resolved_segment.key
            parsed = [SampleRange.coerce(item) for item in ranges]
            self._delivered[key] = merge_ranges([*self._delivered.get(key, []), *parsed])
        return report

    mark_delivered = record_delivery
    report_delivery = record_delivery
    deliver = record_delivery
    record_submission = record_delivery

    def record_receipt(
        self,
        receipt_id: Optional[str] = None,
        accepted: Any = None,
        *,
        segment: Any = None,
        ranges: Optional[Sequence[Any]] = None,
        accepted_ranges: Optional[Sequence[Any]] = None,
        site: Optional[str] = None,
        source_batch: Optional[str] = None,
        batch: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        plan_version: Optional[Union[str, int]] = None,
        assignment_id: Optional[str] = None,
        claim_id: Optional[str] = None,
        received_at: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        **metadata: Any,
    ) -> ReceiptResult:
        raw_ranges: list[Any] = []
        if accepted is not None:
            if isinstance(accepted, Segment):
                segment = accepted
                raw_ranges.append(accepted.range)
            elif isinstance(accepted, SampleRange):
                raw_ranges.append(accepted)
            elif isinstance(accepted, dict) and (
                'ranges' in accepted or 'accepted_ranges' in accepted or 'segments' in accepted
            ):
                raw_ranges.extend(accepted.get('ranges') or accepted.get('accepted_ranges') or accepted.get('segments'))
            elif isinstance(accepted, (list, tuple)):
                raw_ranges.extend(accepted)
            else:
                raw_ranges.append(accepted)
        for value in (ranges or []) + (accepted_ranges or []):
            raw_ranges.append(value)

        claim = self.claims.get(claim_id) if claim_id is not None else None
        if claim is not None:
            segment = segment or claim.segment
            plan_version = plan_version or claim.plan_version
            assignment_id = assignment_id or claim.assignment_id
            if not raw_ranges and start is None and end is None:
                raw_ranges.append(claim.range)

        resolved_segment = _optional_segment(
            segment if segment is not None else (raw_ranges[0] if raw_ranges else None),
            site=site,
            source_batch=source_batch or batch,
            start=start,
            end=end,
        )
        if resolved_segment is None:
            raise InvalidInterval('receipt requires an accepted segment')

        parsed_ranges = [SampleRange.coerce(item, end if isinstance(item, int) else None) for item in raw_ranges]
        if not parsed_ranges and start is not None and end is not None:
            parsed_ranges = [SampleRange(start, end)]
        if not parsed_ranges:
            parsed_ranges = [resolved_segment.range]
        normalized_ranges = tuple(merge_ranges(parsed_ranges))

        identifier = receipt_id or idempotency_key or hashlib.sha1(
            json.dumps(
                [
                    site,
                    source_batch or batch,
                    resolved_segment.start,
                    resolved_segment.end,
                    [item.as_tuple() for item in normalized_ranges],
                    plan_version,
                    assignment_id,
                ],
                default=str,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        fingerprint = _fingerprint_receipt(resolved_segment, normalized_ranges, plan_version, assignment_id)
        if identifier in self.receipts:
            existing = self.receipts[identifier]
            if self._receipt_fingerprints[identifier] != fingerprint:
                raise SchedulingError(f'receipt id {identifier!r} was reused with different payload')
            new_ranges: list[SampleRange] = []
            duplicate_ranges = list(normalized_ranges)
            duplicate_delivery = True
        else:
            before = list(self._settled.get(resolved_segment.key, []))
            covered = merge_ranges([*before, *normalized_ranges])
            new_ranges = _difference(normalized_ranges, before)
            duplicate_ranges = _intersection(normalized_ranges, before)
            self._settled[resolved_segment.key] = covered
            receipt = Receipt(
                receipt_id=identifier,
                segment=resolved_segment,
                accepted_ranges=normalized_ranges,
                plan_version=None if plan_version is None else str(plan_version),
                assignment_id=assignment_id,
                claim_id=claim_id,
                received_at=received_at,
                metadata=metadata,
            )
            self.receipts[identifier] = receipt
            self._receipt_fingerprints[identifier] = fingerprint
            self.receipt_order.append(identifier)
            if claim_id is not None and claim_id in self.claims and not new_ranges:
                pass
            duplicate_delivery = False

        return ReceiptResult(
            receipt=self.receipts[identifier],
            new_ranges=tuple(merge_ranges(new_ranges)),
            duplicate_ranges=tuple(merge_ranges(duplicate_ranges)),
            duplicate_delivery=duplicate_delivery,
        )

    acknowledge = record_receipt
    ack = record_receipt
    receive = record_receipt
    confirm_ingestion = record_receipt
    settle = record_receipt
    add_receipt = record_receipt
    record_ack = record_receipt
    mark_accepted = record_receipt
    accept_ranges = record_receipt
    warehouse_ack = record_receipt

    def invalidate_version(self, plan_version: Union[str, int]) -> list[Claim]:
        version = str(plan_version)
        self.invalidated_versions.add(version)
        self.superseded_versions.add(version)
        invalidated: list[Claim] = []
        for claim_id, claim in list(self.claims.items()):
            if claim.plan_version == version and claim.state == ClaimState.active:
                updated = replace(claim, state=ClaimState.superseded)
                self.claims[claim_id] = updated
                invalidated.append(updated)
        return invalidated

    def release_claim(
        self, claim_id: str, state: Union[ClaimState, str] = ClaimState.released, *, now: Optional[datetime] = None
    ) -> Claim:
        claim = self.claims[claim_id]
        new_state = ClaimState(state) if not isinstance(state, ClaimState) else state
        updated = replace(claim, state=new_state)
        self.claims[claim_id] = updated
        return updated

    def complete_claim(self, claim_id: str) -> Claim:
        return self.release_claim(claim_id, ClaimState.completed)

    def expire_due_claims(self, now: Optional[datetime] = None) -> list[Claim]:
        expired = []
        for claim_id, claim in list(self.claims.items()):
            if claim.state == ClaimState.active and not claim.is_live(now):
                expired.append(self.release_claim(claim_id, ClaimState.expired, now=now))
        return expired

    def settled_ranges(self, site: str = '_default', source_batch: str = '_default') -> list[SampleRange]:
        return [item for item in self._settled.get((site, source_batch), [])]

    accepted_ranges = settled_ranges
    covered_ranges = settled_ranges
    settled_intervals = settled_ranges
    accepted_intervals = settled_ranges
    covered_intervals = settled_ranges

    def settled_segments(self, **_kwargs: Any) -> list[Segment]:
        return [
            Segment(site, batch, interval)
            for (site, batch), intervals in sorted(self._settled.items())
            for interval in intervals
        ]

    accepted_segments = settled_segments
    completed_segments = settled_segments

    def delivered_segments(self, **_kwargs: Any) -> list[Segment]:
        return [
            Segment(site, batch, interval)
            for (site, batch), intervals in sorted(self._delivered.items())
            for interval in intervals
        ]

    def claims_for(
        self,
        *,
        active_only: bool = True,
        live_only: bool = False,
        now: Optional[datetime] = None,
        plan_version: Optional[Union[str, int]] = None,
    ) -> list[Claim]:
        result = []
        for claim_id in self.claim_order:
            claim = self.claims[claim_id]
            if plan_version is not None and claim.plan_version != str(plan_version):
                continue
            if active_only and not claim.active:
                continue
            if live_only and not claim.is_live(now):
                continue
            result.append(claim)
        return result

    active_claims = claims_for

    def held_segments(
        self,
        *,
        unsettled_only: bool = False,
        now: Optional[datetime] = None,
        live_only: bool = False,
        include_superseded: bool = True,
    ) -> list[Segment]:
        result: list[Segment] = []
        for claim in self.claims_for(active_only=False, now=now):
            if claim.state == ClaimState.active:
                pass
            elif include_superseded and claim.state == ClaimState.superseded:
                pass
            else:
                continue
            if live_only and not claim.is_live(now):
                continue
            intervals = [claim.segment.range]
            if unsettled_only:
                intervals = claim.segment.range.subtract(
                    self._settled.get(claim.segment.key, [])
                )
            result.extend(Segment(claim.site, claim.source_batch, interval) for interval in intervals)
        return merge_segments(result)

    def stale_held_segments(self, *, unsettled_only: bool = True) -> list[Segment]:
        return self.held_segments(unsettled_only=unsettled_only, include_superseded=True)

    def active_held_segments(self, **kwargs: Any) -> list[Segment]:
        kwargs['include_superseded'] = False
        return self.held_segments(**kwargs)

    in_flight_segments = held_segments
    claimed_segments = held_segments

    def pending_segments(
        self,
        plan: Union[PlanVersion, str, int, None] = None,
        *,
        include_held: bool = False,
        now: Optional[datetime] = None,
    ) -> list[Segment]:
        plan_version = self._coerce_plan_ref(plan) if plan is not None else self.current_plan()
        if isinstance(plan_version, str):
            plan_obj = self.plans[plan_version]
        else:
            plan_obj = plan_version
        result: list[Segment] = []
        for assignment in plan_obj.assignments:
            if now is not None and not assignment.is_due(now):
                continue
            intervals = assignment.range.subtract(self._settled.get(assignment.segment.key, []))
            if not include_held:
                held = [
                    claim.segment.range
                    for claim in self.claims_for(plan_version=plan_obj.version, live_only=False)
                    if claim.is_live(now)
                    and claim.segment.key == assignment.segment.key
                    and claim.assignment_id == assignment.assignment_id
                ]
                intervals = _difference(intervals, held)
            result.extend(
                Segment(assignment.site, assignment.source_batch, interval) for interval in intervals
            )
        return merge_segments(result)

    unfinished_segments = pending_segments

    def needs_reschedule(
        self,
        plan: Union[PlanVersion, str, int, None] = None,
        *,
        now: Optional[datetime] = None,
        include_unclaimed: bool = True,
    ) -> list[Segment]:
        """Return unsettled current-plan intervals not held by a live explicit claim."""

        target_plan = self.current_plan() if plan is None else self._coerce_plan_object(plan)
        held = self.held_segments(unsettled_only=False, now=now, live_only=True)
        held_by_key = _group_segments(held)
        result: list[Segment] = []
        for assignment in target_plan.assignments:
            if now is not None and not assignment.is_due(now):
                continue
            key = assignment.segment.key
            intervals = assignment.range.subtract(self._settled.get(key, []))
            intervals = _difference(intervals, held_by_key.get(key, []))
            if not include_unclaimed:
                has_claim = any(
                    claim.assignment_id == assignment.assignment_id and claim.is_live(now)
                    for claim in self.claims_for(plan_version=target_plan.version)
                )
                if not has_claim:
                    continue
            result.extend(Segment(assignment.site, assignment.source_batch, interval) for interval in intervals)
        return merge_segments(result)

    reschedule_segments = needs_reschedule
    orphaned_segments = needs_reschedule

    def remaining_ranges(
        self,
        start: Optional[int] = None,
        end: Optional[int] = None,
        *,
        site: str = '_default',
        source_batch: str = '_default',
        batch: Optional[str] = None,
        interval: Any = None,
    ) -> list[SampleRange]:
        if interval is not None:
            target = SampleRange.coerce(interval, end if isinstance(interval, int) else None)
        elif start is not None and end is not None:
            target = SampleRange(start, end)
        else:
            raise InvalidInterval('remaining_ranges requires start/end or interval')
        return target.subtract(self._settled.get((site, source_batch or batch or '_default'), []))

    def next_window(
        self,
        start: Optional[int] = None,
        end: Optional[int] = None,
        *,
        site: str = '_default',
        source_batch: str = '_default',
        batch: Optional[str] = None,
        max_length: Optional[int] = None,
        interval: Any = None,
    ) -> Optional[SampleRange]:
        remaining = self.remaining_ranges(
            start, end, site=site, source_batch=source_batch, batch=batch, interval=interval
        )
        if not remaining:
            return None
        window = remaining[0]
        if max_length is not None and window.length > max_length:
            return SampleRange(window.start, window.start + int(max_length))
        return window

    def create_reschedule_plan(
        self,
        version: Union[str, int],
        *,
        source_plan: Union[PlanVersion, str, int, None] = None,
        publish: bool = True,
        now: Optional[datetime] = None,
        scheduled_at: Optional[datetime] = None,
        assignment_prefix: str = 'reschedule',
    ) -> PlanVersion:
        segments = self.needs_reschedule(source_plan, now=now)
        assignments = [
            {
                'id': f'{assignment_prefix}-{index}',
                'site': segment.site,
                'source_batch': segment.source_batch,
                'start': segment.start,
                'end': segment.end,
                'scheduled_at': scheduled_at,
            }
            for index, segment in enumerate(segments)
        ]
        plan = PlanVersion.build(
            version,
            assignments,
            supersedes=self.current_version if source_plan is None else self._coerce_plan_ref(source_plan),
        )
        if publish:
            self.publish_plan(plan)
        return plan

    def coverage(self) -> dict[SegmentKey, list[SampleRange]]:
        return {key: [item for item in intervals] for key, intervals in sorted(self._settled.items())}

    def view(self, *, now: Optional[datetime] = None) -> 'ScheduleView':
        return ScheduleView(
            settled=self.settled_segments(),
            delivered=self.delivered_segments(),
            held=self.held_segments(unsettled_only=True, now=now),
            stale_held=self.stale_held_segments(unsettled_only=True),
            claims=self.claims_for(active_only=False),
            needs_reschedule=self.needs_reschedule(now=now),
            pending=self.pending_segments(include_held=True, now=now),
            current_version=self.current_version,
        )

    snapshot = view
    status = view

    def list_settled(self) -> list[Segment]:
        return self.settled_segments()

    def list_accepted(self) -> list[Segment]:
        return self.accepted_segments()

    def list_delivered(self) -> list[Segment]:
        return self.delivered_segments()

    def list_held(self, **kwargs: Any) -> list[Segment]:
        return self.held_segments(**kwargs)

    def list_claims(self, **kwargs: Any) -> list[Claim]:
        return self.claims_for(**kwargs)

    def list_pending(self, **kwargs: Any) -> list[Segment]:
        return self.pending_segments(**kwargs)

    def list_needs_reschedule(self, **kwargs: Any) -> list[Segment]:
        return self.needs_reschedule(**kwargs)

    get_settled_segments = settled_segments
    get_accepted_segments = accepted_segments
    get_delivered_segments = delivered_segments
    get_held_segments = held_segments
    get_pending_segments = pending_segments
    get_needs_reschedule = needs_reschedule
    get_view = view
    get_status = view

    def segments(self, state: str = 'all', **kwargs: Any) -> Any:
        normalized = state.replace('-', '_').lower()
        aliases = {
            'accepted': 'settled',
            'complete': 'settled',
            'completed': 'settled',
            'covered': 'settled',
            'in_flight': 'held',
            'claimed': 'held',
            'leased': 'held',
            'to_reschedule': 'needs_reschedule',
            'replan': 'needs_reschedule',
            'unfinished': 'pending',
            'outstanding': 'pending',
        }
        normalized = aliases.get(normalized, normalized)
        methods = {
            'settled': self.settled_segments,
            'delivered': self.delivered_segments,
            'held': self.held_segments,
            'needs_reschedule': self.needs_reschedule,
            'pending': self.pending_segments,
        }
        if normalized == 'all':
            return self.view(**kwargs)
        if normalized not in methods:
            raise SchedulingError(f'unknown segment state {state!r}')
        return methods[normalized](**kwargs)

    def summary(self, *, now: Optional[datetime] = None) -> dict[str, int]:
        view = self.view(now=now)
        return {
            'settled_segments': len(view.settled),
            'delivered_segments': len(view.delivered),
            'held_segments': len(view.held),
            'needs_reschedule_segments': len(view.needs_reschedule),
            'pending_segments': len(view.pending),
            'claims': len(view.claims),
            'active_claims': len([claim for claim in view.claims if claim.state == ClaimState.active]),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            'current_version': self.current_version,
            'plans': {version: plan.to_dict() for version, plan in sorted(self.plans.items())},
            'claims': {claim_id: claim.to_dict() for claim_id, claim in self.claims.items()},
            'receipts': {receipt_id: _receipt_to_dict(receipt) for receipt_id, receipt in self.receipts.items()},
            'job_reports': {report_id: _job_report_to_dict(report) for report_id, report in self.job_reports.items()},
            'settled': {
                f'{site}\u0000{bath}': [interval.as_tuple() for interval in intervals]
                for (site, bath), intervals in self._settled.items()
            },
            'delivered': {
                f'{site}\u0000{bath}': [interval.as_tuple() for interval in intervals]
                for (site, bath), intervals in self._delivered.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ScheduleLedger':
        ledger = cls()
        for raw_plan in data.get('plans', {}).values():
            assignments = []
            for raw_assignment in raw_plan.get('assignments', []):
                segment_data = raw_assignment['segment']
                scheduled = _parse_datetime(raw_assignment.get('scheduled_at'))
                assignments.append(
                    Assignment(
                        assignment_id=raw_assignment['assignment_id'],
                        plan_version=raw_assignment['plan_version'],
                        segment=Segment.coerce(segment_data),
                        scheduled_at=scheduled,
                        metadata=raw_assignment.get('metadata', {}),
                    )
                )
            plan = PlanVersion(
                version=raw_plan['version'],
                assignments=tuple(assignments),
                published_at=_parse_datetime(raw_plan.get('published_at')),
                supersedes=raw_plan.get('supersedes'),
                metadata=raw_plan.get('metadata', {}),
            )
            ledger.plans[plan.version] = plan
        ledger.current_version = data.get('current_version')
        for claim_id, raw_claim in data.get('claims', {}).items():
            ledger.claims[claim_id] = Claim(
                claim_id=claim_id,
                plan_version=raw_claim['plan_version'],
                assignment_id=raw_claim['assignment_id'],
                segment=Segment.coerce(raw_claim['segment']),
                worker_id=raw_claim.get('worker_id'),
                job_id=raw_claim.get('job_id'),
                claimed_at=_parse_datetime(raw_claim.get('claimed_at')),
                lease_expires_at=_parse_datetime(raw_claim.get('lease_expires_at')),
                state=ClaimState(raw_claim.get('state', ClaimState.active.value)),
                metadata=raw_claim.get('metadata', {}),
            )
            ledger.claim_order.append(claim_id)
        for receipt_id, raw_receipt in data.get('receipts', {}).items():
            segment = Segment.coerce(raw_receipt['segment'])
            ranges = tuple(SampleRange.coerce(item) for item in raw_receipt['accepted_ranges'])
            receipt = Receipt(
                receipt_id=receipt_id,
                segment=segment,
                accepted_ranges=ranges,
                plan_version=raw_receipt.get('plan_version'),
                assignment_id=raw_receipt.get('assignment_id'),
                claim_id=raw_receipt.get('claim_id'),
                received_at=_parse_datetime(raw_receipt.get('received_at')),
                metadata=raw_receipt.get('metadata', {}),
            )
            ledger.receipts[receipt_id] = receipt
            ledger.receipt_order.append(receipt_id)
            ledger._receipt_fingerprints[receipt_id] = _fingerprint_receipt(
                segment, ranges, receipt.plan_version, receipt.assignment_id
            )
            ledger._settled[segment.key] = merge_ranges(
                [*ledger._settled.get(segment.key, []), *ranges]
            )
        for key, intervals in data.get('settled', {}).items():
            site, batch = key.split('\u0000', 1)
            ledger._settled[(site, batch)] = merge_ranges(SampleRange(*item) for item in intervals)
        for key, intervals in data.get('delivered', {}).items():
            site, batch = key.split('\u0000', 1)
            ledger._delivered[(site, batch)] = merge_ranges(SampleRange(*item) for item in intervals)
        return ledger

    def merge(self, other: 'ScheduleLedger') -> 'ScheduleLedger':
        """Merge authoritative receipts from another worker/ledger view."""

        merged = ScheduleLedger.from_dict(self.to_dict())
        for receipt_id in other.receipt_order:
            receipt = other.receipts[receipt_id]
            merged.record_receipt(
                receipt_id=receipt.receipt_id,
                accepted=list(receipt.accepted_ranges),
                segment=receipt.segment,
                plan_version=receipt.plan_version,
                assignment_id=receipt.assignment_id,
                claim_id=receipt.claim_id,
                received_at=receipt.received_at,
                **receipt.metadata,
            )
        for report_id in other.job_report_order:
            report = other.job_reports[report_id]
            merged.record_job_report(
                report_id=report.report_id,
                job_id=report.job_id,
                plan_version=report.plan_version,
                assignment_id=report.assignment_id,
                success=report.success,
                segment=report.segment,
                reported_at=report.reported_at,
                result=report.result,
                **report.metadata,
            )
        return merged

    def _resolve_assignment(
        self,
        plan_version_or_claim: Any,
        assignment_id: Optional[str],
        **segment_kwargs: Any,
    ) -> tuple[str, Assignment]:
        if isinstance(plan_version_or_claim, PlanVersion):
            plan = plan_version_or_claim
            if assignment_id is None:
                raise UnknownAssignment('assignment_id is required')
            return plan.version, plan.assignment(assignment_id)
        if isinstance(plan_version_or_claim, Assignment):
            return plan_version_or_claim.plan_version, plan_version_or_claim
        if assignment_id is None and isinstance(plan_version_or_claim, str) and ':' in plan_version_or_claim:
            maybe_version, maybe_assignment = plan_version_or_claim.split(':', 1)
            if maybe_version in self.plans:
                return maybe_version, self.plans[maybe_version].assignment(maybe_assignment)
        plan_ref = self._coerce_plan_ref(plan_version_or_claim)
        plan = self.plans[plan_ref]
        if assignment_id is not None:
            return plan.version, plan.assignment(assignment_id)
        segment = _optional_segment(segment_kwargs.pop('segment', None), **segment_kwargs)
        if segment is None:
            raise UnknownAssignment('assignment_id or segment is required')
        matches = [
            assignment
            for assignment in plan.assignments
            if assignment.segment.key == segment.key and assignment.range == segment.range
        ]
        if len(matches) != 1:
            covering = [
                assignment
                for assignment in plan.assignments
                if assignment.segment.key == segment.key
                and assignment.range.contains(segment.range)
            ]
            if len(covering) == 1:
                return plan.version, covering[0]
            raise UnknownAssignment(f'cannot resolve assignment for {segment.to_dict()}')
        return plan.version, matches[0]

    def _coerce_plan_ref(self, value: Any) -> str:
        if value is None:
            return self.current_plan().version
        if isinstance(value, PlanVersion):
            return value.version
        identifier = str(value)
        if identifier not in self.plans:
            raise UnknownPlanVersion(identifier)
        return identifier

    def _coerce_plan_object(self, value: Union[PlanVersion, str, int]) -> PlanVersion:
        if isinstance(value, PlanVersion):
            return value
        return self.plans[self._coerce_plan_ref(value)]

    def _find_claim(
        self,
        plan_version: str,
        assignment_id: str,
        worker_id: Optional[str],
        job_id: Optional[str],
    ) -> Optional[Claim]:
        generic_match: Optional[Claim] = None
        for claim_id in reversed(self.claim_order):
            claim = self.claims[claim_id]
            if claim.plan_version != plan_version or claim.assignment_id != assignment_id:
                continue
            if generic_match is None:
                generic_match = claim
            if job_id is not None and claim.job_id == job_id:
                return claim
            if worker_id is not None and claim.worker_id == worker_id:
                return claim
        if worker_id is None and job_id is None:
            return generic_match
        return None

    def _claim_result(self, claim: Claim) -> ClaimResult:
        work = tuple(claim.segment.range.subtract(self._settled.get(claim.segment.key, [])))
        return ClaimResult(claim=claim, requested=claim.segment, work_ranges=work)


@dataclass(frozen=True)
class ScheduleView:
    settled: list[Segment]
    delivered: list[Segment]
    held: list[Segment]
    needs_reschedule: list[Segment]
    pending: list[Segment]
    current_version: Optional[str]
    claims: list[Claim] = field(default_factory=list)
    stale_held: list[Segment] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        settled = [segment.to_dict() for segment in self.settled]
        delivered = [segment.to_dict() for segment in self.delivered]
        held = [segment.to_dict() for segment in self.held]
        stale_held = [segment.to_dict() for segment in self.stale_held]
        needs = [segment.to_dict() for segment in self.needs_reschedule]
        pending = [segment.to_dict() for segment in self.pending]
        claims = [claim.to_dict() for claim in self.claims]
        return {
            'current_version': self.current_version,
            'settled': settled,
            'accepted': settled,
            'completed': settled,
            'delivered': delivered,
            'held': held,
            'in_flight': held,
            'claimed': held,
            'stale_held': stale_held,
            'needs_reschedule': needs,
            'to_reschedule': needs,
            'pending': pending,
            'outstanding': pending,
            'claims': claims,
        }

    @property
    def accepted(self) -> list[Segment]:
        return self.settled

    @property
    def in_flight(self) -> list[Segment]:
        return self.held

    @property
    def claimed(self) -> list[Segment]:
        return self.held

    @property
    def outstanding(self) -> list[Segment]:
        return self.pending

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self.as_dict().keys()


class CollectionScheduler(ScheduleLedger):
    """Friendly alias for callers that treat the object as the scheduler side."""


class ScheduleManager(ScheduleLedger):
    pass


class PlanManager(ScheduleLedger):
    pass


class WindowPlanner(ScheduleLedger):
    pass


class VersionedScheduler(ScheduleLedger):
    pass


class PlanRegistry(ScheduleLedger):
    pass


class SettlementLedger(ScheduleLedger):
    pass


def merge_ranges(ranges: Iterable[SampleRange]) -> list[SampleRange]:
    parsed = sorted((SampleRange.coerce(item) for item in ranges), key=lambda item: (item.start, item.end))
    merged: list[SampleRange] = []
    for interval in parsed:
        if not merged or interval.start > merged[-1].end:
            merged.append(interval)
        elif interval.end > merged[-1].end:
            merged[-1] = SampleRange(merged[-1].start, interval.end)
    return merged


def merge_segments(segments: Iterable[Segment]) -> list[Segment]:
    grouped: dict[SegmentKey, list[SampleRange]] = {}
    for segment in segments:
        grouped.setdefault(segment.key, []).append(segment.range)
    result: list[Segment] = []
    for (site, batch), ranges in sorted(grouped.items()):
        result.extend(Segment(site, batch, interval) for interval in merge_ranges(ranges))
    return result


def progress_key(
    source_batch: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
    *,
    site: str = '_default',
    range: Any = None,
    segment: Any = None,
) -> str:
    """Stable progress identity independent of local trigger wall time."""

    resolved = _optional_segment(
        segment if segment is not None else range,
        site=site,
        source_batch=source_batch,
        start=start,
        end=end,
    )
    if resolved is None:
        raise InvalidInterval('progress_key requires source_batch and a half-open sample range')
    raw = f'{resolved.site}\u0000{resolved.source_batch}\u0000{resolved.start}\u0000{resolved.end}'
    return hashlib.sha1(raw.encode()).hexdigest()


def collection_job_id(
    source_batch: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
    *,
    site: str = '_default',
    prefix: str = 'collect',
    **kwargs: Any,
) -> str:
    return f'{prefix}:{site}:{source_batch}:{start}:{end}:{progress_key(source_batch, start, end, site=site, **kwargs)}'


def trigger_identity(scheduled_at: datetime, *, occurrence: Optional[int] = None) -> dict[str, Any]:
    """Identify a local trigger while preserving DST fallback occurrences."""

    offset = scheduled_at.utcoffset()
    return {
        'local_time': scheduled_at.replace(tzinfo=None).isoformat(),
        'fold': getattr(scheduled_at, 'fold', 0),
        'utc_offset_s': None if offset is None else int(offset.total_seconds()),
        'unix_us': int(scheduled_at.timestamp() * 1_000_000) if scheduled_at.tzinfo else None,
        'occurrence': occurrence,
    }


def dst_trigger_keys(scheduled_at: datetime, **progress_kwargs: Any) -> tuple[str, str]:
    trigger = trigger_identity(scheduled_at)
    return json.dumps(trigger, sort_keys=True), progress_key(**progress_kwargs)


idempotency_key = progress_key
window_progress_key = progress_key
sample_progress_key = progress_key
window_key = progress_key
window_job_id = collection_job_id
job_id_for_window = collection_job_id
trigger_key = trigger_identity


def partition_interval(
    target: Any,
    *,
    accepted: Iterable[Any] = (),
    held: Iterable[Any] = (),
    site: str = '_default',
    source_batch: str = '_default',
) -> dict[str, list[SampleRange]]:
    interval = SampleRange.coerce(target)
    accepted_ranges = merge_ranges(accepted)
    held_ranges = merge_ranges(held)
    settled_parts = _intersection([interval], accepted_ranges)
    unsettled = interval.subtract(accepted_ranges)
    held_parts = _intersection(unsettled, held_ranges)
    needs = _difference(unsettled, held_ranges)
    return {'accepted': settled_parts, 'held': held_parts, 'needs_reschedule': needs}


def coverage_covers_once(ranges: Iterable[Any]) -> bool:
    parsed = [SampleRange.coerce(item) for item in ranges]
    merged = merge_ranges(parsed)
    covered_length = sum(interval.length for interval in parsed)
    merged_length = sum(interval.length for interval in merged)
    return covered_length == merged_length


def normalize_coverage(ranges: Iterable[Any]) -> list[SampleRange]:
    return merge_ranges(ranges)


union_ranges = merge_ranges
union_intervals = merge_ranges
normalize_intervals = merge_ranges


def subtract_ranges(source: Any, covered: Iterable[Any] = ()) -> list[SampleRange]:
    return SampleRange.coerce(source).subtract(covered)


subtract_intervals = subtract_ranges
difference_ranges = subtract_ranges


def intersect_ranges(left: Iterable[Any], right: Iterable[Any]) -> list[SampleRange]:
    return _intersection(list(left), list(right))


intersect_intervals = intersect_ranges


def _coerce_assignment(raw: Any, *, plan_version: str, index: int) -> Assignment:
    if isinstance(raw, Assignment):
        return raw
    if isinstance(raw, dict):
        assignment_id = str(
            _first(raw, 'assignment_id', 'id', 'name', 'job', 'worker', 'owner', default=f'{plan_version}-{index}')
        )
        scheduled_at = _parse_datetime(
            _first(raw, 'scheduled_at', 'local_time', 'run_at', 'available_at', 'due_at', default=None)
        )
        segment = Segment.coerce(raw)
        metadata = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                'assignment_id',
                'id',
                'name',
                'job',
                'worker',
                'owner',
                'scheduled_at',
                'local_time',
                'run_at',
                'available_at',
                'due_at',
                'site',
                'site_id',
                'source_batch',
                'batch',
                'source_batch_id',
                'start',
                'end',
                'start_sample',
                'end_sample',
            }
        }
        return Assignment(assignment_id, plan_version, segment, scheduled_at, scheduled_at, metadata)
    if isinstance(raw, (tuple, list)):
        if len(raw) == 4:
            site, batch, start, end = raw
            segment = Segment(str(site), str(batch), SampleRange(int(start), int(end)))
            return Assignment(f'{plan_version}-{index}', plan_version, segment)
        if len(raw) == 3:
            batch, start, end = raw
            segment = Segment('_default', str(batch), SampleRange(int(start), int(end)))
            return Assignment(f'{plan_version}-{index}', plan_version, segment)
        if len(raw) == 5:
            assignment_id, site, batch, start, end = raw
            segment = Segment(str(site), str(batch), SampleRange(int(start), int(end)))
            return Assignment(str(assignment_id), plan_version, segment)
    raise SchedulingError(f'cannot parse assignment {raw!r}')


def _optional_segment(value: Any = None, **kwargs: Any) -> Optional[Segment]:
    if value is None and not any(kwargs.get(name) is not None for name in ('start', 'start_sample')):
        return None
    try:
        return Segment.coerce(value, **kwargs)
    except InvalidInterval:
        if kwargs.get('start') is not None and kwargs.get('end') is not None:
            return Segment.coerce(None, **kwargs)
        raise


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _difference(intervals: Sequence[SampleRange], covered: Sequence[SampleRange]) -> list[SampleRange]:
    result: list[SampleRange] = []
    for interval in intervals:
        result.extend(interval.subtract(covered))
    return merge_ranges(result)


def _intersection(intervals: Sequence[SampleRange], covered: Sequence[SampleRange]) -> list[SampleRange]:
    result: list[SampleRange] = []
    for interval in intervals:
        for other in covered:
            overlap = interval.intersection(SampleRange.coerce(other))
            if overlap is not None:
                result.append(overlap)
    return merge_ranges(result)


def _group_segments(segments: Sequence[Segment]) -> dict[SegmentKey, list[SampleRange]]:
    grouped: dict[SegmentKey, list[SampleRange]] = {}
    for segment in segments:
        grouped.setdefault(segment.key, []).append(segment.range)
    return {key: merge_ranges(ranges) for key, ranges in grouped.items()}


def _fingerprint_receipt(
    segment: Segment,
    ranges: Sequence[SampleRange],
    plan_version: Optional[str],
    assignment_id: Optional[str],
) -> str:
    return json.dumps(
        {
            'segment': segment.to_dict(),
            'ranges': [item.as_tuple() for item in ranges],
            'plan_version': plan_version,
            'assignment_id': assignment_id,
        },
        sort_keys=True,
        default=str,
    )


def _receipt_to_dict(receipt: Receipt) -> dict[str, Any]:
    data = asdict(receipt)
    data['segment'] = receipt.segment.to_dict()
    data['accepted_ranges'] = [item.as_tuple() for item in receipt.accepted_ranges]
    if receipt.received_at is not None:
        data['received_at'] = receipt.received_at.isoformat()
    return data


def _job_report_to_dict(report: JobReport) -> dict[str, Any]:
    data = asdict(report)
    if report.segment is not None:
        data['segment'] = report.segment.to_dict()
    if report.reported_at is not None:
        data['reported_at'] = report.reported_at.isoformat()
    return data


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    return datetime.fromisoformat(str(value))


Interval = SampleRange
HalfOpenInterval = SampleRange
SampleInterval = SampleRange
OriginalSampleRange = SampleRange
RawRange = SampleRange
AcceptedRange = SampleRange

RawSegment = Segment
OriginalSegment = Segment
SampleSegment = Segment
BatchSegment = Segment

Window = SampleRange
CollectionWindow = Assignment
ScheduledAssignment = Assignment
ManifestEntry = Assignment

Plan = PlanVersion
SchedulePlan = PlanVersion
PlanManifest = PlanVersion
Manifest = PlanVersion
VersionedPlan = PlanVersion

Scheduler = ScheduleLedger
Schedule = ScheduleLedger
ScheduleCoordinator = ScheduleLedger
CollectionCoordinator = ScheduleLedger
DispatchBoard = ScheduleLedger
VersionedSchedule = VersionedScheduler
ReconciliationLedger = ScheduleLedger

IngestionReceipt = Receipt
WarehouseReceipt = Receipt
SettlementReceipt = Receipt
AcceptedReceipt = Receipt

__all__ = [
    'AcceptedRange',
    'Assignment',
    'BatchSegment',
    'Claim',
    'ClaimResult',
    'ClaimState',
    'CollectionCoordinator',
    'CollectionScheduler',
    'CollectionWindow',
    'DispatchBoard',
    'HalfOpenInterval',
    'Interval',
    'InvalidInterval',
    'IngestionReceipt',
    'JobReport',
    'Manifest',
    'ManifestEntry',
    'OriginalSampleRange',
    'OriginalSegment',
    'Plan',
    'PlanManifest',
    'PlanManager',
    'PlanRegistry',
    'PlanVersion',
    'RawRange',
    'RawSegment',
    'Receipt',
    'ReceiptResult',
    'ReconciliationLedger',
    'SampleRange',
    'SampleSegment',
    'SampleInterval',
    'Schedule',
    'ScheduleCoordinator',
    'ScheduleLedger',
    'ScheduleManager',
    'ScheduleNotDue',
    'SchedulePlan',
    'ScheduleView',
    'ScheduledAssignment',
    'Scheduler',
    'SchedulingError',
    'Segment',
    'SettlementLedger',
    'SettlementReceipt',
    'StalePlanVersion',
    'UnknownAssignment',
    'UnknownPlanVersion',
    'VersionedPlan',
    'VersionedScheduler',
    'WarehouseReceipt',
    'Window',
    'WindowPlanner',
    'collection_job_id',
    'coverage_covers_once',
    'difference_ranges',
    'dst_trigger_keys',
    'idempotency_key',
    'intersect_intervals',
    'intersect_ranges',
    'job_id_for_window',
    'merge_ranges',
    'merge_segments',
    'normalize_coverage',
    'normalize_intervals',
    'partition_interval',
    'progress_key',
    'sample_progress_key',
    'subtract_intervals',
    'subtract_ranges',
    'trigger_identity',
    'trigger_key',
    'union_intervals',
    'union_ranges',
    'window_job_id',
    'window_key',
    'window_progress_key',
]
