"""Planning and execution of multipart objects assembled from byte ranges.

S3 ``UploadPartCopy`` copies one contiguous range of one source object into
one multipart part.  A part that contains bytes from more than one source
(for example a remote range followed by client-local bytes) cannot be copied
server-side and has to be assembled by the client.  This module plans the part
boundaries, decides which already-confirmed parts can be reused, and records
which source bytes and output intervals each step covers.

The model works with abstract byte counts.  :class:`~s3fs.core.S3FileSystem`
uses the S3 minimum and maximum part sizes; tests can use small integers.
"""

from __future__ import annotations

import bisect
import hashlib

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

# S3 limits.  A part other than the last part must be at least 5 MiB and no
# part may exceed 5 GiB.
S3_MIN_PART_SIZE = 5 * 2**20
S3_MAX_PART_SIZE = 5 * 2**30


class MergePlanError(ValueError):
    """Raised when no plan satisfies the part constraints."""


@dataclass(frozen=True)
class SourceRange:
    """One byte range of one source, in target order.

    ``path`` is an S3 path for a remote source.  A client-local source has
    ``path=None`` and keeps its bytes in ``data``.  ``start``/``end`` are
    zero-based, end-exclusive offsets in the source (or in ``data``).

    ``copyable`` says whether S3 may read this range with
    ``UploadPartCopy``.  A non-copyable remote range can still be used in a
    part, but then the client has to read it and upload the assembled bytes.
    """

    path: str | None
    start: int = 0
    end: int | None = None
    version_id: str | None = None
    copyable: bool = True
    data: bytes | None = None

    def __post_init__(self):
        if self.path is None:
            if self.data is None:
                raise ValueError("A local source needs data bytes")
            if self.end is None:
                object.__setattr__(self, "end", len(self.data))
            if self.start < 0 or self.end < self.start:
                raise ValueError("Invalid local source range")
            if self.end > len(self.data):
                raise ValueError("Local source range exceeds its data")
        else:
            if self.start < 0:
                raise ValueError("Source start must be non-negative")
            if self.end is not None and self.end < self.start:
                raise ValueError("Source end must not precede start")

    @property
    def length(self) -> int:
        if self.end is None:
            raise ValueError(
                "A remote source range needs an explicit end; use "
                "S3FileSystem.plan_merge() to resolve a whole object"
            )
        return self.end - self.start

    @property
    def is_resolved(self) -> bool:
        return self.path is None or self.end is not None

    @property
    def is_local(self) -> bool:
        return self.path is None

    def key(self) -> tuple:
        """Identity used when comparing planned and confirmed contents."""
        data = self.data[self.start : self.end] if self.is_local else None
        return (self.path, self.version_id, self.start, self.end, self.copyable, data)

    @classmethod
    def local(
        cls, data: bytes, start: int = 0, end: int | None = None
    ) -> "SourceRange":
        return cls(path=None, start=start, end=end, data=data)


@dataclass(frozen=True)
class PartSlice:
    """The part of ``source`` occupying ``[output_start, output_end)``."""

    source: SourceRange
    source_start: int
    source_end: int
    output_start: int
    output_end: int

    @property
    def length(self) -> int:
        return self.source_end - self.source_start

    def key(self) -> tuple:
        source = self.source
        data = (
            source.data[self.source_start : self.source_end]
            if source.is_local
            else None
        )
        return (
            source.path,
            source.version_id,
            bool(source.copyable),
            self.source_start,
            self.source_end,
            data,
        )


@dataclass(frozen=True)
class PartConfirmation:
    """A part already present in a multipart upload.

    A confirmation is tied to a target and to the ordered source ranges it
    contains.  It can be reused only at the same part number and only while
    those exact source bytes still occur in the new target.  A confirmation
    without provenance (``slices`` empty) is an unknown part: it is reported
    as ignored, never treated as evidence for the new target.
    """

    part_number: int
    etag: str
    slices: tuple[PartSlice, ...] = ()
    size: int | None = None
    upload_id: str | None = None
    target: str | None = None
    plan_id: str | None = None
    generation: int = 0

    def __post_init__(self):
        if self.part_number < 1:
            raise ValueError("part_number must be >= 1")
        slices = tuple(self.slices)
        object.__setattr__(self, "slices", slices)
        actual = sum(s.length for s in slices)
        if not slices:
            # A part without provenance is an unknown part.  Its size can be
            # retained for reporting, but it cannot validate any new content.
            if self.size is None:
                object.__setattr__(self, "size", 0)
            return
        if self.size is None:
            object.__setattr__(self, "size", actual)
        elif self.size != actual:
            raise ValueError("Confirmation size does not match its slices")

    @property
    def valid(self) -> bool:
        # An empty ETag cannot be a confirmation: plans use it for parts that
        # have not been sent yet, and list-parts entries without provenance
        # are unknown parts for the new target.
        return bool(self.slices) and self.size > 0 and bool(self.etag)


@dataclass(frozen=True)
class PlannedStep:
    """One future part with its sources and output interval."""

    part_number: int
    action: str  # "copy", "upload" or "reuse"
    slices: tuple[PartSlice, ...]
    output_start: int
    output_end: int
    client_read_bytes: int
    client_write_bytes: int
    server_bytes: int
    etag: str | None = None
    confirmation: PartConfirmation | None = None
    generation: int = 0

    @property
    def size(self) -> int:
        return self.output_end - self.output_start

    @property
    def reused(self) -> bool:
        return self.action == "reuse"

    @property
    def client_bytes(self) -> int:
        return self.client_read_bytes + self.client_write_bytes

    def source_ranges(self) -> list[tuple]:
        """Compact description of the source bytes used by this step."""
        return [slice_.key() for slice_ in self.slices]


