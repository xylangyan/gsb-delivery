"""Offline reconciliation queries for paired mDNS listener ledgers.

The replay is clocked on listener A's millisecond timeline. Every B packet is
replayed at ``local_b_time + offset`` for one integer offset shared by the
whole ledger.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Literal

from ._cache import DNSCache
from ._dns import DNSAddress, DNSRecord, DNSPointer, DNSService, DNSText
from ._handlers.record_manager import RecordManager
from ._protocol.incoming import DNSIncoming
from .const import (
    _CLASS_IN,
    _CLASS_UNIQUE,
    _TYPES,
    _TYPE_A,
    _TYPE_AAAA,
    _TYPE_SRV,
    _TYPE_TXT,
)

SourceName = Literal["a", "b"]
QueryStatus = Literal["all", "partial", "none", "conflict"]

_MISSING = object()
_TYPE_BY_NAME = {name.upper(): value for value, name in _TYPES.items()}
_TYPE_BY_NAME.update({"A": _TYPE_A, "AAAA": _TYPE_AAAA, "SRV": _TYPE_SRV, "TXT": _TYPE_TXT})


class InventoryError(ValueError):
    """The offline inventory request or ledger is invalid."""


class OffsetConflict(InventoryError):
    """No integer offset satisfies all handover restrictions."""

    def __init__(self, conflicts: list[dict[str, Any]], feasible: tuple[int, int] | None = None) -> None:
        self.conflicts = conflicts
        self.feasible = feasible
        ids = ", ".join(str(item["id"]) for item in conflicts)
        super().__init__(f"conflicting offset restrictions: {ids}")

    def to_dict(self) -> dict[str, Any]:
        return {"status": "conflict", "conflicts": self.conflicts, "feasible_offsets": self.feasible}


def _first(mapping: dict[str, Any], *keys: str, default: Any = _MISSING) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    if default is _MISSING:
        raise InventoryError(f"missing one of required keys: {', '.join(keys)}")
    return default


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        if not isinstance(value, int):
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise InventoryError(f"{what} must be an integer millisecond value") from exc
    return int(value)


def _range_value(value: Any, what: str = "range") -> tuple[int, int]:
    if isinstance(value, dict):
        low = _first(value, "low", "min", "start", "left", "lower", "起", "下限")
        high = _first(value, "high", "max", "end", "right", "upper", "止", "上限")
        result = _as_int(low, f"{what}.low"), _as_int(high, f"{what}.high")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        result = _as_int(value[0], f"{what}[0]"), _as_int(value[1], f"{what}[1]")
    elif isinstance(value, str) and "," in value:
        left, right = value.split(",", 1)
        result = _as_int(left.strip(), f"{what}.low"), _as_int(right.strip(), f"{what}.high")
    else:
        raise InventoryError(f"{what} must be a closed [low, high] interval")
    if result[0] > result[1]:
        raise OffsetConflict(
            [
                {
                    "id": what,
                    "low": result[0],
                    "high": result[1],
                    "reason": "interval is empty",
                }
            ]
        )
    return result


def _decode_bytes(value: Any, what: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, list | tuple):
        return bytes(_as_int(item, f"{what}[]") for item in value)
    if not isinstance(value, str):
        raise InventoryError(f"{what} must be bytes, hex, base64, or an integer list")
    text = value.strip()
    if text.startswith("base64:"):
        return base64.b64decode(text.split(":", 1)[1], validate=True)
    if all(ch in "0123456789abcdefABCDEF " for ch in text) and len(text.replace(" ", "")) % 2 == 0:
        try:
            return bytes.fromhex(text.replace(" ", ""))
        except ValueError:
            pass
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return text.encode("utf-8")


def _txt_from_mapping(data: dict[str, Any]) -> bytes:
    hex_value = _first(data, "txt_hex", "text_hex", default=None)
    if hex_value is not None:
        return bytes.fromhex(str(hex_value).removeprefix("0x"))
    base64_value = _first(data, "txt_base64", "text_base64", default=None)
    if base64_value is not None:
        return base64.b64decode(str(base64_value), validate=True)
    raw_value = _first(
        data,
        "txt_bytes",
        "text_bytes",
        "txt_raw",
        "raw_txt",
        "txt",
        "text",
        "rdata",
        default=b"",
    )
    if isinstance(raw_value, str):
        return raw_value.encode("utf-8")
    return _decode_bytes(raw_value, "TXT text")


def _decode_wire(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("base64:"):
            return base64.b64decode(text.split(":", 1)[1], validate=True)
        try:
            return bytes.fromhex(text.removeprefix("0x"))
        except ValueError:
            return base64.b64decode(text, validate=True)
    raise InventoryError("packet wire data must be bytes, hex, or base64")


def canonical_name(name: str) -> str:
    """Return the canonical absolute DNS name used by this offline tool."""
    if not isinstance(name, str) or not name:
        raise InventoryError("DNS name must be a non-empty string")
    if name == ".":
        return name
    return name.rstrip(".") + "."


def _source_rank(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return value
        if value in (1, 2):
            return value - 1
    token = str(value).strip().lower()
    if token in {"a", "0", "jia", "listener_a", "listener-1", "1", "甲"}:
        return 0
    if token in {"b", "yi", "listener_b", "listener-2", "2", "乙"}:
        return 1
    raise InventoryError(f"unknown listener/source: {value!r}")


def _class_and_unique(record: dict[str, Any]) -> tuple[int, bool]:
    raw_class = _first(record, "class", "class_", "rrclass", default=_CLASS_IN)
    if isinstance(raw_class, str):
        token = raw_class.strip().upper()
        unique = "UNIQUE" in token or bool(record.get("cache_flush", record.get("unique", False)))
        if token.startswith("IN"):
            qclass = _CLASS_IN
        elif token.isdigit():
            qclass = int(token) & 0x7FFF
        else:
            qclass = _TYPE_BY_NAME.get(token.split("|")[0].strip(), _CLASS_IN)
    else:
        value = _as_int(raw_class, "class")
        unique = bool(value & _CLASS_UNIQUE) or bool(record.get("cache_flush", record.get("unique", False)))
        qclass = value & 0x7FFF
    if record.get("cache_flush") is not None or record.get("unique") is not None:
        unique = bool(record.get("cache_flush", record.get("unique", unique)))
    return qclass, unique


def _rtype(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    token = str(value).strip().upper().replace("TYPE_", "")
    if token in _TYPE_BY_NAME:
        return _TYPE_BY_NAME[token]
    try:
        return int(token)
    except ValueError as exc:
        raise InventoryError(f"unsupported DNS record type: {value!r}") from exc


@dataclass(frozen=True)
class RecordSpec:
    """Serializable description of one cached DNS record."""

    rtype: int
    name: str
    rclass: int = _CLASS_IN
    ttl: int = 120
    unique: bool = False
    address: bytes | None = None
    text: bytes | None = None
    priority: int = 0
    weight: int = 0
    port: int = 0
    server: str | None = None
    alias: str | None = None

    def build(self, now: float) -> DNSRecord:
        class_value = self.rclass | (_CLASS_UNIQUE if self.unique else 0)
        if self.rtype in (_TYPE_A, _TYPE_AAAA):
            return DNSAddress(
                self.name,
                self.rtype,
                class_value,
                self.ttl,
                self.address or b"",
                created=now,
            )
        if self.rtype == _TYPE_TXT:
            return DNSText(self.name, self.rtype, class_value, self.ttl, self.text or b"", created=now)
        if self.rtype == _TYPE_SRV:
            return DNSService(
                self.name,
                self.rtype,
                class_value,
                self.ttl,
                self.priority,
                self.weight,
                self.port,
                self.server or ".",
                created=now,
            )
        from ._dns import DNSPointer

        return DNSPointer(self.name, self.rtype, class_value, self.ttl, self.alias or ".", created=now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordSpec:
        rtype = _rtype(_first(data, "type", "rtype", "record_type"))
        name = canonical_name(str(_first(data, "name", "rrname")))
        rclass, unique = _class_and_unique(data)
        ttl = _as_int(_first(data, "ttl", "ttl_seconds", default=120), "ttl")
        kwargs: dict[str, Any] = {}
        if rtype == _TYPE_A or rtype == _TYPE_AAAA:
            address = _first(data, "address", "ip", "ipv4", "ipv6", "rdata", default=None)
            if address is None:
                raw = _first(data, "address_hex", "rdata_hex", default=None)
                address = bytes.fromhex(str(raw)) if raw is not None else None
            if isinstance(address, str) and rtype == _TYPE_A:
                address = ipaddress.IPv4Address(address).packed
            elif isinstance(address, str) and rtype == _TYPE_AAAA:
                address = ipaddress.IPv6Address(address).packed
            else:
                address = _decode_bytes(address, "address")
            kwargs["address"] = address
        elif rtype == _TYPE_TXT:
            kwargs["text"] = _txt_from_mapping(data)
        elif rtype == _TYPE_SRV:
            server = canonical_name(str(_first(data, "server", "target", "host", "target_name")))
            kwargs.update(
                priority=_as_int(_first(data, "priority", default=0), "priority"),
                weight=_as_int(_first(data, "weight", default=0), "weight"),
                port=_as_int(_first(data, "port", default=0), "port"),
                server=server,
            )
        else:
            alias_value = _first(data, "alias", "target", "pointer", default=None)
            if alias_value is not None:
                kwargs["alias"] = canonical_name(str(alias_value))
        return cls(rtype=rtype, name=name, rclass=rclass, ttl=ttl, unique=unique, **kwargs)

    @property
    def key(self) -> tuple[str, int, int]:
        return self.name_key, self.rtype, self.rclass

    @property
    def name_key(self) -> str:
        return self.name.lower()


def spec_from_dns_record(record: DNSRecord) -> RecordSpec:
    """Convert a zeroconf record template into a serializable replay spec."""
    kwargs: dict[str, Any] = {}
    if isinstance(record, DNSAddress):
        kwargs["address"] = record.address
    elif isinstance(record, DNSText):
        kwargs["text"] = record.text
    elif isinstance(record, DNSService):
        kwargs.update(
            priority=record.priority,
            weight=record.weight,
            port=record.port,
            server=record.server,
        )
    elif isinstance(record, DNSPointer):
        kwargs["alias"] = record.alias
    return RecordSpec(
        rtype=record.type,
        name=record.name,
        rclass=record.class_,
        ttl=record.ttl,
        unique=record.unique,
        **kwargs,
    )


@dataclass(frozen=True)
class RecordFact:
    """Index information for one record inside a packet."""

    packet_idx: int
    source: int
    time: int
    key: tuple[str, int, int]
    ttl: int
    server_key: str | None = None


class _SpecMessage:
    __slots__ = ("now", "_answers")

    def __init__(self, now: float, answers: list[DNSRecord]) -> None:
        self.now = now
        self._answers = answers

    def answers(self) -> list[DNSRecord]:
        return self._answers


@dataclass
class Packet:
    packet_id: str
    source: int
    seq: int
    time: int
    idx: int = -1
    wire: bytes | None = None
    specs: tuple[RecordSpec, ...] = ()
    pair_id: str | None = None
    primary: bool = True
    primary_explicit: bool = False
    facts: tuple[RecordFact, ...] = field(default=())

    def message(self, now: int) -> tuple[Any, dict[int, str]]:
        origin: dict[int, str] = {}
        if self.wire is not None:
            message = DNSIncoming(self.wire, now=float(now))
            answers = message.answers()
        else:
            answers = [spec.build(float(now)) for spec in self.specs]
            message = _SpecMessage(float(now), answers)
        for record in answers:
            origin[id(record)] = self.packet_id
        return message, origin

    def ref(self, adjusted_time: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "packet_id": self.packet_id,
            "source": self.source_name,
            "seq": self.seq,
            "original_time": self.time,
        }
        if adjusted_time is not None:
            result["adjusted_time"] = adjusted_time
        if self.pair_id:
            result["pair_id"] = self.pair_id
        return result

    @property
    def source_name(self) -> SourceName:
        return "a" if self.source == 0 else "b"


@dataclass(frozen=True)
class Pair:
    pair_id: str
    packet_a: str
    packet_b: str
    primary_packet: str
    receive_diff_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class OffsetRestriction:
    restriction_id: str
    low: int
    high: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.restriction_id,
            "low": self.low,
            "high": self.high,
            "kind": self.kind,
            **self.detail,
        }


@dataclass(frozen=True)
class Revision:
    name: str
    restrictions: tuple[OffsetRestriction, ...]
    based_on: str | None = None


@dataclass(frozen=True)
class InventoryQuery:
    at: int
    service: str
    txt: bytes
    ipv4: bytes
    qclass: int = _CLASS_IN
    query_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InventoryQuery:
        service = str(_first(data, "service", "instance", "service_instance", "name"))
        at = _as_int(_first(data, "at", "time", "check_at", "millisecond", "ms"), "query.at")
        txt = _txt_from_mapping(data)
        ip_value = _first(data, "ipv4", "ip", "address", "a", default=None)
        if ip_value is None:
            raw_hex = _first(data, "ipv4_hex", "address_hex", default=None)
            if raw_hex is None:
                raise InventoryError("query requires ipv4")
            ipv4 = bytes.fromhex(str(raw_hex))
        elif isinstance(ip_value, str):
            ipv4 = ipaddress.IPv4Address(ip_value).packed
        else:
            ipv4 = _decode_bytes(ip_value, "ipv4")
        if len(ipv4) != 4:
            raise InventoryError("query address must be IPv4 (4 bytes)")
        qclass, _ = _class_and_unique(data)
        return cls(at=at, service=service, txt=txt, ipv4=ipv4, qclass=qclass, query_id=data.get("id"))

    @property
    def service_key(self) -> str:
        return canonical_name(self.service).lower()


@dataclass
class MatchEvidence:
    simultaneous: bool
    txt_present: bool
    srv_present: bool
    srv_address_present: bool
    txt_origins: list[str] = field(default_factory=list)
    srv_origins: list[str] = field(default_factory=list)
    address_origins: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "simultaneous": self.simultaneous,
            "txt_present": self.txt_present,
            "srv_present": self.srv_present,
            "srv_address_present": self.srv_address_present,
            "txt_origins": self.txt_origins,
            "srv_origins": self.srv_origins,
            "address_origins": self.address_origins,
            "srv_targets": self.targets,
            "matches": self.matches,
        }


@dataclass
class ReplayTrace:
    offset: int
    at: int | None
    packet_order: list[dict[str, Any]]
    cleanups: list[int]
    evidence: MatchEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "offset": self.offset,
            "packet_order": self.packet_order,
            "cleanups": self.cleanups,
        }
        if self.at is not None:
            result["at"] = self.at
        if self.evidence is not None:
            result["evidence"] = self.evidence.to_dict()
        return result


@dataclass
class QueryResult:
    query_id: str | None
    at: int
    status: QueryStatus
    feasible_offsets: tuple[int, int] | None = None
    true_ranges: list[tuple[int, int]] = field(default_factory=list)
    false_ranges: list[tuple[int, int]] = field(default_factory=list)
    true_example: int | None = None
    false_example: int | None = None
    true_witness: ReplayTrace | None = None
    false_witness: ReplayTrace | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    pair_checks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "verdict": "status",
            "classification": "status",
            "state": "status",
            "true_delta": "true_example",
            "false_delta": "false_example",
            "true_sample": "true_example",
            "false_sample": "false_example",
            "true_offset_ranges": "true_ranges",
            "false_offset_ranges": "false_ranges",
        }
        data = self.to_dict()
        return data[aliases.get(key, key)]

    @property
    def all_hold(self) -> bool:
        return self.status == "all"

    @property
    def any_hold(self) -> bool:
        return self.status in {"all", "partial"}

    def to_dict(self) -> dict[str, Any]:
        status_zh = {
            "all": "全部成立",
            "partial": "部分成立",
            "none": "全部不成立",
            "conflict": "无可行差值",
        }[self.status]
        result: dict[str, Any] = {
            "id": self.query_id,
            "at": self.at,
            "status": self.status,
            "verdict": self.status,
            "classification": self.status,
            "status_zh": status_zh,
            "all_hold": self.all_hold,
            "any_hold": self.any_hold,
            "feasible_offsets": list(self.feasible_offsets) if self.feasible_offsets else None,
            "true_ranges": [list(item) for item in self.true_ranges],
            "false_ranges": [list(item) for item in self.false_ranges],
            "true_example": self.true_example,
            "false_example": self.false_example,
            "true_delta": self.true_example,
            "false_delta": self.false_example,
            "true_sample": self.true_example,
            "false_sample": self.false_example,
            "conflicts": self.conflicts,
            "pair_checks": self.pair_checks,
        }
        if self.true_witness is not None:
            result["true_witness"] = self.true_witness.to_dict()
        if self.false_witness is not None:
            result["false_witness"] = self.false_witness.to_dict()
        return result


class HandoverLedger:
    """Normalized packet ledger, cleanup ledger, and handover revisions."""

    def __init__(
        self,
        packets: list[Packet],
        cleanups: Iterable[int],
        revisions: dict[str, Revision],
        pairs: dict[str, Pair] | None = None,
        default_revision: str = "original",
        checks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.packets = packets
        self.pairs = pairs or {}
        self.cleanups = sorted({_as_int(item, "cleanup time") for item in cleanups})
        self.revisions = revisions
        self.default_revision = default_revision if default_revision in revisions else "original"
        self.checks = checks or []
        self.by_id = {packet.packet_id: packet for packet in packets}
        if len(self.by_id) != len(packets):
            raise InventoryError("packet ids must be unique")
        for idx, packet in enumerate(packets):
            packet.idx = idx
            if not packet.facts:
                packet.facts = _facts_for_packet(idx, packet)
        self.effective_packets = [packet for packet in packets if packet.primary]
        self.a_packets = sorted(
            (packet for packet in self.effective_packets if packet.source == 0),
            key=lambda packet: (packet.time, packet.seq, packet.idx),
        )
        self.b_packets = sorted(
            (packet for packet in self.effective_packets if packet.source == 1),
            key=lambda packet: (packet.time, packet.seq, packet.idx),
        )
        self.rrset_facts: dict[tuple[str, int, int], list[RecordFact]] = {}
        self.rrset_packets: dict[tuple[str, int, int], set[int]] = {}
        for packet in packets:
            for fact in packet.facts:
                self.rrset_facts.setdefault(fact.key, []).append(fact)
                self.rrset_packets.setdefault(fact.key, set()).add(packet.idx)
        self._projection_cache: dict[tuple[Any, ...], Projection] = {}

    def check_pair_restrictions(self, offset: int, revision: str | None = None) -> list[dict[str, Any]]:
        name = revision or self.default_revision
        rev = self.revisions[name]
        checks: list[dict[str, Any]] = []
        for restriction in rev.restrictions:
            if restriction.kind != "pair_receive_diff":
                continue
            detail = restriction.detail
            packet_a = self.by_id[str(detail["packet_a"])]
            packet_b = self.by_id[str(detail["packet_b"])]
            adjusted_diff = packet_b.time + offset - packet_a.time
            low, high = detail["receive_diff_range"]
            checks.append(
                {
                    "restriction_id": restriction.restriction_id,
                    "pair_id": detail["pair_id"],
                    "packet_a": packet_a.ref(),
                    "packet_b": packet_b.ref(packet_b.time + offset),
                    "adjusted_b_minus_a": adjusted_diff,
                    "receive_diff_range": [low, high],
                    "satisfied": low <= adjusted_diff <= high,
                }
            )
        return checks

    def critical_offsets(self, query: InventoryQuery, low: int, high: int) -> set[int]:
        projection = self.projection(query)
        fixed_times = {query.at}
        fixed_times.update(cleanup for cleanup in self.cleanups if cleanup <= query.at)
        critical: set[int] = set()
        for key in projection.keys:
            facts = self.rrset_facts.get(key, ())
            a_facts = [fact for fact in facts if fact.source == 0]
            b_facts = [fact for fact in facts if fact.source == 1]
            for b_fact in b_facts:
                for fixed_time in fixed_times:
                    critical.add(fixed_time - b_fact.time)
                    critical.add(fixed_time - b_fact.time - b_fact.ttl * 1000)
                    critical.add(fixed_time - b_fact.time - 1000)
                for a_fact in a_facts:
                    critical.add(a_fact.time - b_fact.time)
                    critical.add(a_fact.time + 1001 - b_fact.time)
                    critical.add(a_fact.time - 1001 - b_fact.time)
        return {value for value in critical if low <= value <= high}

    def feasible_offsets(self, revision: str | None = None) -> tuple[int, int]:
        name = revision or self.default_revision
        if name not in self.revisions:
            raise InventoryError(f"unknown handover revision: {name}")
        rev = self.revisions[name]
        low = max(item.low for item in rev.restrictions)
        high = min(item.high for item in rev.restrictions)
        if low <= high:
            return low, high
        max_low = max(item.low for item in rev.restrictions)
        min_high = min(item.high for item in rev.restrictions)
        conflicts: list[OffsetRestriction] = []
        if max_low > min_high:
            conflicts.extend(item for item in rev.restrictions if item.low == max_low)
            conflicts.extend(item for item in rev.restrictions if item.high == min_high)
        else:
            conflicts.extend(item for item in rev.restrictions if item.low > item.high)
        unique_conflicts = list({item.restriction_id: item for item in conflicts}.values())
        raise OffsetConflict([item.to_dict() for item in unique_conflicts])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoverLedger:
        return _ledger_from_dict(data)

    def projection(self, query: InventoryQuery) -> Projection:
        cache_key = (query.service_key, query.qclass, query.txt, query.ipv4)
        if cache_key in self._projection_cache:
            return self._projection_cache[cache_key]
        txt_key = (query.service_key, _TYPE_TXT, query.qclass)
        srv_key = (query.service_key, _TYPE_SRV, query.qclass)
        keys = {txt_key, srv_key}
        for fact in self.rrset_facts.get(srv_key, ()):
            if fact.server_key:
                keys.add((fact.server_key, _TYPE_A, query.qclass))
        packet_indices: set[int] = set()
        for key in keys:
            packet_indices.update(self.rrset_packets.get(key, ()))
        projection = Projection(self, packet_indices, keys)
        self._projection_cache[cache_key] = projection
        return projection

    def query(
        self,
        query: InventoryQuery | dict[str, Any],
        *,
        revision: str | None = None,
        include_witness_trace: bool = True,
    ) -> QueryResult:
        normalized = query if isinstance(query, InventoryQuery) else InventoryQuery.from_dict(query)
        try:
            low, high = self.feasible_offsets(revision)
        except OffsetConflict as exc:
            return QueryResult(
                query_id=normalized.query_id,
                at=normalized.at,
                status="conflict",
                conflicts=exc.conflicts,
            )
        projection = self.projection(normalized)
        true_segments: list[tuple[int, int]] = []
        false_segments: list[tuple[int, int]] = []
        true_example: int | None = None
        false_example: int | None = None
        true_result: ReplayTrace | None = None
        false_result: ReplayTrace | None = None
        for seg_low, seg_high, representative in self._offset_segments(normalized, low, high):
            trace = projection.run(representative, normalized.at, normalized, trace=include_witness_trace)
            holds = trace.evidence is not None and trace.evidence.simultaneous
            target = true_segments if holds else false_segments
            target.append((seg_low, seg_high))
            if holds and true_example is None:
                true_example = representative
                true_result = trace
            if not holds and false_example is None:
                false_example = representative
                false_result = trace
        true_ranges = _merge_ranges(true_segments)
        false_ranges = _merge_ranges(false_segments)
        if true_example is not None and false_example is not None:
            status: QueryStatus = "partial"
        elif true_example is not None:
            status = "all"
        else:
            status = "none"
        revision_name = revision or self.default_revision
        pair_checks: dict[str, list[dict[str, Any]]] = {}
        if true_example is not None:
            pair_checks["true"] = self.check_pair_restrictions(true_example, revision_name)
        if false_example is not None:
            pair_checks["false"] = self.check_pair_restrictions(false_example, revision_name)
        return QueryResult(
            query_id=normalized.query_id,
            at=normalized.at,
            status=status,
            feasible_offsets=(low, high),
            true_ranges=true_ranges,
            false_ranges=false_ranges,
            true_example=true_example,
            false_example=false_example,
            true_witness=true_result if include_witness_trace else None,
            false_witness=false_result if include_witness_trace else None,
            pair_checks=pair_checks,
        )

    def query_all(
        self,
        queries: Iterable[InventoryQuery | dict[str, Any]] | None = None,
        *,
        revision: str | None = None,
        include_witness_trace: bool = True,
    ) -> list[QueryResult]:
        source = list(queries) if queries is not None else self.checks
        return [
            self.query(item, revision=revision, include_witness_trace=include_witness_trace)
            for item in source
        ]

    def replay(
        self,
        offset: int,
        at: int | None = None,
        query: InventoryQuery | dict[str, Any] | None = None,
        *,
        projection_only: bool = False,
    ) -> ReplayTrace:
        normalized = None
        indices: set[int] | None = None
        run_at = at
        if query is not None:
            if isinstance(query, InventoryQuery):
                normalized = query
            else:
                query_data = dict(query)
                if at is not None:
                    query_data["at"] = at
                normalized = InventoryQuery.from_dict(query_data)
            if run_at is None:
                run_at = normalized.at
            projection = self.projection(normalized)
            if projection_only:
                indices = projection.packet_indices
        runner = Projection(self, indices) if indices is not None else Projection(self, None)
        return runner.run(offset, run_at, query=normalized, trace=True)

    def with_pair_limit(
        self,
        pair_id: str,
        receive_diff_range: tuple[int, int] | list[int],
        *,
        revision: str = "signed",
        based_on: str | None = None,
        restriction_id: str | None = None,
    ) -> HandoverLedger:
        """Return a ledger with a new revision containing an added pair limit."""
        if pair_id not in self.pairs:
            raise InventoryError(f"unknown pair: {pair_id}")
        base_name = based_on or ("original" if "original" in self.revisions else self.default_revision)
        if base_name not in self.revisions:
            raise InventoryError(f"unknown base revision: {base_name}")
        pair = self.pairs[pair_id]
        pair_range = _range_value(receive_diff_range, f"pair {pair_id} receive_diff_range")
        restriction = make_pair_restriction(
            pair,
            pair_range,
            self.by_id[pair.packet_a].time,
            self.by_id[pair.packet_b].time,
            restriction_id,
        )
        base = self.revisions[base_name]
        new_rev = Revision(
            name=revision,
            restrictions=(*base.restrictions, restriction),
            based_on=base_name,
        )
        revisions = dict(self.revisions)
        revisions[revision] = new_rev
        return HandoverLedger(
            packets=self.packets,
            cleanups=self.cleanups,
            revisions=revisions,
            pairs=self.pairs,
            default_revision=self.default_revision,
            checks=self.checks,
        )

    def _offset_segments(
        self, query: InventoryQuery, low: int, high: int
    ) -> Iterable[tuple[int, int, int]]:
        critical = sorted(self.critical_offsets(query, low, high))
        cursor = low
        for point in critical:
            if point < low:
                continue
            if point > high:
                break
            if cursor < point:
                yield cursor, point - 1, cursor
            yield point, point, point
            cursor = point + 1
        if cursor <= high:
            yield cursor, high, cursor


def make_pair_restriction(
    pair: Pair,
    receive_diff_range: tuple[int, int],
    packet_a_time: int,
    packet_b_time: int,
    restriction_id: str | None = None,
) -> OffsetRestriction:
    low_diff, high_diff = receive_diff_range
    skew = packet_b_time - packet_a_time
    return OffsetRestriction(
        restriction_id or f"pair:{pair.pair_id}",
        low_diff - skew,
        high_diff - skew,
        "pair_receive_diff",
        {
            "pair_id": pair.pair_id,
            "packet_a": pair.packet_a,
            "packet_b": pair.packet_b,
            "receive_diff_range": [low_diff, high_diff],
            "b_minus_a_local": skew,
            "equation": "receive_diff_range[0] <= (b_time + offset) - a_time <= range[1]",
        },
    )


@dataclass
class Projection:
    ledger: HandoverLedger
    packet_indices: set[int] | None
    keys: set[tuple[str, int, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.packet_indices is None:
            self.a_packets = list(self.ledger.a_packets)
            self.b_packets = list(self.ledger.b_packets)
            return
        selected_packets = [self.ledger.packets[index] for index in self.packet_indices]
        self.a_packets = sorted(
            (packet for packet in selected_packets if packet.source == 0),
            key=lambda packet: (packet.time, packet.seq, packet.idx),
        )
        self.b_packets = sorted(
            (packet for packet in selected_packets if packet.source == 1),
            key=lambda packet: (packet.time, packet.seq, packet.idx),
        )

    def run(
        self,
        offset: int,
        at: int | None,
        query: InventoryQuery | None = None,
        *,
        trace: bool = True,
    ) -> ReplayTrace:
        cache = DNSCache()
        manager = RecordManager(SimpleNamespace(cache=cache, async_notify_all=lambda: None))
        events: list[tuple[int, int, int, int, str, Any]] = []
        for packet in self.a_packets:
            events.append((packet.time, 0, 0, packet.seq, "packet", packet))
        for packet in self.b_packets:
            events.append((packet.time + offset, 0, 1, packet.seq, "packet", packet))
        for cleanup in self.ledger.cleanups:
            events.append((cleanup, 1, 2, cleanup, "cleanup", cleanup))
        events.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

        origin_by_record: dict[int, str] = {}
        packet_order: list[dict[str, Any]] = []
        processed_cleanups: list[int] = []
        for event_time, phase, _source, _seq, kind, payload in events:
            if at is not None and event_time > at:
                break
            if kind == "cleanup":
                cache.async_expire(event_time)
                processed_cleanups.append(event_time)
                continue
            packet: Packet = payload
            message, origins = packet.message(event_time)
            origin_by_record.update(origins)
            manager.async_updates_from_response(message)
            if trace:
                packet_order.append(packet.ref(event_time))

        evidence = None
        if query is not None and at is not None:
            evidence = self._match(cache, query, at, origin_by_record)
        return ReplayTrace(
            offset=offset,
            at=at,
            packet_order=packet_order,
            cleanups=processed_cleanups,
            evidence=evidence,
        )

    def _match(
        self,
        cache: DNSCache,
        query: InventoryQuery,
        at: int,
        origin_by_record: dict[int, str],
    ) -> MatchEvidence:
        service_name = canonical_name(query.service)
        txt_records = [
            record
            for record in cache.async_all_by_details(service_name, _TYPE_TXT, query.qclass)
            if isinstance(record, DNSText) and record.text == query.txt and not record.is_expired(at)
        ]
        srv_records = [
            record
            for record in cache.async_all_by_details(service_name, _TYPE_SRV, query.qclass)
            if isinstance(record, DNSService) and not record.is_expired(at)
        ]
        matches: list[dict[str, Any]] = []
        address_origins: list[str] = []
        for srv in srv_records:
            for record in cache.async_all_by_details(srv.server, _TYPE_A, srv.class_):
                if not isinstance(record, DNSAddress) or len(record.address) != 4:
                    continue
                if record.address != query.ipv4 or record.is_expired(at):
                    continue
                a_origin = origin_by_record.get(id(record))
                if a_origin:
                    address_origins.append(a_origin)
                matches.append(
                    {
                        "target": srv.server,
                        "srv_origin": origin_by_record.get(id(srv)),
                        "a_origin": a_origin,
                    }
                )
        txt_origins = [origin for record in txt_records if (origin := origin_by_record.get(id(record)))]
        srv_origins = [origin for record in srv_records if (origin := origin_by_record.get(id(record)))]
        targets = [record.server for record in srv_records]
        srv_address_present = bool(matches)
        return MatchEvidence(
            simultaneous=bool(txt_records and matches),
            txt_present=bool(txt_records),
            srv_present=bool(srv_records),
            srv_address_present=srv_address_present,
            txt_origins=sorted(set(txt_origins)),
            srv_origins=sorted(set(srv_origins)),
            address_origins=sorted(set(address_origins)),
            targets=targets,
            matches=matches,
        )


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[list[int]] = [list(ordered[0])]
    for low, high in ordered[1:]:
        if low <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return [(item[0], item[1]) for item in merged]


def _records_from_wire(wire: bytes) -> list[DNSRecord]:
    message = DNSIncoming(wire)
    return message.answers()


def _fact_from_record(packet_idx: int, packet: Packet, record: DNSRecord) -> RecordFact:
    server_key = getattr(record, "server_key", None)
    return RecordFact(
        packet_idx=packet_idx,
        source=packet.source,
        time=packet.time,
        key=(record.key, record.type, record.class_),
        ttl=record.ttl,
        server_key=server_key,
    )


def _facts_for_packet(packet_idx: int, packet: Packet) -> tuple[RecordFact, ...]:
    if packet.wire is not None:
        return tuple(
            _fact_from_record(packet_idx, packet, record)
            for record in _records_from_wire(packet.wire)
        )
    return tuple(
        RecordFact(
            packet_idx=packet_idx,
            source=packet.source,
            time=packet.time,
            key=spec.key,
            ttl=spec.ttl,
            server_key=spec.server.lower() if spec.rtype == _TYPE_SRV and spec.server else None,
        )
        for spec in packet.specs
    )


def _ledger_from_dict(data: dict[str, Any]) -> HandoverLedger:
    if "ledger" in data and isinstance(data["ledger"], dict):
        data = data["ledger"]
    raw_packets = _first(
        data,
        "packets",
        "packet_index",
        "packet_log",
        "records",
        "包",
        "报文",
        "台账",
        default=[],
    )
    if not isinstance(raw_packets, list):
        raise InventoryError("packets must be a list")

    packets: list[Packet] = []
    auto_seq = {0: 0, 1: 0}
    for raw in raw_packets:
        if not isinstance(raw, dict):
            raise InventoryError("each packet must be an object")
        source = _source_rank(_first(raw, "source", "listener", "from", "agent", "监听器", "来源"))
        seq_value = _first(
            raw,
            "seq",
            "sequence",
            "sequence_number",
            "packet_seq",
            "index",
            "packet_index",
            "no",
            "序号",
            default=None,
        )
        if seq_value is None:
            auto_seq[source] += 1
            seq = auto_seq[source]
        else:
            seq = _as_int(seq_value, "packet.seq")
        time = _as_int(
            _first(
                raw,
                "time",
                "at",
                "timestamp",
                "received_at",
                "receive_time",
                "local_time",
                "timestamp_ms",
                "receive_time_ms",
                "本机时间",
                "接收时间",
            ),
            "packet.time",
        )
        packet_id = str(_first(raw, "id", "packet_id", "pid", default=f"{'ab'[source]}-{seq}"))
        wire_value = _first(
            raw,
            "wire",
            "wire_hex",
            "raw",
            "raw_hex",
            "packet_hex",
            "data",
            "dns",
            "packet",
            "data_base64",
            "raw_base64",
            "wire_base64",
            "packet_base64",
            "dns_base64",
            "hex",
            default=None,
        )
        if isinstance(wire_value, dict):
            wire_value = None
        wire = _decode_wire(wire_value) if wire_value is not None else None
        record_items = _first(raw, "records", "rrs", "answers", "resource_records", default=[])
        specs = tuple(
            item
            if isinstance(item, RecordSpec)
            else spec_from_dns_record(item)
            if isinstance(item, DNSRecord)
            else RecordSpec.from_dict(item)
            for item in record_items
        )
        pair_id = _first(raw, "pair_id", "pair", "broadcast_pair", "sync_pair", "配对", default=None)
        pair_id = str(pair_id) if pair_id is not None else None
        primary_value = _first(raw, "primary", "is_primary", "main", "is_main_packet", "主包", default=None)
        primary_explicit = primary_value is not None
        primary = bool(primary_value) if primary_explicit else True
        packet = Packet(
            packet_id=packet_id,
            source=source,
            seq=seq,
            time=time,
            wire=wire,
            specs=specs,
            pair_id=pair_id,
            primary=primary,
            primary_explicit=primary_explicit,
        )
        packets.append(packet)

    for idx, packet in enumerate(packets):
        packet.idx = idx
        packet.facts = _facts_for_packet(idx, packet)

    packet_by_id = {packet.packet_id: packet for packet in packets}

    raw_pairs = _first(data, "pairs", "broadcast_pairs", "sync_pairs", "paired_broadcasts", "配对", default=[])
    pairs: dict[str, Pair] = {}
    pair_primary_overrides: dict[str, str] = {}
    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, dict):
            continue
        pair_id = str(_first(raw_pair, "id", "pair_id", "pair", default=f"pair-{len(pairs) + 1}"))
        packet_a = _first(raw_pair, "packet_a", "a", "jia", "first", default=None)
        packet_b = _first(raw_pair, "packet_b", "b", "yi", "second", default=None)
        members = _first(raw_pair, "packets", "packet_ids", "members", default=None)
        if members is not None:
            if not isinstance(members, list) or len(members) != 2:
                raise InventoryError(f"pair {pair_id} must name exactly two packets")
            ids = [str(item) for item in members]
            source_by_id = {packet.packet_id: packet.source for packet in packets}
            a_candidates = [item for item in ids if source_by_id.get(item) == 0]
            b_candidates = [item for item in ids if source_by_id.get(item) == 1]
            packet_a = packet_a or (a_candidates[0] if a_candidates else ids[0])
            packet_b = packet_b or (b_candidates[0] if b_candidates else ids[1])
        if packet_a is None or packet_b is None:
            raise InventoryError(f"pair {pair_id} requires packet_a and packet_b")
        packet_a, packet_b = str(packet_a), str(packet_b)
        primary = _first(raw_pair, "primary", "primary_packet", "main_packet", default=None)
        primary_source = _first(raw_pair, "primary_source", "main_source", default=None)
        if primary is None and primary_source is not None:
            primary_rank = _source_rank(primary_source)
            primary = packet_a if primary_rank == 0 else packet_b
        if primary is None:
            primary = _first(raw_pair, "main", default=None)
        if primary is None:
            raise InventoryError(f"pair {pair_id} must identify its primary/main packet")
        primary = str(primary)
        diff_range_value = _first(
            raw_pair,
            "receive_diff_range",
            "receive_time_diff_range",
            "time_diff_range",
            "diff_range",
            "receiver_diff_ms",
            "接收时差范围",
            default=None,
        )
        if diff_range_value is not None:
            diff_range = _range_value(diff_range_value, f"pair {pair_id} range")
        else:
            diff_range = None
        pair = Pair(pair_id, packet_a, packet_b, primary, diff_range)
        pairs[pair_id] = pair
        pair_primary_overrides[primary] = pair_id

    for packet in packets:
        if packet.packet_id in pair_primary_overrides:
            packet.primary = True
            pair_id = pair_primary_overrides[packet.packet_id]
            packet.pair_id = pair_id
            continue
        matching_pair = next(
            (pair for pair in pairs.values() if packet.packet_id in (pair.packet_a, pair.packet_b)),
            None,
        )
        if matching_pair is not None:
            packet.pair_id = matching_pair.pair_id
            packet.primary = packet.packet_id == matching_pair.primary_packet

    pair_groups: dict[str, list[Packet]] = {}
    for packet in packets:
        if packet.pair_id is not None:
            pair_groups.setdefault(packet.pair_id, []).append(packet)
    for pair_id, group in pair_groups.items():
        if pair_id in pairs:
            pair = pairs[pair_id]
            for packet in group:
                packet.primary = packet.packet_id == pair.primary_packet
            continue
        if len(group) != 2:
            raise InventoryError(f"pair {pair_id} must contain exactly two packets")
        explicit_primaries = [packet for packet in group if packet.primary_explicit and packet.primary]
        if len(explicit_primaries) != 1:
            raise InventoryError(f"pair {pair_id} must mark exactly one primary packet")
        primary_packet = explicit_primaries[0].packet_id
        source_by_id = {packet.packet_id: packet.source for packet in group}
        packet_a = next((pid for pid, source in source_by_id.items() if source == 0), group[0].packet_id)
        packet_b = next((pid for pid, source in source_by_id.items() if source == 1), group[1].packet_id)
        pairs[pair_id] = Pair(pair_id, packet_a, packet_b, primary_packet)
        for packet in group:
            packet.primary = packet.packet_id == primary_packet

    offset_range_value = _first(
        data,
        "offset_range",
        "delta_range",
        "clock_offset_range",
        "skew_range",
        "shift_range",
        "offset",
        "delta",
        "差值范围",
        "偏移范围",
        default=(-10**12, 10**12),
    )
    offset_range = _range_value(offset_range_value, "offset_range")
    restrictions: list[OffsetRestriction] = [
        OffsetRestriction("handover.offset_range", offset_range[0], offset_range[1], "offset_range")
    ]
    for pair in pairs.values():
        if pair.receive_diff_range is not None:
            restrictions.append(
                make_pair_restriction(
                    pair,
                    pair.receive_diff_range,
                    packet_by_id[pair.packet_a].time,
                    packet_by_id[pair.packet_b].time,
                )
            )
    revisions = {"original": Revision("original", tuple(restrictions))}

    raw_revisions = _first(data, "revisions", "versions", "handsheets", "交班版本", default=None)
    if isinstance(raw_revisions, dict):
        raw_revision_list = [
            {"name": name, **(value if isinstance(value, dict) else {"range": value})}
            for name, value in raw_revisions.items()
        ]
    else:
        raw_revision_list = raw_revisions or []
    for raw_rev in raw_revision_list:
        name = str(_first(raw_rev, "name", "version", "id"))
        base_name = str(_first(raw_rev, "based_on", "base", "parent", default="original"))
        base = revisions.get(base_name, revisions["original"])
        rev_restrictions = list(base.restrictions)
        rev_offset = _first(raw_rev, "offset_range", "delta_range", "range", default=None)
        if rev_offset is not None:
            low, high = _range_value(rev_offset, f"revision {name} offset_range")
            rev_restrictions.append(
                OffsetRestriction(f"revision:{name}:offset_range", low, high, "offset_range")
            )
        pair_ranges = _first(raw_rev, "pair_ranges", "pair_limits", "pair_constraints", default={})
        if isinstance(pair_ranges, dict):
            pair_items = [
                {"pair_id": pair_id, "receive_diff_range": value} for pair_id, value in pair_ranges.items()
            ]
        else:
            pair_items = pair_ranges
        for item in pair_items:
            pair_id = str(_first(item, "pair_id", "pair", "id"))
            if pair_id not in pairs:
                raise InventoryError(f"revision {name} references unknown pair {pair_id}")
            range_value = _first(item, "receive_diff_range", "range", "diff_range")
            low, high = _range_value(range_value, f"revision {name} pair {pair_id}")
            restriction_id = _first(item, "id", "restriction_id", default=f"pair:{pair_id}@{name}")
            rev_restrictions.append(
                make_pair_restriction(
                    pairs[pair_id],
                    (low, high),
                    packet_by_id[pairs[pair_id].packet_a].time,
                    packet_by_id[pairs[pair_id].packet_b].time,
                    str(restriction_id),
                )
            )
        revisions[name] = Revision(name, tuple(rev_restrictions), base_name)

    cleanups_raw = _first(
        data,
        "cleanups",
        "cleanup_times",
        "cleanup_ledger",
        "expiry_cleanups",
        "清理台账",
        default=[],
    )
    cleanups: list[int] = []
    for item in cleanups_raw:
        if isinstance(item, dict):
            cleanups.append(_as_int(_first(item, "at", "time", "ms"), "cleanup.at"))
        else:
            cleanups.append(_as_int(item, "cleanup"))
    checks = _first(data, "queries", "checks", "query", "检查点", "查询", default=[])
    if isinstance(checks, dict):
        checks = [checks]
    default_revision = str(_first(data, "default_revision", "revision", "version", default="original"))
    return HandoverLedger(
        packets=packets,
        cleanups=cleanups,
        revisions=revisions,
        pairs=pairs,
        default_revision=default_revision,
        checks=checks if isinstance(checks, list) else [],
    )


def _unwrap_request(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data.get("ledger"), dict):
        return data
    ledger_data = dict(data["ledger"])
    for key in ("queries", "checks"):
        if key in data and key not in ledger_data:
            ledger_data[key] = data[key]
    for key in ("revision", "default_revision"):
        if key in data and key not in ledger_data:
            ledger_data[key] = data[key]
    return ledger_data


def load_ledger(source: str | Path | dict[str, Any] | bytes) -> HandoverLedger:
    """Load a ledger from a dict, JSON path, JSON bytes, or JSON string."""
    if isinstance(source, HandoverLedger):
        return source
    if isinstance(source, dict):
        return HandoverLedger.from_dict(_unwrap_request(source))
    if isinstance(source, (bytes, bytearray)):
        data = json.loads(source)
    else:
        path = Path(source)
        text = source if not path.exists() else path.read_text(encoding="utf-8")
        data = json.loads(text)
    if isinstance(data, dict):
        data = _unwrap_request(data)
    return HandoverLedger.from_dict(data)


def answer_queries(
    ledger: HandoverLedger | dict[str, Any] | str | Path,
    queries: Iterable[InventoryQuery | dict[str, Any]] | None = None,
    *,
    revision: str | None = None,
    include_witness_trace: bool = True,
) -> list[QueryResult]:
    """Load/index a ledger once and answer many checkpoints."""
    handover = load_ledger(ledger) if not isinstance(ledger, HandoverLedger) else ledger
    return handover.query_all(queries, revision=revision, include_witness_trace=include_witness_trace)


def query_inventory(
    ledger: HandoverLedger | dict[str, Any] | str | Path,
    at: int | dict[str, Any] | InventoryQuery | None = None,
    *,
    service: str | None = None,
    txt: bytes | str | None = None,
    ipv4: str | bytes | None = None,
    revision: str | None = None,
    query: dict[str, Any] | InventoryQuery | None = None,
) -> QueryResult:
    """Convenience entry for one offline inventory query."""
    handover = load_ledger(ledger) if not isinstance(ledger, HandoverLedger) else ledger
    if query is None:
        if at is None:
            raise InventoryError("query time is required")
        if isinstance(at, (dict, InventoryQuery)):
            query = at
        else:
            if service is None or txt is None or ipv4 is None:
                raise InventoryError("service, txt, and ipv4 are required")
            if isinstance(ipv4, str):
                packed_ipv4 = ipaddress.IPv4Address(ipv4).packed
            else:
                packed_ipv4 = _decode_bytes(ipv4, "ipv4")
            query = InventoryQuery(
                at=_as_int(at, "at"),
                service=service,
                txt=_txt_from_mapping({"txt": txt}) if isinstance(txt, str) else _decode_bytes(txt, "txt"),
                ipv4=packed_ipv4,
            )
    elif not isinstance(query, InventoryQuery):
        query = InventoryQuery.from_dict(query)
    return handover.query(query, revision=revision)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Offline mDNS handover inventory query")
    parser.add_argument("ledger", help="JSON ledger path or - for stdin")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--at", type=int, default=None)
    parser.add_argument("--service", default=None)
    parser.add_argument("--txt-hex", default=None)
    parser.add_argument("--ipv4", default=None)
    parser.add_argument("--no-witness-trace", action="store_true")
    args = parser.parse_args(argv)

    raw = sys_stdin_read() if args.ledger == "-" else Path(args.ledger).read_text(encoding="utf-8")
    ledger = load_ledger(json.loads(raw))
    if args.at is not None:
        result = query_inventory(
            ledger,
            args.at,
            service=args.service,
            txt=bytes.fromhex(args.txt_hex or ""),
            ipv4=args.ipv4,
            revision=args.revision,
        )
        output: Any = result.to_dict()
    else:
        results = ledger.query_all(revision=args.revision, include_witness_trace=not args.no_witness_trace)
        output = {"results": [item.to_dict() for item in results]}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def sys_stdin_read() -> str:
    import sys

    return sys.stdin.read()


__all__ = [
    "HandoverLedger",
    "InventoryQuery",
    "InventoryError",
    "OffsetConflict",
    "Packet",
    "Pair",
    "Projection",
    "QueryResult",
    "RecordSpec",
    "ReplayTrace",
    "answer_queries",
    "load_ledger",
    "query_inventory",
]


if __name__ == "__main__":
    raise SystemExit(main())
