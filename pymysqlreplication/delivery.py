"""Resumable delivery checkpoints for binlog consumers.

``BinLogStreamReader`` can already reconnect from the set of GTIDs it has
*read*.  A warehouse delivery needs a different, stronger statement:

* rows that were read while an XA group was only prepared are kept locally
  even when the source that later commits the group has discarded its
  prepare segment;
* a transaction is published once, with one complete result, even when the
  checkpoint is closed and opened again;
* reading a GTID is evidence about the stream, never evidence that the
  target warehouse accepted a write;
* the next source and the range that is still missing are part of the same
  durable record.

The module deliberately has no dependency on a live MySQL connection.  The
stream reader feeds events into it, and applications may also feed a test
source directly through the ``record_*`` methods.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .event import QueryEvent, XAPrepareEvent
from .gtid import Gtid, GtidSet


class DeliveryCheckpointError(Exception):
    """Base class for inconsistent delivery checkpoint operations."""


class AmbiguousEffectError(DeliveryCheckpointError):
    """Raised when an XA name is not enough to select one prepared group."""


class TargetConfirmationError(DeliveryCheckpointError):
    """Raised when a target receipt cannot be attached to that transaction."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, (datetime,)):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, set):
        return sorted(value, key=repr)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_hook(value: Dict[str, Any]) -> Any:
    if set(value) == {"__bytes__"}:
        return bytes.fromhex(value["__bytes__"])
    return value