@dataclass(frozen=True)
class IgnoredConfirmation:
    confirmation: PartConfirmation
    reason: str


@dataclass(frozen=True)
class MergePlan:
    """Result of planning one multipart assembly.

    ``sources`` describes every source's path, version and range, and whether
    a range can be copied server-side.  ``min_part_size``,
    ``max_part_size`` and ``max_parts`` are the constraints the plan was
    solved under, and ``total_size`` is the target size.
    """

    path: str
    sources: tuple[SourceRange, ...]
    min_part_size: int
    max_part_size: int
    max_parts: int
    steps: tuple[PlannedStep, ...]
    total_size: int
    client_read_bytes: int
    client_write_bytes: int
    reused_parts: tuple[int, ...]
    resent_parts: tuple[int, ...]
    upload_id: str | None = None
    ignored_confirmations: tuple[IgnoredConfirmation, ...] = ()
    plan_id: str = ""

    @property
    def client_bytes(self) -> int:
        return self.client_read_bytes + self.client_write_bytes

    @property
    def part_sizes(self) -> tuple[int, ...]:
        return tuple(step.size for step in self.steps)

    @property
    def steps_to_send(self) -> tuple[PlannedStep, ...]:
        return tuple(step for step in self.steps if not step.reused)

    def by_part(self) -> dict[int, PlannedStep]:
        return {step.part_number: step for step in self.steps}

    def describe(self) -> dict:
        """A serialisable summary of the constraints and source ranges."""
        return {
            "path": self.path,
            "plan_id": self.plan_id,
            "upload_id": self.upload_id,
            "total_size": self.total_size,
            "min_part_size": self.min_part_size,
            "max_part_size": self.max_part_size,
            "max_parts": self.max_parts,
            "sources": [
                {
                    "path": source.path,
                    "start": source.start,
                    "end": source.end,
                    "version_id": source.version_id,
                    "copyable": source.copyable and not source.is_local,
                    "local": source.is_local,
                }
                for source in self.sources
            ],
            "steps": [
                {
                    "part_number": step.part_number,
                    "action": step.action,
                    "output_start": step.output_start,
                    "output_end": step.output_end,
                    "client_read_bytes": step.client_read_bytes,
                    "client_write_bytes": step.client_write_bytes,
                    "server_bytes": step.server_bytes,
                    "slices": [
                        {
                            "path": slice_.source.path,
                            "version_id": slice_.source.version_id,
                            "source_start": slice_.source_start,
                            "source_end": slice_.source_end,
                        }
                        for slice_ in step.slices
                    ],
                }
                for step in self.steps
            ],
        }

    def confirmations(self) -> tuple[PartConfirmation, ...]:
        """Confirmations that would exist after this plan succeeds.

        Reused parts keep their original ETag.  New parts get an empty ETag
        until execution supplies the real one.
        """
        out = []
        for step in self.steps:
            if step.reused and step.confirmation is not None:
                out.append(
                    replace(
                        step.confirmation,
                        part_number=step.part_number,
                        slices=step.slices,
                        size=step.size,
                        target=self.path,
                        plan_id=self.plan_id,
                        upload_id=self.upload_id,
                    )
                )
            else:
                out.append(
                    PartConfirmation(
                        part_number=step.part_number,
                        etag=step.etag or "",
                        slices=step.slices,
                        size=step.size,
                        target=self.path,
                        plan_id=self.plan_id,
                        upload_id=self.upload_id,
                        generation=step.generation,
                    )
                )
        return tuple(out)


@dataclass(frozen=True)
class ExecutedStep:
    """A planned step with the bytes actually transferred during execution."""

    part_number: int
    action: str
    slices: tuple[PartSlice, ...]
    output_start: int
    output_end: int
    client_read_bytes: int
    client_write_bytes: int
    server_bytes: int
    etag: str
    reused: bool = False
    upload_id: str | None = None
    confirmation: PartConfirmation | None = None

    @property
    def size(self) -> int:
        return self.output_end - self.output_start

    @property
    def client_bytes(self) -> int:
        return self.client_read_bytes + self.client_write_bytes


@dataclass(frozen=True)
class MergeResult:
    path: str
    upload_id: str
    steps: tuple[ExecutedStep, ...]
    confirmations: tuple[PartConfirmation, ...]
    completed: Mapping[str, Any] = field(default_factory=dict)
    plan: MergePlan | None = None

    @property
    def client_read_bytes(self) -> int:
        return sum(step.client_read_bytes for step in self.steps)

    @property
    def client_write_bytes(self) -> int:
        return sum(step.client_write_bytes for step in self.steps)

    @property
    def client_bytes(self) -> int:
        return self.client_read_bytes + self.client_write_bytes

    @property
    def reused_parts(self) -> tuple[int, ...]:
        return tuple(step.part_number for step in self.steps if step.reused)


class MergeExecutionError(RuntimeError):
    """An execution failed after some parts were already sent.

    The carried steps and confirmations let a caller retry with
    ``confirmed=error.confirmations`` without pretending that untouched or
    unknown parts belong to the new target.
    """

    def __init__(self, message, plan, upload_id, steps, confirmations, cause):
        super().__init__(message)
        self.plan = plan
        self.upload_id = upload_id
        self.steps = tuple(steps)
        self.confirmations = tuple(confirmations)
        self.cause = cause


def _coerce_source(value: Any, default_version=None) -> SourceRange:
    if isinstance(value, SourceRange):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return SourceRange.local(bytes(value))
    if isinstance(value, str):
        return SourceRange(path=value, start=0, end=None)
    if isinstance(value, Mapping):
        path = value.get("path", value.get("name", value.get("source")))
        data = value.get("data")
        version = value.get("version_id", value.get("version", default_version))
        start = value.get("start", value.get("offset", 0))
        end = value.get("end", value.get("stop"))
        if data is not None and path is None:
            return SourceRange(
                path=None,
                start=start,
                end=end,
                version_id=version,
                copyable=value.get("copyable", False),
                data=bytes(data),
            )
        if path is None:
            raise ValueError("Source mapping needs a path or data")
        if end is None:
            length = value.get("length", value.get("size"))
            if length is None:
                raise ValueError("A remote source range needs an end or length")
            end = start + length
        return SourceRange(
            path=path,
            start=start,
            end=end,
            version_id=version,
            copyable=value.get("copyable", True),
        )
    if isinstance(value, (tuple, list)):
        if not value:
            raise ValueError("Empty source tuple")
        if len(value) == 1:
            return _coerce_source(value[0], default_version)
        if len(value) == 2:
            first, second = value
            if isinstance(first, (bytes, bytearray, memoryview)):
                return SourceRange.local(bytes(first))
            if isinstance(second, (bytes, bytearray, memoryview)):
                return SourceRange.local(bytes(second))
            raise ValueError("A two-item remote source needs start and end")
        if len(value) == 3:
            path, start, end = value
            return SourceRange(path=path, start=start, end=end)
        if len(value) == 4:
            path, start, end, version = value
            return SourceRange(path=path, start=start, end=end, version_id=version)
        if len(value) == 5:
            path, start, end, version, copyable = value
            return SourceRange(
                path=path,
                start=start,
                end=end,
                version_id=version,
                copyable=copyable,
            )
    raise TypeError(f"Cannot interpret source {value!r}")


def coerce_sources(sources: Iterable[Any]) -> tuple[SourceRange, ...]:
    return tuple(_coerce_source(source) for source in sources)


def source_from_mapping(value: Mapping[str, Any]) -> SourceRange:
    return _coerce_source(value)


def source_from_path(
    path: str, start: int = 0, end: int | None = None, **kwargs
) -> SourceRange:
    return SourceRange(path=path, start=start, end=end, **kwargs)


def source_from_bytes(data: bytes) -> SourceRange:
    return SourceRange.local(data)


def _normalise_path(path: str) -> str:
    if path.startswith("s3://"):
        path = path[5:]
    elif path.startswith("s3a://"):
        path = path[6:]
    return path.lstrip("/")


def _split_version(path: str, version_id: str | None) -> tuple[str, str | None]:
    path = _normalise_path(path)
    if "?versionId=" in path:
        path, _, embedded = path.partition("?versionId=")
        if version_id is None:
            version_id = embedded
    return path, version_id


def _canonical_slices(slices: Sequence[PartSlice]) -> tuple[tuple, ...]:
    """Canonical source runs for content comparison.

    Adjacent slices of the same source identity in contiguous source offsets
    are the same bytes as one merged slice.  Output offsets are ignored, so a
    confirmed part can be reused when it has moved within the new target.
    """
    runs: list[tuple] = []
    for slice_ in slices:
        source = slice_.source
        if source.path is not None:
            path, version = _split_version(source.path, source.version_id)
        else:
            path, version = None, source.version_id
        data = None
        if source.is_local:
            data = source.data[slice_.source_start : slice_.source_end]
        item = (
            path,
            version,
            slice_.source_start,
            slice_.source_end,
            data,
        )
        if (
            runs
            and runs[-1][0] == item[0]
            and runs[-1][1] == item[1]
            and runs[-1][4] == item[4]
            and runs[-1][3] == item[2]
        ):
            runs[-1] = (
                item[0],
                item[1],
                runs[-1][2],
                item[3],
                item[4],
            )
        else:
            runs.append(item)
    return tuple(runs)


class _Target:
    def __init__(self, sources: Sequence[SourceRange]):
        self.sources = tuple(sources)
        unresolved = [
            source
            for source in self.sources
            if source.path is not None and source.end is None
        ]
        if unresolved:
            raise MergePlanError(
                "Unresolved remote source range; pass an explicit end or use "
                "S3FileSystem.plan_merge()"
            )
        self.starts: list[int] = []
        total = 0
        for source in self.sources:
            self.starts.append(total)
            total += source.length
        self.total = total
        self.ends = [
            start + source.length
            for start, source in zip(self.starts, self.sources)
        ]

    def slices(self, start: int, end: int) -> tuple[PartSlice, ...]:
        if start < 0 or end < start or end > self.total:
            raise ValueError("Part interval outside the target")
        out: list[PartSlice] = []
        index = max(0, bisect.bisect_right(self.ends, start))
        for index in range(index, len(self.sources)):
            seg_start = self.starts[index]
            seg_end = self.ends[index]
            if seg_end <= start:
                continue
            if seg_start >= end:
                break
            piece_start = max(start, seg_start)
            piece_end = min(end, seg_end)
            if piece_start >= piece_end:
                continue
            source = self.sources[index]
            offset = piece_start - seg_start
            out.append(
                PartSlice(
                    source=source,
                    source_start=source.start + offset,
                    source_end=source.start + offset + (piece_end - piece_start),
                    output_start=piece_start,
                    output_end=piece_end,
                )
            )
        return tuple(out)

    def boundary_candidates(self) -> set[int]:
        candidates = {0, self.total}
        candidates.update(self.starts)
        candidates.update(self.ends)
        return {c for c in candidates if 0 <= c <= self.total}