def _stable(value: Any) -> Any:
    """Normalise values so equal rows from two sources deduplicate."""
    if isinstance(value, dict):
        return tuple(sorted((str(k), _stable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted((_stable(v) for v in value), key=repr))
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    if isinstance(value, (datetime,)):
        return ("datetime", value.isoformat())
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _decode_xid_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", "backslashreplace")
    return str(value)


@dataclass(frozen=True)
class XAIdentity:
    """The identity an XA transaction is known by on both sources.

    ``gtrid``/``bqual``/``format_id`` come from the XA XID.  The prepare GTID
    is deliberately *not* part of :meth:`xa_key`: a commit is allowed to
    arrive with a different GTID.  A group key additionally carries the
    prepare generation so a reused XA name does not silently share target
    receipts.
    """

    format_id: Optional[int] = 1
    gtrid: str = ""
    bqual: str = ""

    @property
    def xid(self) -> str:
        return self.gtrid + self.bqual

    @property
    def xa_key(self) -> str:
        return "xa:%s:%s:%s" % (self.format_id, self.gtrid, self.bqual)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format_id": self.format_id,
            "gtrid": self.gtrid,
            "bqual": self.bqual,
        }

    @classmethod
    def from_event(cls, event: XAPrepareEvent) -> "XAIdentity":
        return cls(
            format_id=getattr(event, "xid_format_id", 1),
            gtrid=_decode_xid_part(getattr(event, "xid_gtrid", b"")),
            bqual=_decode_xid_part(getattr(event, "xid_bqual", b"")),
        )

    @classmethod
    def from_xid(cls, xid: Any) -> "XAIdentity":
        if xid is None:
            return cls()
        if isinstance(xid, XAIdentity):
            return xid
        if isinstance(xid, XAPrepareEvent):
            return cls.from_event(xid)
        if isinstance(xid, dict):
            return cls(
                format_id=int(xid.get("format_id", xid.get("xid_format_id", 1))),
                gtrid=_decode_xid_part(xid.get("gtrid", xid.get("xid_gtrid", ""))),
                bqual=_decode_xid_part(xid.get("bqual", xid.get("xid_bqual", ""))),
            )
        if isinstance(xid, (tuple, list)):
            values = list(xid)
            if len(values) >= 3:
                return cls(
                    format_id=int(values[2]),
                    gtrid=_decode_xid_part(values[0]),
                    bqual=_decode_xid_part(values[1]),
                )
        return _parse_xid_text(str(xid))


_XA_COMMAND_RE = re.compile(
    r"^(?:/\*.*?\*/\s*)*XA\s+(START|BEGIN|END|PREPARE|COMMIT|ROLLBACK)\b(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ONLY_TOKEN_RE = re.compile(r"^[\w.+-]+$")


def _split_xid_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    current.append(text[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == ",":
            tokens.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    tokens.append("".join(current).strip())
    return tokens


def _decode_xid_token(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        body = token[1:-1]
        quote = token[0]
        return body.replace(quote * 2, quote)
    if token.lower().startswith("0x"):
        raw = token[2:]
        if len(raw) % 2:
            raw = "0" + raw
        try:
            return bytes.fromhex(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            try:
                return bytes.fromhex(raw).decode("latin-1")
            except ValueError:
                return token
    return token


def _parse_xid_text(text: str) -> XAIdentity:
    text = text.strip()
    if not text:
        return XAIdentity()
    tokens = _split_xid_tokens(text)
    if not tokens:
        return XAIdentity()
    format_id: Optional[int] = None
    if len(tokens) >= 2 and re.fullmatch(r"[+-]?\d+", tokens[-1].strip()):
        format_id = int(tokens[-1].strip())
        tokens = tokens[:-1]
    elif len(tokens) >= 3 and tokens[-1].strip().upper() == "ONE PHASE":
        tokens = tokens[:-1]
        if re.fullmatch(r"[+-]?\d+", tokens[-1].strip()):
            format_id = int(tokens[-1].strip())
            tokens = tokens[:-1]
    gtrid = _decode_xid_token(tokens[0]) if tokens else ""
    bqual = _decode_xid_token(tokens[1]) if len(tokens) > 1 else ""
    if format_id is None:
        format_id = 1
    return XAIdentity(format_id=format_id, gtrid=gtrid, bqual=bqual)


def parse_xa_query(query: Any) -> Optional[Tuple[str, XAIdentity, bool]]:
    """Return ``(command, identity, one_phase)`` for an XA query.

    ``None`` is returned for ordinary statements.  The parser accepts both
    the quoted XID form and the hexadecimal serialized form used in binlog
    ``XA COMMIT 0x78,0x,1`` statements.
    """

    if isinstance(query, bytes):
        query = query.decode("utf-8", "replace")
    if not query:
        return None
    match = _XA_COMMAND_RE.match(query.strip())
    if not match:
        return None
    command = match.group(1).upper()
    rest = match.group(2).strip()
    one_phase = bool(re.search(r"\bONE\s+PHASE\b", rest, re.IGNORECASE))
    rest = re.sub(r"\bONE\s+PHASE\b", "", rest, flags=re.IGNORECASE).strip()
    return command, _parse_xid_text(rest), one_phase


@dataclass(frozen=True)
class TransactionIdentity:
    """Effect identity and the matching basis supplied by a source.

    A caller that knows more than the XA name (for example a source-side
    transaction identifier, or the prepare GTID it actually saw) can pass it
    here.  It is stored with the prepared group and used before the weaker
    XA-name fallback.
    """

    key: Optional[str] = None
    prepare_gtid: Optional[str] = None
    xid: Optional[XAIdentity] = None
    basis: Optional[str] = None

    def group_key(self, default_gtid: Optional[str] = None) -> str:
        if self.key:
            return str(self.key)
        generation = self.prepare_gtid or default_gtid or "-"
        if self.xid is not None:
            return "%s@%s" % (self.xid.xa_key, generation)
        return "txn:%s" % generation


@dataclass
class SourceCursor:
    source: str
    gtids: GtidSet = field(default_factory=lambda: GtidSet(None))
    read_gtids: Set[str] = field(default_factory=set)
    last_gtid: Optional[str] = None
    manifest: Optional["MirrorManifest"] = None
    updated_at: Optional[str] = None

    def record(self, gtid: Optional[str]) -> None:
        if not gtid:
            return
        self.last_gtid = gtid
        if gtid in self.read_gtids:
            return
        self.read_gtids.add(gtid)
        try:
            self.gtids.merge_gtid(Gtid(gtid))
        except Exception:
            # A malformed or duplicate GTID must not lose the stronger
            # evidence that the stream delivered it.
            pass
        self.updated_at = _utcnow()

    @property
    def resume_gtid(self) -> Optional[str]:
        if not self.gtids.gtids:
            return None
        return ",".join(str(gtid) for gtid in self.gtids.gtids)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "gtids": self.resume_gtid,
            "read_gtids": sorted(self.read_gtids),
            "last_gtid": self.last_gtid,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceCursor":
        cursor = cls(
            source=data["source"],
            gtids=GtidSet(data.get("gtids")),
            read_gtids=set(data.get("read_gtids") or ()),
            last_gtid=data.get("last_gtid"),
            updated_at=data.get("updated_at"),
        )
        manifest = data.get("manifest")
        if manifest:
            cursor.manifest = MirrorManifest.from_dict(manifest)
        return cursor


@dataclass
class MirrorManifest:
    """A source's statement about which GTIDs belong to the same group.

    ``tail_only`` means the source can attest the tail of its binlog but can
    no longer locate the prepare content.  Such a manifest is useful as
    corroboration, but it must not be turned into a published prepare row.
    """

    source: Optional[str] = None
    groups: List[Dict[str, Any]] = field(default_factory=list)
    gtids: List[str] = field(default_factory=list)
    tail_only: bool = False
    complete: bool = True
    missing_ranges: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalised: List[Dict[str, Any]] = []
        for group in self.groups:
            item = dict(group)
            item.setdefault("gtids", [])
            item["gtids"] = [str(g) for g in item["gtids"]]
            item.setdefault("has_prepare_content", not self.tail_only)
            normalised.append(item)
        self.groups = normalised
        self.gtids = [str(g) for g in self.gtids]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "groups": self.groups,
            "gtids": self.gtids,
            "tail_only": self.tail_only,
            "complete": self.complete,
            "missing_ranges": self.missing_ranges,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MirrorManifest":
        return cls(
            source=data.get("source"),
            groups=list(data.get("groups") or ()),
            gtids=list(data.get("gtids") or ()),
            tail_only=bool(data.get("tail_only", False)),
            complete=bool(data.get("complete", True)),
            missing_ranges=list(data.get("missing_ranges") or ()),
            metadata=dict(data.get("metadata") or ()),
        )

    def _all_gtids(self) -> Set[str]:
        result = set(self.gtids)
        for group in self.groups:
            result.update(group.get("gtids") or ())
        return result

    def attests(self, gtid: Optional[str]) -> bool:
        if not gtid:
            return False
        return gtid in self._all_gtids()

    def group_for_gtid(self, gtid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not gtid:
            return None
        for group in self.groups:
            if gtid in (group.get("gtids") or ()):
                return group
        return None

    def group_for_identity(
        self, identity: Optional[XAIdentity], prepare_gtid: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        for group in self.groups:
            if prepare_gtid and prepare_gtid in (group.get("gtids") or ()):
                return group
            group_xid = group.get("xid")
            if identity and group_xid:
                if XAIdentity.from_xid(group_xid) == identity:
                    return group
            if (
                identity
                and group.get("gtrid") == identity.gtrid
                and str(group.get("bqual", "")) == identity.bqual
            ):
                return group
        return None

    def prepare_content(
        self, identity: Optional[XAIdentity]
    ) -> Optional[Dict[str, Any]]:
        """Return a group that actually carries prepare content.

        A tail-only manifest never does, even when it can correlate GTIDs.
        """

        if self.tail_only:
            return None
        group = self.group_for_identity(identity)
        if group and group.get("has_prepare_content", True):
            return group
        return None

    def same_group(self, first: str, second: str) -> bool:
        group = self.group_for_gtid(first)
        return bool(group and second in (group.get("gtids") or ()))


@dataclass
class RowRecord:
    source: str
    gtid: Optional[str]
    schema: Optional[str]
    table: Optional[str]
    kind: str
    primary_key: Any = None
    before: Any = None
    after: Any = None
    values: Any = None
    log_file: Optional[str] = None
    log_pos: Optional[int] = None

    @property
    def identity(self) -> Tuple[Any, ...]:
        # The same effect can be reported by two sources under different
        # GTIDs (prepare on A, commit on B).  Within one group the GTID is
        # therefore not part of the row identity; the transaction identity
        # is already carried by the group.
        return (
            self.schema,
            self.table,
            self.kind,
            _stable(self.primary_key),
            _stable(self.before),
            _stable(self.after),
            _stable(self.values),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "gtid": self.gtid,
            "schema": self.schema,
            "table": self.table,
            "kind": self.kind,
            "primary_key": self.primary_key,
            "before": self.before,
            "after": self.after,
            "values": self.values,
            "log_file": self.log_file,
            "log_pos": self.log_pos,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RowRecord":
        return cls(**data)


@dataclass
class PublishedTransaction:
    group_id: str
    outcome: str
    rows: List[RowRecord]
    prepare_gtid: Optional[str] = None
    final_gtid: Optional[str] = None
    source: Optional[str] = None
    target_confirmed: bool = False
    target_receipt: Any = None
    complete: bool = True
    published_at: str = field(default_factory=_utcnow)
    confirmed_at: Optional[str] = None

    @property
    def key(self) -> str:
        return self.group_id

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "outcome": self.outcome,
            "rows": [row.to_dict() for row in self.rows],
            "prepare_gtid": self.prepare_gtid,
            "final_gtid": self.final_gtid,
            "source": self.source,
            "target_confirmed": self.target_confirmed,
            "target_receipt": self.target_receipt,
            "complete": self.complete,
            "published_at": self.published_at,
            "confirmed_at": self.confirmed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishedTransaction":
        return cls(
            group_id=data["group_id"],
            outcome=data["outcome"],
            rows=[RowRecord.from_dict(row) for row in data.get("rows") or ()],
            prepare_gtid=data.get("prepare_gtid"),
            final_gtid=data.get("final_gtid"),
            source=data.get("source"),
            target_confirmed=bool(data.get("target_confirmed", False)),
            target_receipt=data.get("target_receipt"),
            complete=bool(data.get("complete", True)),
            published_at=data.get("published_at") or _utcnow(),
            confirmed_at=data.get("confirmed_at"),
        )


@dataclass
class PreparedGroup:
    group_id: str
    identity: Optional[XAIdentity] = None
    prepare_gtid: Optional[str] = None
    prepare_source: Optional[str] = None
    final_gtid: Optional[str] = None
    final_source: Optional[str] = None
    outcome: Optional[str] = None  # committed | rolled_back
    rows: List[RowRecord] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)
    manifest_groups: Set[str] = field(default_factory=set)
    match_basis: Optional[Any] = None
    prepare_seen: bool = False
    requires_prepare: bool = False
    missing_rows: bool = False
    blocked_reason: Optional[str] = None
    target_confirmed: bool = False
    target_receipt: Any = None
    target_confirmed_at: Optional[str] = None
    publication: Optional[PublishedTransaction] = None
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    @property
    def is_final(self) -> bool:
        return self.outcome in ("committed", "rolled_back")

    @property
    def has_complete_result(self) -> bool:
        return self.is_final and not self.missing_rows and self.blocked_reason is None

    @property
    def is_prepared_only(self) -> bool:
        return self.prepare_seen and not self.is_final

    @property
    def publishable(self) -> bool:
        return (
            self.has_complete_result
            and self.publication is None
            and (not self.requires_prepare or self.prepare_seen)
        )

    @property
    def missing_prepare(self) -> bool:
        return self.missing_rows and (self.requires_prepare and not self.prepare_seen)

    def add_rows(self, rows: Iterable[RowRecord]) -> None:
        seen = {row.identity for row in self.rows}
        for row in rows:
            if row.identity in seen:
                continue
            seen.add(row.identity)
            self.rows.append(row)
        self.updated_at = _utcnow()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "identity": (
                {
                    "format_id": self.identity.format_id,
                    "gtrid": self.identity.gtrid,
                    "bqual": self.identity.bqual,
                }
                if self.identity
                else None
            ),
            "prepare_gtid": self.prepare_gtid,
            "prepare_source": self.prepare_source,
            "final_gtid": self.final_gtid,
            "final_source": self.final_source,
            "outcome": self.outcome,
            "rows": [row.to_dict() for row in self.rows],
            "sources": sorted(self.sources),
            "manifest_groups": sorted(self.manifest_groups),
            "match_basis": self.match_basis,
            "prepare_seen": self.prepare_seen,
            "requires_prepare": self.requires_prepare,
            "missing_rows": self.missing_rows,
            "blocked_reason": self.blocked_reason,
            "target_confirmed": self.target_confirmed,
            "target_receipt": self.target_receipt,
            "target_confirmed_at": self.target_confirmed_at,
            "publication": self.publication.to_dict() if self.publication else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreparedGroup":
        identity = data.get("identity")
        group = cls(
            group_id=data["group_id"],
            identity=XAIdentity(**identity) if identity else None,
            prepare_gtid=data.get("prepare_gtid"),
            prepare_source=data.get("prepare_source"),
            final_gtid=data.get("final_gtid"),
            final_source=data.get("final_source"),
            outcome=data.get("outcome"),
            rows=[RowRecord.from_dict(row) for row in data.get("rows") or ()],
            sources=set(data.get("sources") or ()),
            manifest_groups=set(data.get("manifest_groups") or ()),
            match_basis=data.get("match_basis"),
            prepare_seen=bool(data.get("prepare_seen", False)),
            requires_prepare=bool(data.get("requires_prepare", False)),
            missing_rows=bool(data.get("missing_rows", False)),
            blocked_reason=data.get("blocked_reason"),
            target_confirmed=bool(data.get("target_confirmed", False)),
            target_receipt=data.get("target_receipt"),
            target_confirmed_at=data.get("target_confirmed_at"),
            created_at=data.get("created_at") or _utcnow(),
            updated_at=data.get("updated_at") or _utcnow(),
        )
        publication = data.get("publication")
        if publication:
            group.publication = PublishedTransaction.from_dict(publication)
        return group


class DeliveryCheckpoint:
    """Durable, reopenable state shared by two overlapping sources.

    The object is intentionally JSON serialisable.  A stream reader may feed
    it automatically through ``delivery_checkpoint=`` or callers may use the
    explicit ``record_*`` methods from a test source.
    """

    VERSION = 1

    def __init__(
        self,
        path: Optional[str] = None,
        default_source: str = "default",
        autosave: bool = False,
    ):
        """Create an empty or path-backed delivery checkpoint.

        Args:
            path: Optional JSON file used by :meth:`save` and
                :meth:`reopen`.
            default_source: Source name used when the caller does not set
                one explicitly.
            autosave: Persist after every mutation.  Without it, call
                :meth:`save` at the delivery boundary.
        """
        self.path = path
        self.default_source = default_source
        self.autosave = autosave
        self.groups: Dict[str, PreparedGroup] = {}
        self.published: Dict[str, PublishedTransaction] = {}
        self.cursors: Dict[str, SourceCursor] = {}
        self.missing_ranges: List[Dict[str, Any]] = []
        self.target_rejections: List[Dict[str, Any]] = []
        self.pending_target_acks: Dict[str, Dict[str, Any]] = {}
        self.next_source: Optional[str] = None
        self.last_source: Optional[str] = None
        self._current_source: Optional[str] = None
        self._current_gtid: Optional[str] = None
        self._current_rows: List[RowRecord] = []
        self._current_xa: Optional[XAIdentity] = None
        self._current_match_basis: Optional[Any] = None
        self._current_in_txn = False
        self._ambiguous: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Construction and persistence
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str, **kwargs: Any) -> "DeliveryCheckpoint":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle, object_hook=_json_hook)
        checkpoint = cls.from_dict(data, **kwargs)
        checkpoint.path = path
        return checkpoint

    @classmethod
    def from_dict(cls, data: Dict[str, Any], **kwargs: Any) -> "DeliveryCheckpoint":
        checkpoint = cls(
            path=data.get("path"),
            default_source=data.get("default_source", "default"),
            autosave=bool(data.get("autosave", False)),
        )
        checkpoint.groups = {
            key: PreparedGroup.from_dict(value)
            for key, value in (data.get("groups") or {}).items()
        }
        checkpoint.published = {
            key: PublishedTransaction.from_dict(value)
            for key, value in (data.get("published") or {}).items()
        }
        checkpoint.cursors = {
            key: SourceCursor.from_dict(value)
            for key, value in (data.get("cursors") or {}).items()
        }
        checkpoint.missing_ranges = list(data.get("missing_ranges") or ())
        checkpoint.target_rejections = list(data.get("target_rejections") or ())
        checkpoint.pending_target_acks = dict(data.get("pending_target_acks") or ())
        checkpoint.next_source = data.get("next_source")
        checkpoint.last_source = data.get("last_source")
        checkpoint._ambiguous = list(data.get("ambiguous") or ())
        for group in checkpoint.groups.values():
            if group.publication:
                checkpoint.published.setdefault(
                    group.publication.group_id, group.publication
                )
        return checkpoint

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "path": self.path,
            "default_source": self.default_source,
            "autosave": self.autosave,
            "groups": {
                key: group.to_dict() for key, group in sorted(self.groups.items())
            },
            "published": {
                key: published.to_dict()
                for key, published in sorted(self.published.items())
            },
            "cursors": {
                key: cursor.to_dict() for key, cursor in sorted(self.cursors.items())
            },
            "missing_ranges": self.missing_ranges,
            "target_rejections": self.target_rejections,
            "pending_target_acks": self.pending_target_acks,
            "next_source": self.next_source,
            "last_source": self.last_source,
            "ambiguous": self._ambiguous,
        }

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.path
        if not target:
            raise DeliveryCheckpointError("No path configured for delivery checkpoint")
        directory = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=".delivery-checkpoint-", dir=directory, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.path = target
        return target

    def _touch(self) -> None:
        if self.autosave and self.path:
            self.save()

    def reopen(self) -> "DeliveryCheckpoint":
        """Reload the persisted checkpoint and return a verified view."""

        if not self.path:
            raise DeliveryCheckpointError("No path configured for delivery checkpoint")
        restored = DeliveryCheckpoint.load(self.path)
        self.__dict__.update(restored.__dict__)
        return self

    # ------------------------------------------------------------------
    # Source and range bookkeeping
    # ------------------------------------------------------------------
    def set_source(self, source: Optional[str]) -> str:
        source = source or self.default_source
        if source != self._current_source or source not in self.cursors:
            self._current_source = source
            self.cursor(source)
            self._touch()
        return source

    def cursor(self, source: Optional[str] = None) -> SourceCursor:
        source = source or self._current_source or self.default_source
        if source not in self.cursors:
            self.cursors[source] = SourceCursor(source=source)
        return self.cursors[source]

    def record_stream_read(
        self, gtid: Optional[str], source: Optional[str] = None
    ) -> SourceCursor:
        """Remember that a source delivered a GTID.

        This is stream evidence only.  It never confirms the target
        warehouse write.
        """

        cursor = self.cursor(source)
        cursor.record(gtid)
        self._touch()
        return cursor

    def stream_read_gtids(self, source: Optional[str] = None) -> Set[str]:
        """GTIDs seen on the stream, independent of target confirmation.

        Without a source, this is the union across all sources, so a caller
        cannot mistake per-source cursors for target confirmations.
        """

        if source is not None:
            return set(self.cursor(source).read_gtids)
        result: Set[str] = set()
        for cursor in self.cursors.values():
            result.update(cursor.read_gtids)
        return result

    def target_confirmed_gtids(self, source: Optional[str] = None) -> Set[str]:
        """GTIDs whose transaction received an explicit target receipt."""

        confirmed: Set[str] = set()
        for group in self.groups.values():
            if not group.target_confirmed:
                continue
            if source and source not in group.sources:
                continue
            for gtid in (group.prepare_gtid, group.final_gtid):
                if gtid:
                    confirmed.add(gtid)
        return confirmed

    def switch_source(
        self,
        source: str,
        resume_gtid: Optional[str] = None,
        pending_write_ack: Optional[Any] = None,
        manifest: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Move the delivery cursor, preserving anything not yet confirmed.

        ``pending_write_ack`` records the transaction (group id, GTID or
        identity) whose target write was in flight before the switch.  It
        stays pending until :meth:`confirm_target` is called; merely reading
        the GTID from the new source does not settle it.
        """

        previous = self._current_source
        if previous:
            self.last_source = previous
        self.next_source = source
        self.set_source(source)
        if resume_gtid:
            self.seed_source(source, resume_gtid)
        if manifest is not None:
            self.apply_manifest(source=source, manifest=manifest)
        if pending_write_ack is not None:
            key = self._describe_key(pending_write_ack)
            self.pending_target_acks[key] = {
                "source": previous or source,
                "recorded_at": _utcnow(),
            }
        self._touch()
        return self.resume_plan()

    def seed_source(self, source: str, gtid_set: Optional[str]) -> SourceCursor:
        cursor = self.cursor(source)
        if not gtid_set:
            return cursor
        cursor.gtids = GtidSet(gtid_set)
        cursor.updated_at = _utcnow()
        return cursor

    def set_resume_gtid(self, source: str, gtid_set: Optional[str]) -> SourceCursor:
        cursor = self.seed_source(source, gtid_set)
        self._touch()
        return cursor

    def record_missing_range(
        self,
        source: Optional[str],
        start_gtid: Optional[str],
        end_gtid: Optional[str] = None,
        reason: str = "not covered",
    ) -> Dict[str, Any]:
        entry = {
            "source": source or self.default_source,
            "start": start_gtid,
            "end": end_gtid,
            "reason": reason,
            "recorded_at": _utcnow(),
        }
        self.missing_ranges.append(entry)
        self._touch()
        return entry

    def missing_range(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.record_missing_range(*args, **kwargs)

    def add_missing_range(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.record_missing_range(*args, **kwargs)

    def uncovered_ranges(self) -> List[Dict[str, Any]]:
        return list(self.missing_ranges)

    def resume_gtid(self, source: Optional[str] = None) -> Optional[str]:
        source = (
            source or self.next_source or self._current_source or self.default_source
        )
        return self.cursor(source).resume_gtid

    def resume_plan(self) -> Dict[str, Any]:
        source = self.next_source or self._current_source or self.default_source
        return {
            "source": source,
            "gtid": self.resume_gtid(source),
            "sources": self.resume_targets(),
            "missing_ranges": list(self.missing_ranges),
            "pending_target_acks": sorted(self.pending_target_acks),
        }

    def resume_targets(self) -> Dict[str, Optional[str]]:
        """Per-source resumable position.

        Each collector covers a different binlog range, so a single merged
        set is not a valid position for either one.  The per-source cursors
        are kept independently.
        """

        return {
            source: cursor.resume_gtid
            for source, cursor in sorted(self.cursors.items())
        }

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------
    def observe_event(
        self,
        event: Any,
        source: Optional[str] = None,
        event_type: Optional[int] = None,
        log_file: Optional[str] = None,
        log_pos: Optional[int] = None,
        effect_key: Optional[str] = None,
        match_basis: Optional[Any] = None,
    ) -> Optional[PreparedGroup]:
        """Feed one decoded binlog event.

        Unknown events are ignored, which keeps this hook usable from
        ``BinLogStreamReader`` without changing its event filtering.
        """

        if event is None:
            return None
        source = source or self._current_source or self.default_source
        self.set_source(source)

        from .event import GtidEvent, XidEvent
        from .row_event import (
            DeleteRowsEvent,
            PartialUpdateRowsEvent,
            UpdateRowsEvent,
            WriteRowsEvent,
        )

        if isinstance(event, GtidEvent):
            self._current_gtid = event.gtid
            self._current_rows = []
            self._current_xa = None
            self._current_match_basis = None
            # Do not advance the resume position here.  The GTID counts as
            # read only at its real boundary (XAPrepare, Xid, COMMIT/ROLLBACK,
            # or a single non-transactional statement), matching the
            # reconnect contract of BinLogStreamReader.
            return None

        if isinstance(
            event,
            (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, PartialUpdateRowsEvent),
        ):
            row = self._row_from_event(event, source, log_file, log_pos)
            if row is not None:
                self._current_rows.append(row)
            return None

        if isinstance(event, XAPrepareEvent):
            identity = XAIdentity.from_event(event)
            self._current_xa = identity
            group = self.record_prepare(
                gtid=self._current_gtid,
                xid=identity,
                rows=list(self._current_rows),
                source=source,
                effect_key=effect_key,
                match_basis=match_basis,
                one_phase=bool(getattr(event, "one_phase", False)),
                log_file=log_file,
                log_pos=log_pos,
            )
            self._current_gtid = None
            self._current_rows = []
            self._current_xa = None
            return group

        if isinstance(event, XidEvent):
            if self._current_gtid is None:
                # Without a GTID there is no stable identity that survives a
                # reopen; do not collapse unrelated transactions into one
                # anonymous group.
                self._current_rows = []
                self._current_xa = None
                self._current_in_txn = False
                return None
            committed = self.record_commit(
                gtid=self._current_gtid,
                rows=list(self._current_rows),
                source=source,
                match_basis=match_basis,
            )
            self._current_gtid = None
            self._current_rows = []
            self._current_xa = None
            self._current_in_txn = False
            return committed

        if isinstance(event, QueryEvent):
            parsed = parse_xa_query(event.query)
            if parsed is None:
                text = (event.query or "").strip()
                upper = text.upper()
                if upper == "BEGIN" or upper.startswith("START TRANSACTION"):
                    self._current_in_txn = True
                    return None
                if upper in ("COMMIT", "ROLLBACK", "XA COMMIT", "XA ROLLBACK"):
                    if self._current_gtid is None and not self._current_rows:
                        self._current_in_txn = False
                        return None
                    group = self.record_commit(
                        gtid=self._current_gtid,
                        rows=list(self._current_rows),
                        source=source,
                        rollback=upper == "ROLLBACK",
                        match_basis=match_basis,
                    )
                    self._current_gtid = None
                    self._current_rows = []
                    self._current_in_txn = False
                    return group
                if self._current_in_txn:
                    # A statement inside BEGIN/START TRANSACTION does not
                    # end its GTID group.
                    return None
                if self._current_gtid is not None:
                    # DDL and other single statements are their own group.
                    group = self.record_commit(
                        gtid=self._current_gtid,
                        rows=list(self._current_rows),
                        source=source,
                        match_basis=match_basis,
                    )
                    self._current_gtid = None
                    self._current_rows = []
                    return group
                return None
            command, identity, one_phase = parsed
            if command in ("START", "BEGIN"):
                self._current_xa = identity
                self._current_in_txn = True
                return None
            if command == "END":
                return None
            if command == "PREPARE":
                self._current_xa = identity
                group = self.record_prepare(
                    gtid=self._current_gtid,
                    xid=identity,
                    rows=list(self._current_rows),
                    source=source,
                    effect_key=effect_key,
                    match_basis=match_basis,
                    one_phase=one_phase,
                )
                self._current_gtid = None
                self._current_rows = []
                self._current_xa = None
                self._current_in_txn = False
                return group
            if command == "COMMIT":
                group = self.record_commit(
                    gtid=self._current_gtid,
                    xid=identity,
                    rows=list(self._current_rows),
                    source=source,
                    effect_key=effect_key,
                    match_basis=match_basis,
                    one_phase=one_phase,
                )
                self._current_gtid = None
                self._current_rows = []
                self._current_xa = None
                self._current_in_txn = False
                return group
            if command == "ROLLBACK":
                group = self.record_commit(
                    gtid=self._current_gtid,
                    xid=identity,
                    rows=list(self._current_rows),
                    source=source,
                    effect_key=effect_key,
                    match_basis=match_basis,
                    rollback=True,
                )
                self._current_gtid = None
                self._current_rows = []
                self._current_xa = None
                self._current_in_txn = False
                return group
        return None

    def record_row(
        self,
        gtid: Optional[str],
        kind: str,
        schema: Optional[str] = None,
        table: Optional[str] = None,
        primary_key: Any = None,
        before: Any = None,
        after: Any = None,
        values: Any = None,
        source: Optional[str] = None,
        log_file: Optional[str] = None,
        log_pos: Optional[int] = None,
    ) -> RowRecord:
        row = RowRecord(
            source=source or self._current_source or self.default_source,
            gtid=gtid,
            schema=schema,
            table=table,
            kind=kind,
            primary_key=primary_key,
            before=before,
            after=after,
            values=values,
            log_file=log_file,
            log_pos=log_pos,
        )
        self._current_rows.append(row)
        return row

    def _row_from_event(
        self,
        event: Any,
        source: str,
        log_file: Optional[str],
        log_pos: Optional[int],
    ) -> Optional[RowRecord]:
        rows = getattr(event, "rows", None)
        if not rows:
            return None
        class_name = event.__class__.__name__
        if "Write" in class_name:
            kind = "insert"
        elif "Delete" in class_name:
            kind = "delete"
        else:
            kind = "update"

        records: List[RowRecord] = []
        # One event can contain several rows; keep each row independently
        # addressable in the checkpoint.
        for row in rows:
            records.append(
                RowRecord(
                    source=source,
                    gtid=self._current_gtid,
                    schema=getattr(event, "schema", None),
                    table=getattr(event, "table", None),
                    kind=kind,
                    primary_key=getattr(event, "primary_key", None),
                    before=row.get("before_values"),
                    after=row.get("after_values"),
                    values=row.get("values"),
                    log_file=log_file,
                    log_pos=log_pos,
                )
            )
        if not records:
            return None
        # Stash all but the first; the caller only appends one return value.
        self._current_rows.extend(records[:-1])
        return records[-1]

    # ------------------------------------------------------------------
    # XA lifecycle
    # ------------------------------------------------------------------
    def record_prepare(
        self,
        gtid: Optional[str],
        xid: Any = None,
        rows: Optional[Iterable[Any]] = None,
        source: Optional[str] = None,
        effect_key: Optional[str] = None,
        identity: Optional[TransactionIdentity] = None,
        match_basis: Optional[Any] = None,
        one_phase: bool = False,
        log_file: Optional[str] = None,
        log_pos: Optional[int] = None,
    ) -> PreparedGroup:
        source = source or self._current_source or self.default_source
        self.set_source(source)
        xa_identity = (
            identity.xid if identity and identity.xid else XAIdentity.from_xid(xid)
        )
        if match_basis is None and identity is not None and identity.basis:
            match_basis = identity.basis
        if match_basis is None:
            match_basis = self._current_match_basis
        if effect_key is None and identity is not None:
            effect_key = identity.key
        if effect_key is None:
            # A commit may have arrived from the other source before its
            # prepare content was located.  Fill that held group instead of
            # creating a second effect for the same XA name.  A group that
            # already has prepare content (including an older transaction
            # that reused the same XA name) is never a candidate.
            held = [
                candidate
                for candidate in self.groups.values()
                if candidate.requires_prepare
                and not candidate.prepare_seen
                and candidate.missing_rows
                and not candidate.target_confirmed
                and (xa_identity is None or candidate.identity == xa_identity)
            ]
            if len(held) == 1:
                effect_key = held[0].group_id
            elif len(held) > 1:
                self._ambiguous.append(
                    {
                        "phase": "prepare",
                        "gtid": gtid,
                        "candidates": [candidate.group_id for candidate in held],
                    }
                )
        if effect_key is None and gtid is not None:
            # A complete manifest may already have materialised this prepare
            # before the real prepare event arrives.  Join that placeholder
            # instead of creating a second group for the same prepare GTID.
            manifest_group = self._manifest_group_for_commit(gtid, xa_identity)
            if manifest_group is not None:
                effect_key = manifest_group.group_id
        if effect_key is None:
            effect_key = TransactionIdentity(
                prepare_gtid=gtid,
                xid=xa_identity,
                basis=str(match_basis) if match_basis else None,
            ).group_key()
        existing = self.groups.get(effect_key)
        if (
            existing is not None
            and existing.is_final
            # A final group that is still waiting for its prepare content is
            # this transaction's held record, not an older generation.
            and (existing.prepare_seen or not existing.requires_prepare)
            and gtid not in (existing.prepare_gtid, existing.final_gtid)
        ):
            # An explicit effect identity that was reused for a later
            # transaction must start a new generation instead of overwriting
            # the earlier final result.
            effect_key = "%s#%s" % (effect_key, gtid or "generation")
        group = self.groups.get(effect_key)
        if group is None:
            group = PreparedGroup(
                group_id=effect_key,
                identity=xa_identity,
                prepare_gtid=gtid,
                prepare_source=source,
                match_basis=match_basis,
            )
            self.groups[effect_key] = group
        else:
            if group.identity is None:
                group.identity = xa_identity
            if not group.prepare_gtid:
                group.prepare_gtid = gtid
                group.prepare_source = source
        group.prepare_seen = True
        group.sources.add(source)
        if group.missing_rows and not group.requires_prepare:
            # A placeholder created by a commit that could not be correlated
            # has no prepare requirement of its own: this prepare event is
            # its content.
            group.missing_rows = False
            if group.blocked_reason:
                group.blocked_reason = None
        if group.requires_prepare:
            # The missing prepare content has now been recovered from the
            # other source (or from this source's retained segment).
            group.missing_rows = False
            if group.blocked_reason == "prepared content unavailable":
                group.blocked_reason = None
        if gtid:
            self.record_stream_read(gtid, source)
        self._append_rows(group, rows or (), source, gtid, log_file, log_pos)
        if one_phase:
            # XA COMMIT ... ONE PHASE: the prepare event itself is the final
            # result and carries its rows.
            group.outcome = "committed"
            group.requires_prepare = False
            group.final_gtid = group.final_gtid or gtid
            group.final_source = group.final_source or source
        group.updated_at = _utcnow()
        self._touch()
        return group

    def record_commit(
        self,
        gtid: Optional[str] = None,
        xid: Any = None,
        rows: Optional[Iterable[Any]] = None,
        source: Optional[str] = None,
        effect_key: Optional[str] = None,
        identity: Optional[TransactionIdentity] = None,
        match_key: Optional[str] = None,
        match_basis: Optional[Any] = None,
        rollback: bool = False,
        log_file: Optional[str] = None,
        log_pos: Optional[int] = None,
        prepare_gtid: Optional[str] = None,
        one_phase: bool = False,
    ) -> PreparedGroup:
        source = source or self._current_source or self.default_source
        self.set_source(source)
        xa_identity = (
            identity.xid if identity and identity.xid else XAIdentity.from_xid(xid)
        )
        # An XA commit/rollback needs a prepared group to supply the rows.
        # A plain transaction (XidEvent or COMMIT without XA) does not.
        is_xa = (
            xa_identity is not None
            and bool(xa_identity.xa_key)
            and (xid is not None or (identity is not None and identity.xid is not None))
        )
        if match_basis is None and identity is not None and identity.basis:
            match_basis = identity.basis
        if effect_key is None and identity is not None:
            effect_key = identity.key
        if effect_key is not None and effect_key in self.groups:
            candidate = self.groups[effect_key]
            same_generation = (
                gtid is not None
                and gtid in (candidate.prepare_gtid, candidate.final_gtid)
            ) or (
                identity is not None
                and identity.prepare_gtid is not None
                and identity.prepare_gtid == candidate.prepare_gtid
            )
            if candidate.is_final and not same_generation:
                # The explicit effect identity was reused for a later
                # transaction; select that generation instead of the old one.
                generation = (
                    identity.prepare_gtid
                    if identity and identity.prepare_gtid
                    else gtid
                )
                effect_key = "%s#%s" % (effect_key, generation or "generation")
        if effect_key is None:
            manifest_group = self._manifest_group_for_commit(gtid, xa_identity)
            if (
                manifest_group is not None
                and not manifest_group.prepare_seen
                and not manifest_group.is_final
                and dict(self.groups).get(manifest_group.group_id) is not None
            ):
                # Prefer an existing unresolved group over a manifest
                # materialisation with the same evidence.
                manifest_group = None
            if effect_key is None and manifest_group is not None:
                effect_key = manifest_group.group_id
        if effect_key is None:
            effect_key = match_key
        if match_basis is not None:
            self._current_match_basis = match_basis
        group, ambiguous = self._find_commit_group(
            gtid=gtid,
            identity=xa_identity if is_xa else None,
            effect_key=effect_key,
            prepare_gtid=prepare_gtid or (identity.prepare_gtid if identity else None),
            source=source,
            match_basis=match_basis,
        )
        if group is None:
            needs_prepare = is_xa and not one_phase and not rollback
            if is_xa:
                key = effect_key or "unresolved:%s" % (gtid or xa_identity.xa_key)
            else:
                key = effect_key or TransactionIdentity(prepare_gtid=gtid).group_key()
            group = PreparedGroup(
                group_id=key,
                identity=xa_identity if is_xa else None,
                final_gtid=gtid,
                final_source=source,
                outcome="rolled_back" if rollback else "committed",
                match_basis=match_basis,
                requires_prepare=needs_prepare,
                missing_rows=needs_prepare or bool(ambiguous),
                blocked_reason=(
                    "ambiguous XA identity"
                    if ambiguous
                    else ("prepared content unavailable" if needs_prepare else None)
                ),
            )
            self.groups[key] = group
        else:
            if (
                group.is_final
                and (group.prepare_seen or not group.requires_prepare)
                and gtid is not None
                and gtid not in (group.prepare_gtid, group.final_gtid)
            ):
                # The same effect identity arrived again for a different
                # transaction.  Never rewrite the earlier final result.
                key = "%s#%s" % (group.group_id, gtid)
                group = self.groups.get(key)
                if group is None:
                    group = PreparedGroup(
                        group_id=key,
                        identity=xa_identity if is_xa else None,
                        final_gtid=gtid,
                        final_source=source,
                        outcome="rolled_back" if rollback else "committed",
                        match_basis=match_basis,
                        requires_prepare=(is_xa and not one_phase and not rollback),
                        missing_rows=(is_xa and not one_phase and not rollback),
                        blocked_reason=(
                            "prepared content unavailable"
                            if (is_xa and not one_phase and not rollback)
                            else None
                        ),
                    )
                    self.groups[key] = group
            group.outcome = "rolled_back" if rollback else "committed"
            group.final_gtid = gtid or group.final_gtid
            group.final_source = source
            if is_xa and not one_phase and not rollback:
                group.requires_prepare = True
            if match_basis is not None:
                group.match_basis = match_basis
        group.sources.add(source)
        if gtid:
            self.record_stream_read(gtid, source)
        self._append_rows(group, rows or (), source, gtid, log_file, log_pos)
        group.updated_at = _utcnow()
        if ambiguous:
            group.blocked_reason = group.blocked_reason or "ambiguous XA identity"
            group.missing_rows = True
            self._ambiguous.append(
                {
                    "gtid": gtid,
                    "xid": xa_identity.to_dict()
                    if hasattr(xa_identity, "to_dict")
                    else None,
                    "candidates": [candidate.group_id for candidate in ambiguous],
                }
            )
        elif group.prepare_seen and not group.rows:
            # A prepared group with no row events is a valid empty effect.
            group.missing_rows = False
            group.blocked_reason = None
        self._touch()
        return group

    def record_rollback(self, *args: Any, **kwargs: Any) -> PreparedGroup:
        kwargs["rollback"] = True
        return self.record_commit(*args, **kwargs)

    def _manifest_group_for_commit(
        self,
        gtid: Optional[str],
        identity: Optional[XAIdentity],
    ) -> Optional[PreparedGroup]:
        """Find a manifest-derived prepare group for a commit.

        The manifest is only a correlation source here; whether its content
        may actually fill the group is decided by the normal
        ``tail_only``/``prepare_seen`` rules.
        """

        if not gtid:
            return None
        for cursor in self.cursors.values():
            manifest = cursor.manifest
            if manifest is None or manifest.tail_only:
                continue
            group_data = manifest.group_for_gtid(gtid)
            if not group_data:
                continue
            prepare_gtid = group_data.get("prepare_gtid")
            for group in self.groups.values():
                if prepare_gtid and group.prepare_gtid == prepare_gtid:
                    return group
                if group.prepare_gtid in (group_data.get("gtids") or ()):
                    return group
            if identity is not None:
                candidate = manifest.group_for_identity(identity)
                if candidate is group_data:
                    for group in self.groups.values():
                        if group.identity == identity and group.prepare_seen:
                            return group
        return None

    def _find_commit_group(
        self,
        gtid: Optional[str],
        identity: Optional[XAIdentity],
        effect_key: Optional[str],
        prepare_gtid: Optional[str],
        source: str,
        match_basis: Optional[Any],
    ) -> Tuple[Optional[PreparedGroup], List[PreparedGroup]]:
        if effect_key and effect_key in self.groups:
            return self.groups[effect_key], []
        if prepare_gtid:
            for group in self.groups.values():
                if group.prepare_gtid == prepare_gtid:
                    return group, []
        if gtid:
            for group in self.groups.values():
                if gtid in (group.prepare_gtid, group.final_gtid):
                    return group, []
            if self.cursors:
                for cursor in self.cursors.values():
                    if cursor.manifest and cursor.manifest.attests(gtid):
                        group = self._group_from_manifest(cursor.manifest, gtid, source)
                        if group is not None:
                            return group, []
        if identity is not None:
            candidates = [
                group
                for group in self.groups.values()
                if group.identity == identity
                and group.prepare_seen
                and not group.is_final
            ]
            if len(candidates) == 1:
                return candidates[0], []
            if len(candidates) > 1:
                return None, candidates
        if match_basis is not None:
            candidates = [
                group
                for group in self.groups.values()
                if group.match_basis == match_basis and not group.is_final
            ]
            if len(candidates) == 1:
                return candidates[0], []
            if len(candidates) > 1:
                return None, candidates
        return None, []

    def _group_from_manifest(
        self, manifest: MirrorManifest, gtid: str, source: str
    ) -> Optional[PreparedGroup]:
        group_data = manifest.group_for_gtid(gtid)
        if not group_data:
            return None
        gtids = set(group_data.get("gtids") or ())
        prepare_gtid = group_data.get("prepare_gtid")
        group = None
        for candidate in self.groups.values():
            if candidate.prepare_gtid in gtids or candidate.final_gtid in gtids:
                group = candidate
                break
            if prepare_gtid and candidate.prepare_gtid == prepare_gtid:
                group = candidate
                break
        if group is None:
            return None
        group.manifest_groups.add(str(group_data.get("group_id", gtid)))
        group.sources.add(source)
        group.manifest_groups.add(manifest.source or source)
        if manifest.tail_only:
            # Corroboration is useful, but tail-only evidence cannot supply
            # the missing prepare rows.
            if not group.prepare_seen:
                group.missing_rows = True
                group.blocked_reason = (
                    group.blocked_reason or "prepared content unavailable"
                )
        elif not group.prepare_seen:
            self._adopt_manifest_prepare(group, group_data, source)
        return group

    def _append_rows(
        self,
        group: PreparedGroup,
        rows: Iterable[Any],
        source: str,
        gtid: Optional[str],
        log_file: Optional[str],
        log_pos: Optional[int],
    ) -> None:
        converted: List[RowRecord] = []
        for item in rows:
            if isinstance(item, RowRecord):
                converted.append(item)
            elif isinstance(item, dict):
                converted.append(
                    RowRecord(
                        source=source,
                        gtid=item.get("gtid", gtid),
                        schema=item.get("schema"),
                        table=item.get("table"),
                        kind=item.get("kind", "row"),
                        primary_key=item.get("primary_key"),
                        before=item.get("before", item.get("before_values")),
                        after=item.get("after", item.get("after_values")),
                        values=item.get("values"),
                        log_file=item.get("log_file", log_file),
                        log_pos=item.get("log_pos", log_pos),
                    )
                )
            else:
                converted.append(
                    RowRecord(
                        source=source,
                        gtid=gtid,
                        schema=getattr(item, "schema", None),
                        table=getattr(item, "table", None),
                        kind=getattr(item, "kind", item.__class__.__name__),
                        primary_key=getattr(item, "primary_key", None),
                        before=getattr(item, "before", None),
                        after=getattr(item, "after", None),
                        values=getattr(item, "values", None),
                        log_file=log_file,
                        log_pos=log_pos,
                    )
                )
        group.add_rows(converted)

    # ------------------------------------------------------------------
    # Manifests
    # ------------------------------------------------------------------
    def apply_manifest(
        self,
        manifest: Any,
        source: Optional[str] = None,
        **kwargs: Any,
    ) -> MirrorManifest:
        if isinstance(manifest, dict):
            manifest = MirrorManifest.from_dict(manifest)
        elif not isinstance(manifest, MirrorManifest):
            manifest = MirrorManifest(source=source, gtids=list(manifest or ()))
        if source:
            manifest.source = source
        elif manifest.source:
            source = manifest.source
        source = source or self._current_source or self.default_source
        self.cursor(source).manifest = manifest
        for missing in manifest.missing_ranges:
            self.record_missing_range(
                missing.get("source", source),
                missing.get("start"),
                missing.get("end"),
                missing.get("reason", "manifest"),
            )
        for group_data in manifest.groups:
            gtids = list(group_data.get("gtids") or ())
            local = None
            for gtid in gtids:
                local = local or self.groups.get(gtid)
                if local is None:
                    for candidate in self.groups.values():
                        if gtid in (candidate.prepare_gtid, candidate.final_gtid):
                            local = candidate
                            break
                if local is not None:
                    break
            if local is None:
                prepare_gtid = group_data.get("prepare_gtid")
                if prepare_gtid:
                    local = self.groups.get(prepare_gtid)
                if local is None:
                    if not manifest.tail_only and (
                        group_data.get("prepare_rows") or group_data.get("rows")
                    ):
                        local = self._group_from_manifest_prepare(
                            group_data, gtids, source
                        )
                    if local is None:
                        continue
            local.sources.add(source)
            local.manifest_groups.add(source)
            local.manifest_groups.add(str(group_data.get("group_id", source)))
            if manifest.tail_only and not local.prepare_seen:
                local.missing_rows = True
                local.blocked_reason = (
                    local.blocked_reason or "prepared content unavailable"
                )
            elif not manifest.tail_only and not local.prepare_seen:
                self._adopt_manifest_prepare(local, group_data, source)
        self.next_source = self.next_source or source
        self._touch()
        return manifest

    def _group_from_manifest_prepare(
        self,
        group_data: Dict[str, Any],
        gtids: List[str],
        source: str,
    ) -> Optional[PreparedGroup]:
        """Materialise a group from a complete manifest's prepare content.

        The group id is derived from the prepare GTID so that a later event
        carrying the same prepare identity joins this group, and a commit
        carrying only the other GTID can still be correlated through the
        manifest.
        """

        prepare_gtid = group_data.get("prepare_gtid") or (gtids[0] if gtids else None)
        if not prepare_gtid:
            return None
        group_id = str(
            TransactionIdentity(
                prepare_gtid=prepare_gtid,
                xid=XAIdentity.from_xid(group_data.get("xid"))
                if group_data.get("xid")
                else None,
                basis="manifest-prepare",
            ).group_key()
        )
        group = PreparedGroup(
            group_id=group_id,
            identity=(
                XAIdentity.from_xid(group_data.get("xid"))
                if group_data.get("xid")
                else None
            ),
            prepare_gtid=prepare_gtid,
            prepare_source=source,
            match_basis="manifest-prepare",
        )
        group.prepare_seen = True
        group.sources.add(source)
        self._adopt_manifest_prepare(group, group_data, source)
        self.groups[group_id] = group
        return group

    def _adopt_manifest_prepare(
        self,
        group: PreparedGroup,
        group_data: Dict[str, Any],
        source: str,
    ) -> None:
        """Use prepare content that a complete manifest actually carries.

        A mirror manifest is allowed to reproduce the prepared rows when the
        source still has them.  This path is never used for a tail-only
        manifest, which cannot locate the prepare content.
        """

        rows = group_data.get("prepare_rows") or group_data.get("rows")
        if not rows:
            return
        prepare_gtid = group_data.get("prepare_gtid")
        group.prepare_seen = True
        group.prepare_source = group.prepare_source or source
        if prepare_gtid:
            group.prepare_gtid = group.prepare_gtid or prepare_gtid
        self._append_rows(
            group, rows, source, prepare_gtid or group.prepare_gtid, None, None
        )
        group.missing_rows = False
        if group.blocked_reason == "prepared content unavailable":
            group.blocked_reason = None
        group.updated_at = _utcnow()

    def same_group_evidence(self, group_id: str) -> Dict[str, Any]:
        group = self.groups[group_id]
        manifests = []
        for cursor in self.cursors.values():
            if cursor.manifest and (
                cursor.manifest.attests(group.prepare_gtid)
                or cursor.manifest.attests(group.final_gtid)
                or str(group_id) in (cursor.manifest.metadata.get("group_ids") or ())
            ):
                manifests.append(cursor.manifest.to_dict())
        return {
            "group_id": group_id,
            "prepare_gtid": group.prepare_gtid,
            "final_gtid": group.final_gtid,
            "sources": sorted(group.sources),
            "manifest_groups": sorted(group.manifest_groups),
            "manifests": manifests,
            "stream_confirmed": bool(group.prepare_gtid or group.final_gtid),
            "target_confirmed": group.target_confirmed,
        }

    # ------------------------------------------------------------------
    # Target confirmations
    # ------------------------------------------------------------------
    def confirm_target(
        self,
        key: Any = None,
        gtid: Optional[str] = None,
        effect_key: Optional[str] = None,
        receipt: Any = None,
        identity: Any = None,
    ) -> bool:
        """Attach a target warehouse receipt to exactly one transaction.

        This is intentionally separate from :meth:`record_stream_read`.
        The receipt is accepted only for a final group; if the XA name is
        ambiguous or the group has no prepared content, the call is rejected
        instead of being re-used for a newer transaction with the same name.
        """

        if receipt is None:
            self._reject_target(
                key, gtid, effect_key, receipt, "missing target receipt"
            )
            return False
        group = self._resolve_group(
            key=key, gtid=gtid, effect_key=effect_key, identity=identity
        )
        if group is None:
            self._reject_target(key, gtid, effect_key, receipt, "unknown transaction")
            return False
        if not group.is_final:
            self._reject_target(
                key, gtid, effect_key, receipt, "transaction has no final result"
            )
            return False
        if group.outcome == "rolled_back":
            # A rolled-back transaction has no target write to confirm.  A
            # receipt from some other attempt must not be attached here.
            self._reject_target(
                key,
                gtid,
                effect_key,
                receipt,
                "transaction rolled back; no target write",
            )
            return False
        if group.missing_rows or group.blocked_reason:
            self._reject_target(
                key, gtid, effect_key, receipt, "prepared content unavailable"
            )
            return False
        if group.target_confirmed or self._receipt_used_elsewhere(group, receipt):
            if group.target_confirmed and group.target_receipt == receipt:
                return True
            reason = (
                "transaction already confirmed"
                if group.target_confirmed
                else "receipt belongs to another transaction attempt"
            )
            self._reject_target(key, gtid, effect_key, receipt, reason)
            return False
        group.target_confirmed = True
        group.target_receipt = receipt
        group.target_confirmed_at = _utcnow()
        if group.publication is not None:
            group.publication.target_confirmed = True
            group.publication.target_receipt = receipt
            group.publication.confirmed_at = group.target_confirmed_at
        self._settle_pending_ack(group)
        self._touch()
        return True

    def _settle_pending_ack(self, group: PreparedGroup) -> None:
        aliases = {
            str(group.group_id),
            str(group.prepare_gtid),
            str(group.final_gtid),
        }
        for pending in list(self.pending_target_acks):
            if pending in aliases:
                self.pending_target_acks.pop(pending, None)
                continue
            entry = self.pending_target_acks[pending]
            if isinstance(entry, dict) and entry.get("group_id") in aliases:
                self.pending_target_acks.pop(pending, None)

    def record_target_confirmation(self, *args: Any, **kwargs: Any) -> bool:
        return self.confirm_target(*args, **kwargs)

    def record_target_ack(self, *args: Any, **kwargs: Any) -> bool:
        return self.confirm_target(*args, **kwargs)

    def confirm_write(self, *args: Any, **kwargs: Any) -> bool:
        return self.confirm_target(*args, **kwargs)

    def _resolve_group(
        self,
        key: Any = None,
        gtid: Optional[str] = None,
        effect_key: Optional[str] = None,
        identity: Any = None,
    ) -> Optional[PreparedGroup]:
        candidates: List[PreparedGroup] = []
        for value in (effect_key, key):
            if value is None:
                continue
            text = str(value)
            group = self.groups.get(text)
            if group is not None:
                return group
            published = self.published.get(text)
            if published is not None:
                group = self.groups.get(published.group_id)
                if group is not None:
                    return group
        xa_identity = XAIdentity.from_xid(identity) if identity is not None else None
        for group in self.groups.values():
            if gtid and gtid in (group.prepare_gtid, group.final_gtid):
                candidates.append(group)
            elif xa_identity is not None and group.identity == xa_identity:
                candidates.append(group)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # A GTID or XA name that selects several transaction attempts is
            # not a valid confirmation key.
            self._ambiguous.append(
                {
                    "phase": "target-confirmation",
                    "gtid": gtid,
                    "candidates": [group.group_id for group in candidates],
                }
            )
        return None

    def _receipt_used_elsewhere(self, group: PreparedGroup, receipt: Any) -> bool:
        """A receipt is bound to the attempt that produced it.

        A reused XA name creates a new group, so an old receipt must not be
        accepted for the new group merely because the name matches.
        """

        if receipt is None:
            return False
        for other in self.groups.values():
            if other.group_id == group.group_id:
                continue
            if other.target_confirmed and other.target_receipt == receipt:
                return True
        return False

    def _reject_target(
        self,
        key: Any,
        gtid: Optional[str],
        effect_key: Optional[str],
        receipt: Any,
        reason: str,
    ) -> None:
        self.target_rejections.append(
            {
                "key": key,
                "gtid": gtid,
                "effect_key": effect_key,
                "receipt": receipt,
                "reason": reason,
                "at": _utcnow(),
            }
        )
        self._touch()

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------
    def publish(
        self,
        group_id: Optional[str] = None,
        include_rolled_back: bool = True,
        require_target_confirmation: bool = False,
    ) -> List[PublishedTransaction]:
        """Publish each complete final group at most once.

        Groups whose prepared content is unavailable are left pending; they
        neither disappear nor prevent unrelated complete groups from being
        published.
        """

        results: List[PublishedTransaction] = []
        if group_id is not None:
            group = self.groups.get(str(group_id))
            if group is None or group.publication is not None:
                return results
            if group.outcome == "rolled_back" and not include_rolled_back:
                return results
            if not group.publishable:
                return results
            if require_target_confirmation and not group.target_confirmed:
                return results
            results.append(self._publish_group(group))
            self._touch()
            return results

        for key in sorted(self.groups):
            group = self.groups[key]
            if group.outcome == "rolled_back" and not include_rolled_back:
                continue
            if not group.publishable:
                continue
            if require_target_confirmation and not group.target_confirmed:
                continue
            results.append(self._publish_group(group))
        self._touch()
        return results

    def publish_pending(self, **kwargs: Any) -> List[PublishedTransaction]:
        return self.publish(**kwargs)

    def _publish_group(self, group: PreparedGroup) -> PublishedTransaction:
        rows = [] if group.outcome == "rolled_back" else list(group.rows)
        publication = PublishedTransaction(
            group_id=group.group_id,
            outcome=group.outcome or "committed",
            rows=rows,
            prepare_gtid=group.prepare_gtid,
            final_gtid=group.final_gtid,
            source=group.final_source or group.prepare_source,
            target_confirmed=group.target_confirmed,
            target_receipt=group.target_receipt,
            complete=True,
            confirmed_at=group.target_confirmed_at,
        )
        group.publication = publication
        self.published[group.group_id] = publication
        return publication

    @property
    def published_transactions(self) -> List[PublishedTransaction]:
        return [self.published[key] for key in sorted(self.published)]

    def publication_count(self) -> int:
        return len(self.published)

    def verify_publications(self) -> List[str]:
        issues: List[str] = []
        for key, publication in self.published.items():
            if not publication.complete:
                issues.append("%s: publication is incomplete" % key)
            group = self.groups.get(key)
            if group is None:
                issues.append("%s: published transaction has no group" % key)
                continue
            if group.publication is None:
                issues.append("%s: group has no publication record" % key)
            if group.outcome == "committed" and group.missing_rows:
                issues.append("%s: published without prepared rows" % key)
            if group.outcome == "rolled_back" and publication.rows:
                issues.append("%s: rollback publication carries rows" % key)
        for key, group in self.groups.items():
            if group.publication is not None and key not in self.published:
                issues.append("%s: group publication missing from index" % key)
        return issues

    def verify(self) -> Dict[str, Any]:
        issues = self.verify_publications()
        return {
            "ok": not issues,
            "issues": issues,
            "published_count": len(self.published),
            "pending_prepared": [g.group_id for g in self.pending_prepared()],
            "blocked": [g.group_id for g in self.blocked_groups()],
            "missing_ranges": list(self.missing_ranges),
            "resume": self.resume_plan(),
        }

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    def prepared_only(self) -> List[PreparedGroup]:
        return [
            group
            for group in self._sorted_groups()
            if group.prepare_seen and not group.is_final
        ]

    def pending_prepared(self) -> List[PreparedGroup]:
        return [
            group
            for group in self._sorted_groups()
            if not group.is_final or group.missing_rows or group.blocked_reason
        ]

    def final_groups(self) -> List[PreparedGroup]:
        return [group for group in self._sorted_groups() if group.is_final]

    def blocked_groups(self) -> List[PreparedGroup]:
        return [
            group
            for group in self._sorted_groups()
            if group.blocked_reason or (group.is_final and group.missing_rows)
        ]

    def prepared_only_rows(self) -> List[RowRecord]:
        rows: List[RowRecord] = []
        for group in self.prepared_only():
            rows.extend(group.rows)
        return rows

    def final_rows(self, outcome: Optional[str] = None) -> List[RowRecord]:
        """Rows that reached a final result.

        A rolled-back group has a final outcome but contributes no rows,
        because its effects never became visible.
        """

        rows: List[RowRecord] = []
        for group in self.final_groups():
            if outcome and group.outcome != outcome:
                continue
            if group.outcome == "rolled_back":
                continue
            rows.extend(group.rows)
        return rows

    def _sorted_groups(self) -> List[PreparedGroup]:
        return [self.groups[key] for key in sorted(self.groups)]

    def status(self) -> Dict[str, Any]:
        return {
            "prepared_only": [
                {
                    "group_id": group.group_id,
                    "prepare_gtid": group.prepare_gtid,
                    "row_count": len(group.rows),
                    "rows": [row.to_dict() for row in group.rows],
                }
                for group in self.prepared_only()
            ],
            "final_results": [
                {
                    "group_id": group.group_id,
                    "outcome": group.outcome,
                    "prepare_gtid": group.prepare_gtid,
                    "final_gtid": group.final_gtid,
                    "row_count": len(group.rows),
                    "published": group.group_id in self.published,
                    "target_confirmed": group.target_confirmed,
                }
                for group in self.final_groups()
            ],
            "published": [
                publication.to_dict() for publication in self.published_transactions
            ],
            "pending_prepared": [
                {
                    "group_id": group.group_id,
                    "prepare_gtid": group.prepare_gtid,
                    "missing_rows": group.missing_rows,
                    "blocked_reason": group.blocked_reason,
                }
                for group in self.pending_prepared()
            ],
            "blocked": [
                {
                    "group_id": group.group_id,
                    "reason": group.blocked_reason,
                    "final_gtid": group.final_gtid,
                }
                for group in self.blocked_groups()
            ],
            "missing_ranges": list(self.missing_ranges),
            "resume": self.resume_plan(),
        }

    def snapshot(self) -> Dict[str, Any]:
        return self.status()

    def report(self) -> Dict[str, Any]:
        return self.status()

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _describe_key(self, value: Any) -> str:
        if isinstance(value, PreparedGroup):
            return value.group_id
        if isinstance(value, PublishedTransaction):
            return value.group_id
        return str(value)


__all__ = [
    "AmbiguousEffectError",
    "DeliveryCheckpoint",
    "DeliveryCheckpointError",
    "MirrorManifest",
    "PreparedGroup",
    "PublishedTransaction",
    "RowRecord",
    "SourceCursor",
    "TargetConfirmationError",
    "TransactionIdentity",
    "XAIdentity",
    "parse_xa_query",
]