def _single_copy_source(slices: Sequence[PartSlice]) -> SourceRange | None:
    if not slices:
        return None
    first = slices[0]
    if first.source.path is None or not first.source.copyable:
        return None
    path, version = _split_version(first.source.path, first.source.version_id)
    expected = first.source_start
    for slice_ in slices:
        source = slice_.source
        if source.path is None or not source.copyable:
            return None
        this_path, this_version = _split_version(source.path, source.version_id)
        if this_path != path or this_version != version:
            return None
        if slice_.source_start != expected:
            return None
        expected = slice_.source_end
    return first.source


def _step_cost(slices: Sequence[PartSlice]) -> tuple[str, int, int, int]:
    """Return ``(action, client_read, client_write, server_bytes)``.

    A server-side copy transfers no bytes through the client.  A client-side
    part reads its remote ranges once and uploads the whole part once; local
    bytes only contribute to the upload.  The result reflects the real
    multipart calls, not the number of source segments.
    """
    if _single_copy_source(slices) is not None:
        size = sum(s.length for s in slices)
        return "copy", 0, 0, size
    size = sum(s.length for s in slices)
    remote = sum(s.length for s in slices if s.source.path is not None)
    return "upload", remote, size, 0


def _copy_span(slices: Sequence[PartSlice]) -> tuple[SourceRange, int, int] | None:
    """Return the single source and contiguous span for a pure copy part."""
    if _single_copy_source(slices) is None:
        return None
    first = slices[0]
    return first.source, first.source_start, slices[-1].source_end


def _plan_id(path, sources, min_part_size, max_part_size, max_parts, steps) -> str:
    digest = hashlib.sha256()
    digest.update(repr(_normalise_path(path)).encode())
    for source in sources:
        digest.update(repr(source.key()).encode())
    digest.update(repr((min_part_size, max_part_size, max_parts)).encode())
    for step in steps:
        digest.update(repr((step.part_number, step.action, step.size)).encode())
        for slice_ in step.slices:
            digest.update(repr(slice_.key()).encode())
    return digest.hexdigest()[:16]


@dataclass
class _Node:
    position: int
    parts_used: int
    client_bytes: int
    reuse_count: int
    step: PlannedStep | None
    previous: "_Node | None"

    def lineage(self) -> list[PlannedStep]:
        steps = []
        node: _Node | None = self
        while node is not None and node.step is not None:
            steps.append(node.step)
            node = node.previous
        steps.reverse()
        return steps


def _target_contains(target: "_Target", canonical: tuple[tuple, ...]) -> bool:
    """Whether ``canonical`` source runs occur contiguously in ``target``.

    A confirmation whose source bytes are absent from the new target is an
    old result, not a confirmation for the new version.  It is reported as
    ignored instead of forcing the plan to fail.
    """
    if not canonical:
        return False
    size = sum(run[3] - run[2] for run in canonical)
    if size > target.total:
        return False
    first = canonical[0]
    candidates = set()
    for index, source in enumerate(target.sources):
        if source.path is not None:
            path, version = _split_version(source.path, source.version_id)
        else:
            path, version = None, source.version_id
        if (first[0], first[1]) != (path, version):
            continue
        if source.path is None:
            if first[4] is None:
                continue
            found = source.data.find(first[4])
            while found >= 0:
                if found >= source.start:
                    candidates.add(target.starts[index] + found - source.start)
                found = source.data.find(first[4], found + 1)
        else:
            if not (source.start <= first[2] and first[3] <= source.end):
                continue
            candidates.add(target.starts[index] + (first[2] - source.start))
    digests = {}
    for offset in candidates:
        if offset < 0 or offset + size > target.total:
            continue
        key = (offset, size)
        if key in digests:
            signature = digests[key]
        else:
            signature = _canonical_slices(target.slices(offset, offset + size))
            digests[key] = signature
        if signature == canonical:
            return True
    return False


class _Planner:
    def __init__(
        self,
        path,
        sources,
        min_part_size,
        max_part_size,
        max_parts,
        confirmed,
        resend_parts,
        upload_id,
        allow_small_last,
        exact_limit,
    ):
        if min_part_size < 0 or max_part_size < min_part_size:
            raise ValueError("Invalid part-size bounds")
        if max_parts < 1:
            raise ValueError("max_parts must be >= 1")
        self.path = path
        self.sources = tuple(sources)
        self.target = _Target(self.sources)
        self.total = self.target.total
        self.min_part_size = min_part_size
        self.max_part_size = max_part_size
        self.max_parts = max_parts
        self.allow_small_last = allow_small_last
        self.exact_limit = exact_limit
        self.upload_id = upload_id

        if resend_parts is None:
            self.resend_allowed: set[int] | None = None
        else:
            self.resend_allowed = {int(number) for number in resend_parts}
        for number in self.resend_allowed or ():
            if number < 1:
                raise ValueError("resend_parts only accepts positive part numbers")

        self.pending: dict[int, list[PartConfirmation]] = {}
        self.ignored: list[IgnoredConfirmation] = []
        target_name = _normalise_path(path)
        confirmed = tuple(confirmed)
        if self.upload_id is None:
            upload_ids = {
                conf.upload_id for conf in confirmed if getattr(conf, "upload_id", None)
            }
            if len(upload_ids) == 1:
                # Confirmations from one existing upload can be continued
                # without the caller repeating its id.
                self.upload_id = upload_ids.pop()
            elif len(upload_ids) > 1:
                self.ignored.extend(
                    IgnoredConfirmation(conf, "ambiguous upload")
                    for conf in confirmed
                    if getattr(conf, "upload_id", None)
                )
                confirmed = tuple(
                    conf for conf in confirmed if not getattr(conf, "upload_id", None)
                )
        for conf in confirmed:
            if not isinstance(conf, PartConfirmation):
                raise TypeError("confirmed entries must be PartConfirmation instances")
            if conf.target is not None and _normalise_path(conf.target) != target_name:
                self.ignored.append(IgnoredConfirmation(conf, "different target"))
                continue
            if self.upload_id is not None and conf.upload_id not in (
                None,
                self.upload_id,
            ):
                self.ignored.append(IgnoredConfirmation(conf, "different upload"))
                continue
            if not conf.valid:
                reason = (
                    "missing ETag"
                    if conf.slices and not conf.etag
                    else "missing source provenance"
                )
                self.ignored.append(IgnoredConfirmation(conf, reason))
                continue
            if conf.part_number > max_parts:
                self.ignored.append(
                    IgnoredConfirmation(conf, "part number exceeds max_parts")
                )
                continue
            if not _target_contains(self.target, self._canonical_conf(conf)):
                self.ignored.append(
                    IgnoredConfirmation(
                        conf, "source bytes are not in the new target order"
                    )
                )
                continue
            self.pending.setdefault(conf.part_number, []).append(conf)

        if self.pending:
            newest = {}
            for number, options in self.pending.items():
                newest[number] = max(options, key=lambda c: (c.generation, c.etag))
            self.pending = {number: [conf] for number, conf in newest.items()}

        self.mandatory = {
            number
            for number in self.pending
            if not self._may_resend(number)
        }
        self._conf_cache: dict[tuple, PartConfirmation | None] = {}

    def _may_resend(self, number: int) -> bool:
        if self.resend_allowed is None:
            return True
        return number in self.resend_allowed

    def _canonical_conf(self, conf: PartConfirmation) -> tuple[tuple, ...]:
        return _canonical_slices(conf.slices)

    def compatible(
        self, part_number: int, start: int, end: int, slices: Sequence[PartSlice]
    ) -> PartConfirmation | None:
        if not self.pending.get(part_number):
            return None
        key = (part_number, start, end)
        if key in self._conf_cache:
            return self._conf_cache[key]
        canonical = _canonical_slices(slices)
        options = [
            conf
            for conf in self.pending.get(part_number, ())
            if conf.size == end - start and self._canonical_conf(conf) == canonical
        ]
        result = options[0] if options else None
        self._conf_cache[key] = result
        return result

    def step_for(self, part_number: int, start: int, end: int) -> PlannedStep | None:
        slices = self.target.slices(start, end)
        if not slices:
            return None
        conf = self.compatible(part_number, start, end, slices)
        if conf is not None:
            return PlannedStep(
                part_number=part_number,
                action="reuse",
                slices=slices,
                output_start=start,
                output_end=end,
                client_read_bytes=0,
                client_write_bytes=0,
                server_bytes=0,
                etag=conf.etag,
                confirmation=conf,
            )
        if part_number in self.mandatory:
            return None
        action, read, write, server = _step_cost(slices)
        generation = 0
        if self.pending.get(part_number):
            generation = 1 + max(conf.generation for conf in self.pending[part_number])
        return PlannedStep(
            part_number=part_number,
            action=action,
            slices=slices,
            output_start=start,
            output_end=end,
            client_read_bytes=read,
            client_write_bytes=write,
            server_bytes=server,
            generation=generation,
        )

    def _remainder_feasible(self, end: int, parts_left: int) -> bool:
        """Can the bytes after ``end`` still be split into allowed parts?

        The plan does not have to use every available part slot.  The
        remainder needs ``r = ceil(remaining / max)`` parts at minimum; all
        but the last must reach the minimum size, and the last may be short
        only when ``allow_small_last`` is set.
        """
        remaining = self.total - end
        if remaining == 0:
            return True
        if parts_left <= 0 or remaining > parts_left * self.max_part_size:
            return False
        required_parts = (remaining + self.max_part_size - 1) // self.max_part_size
        if self.allow_small_last:
            minimum = max(0, required_parts - 1) * self.min_part_size
        else:
            minimum = required_parts * self.min_part_size
        return remaining >= minimum

    def ends_from(self, start: int, part_number: int, exact: bool, positions):
        total = self.total
        if part_number in self.mandatory:
            # A preserved part fixes the size and content of this part.  Its
            # size still has to obey the same part bounds as a new part.
            sizes = sorted(
                {
                    conf.size
                    for conf in self.pending.get(part_number, ())
                    if conf.size and start + conf.size <= total
                }
            )
            out = []
            for size in sizes:
                end = start + size
                if size > self.max_part_size:
                    continue
                if end < total and size < self.min_part_size:
                    continue
                if (
                    end == total
                    and not self.allow_small_last
                    and size < self.min_part_size
                ):
                    continue
                out.append(end)
            return out

        high = min(self.max_part_size, total - start)
        low = 0 if start + self.min_part_size > total else self.min_part_size
        if high < low:
            return []
        if exact:
            candidates = range(start + low, start + high + 1)
        else:
            candidates = self._candidate_ends(start, high, part_number, positions)
        out = []
        parts_left = self.max_parts - part_number
        for end in candidates:
            if end <= start or end > total:
                continue
            if end < total and end - start < self.min_part_size:
                continue
            if (
                end == total
                and not self.allow_small_last
                and end - start < self.min_part_size
            ):
                continue
            if not self._remainder_feasible(end, parts_left):
                continue
            out.append(end)
        return sorted(set(out))
    def _candidate_ends(self, start: int, high: int, part_number: int, positions):
        total = self.total
        lo = start + min(self.min_part_size, high)
        hi = start + high
        ends = set()
        index = bisect.bisect_left(positions, lo)
        while index < len(positions) and positions[index] <= hi:
            ends.add(positions[index])
            index += 1
        ends.update((lo, hi))
        for conf in self.pending.get(part_number, ()):
            if conf.size and lo <= start + conf.size <= hi:
                ends.add(start + conf.size)
        parts_left = self.max_parts - part_number
        for remaining_parts in range(1, parts_left + 1):
            end = total - remaining_parts * self.max_part_size
            if lo <= end <= hi:
                ends.add(end)
            if self.allow_small_last:
                # Leave one short tail after some full-size parts.
                end = total - (remaining_parts - 1) * self.min_part_size
                if lo <= end <= hi:
                    ends.add(end)
        return ends

    def _positions(self):
        """Sparse boundary candidates for large targets.

        Only a few positions can improve the cost: source boundaries,
        confirmation boundaries, and the arithmetic boundaries implied by
        filling parts to the minimum or maximum size.  A dense grid would
        make the frontier explode without adding better plans.
        """
        total = self.total
        minimum = max(1, self.min_part_size)
        maximum = max(1, self.max_part_size)
        base = set(self.target.boundary_candidates())
        base.update({0, total})

        confirmation_sizes = []
        for options in self.pending.values():
            sizes = {conf.size for conf in options if conf.size}
            if len(sizes) == 1:
                confirmation_sizes.append(sizes.pop())
        if confirmation_sizes:
            cursor = 0
            for size in confirmation_sizes:
                cursor += size
                base.add(min(cursor, total))

        max_by_min = min(self.max_parts, (total + minimum - 1) // minimum)
        max_by_max = max(1, (total + maximum - 1) // maximum)
        anchors = set(range(1, min(max_by_min, 48) + 1))
        anchors.update(
            range(max_by_max, min(max_by_min, max_by_max + 48) + 1)
        )
        anchors.update(
            range(max(1, max_by_min - 48), max_by_min + 1)
        )
        for k in anchors:
            for end in (
                total - k * maximum,
                total - (k - 1) * minimum,
                total - k * minimum,
            ):
                if 0 <= end <= total:
                    base.add(end)

        for pos in list(base):
            for delta in (-minimum, minimum, -maximum, maximum):
                new = pos + delta
                if 0 <= new <= total:
                    base.add(new)

        if len(base) > 3000:
            structural = set(self.target.boundary_candidates()) | {0, total}
            anchors_positions = set()
            for k in sorted(anchors):
                for end in (total - k * maximum, total - k * minimum):
                    if 0 <= end <= total:
                        anchors_positions.add(end)
            base = structural | anchors_positions
        return sorted(base)

    def solve(self):
        if self.total <= 0:
            raise MergePlanError("The target is empty; a multipart plan needs bytes")
        if self.max_parts * self.max_part_size < self.total:
            raise MergePlanError(
                f"{self.total} bytes cannot fit in {self.max_parts} parts of at "
                f"most {self.max_part_size} bytes"
            )
        if self.mandatory and max(self.mandatory) > self.max_parts:
            raise MergePlanError(
                "A confirmed part number exceeds max_parts and may not be resent"
            )
        if self.total > self.max_parts * self.max_part_size:
            raise MergePlanError("The target is too large for the part limits")

        exact_positions = self.total * min(self.max_part_size, self.total)
        exact = exact_positions <= self.exact_limit

        positions = None if exact else self._positions()
        max_parts_by_size = max(
            1,
            (self.total + max(1, self.min_part_size) - 1)
            // max(1, self.min_part_size),
        )
        k_max = min(self.max_parts, max_parts_by_size)
        if self.mandatory:
            k_max = max(k_max, max(self.mandatory))
        k_max = min(k_max, self.max_parts)
        # A frontier is keyed by (position, parts_used).  Part numbers are
        # 1-based and equal parts_used+1, so nodes that have used a different
        # number of parts cannot be compared: a mandatory confirmation at a
        # fixed part number makes a "fewer parts so far" state not strictly
        # better.
        frontiers: dict[tuple[int, int], list[_Node]] = {}
        by_position: dict[int, list[int]] = {}
        start_node = _Node(
            position=0,
            parts_used=0,
            client_bytes=0,
            reuse_count=0,
            step=None,
            previous=None,
        )
        frontiers[(0, 0)] = [start_node]
        by_position[0] = [0]
        import heapq

        pending = [0]
        queued = {0}
        processed = set()
        while pending:
            position = heapq.heappop(pending)
            queued.discard(position)
            if position in processed:
                continue
            processed.add(position)
            for parts_used in by_position.get(position, ()):
                part_number = parts_used + 1
                if part_number > k_max:
                    continue
                nodes = frontiers.get((position, parts_used), ())
                for node in list(nodes):
                    ends = self.ends_from(
                        position, part_number, exact, positions
                    )
                    for end in ends:
                        step = self.step_for(part_number, position, end)
                        if step is None:
                            continue
                        new = _Node(
                            position=end,
                            parts_used=part_number,
                            client_bytes=node.client_bytes + step.client_bytes,
                            reuse_count=node.reuse_count
                            + (1 if step.reused else 0),
                            step=step,
                            previous=node,
                        )
                        key = (end, part_number)
                        bucket = frontiers.setdefault(key, [])
                        if any(
                            _dominates(existing, new) for existing in bucket
                        ):
                            continue
                        bucket[:] = [
                            existing
                            for existing in bucket
                            if not _dominates(new, existing)
                        ]
                        bucket.append(new)
                        bucket_positions = by_position.setdefault(end, [])
                        if part_number not in bucket_positions:
                            bucket_positions.append(part_number)
                        if end not in processed and end not in queued:
                            heapq.heappush(pending, end)
                            queued.add(end)

        final_nodes = []
        for (position, parts_used), nodes in frontiers.items():
            if position != self.total or parts_used > self.max_parts:
                continue
            final_nodes.extend(nodes)
        if self.mandatory:
            required = max(self.mandatory)
            final_nodes = [
                node for node in final_nodes if node.parts_used >= required
            ]
        if not final_nodes:
            reasons = ["no feasible part partition"]
            if self.mandatory:
                reasons.insert(
                    0,
                    "confirmed parts "
                    + ", ".join(str(n) for n in sorted(self.mandatory))
                    + " cannot all be reused",
                )
            reasons.append(
                f"target={self.total} min={self.min_part_size} "
                f"max={self.max_part_size} max_parts={self.max_parts}"
            )
            raise MergePlanError("; ".join(reasons))

        best = min(
            final_nodes,
            key=lambda node: (node.client_bytes, -node.reuse_count, node.parts_used),
        )
        steps = best.lineage()
        reused = tuple(step.part_number for step in steps if step.reused)
        resent = tuple(
            step.part_number
            for step in steps
            if not step.reused and step.part_number in self.pending
        )
        plan_id = _plan_id(
            self.path,
            self.sources,
            self.min_part_size,
            self.max_part_size,
            self.max_parts,
            steps,
        )
        return MergePlan(
            path=self.path,
            sources=self.sources,
            min_part_size=self.min_part_size,
            max_part_size=self.max_part_size,
            max_parts=self.max_parts,
            steps=tuple(steps),
            total_size=self.total,
            client_read_bytes=sum(s.client_read_bytes for s in steps),
            client_write_bytes=sum(s.client_write_bytes for s in steps),
            reused_parts=reused,
            resent_parts=resent,
            upload_id=self.upload_id,
            ignored_confirmations=tuple(self.ignored),
            plan_id=plan_id,
        )
def _dominates(a: _Node, b: _Node) -> bool:
    """Whether ``a`` is at least as good as ``b`` for every remaining choice."""
    if a.position != b.position:
        return False
    return (
        a.parts_used <= b.parts_used
        and a.client_bytes <= b.client_bytes
        and a.reuse_count >= b.reuse_count
    )


def plan_merge(
    path: str,
    sources: Iterable[Any],
    *,
    min_part_size: int = S3_MIN_PART_SIZE,
    max_part_size: int = S3_MAX_PART_SIZE,
    max_parts: int = 10_000,
    confirmed: Iterable[PartConfirmation] = (),
    resend_parts: Iterable[int] | None = (),
    upload_id: str | None = None,
    allow_small_last: bool = True,
    exact_limit: int = 20_000,
) -> MergePlan:
    """Plan an ordered multipart assembly.

    Parameters
    ----------
    path : str
        Final target path.
    sources : iterable
        Ordered source ranges.  Entries can be :class:`SourceRange`, bytes
        (client-local data), a mapping, or a tuple
        ``(path, start, end[, version_id[, copyable]])``.  A whole remote
        object has to be given as an explicit ``0..size`` range because its
        size is not known to this pure planner.
    min_part_size, max_part_size : int
        Part-size limits.  The final part may be shorter than the minimum
        unless ``allow_small_last=False``.
    max_parts : int
        Maximum number of parts.
    confirmed : iterable of PartConfirmation
        Parts already uploaded for the same target and upload.  Every valid
        confirmation is preserved at its original part number unless its
        number appears in ``resend_parts`` or ``resend_parts`` is ``None``.
    resend_parts : iterable of int or None
        Part numbers the caller permits replacing.  The default ``()`` keeps
        every confirmed part.  ``None`` permits replacing any confirmed part.
    upload_id : str or None
        Current multipart upload.  Confirmations from another upload are
        ignored rather than reused.
    allow_small_last : bool
        Whether the final part may be shorter than ``min_part_size``.
    exact_limit : int
        Positions up to this size are planned byte-by-byte.  Larger targets
        use candidate boundaries derived from source, confirmation and
        part-size limits.
    """
    return _Planner(
        path=path,
        sources=tuple(sources),
        min_part_size=min_part_size,
        max_part_size=max_part_size,
        max_parts=max_parts,
        confirmed=tuple(confirmed),
        resend_parts=resend_parts,
        upload_id=upload_id,
        allow_small_last=allow_small_last,
        exact_limit=exact_limit,
    ).solve()


async def _assemble_part(executor, step: PlannedStep) -> tuple[bytes, int]:
    chunks = []
    remote_read = 0
    for slice_ in step.slices:
        if slice_.source.is_local:
            piece = slice_.source.data[slice_.source_start : slice_.source_end]
        else:
            piece = await executor.read_range(
                slice_.source.path,
                slice_.source.version_id,
                slice_.source_start,
                slice_.source_end,
            )
            remote_read += slice_.length
        if len(piece) != slice_.length:
            raise OSError(
                f"short read for part {step.part_number}: expected "
                f"{slice_.length} bytes, got {len(piece)}"
            )
        chunks.append(piece)
    data = b"".join(chunks)
    if len(data) != step.size:
        raise OSError(
            f"part {step.part_number} assembled {len(data)} bytes, "
            f"expected {step.size}"
        )
    return data, remote_read


async def execute_merge(
    plan: MergePlan,
    executor,
    *,
    upload_id: str | None = None,
    complete: bool = True,
    on_step: Callable[[ExecutedStep], None] | None = None,
) -> MergeResult:
    """Execute a plan with ``executor`` and record actual client bytes.

    ``executor`` supplies async ``create_upload(path)``,
    ``read_range(path, version_id, start, end)``, ``copy_part(...)``,
    ``upload_part(...)`` and ``complete_upload(...)`` methods.  A failure is
    raised as :class:`MergeExecutionError` carrying the parts that were
    actually confirmed before the failure, so the upload can be retried
    without mistaking untouched parts for new confirmations.
    """
    upload_id = upload_id or plan.upload_id
    executed: list[ExecutedStep] = []
    confirmations: list[PartConfirmation] = []
    completed: Mapping[str, Any] = {}
    try:
        if upload_id is None:
            upload_id = await executor.create_upload(plan.path)
        for step in plan.steps:
            if step.reused:
                if step.confirmation is None:
                    raise RuntimeError(
                        f"Part {step.part_number} is reused without a confirmation"
                    )
                etag = step.confirmation.etag
                executed_step = ExecutedStep(
                    part_number=step.part_number,
                    action="reuse",
                    slices=step.slices,
                    output_start=step.output_start,
                    output_end=step.output_end,
                    client_read_bytes=0,
                    client_write_bytes=0,
                    server_bytes=0,
                    etag=etag,
                    reused=True,
                    upload_id=upload_id,
                    confirmation=step.confirmation,
                )
            elif step.action == "copy":
                span = _copy_span(step.slices)
                if span is None:
                    raise RuntimeError(
                        f"Part {step.part_number} is not a single copyable range"
                    )
                source, start, end = span
                etag = await executor.copy_part(
                    upload_id,
                    plan.path,
                    step.part_number,
                    source.path,
                    source.version_id,
                    start,
                    end,
                )
                executed_step = ExecutedStep(
                    part_number=step.part_number,
                    action="copy",
                    slices=step.slices,
                    output_start=step.output_start,
                    output_end=step.output_end,
                    client_read_bytes=0,
                    client_write_bytes=0,
                    server_bytes=step.size,
                    etag=etag,
                    upload_id=upload_id,
                )
            else:
                data, remote_read = await _assemble_part(executor, step)
                etag = await executor.upload_part(
                    upload_id, plan.path, step.part_number, data
                )
                executed_step = ExecutedStep(
                    part_number=step.part_number,
                    action="upload",
                    slices=step.slices,
                    output_start=step.output_start,
                    output_end=step.output_end,
                    client_read_bytes=remote_read,
                    client_write_bytes=len(data),
                    server_bytes=0,
                    etag=etag,
                    upload_id=upload_id,
                )
            executed.append(executed_step)
            confirmation = PartConfirmation(
                part_number=step.part_number,
                etag=etag,
                slices=step.slices,
                size=step.size,
                upload_id=upload_id,
                target=plan.path,
                plan_id=plan.plan_id,
                generation=(
                    step.confirmation.generation
                    if step.confirmation is not None
                    else step.generation
                ),
            )
            confirmations.append(confirmation)
            if on_step is not None:
                on_step(executed_step)
        if complete:
            completed = await executor.complete_upload(
                upload_id,
                plan.path,
                [(step.part_number, step.etag) for step in executed],
            )
    except Exception as exc:
        raise MergeExecutionError(
            f"Merge of {plan.path!r} failed after {len(executed)} parts: {exc}",
            plan,
            upload_id,
            executed,
            confirmations,
            exc,
        ) from exc

    return MergeResult(
        path=plan.path,
        upload_id=upload_id,
        steps=tuple(executed),
        confirmations=tuple(confirmations),
        completed=completed,
        plan=plan,
    )


def confirmations_from_result(result: MergeResult) -> tuple[PartConfirmation, ...]:
    return result.confirmations
