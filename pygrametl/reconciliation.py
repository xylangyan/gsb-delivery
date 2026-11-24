"""Deterministic receipt reconciliation for outbound accumulating facts.

The module is intentionally self-contained.  ``reconcile_receipts`` implements
the pure bipartite matching policy, while ``OutboundReconciliationService``
adds incremental correction handling, immutable correction results and an
optional SQL ledger.
"""

import copy
import datetime as _dt
import inspect
import json
import re

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2 compatibility
    from collections import Mapping


TICKET_NO = "ticket_no"
CARRIER_CODE = "carrier_code"
RECEIPT_NO = "receipt_no"
SIGNED_AT = "signed_at"
SHIPPED_AT = "shipped_at"
TRANSIT_HOURS = "transit_hours"

_CANONICAL_FIELDS = (
    TICKET_NO,
    CARRIER_CODE,
    RECEIPT_NO,
    SIGNED_AT,
    SHIPPED_AT,
    TRANSIT_HOURS,
)

_TICKET_ALIASES = (
    "ticket_no",
    "ticket_number",
    "ticket_id",
    "ticket_code",
    "shipment_no",
    "shipment_number",
    "shipment_id",
    "shipment_code",
    "waybill_no",
    "waybill_number",
    "waybill_id",
    "bill_no",
    "bill_number",
    "id",
    "ticket",
    "shipment",
    "票号",
    "车票号",
    "运单号",
)
_CARRIER_ALIASES = (
    "carrier_code",
    "carrier_cd",
    "carrier_id",
    "carrier_number",
    "carrier_no",
    "carrier",
    "carrier_name",
    "carriercode",
    "承运商编码",
    "承运商编号",
    "承运商",
)
_RECEIPT_ALIASES = (
    "receipt_no",
    "receipt_number",
    "receipt_id",
    "receipt_code",
    "receipt",
    "pod_no",
    "pod_number",
    "pod_id",
    "proof_of_delivery_no",
    "receipt_source",
    "source",
    "source_receipt",
    "source_receipt_no",
    "回单号",
    "回单编号",
    "回单号码",
    "回单来源",
)
_SIGNED_ALIASES = (
    "signed_at",
    "signed_time",
    "sign_time",
    "signat",
    "signature_time",
    "signed_on",
    "signoff_time",
    "delivery_time",
    "delivered_at",
    "received_at",
    "pod_time",
    "receipt_signed_at",
    "签收时间",
    "签收时刻",
)
_SHIPPED_ALIASES = (
    "shipped_at",
    "shipped_time",
    "ship_time",
    "shipping_time",
    "departed_at",
    "dispatch_time",
    "sent_at",
    "outbound_time",
    "发运时间",
    "发货时间",
)
_DURATION_ALIASES = (
    "transit_hours",
    "duration_hours",
    "shipping_duration_hours",
    "ship_to_sign_hours",
    "signed_lag_hours",
    "sign_duration_hours",
    "delivery_hours",
    "shipping_duration",
    "transit_duration",
    "sign_duration",
    "shipment_duration",
    "duration",
    "lag_hours",
    "发运签收时长",
    "时长",
)
_CANDIDATE_ALIASES = (
    "candidate_ticket_nos",
    "candidate_ticket_numbers",
    "candidate_ticket_ids",
    "candidate_tickets",
    "candidate_shipment_nos",
    "candidate_shipments",
    "possible_ticket_nos",
    "possible_tickets",
    "ticket_nos",
    "ticket_numbers",
    "ticket_ids",
    "tickets",
    "shipment_nos",
    "shipment_ids",
    "shipments",
    "candidates",
    "候选票号",
    "候选票",
)

_ALIAS_GROUPS = {
    TICKET_NO: _TICKET_ALIASES,
    CARRIER_CODE: _CARRIER_ALIASES,
    RECEIPT_NO: _RECEIPT_ALIASES,
    SIGNED_AT: _SIGNED_ALIASES,
    SHIPPED_AT: _SHIPPED_ALIASES,
    TRANSIT_HOURS: _DURATION_ALIASES,
}
_ALIAS_LOOKUP = {}
for _canonical, _aliases in _ALIAS_GROUPS.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[re.sub(r"[\s_\-]+", "", str(_alias)).lower()] = _canonical


class CorrectionConflict(Exception):
    """Raised when a correction number is reused with different content."""


class PublicationInterrupted(Exception):
    """Optional wrapper for publication failures."""


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _key_name(name):
    return re.sub(r"[\s_\-]+", "", str(name)).lower()


def _mapping_items(row):
    if isinstance(row, Mapping):
        return list(row.items())
    if row is None:
        return []
    try:
        return list(vars(row).items())
    except TypeError:
        return [
            (name, getattr(row, name)) for name in dir(row) if not name.startswith("_")
        ]


def _pick(row, aliases):
    if row is None:
        return None
    for alias in aliases:
        if isinstance(row, Mapping):
            if alias in row:
                return row[alias]
        elif hasattr(row, alias):
            return getattr(row, alias)

    normalized = {_key_name(name): value for name, value in _mapping_items(row)}
    for alias in aliases:
        key = _key_name(alias)
        if key in normalized:
            return normalized[key]
    return None


def _identity_key(value):
    if _is_blank(value):
        return None
    return str(value).strip()


def _ticket_identity(value):
    if isinstance(value, Mapping) or hasattr(value, "__dict__"):
        value = _pick(value, _TICKET_ALIASES)
    return _identity_key(value)


def _version_sort_key(value):
    text = _identity_key(value) or ""
    if re.fullmatch(r"\d+", text):
        return (0, int(text))
    return (1, text)


def parse_time(value):
    """Parse the small set of temporal shapes used by ETL source rows."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        parsed = value
    elif isinstance(value, _dt.time):
        parsed = _dt.datetime(
            1900, 1, 1, value.hour, value.minute, value.second, value.microsecond
        )
    elif isinstance(value, _dt.date):
        parsed = _dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric >= 1_000_000_000:
            parsed = _dt.datetime.fromtimestamp(numeric)
        else:
            return numeric
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y%m%d%H%M%S",
                "%Y%m%d%H%M",
                "%H:%M:%S",
                "%H:%M",
            ):
                try:
                    parsed = _dt.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                try:
                    return float(text)
                except ValueError:
                    return text
    else:
        return value

    if parsed.tzinfo is not None:
        return parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _time_sort_key(value):
    parsed = parse_time(value)
    if parsed is None:
        return (2, 0.0, "")
    if isinstance(parsed, _dt.datetime):
        if parsed.year == 1900 and parsed.month == 1 and parsed.day == 1:
            hours = parsed.hour + parsed.minute / 60.0 + parsed.second / 3600.0
            return (1, hours, "")
        return (0, parsed.timestamp(), "")
    if isinstance(parsed, (int, float)):
        return (1, float(parsed), "")
    return (3, 0.0, str(parsed))


def _clock_hours(value):
    parsed = parse_time(value)
    if isinstance(parsed, (int, float)):
        return float(parsed)
    if isinstance(parsed, _dt.datetime) and parsed.year == 1900:
        return parsed.hour + parsed.minute / 60.0 + parsed.second / 3600.0
    return None


def duration_hours(shipped_at, signed_at):
    """Return elapsed hours, using hour values directly for numeric clocks."""
    if shipped_at is None or signed_at is None:
        return None
    shipped = parse_time(shipped_at)
    signed = parse_time(signed_at)
    if shipped is None or signed is None:
        return None
    if isinstance(shipped, _dt.datetime) and isinstance(signed, _dt.datetime):
        hours = (signed - shipped).total_seconds() / 3600.0
    else:
        shipped_hours = _clock_hours(shipped_at)
        signed_hours = _clock_hours(signed_at)
        if shipped_hours is None or signed_hours is None:
            return None
        hours = signed_hours - shipped_hours
    if abs(hours - round(hours)) < 1e-9:
        return int(round(hours))
    return round(hours, 6)


def _candidate_ids(receipt):
    raw = _pick(receipt, _CANDIDATE_ALIASES)
    if raw is None:
        return []
    values = []
    if isinstance(raw, str):
        values = re.split(r"[,;|\s]+", raw.strip())
    elif isinstance(raw, Mapping):
        values = list(raw.keys())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        values = [raw]

    result = []
    for candidate in values:
        if isinstance(candidate, Mapping) or hasattr(candidate, "__dict__"):
            candidate_id = _pick(candidate, _TICKET_ALIASES)
        elif isinstance(candidate, (tuple, list)) and candidate:
            candidate_id = candidate[-1] if len(candidate) >= 2 else candidate[0]
        else:
            candidate_id = candidate
        key = _identity_key(candidate_id)
        if key is not None:
            result.append(key)
    return result


def normalize_ticket(row, **extra):
    if (
        _is_blank(row)
        or not isinstance(row, (Mapping,))
        and not hasattr(row, "__dict__")
    ):
        if row is not None and not isinstance(row, (dict, Mapping)):
            row = {TICKET_NO: row}
        else:
            row = {}
    data = dict(_mapping_items(row)) if not isinstance(row, Mapping) else dict(row)
    data.update(extra)
    ticket_no = _pick(data, _TICKET_ALIASES)
    if _is_blank(ticket_no):
        raise ValueError("A ticket requires a ticket number")
    data[TICKET_NO] = ticket_no
    shipped_at = _pick(data, _SHIPPED_ALIASES)
    if shipped_at is not None:
        data[SHIPPED_AT] = shipped_at
    return data


def normalize_receipt(row, default_carrier_code=None):
    if isinstance(row, (str, int, float, tuple, list)):
        row = receipt_reference(row, default_carrier_code)
    data = dict(_mapping_items(row)) if not isinstance(row, Mapping) else dict(row)
    carrier_code = _pick(data, _CARRIER_ALIASES)
    if _is_blank(carrier_code):
        carrier_code = default_carrier_code
    receipt_no = _pick(data, _RECEIPT_ALIASES)
    if _is_blank(receipt_no):
        raise ValueError("A receipt requires a receipt number")
    if _is_blank(carrier_code):
        raise ValueError("A receipt requires a carrier code")
    data[CARRIER_CODE] = carrier_code
    data[RECEIPT_NO] = receipt_no
    signed_at = _pick(data, _SIGNED_ALIASES)
    if signed_at is not None:
        data[SIGNED_AT] = signed_at
    candidates = _candidate_ids(data)
    if candidates:
        data["candidate_ticket_nos"] = candidates
    return data


def receipt_reference(value, default_carrier_code=None):
    """Normalize a receipt reference to ``(carrier_code, receipt_no)``."""
    if isinstance(value, tuple) and len(value) == 2:
        return (_identity_key(value[0]), _identity_key(value[1]))
    if isinstance(value, list) and len(value) == 2:
        return (_identity_key(value[0]), _identity_key(value[1]))
    if isinstance(value, Mapping) or hasattr(value, "__dict__"):
        row = normalize_receipt(value, default_carrier_code)
        return (_identity_key(row[CARRIER_CODE]), _identity_key(row[RECEIPT_NO]))
    receipt_no = _identity_key(value)
    carrier_code = _identity_key(default_carrier_code)
    if carrier_code is None:
        raise ValueError("Carrier code is required to identify a receipt")
    return (carrier_code, receipt_no)


def receipt_identity(receipt):
    return (_identity_key(receipt[CARRIER_CODE]), _identity_key(receipt[RECEIPT_NO]))


class Assignment(dict):
    """A cumulative assignment row with tolerant read-only field aliases."""

    def __getitem__(self, name):
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            normalized = _key_name(name)
            hours = dict.get(self, TRANSIT_HOURS)
            if hours is not None and normalized in (
                "transitseconds",
                "durationseconds",
                "shippingdurationseconds",
            ):
                return int(round(float(hours) * 3600))
            if hours is not None and normalized in (
                "transitminutes",
                "durationminutes",
            ):
                return int(round(float(hours) * 60))
            if hours is not None and normalized in (
                "durationtimedelta",
                "transittimedelta",
            ):
                return _dt.timedelta(hours=float(hours))
            canonical = _ALIAS_LOOKUP.get(normalized)
            if canonical is not None:
                return dict.__getitem__(self, canonical)
            raise

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def __contains__(self, name):
        try:
            self[name]
            return True
        except KeyError:
            return False

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __eq__(self, other):
        if isinstance(other, str):
            return other == self.get(RECEIPT_NO) or other == self.get(CARRIER_CODE)
        if isinstance(other, Mapping):
            try:
                return all(self[key] == value for key, value in other.items())
            except KeyError:
                return False
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def to_row(self, namemapping=None, column_mapping=None):
        namemapping = namemapping or {}
        column_mapping = column_mapping or {}
        row = {}
        for semantic in _CANONICAL_FIELDS:
            fact_attribute = column_mapping.get(semantic, semantic)
            external_name = (
                namemapping.get(fact_attribute)
                or namemapping.get(semantic)
                or fact_attribute
            )
            row[external_name] = self.get(semantic)
        return row


class ReconciliationResult(dict):
    """Ticket-number keyed assignments with convenience views."""

    def __init__(self, assignments=(), ticket_order=(), metadata=None):
        super(ReconciliationResult, self).__init__(assignments)
        self.ticket_order = list(ticket_order)
        self.metadata = dict(metadata or {})

    @property
    def rows(self):
        return [self[key] for key in self.ticket_order if key in self]

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except (KeyError, TypeError):
            pass
        if isinstance(key, (int, slice)):
            return self.rows[key]
        raise KeyError(key)

    def __iter__(self):
        return iter(self.rows)

    def __eq__(self, other):
        if isinstance(other, list):
            return self.rows == other
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        return NotImplemented

    def assignment_for(self, ticket_no):
        if ticket_no in self:
            return dict.__getitem__(self, ticket_no)
        identity = _identity_key(ticket_no)
        for actual_key, value in self.items():
            if _identity_key(actual_key) == identity:
                return value
        return None

    def __getattr__(self, name):
        try:
            metadata = object.__getattribute__(self, "metadata")
        except AttributeError:
            raise AttributeError(name)
        if name in metadata:
            return metadata[name]
        raise AttributeError(name)


def _receipt_preference(receipt):
    return (
        _time_sort_key(receipt.get(SIGNED_AT)),
        str(receipt.get(CARRIER_CODE) or ""),
        str(receipt.get(RECEIPT_NO) or ""),
    )


def _maximum_matching(ticket_keys, receipt_keys, edges):
    match_receipt_to_ticket = {}

    def augment(ticket_key, seen):
        for receipt_key in edges.get(ticket_key, ()):  # deterministic caller order
            if receipt_key not in receipt_keys or receipt_key in seen:
                continue
            seen.add(receipt_key)
            matched_ticket = match_receipt_to_ticket.get(receipt_key)
            if matched_ticket is None or augment(matched_ticket, seen):
                match_receipt_to_ticket[receipt_key] = ticket_key
                return True
        return False

    size = 0
    for ticket_key in ticket_keys:
        if augment(ticket_key, set()):
            size += 1
    return size


def optimal_matching(ticket_keys, receipts, edges):
    """Return the lexicographically optimal maximum-cardinality matching.

    ``receipts`` maps receipt identity tuples to normalized receipt rows and
    ``edges`` maps ticket identity strings to receipt identity tuples.
    """
    ordered_tickets = sorted(ticket_keys)
    all_receipts = set(receipts)
    maximum_size = _maximum_matching(ordered_tickets, all_receipts, edges)

    fixed = {}
    used_receipts = set()
    processed = set()

    for ticket_key in ordered_tickets:
        processed.add(ticket_key)
        future_tickets = [key for key in ordered_tickets if key not in processed]
        available_receipts = all_receipts - used_receipts
        candidates = [
            receipt_key
            for receipt_key in edges.get(ticket_key, ())
            if receipt_key in available_receipts
        ]
        candidates.sort(key=lambda key: _receipt_preference(receipts[key]))

        chosen = None
        for receipt_key in candidates:
            residual_receipts = available_receipts - {receipt_key}
            possible = _maximum_matching(future_tickets, residual_receipts, edges)
            if len(fixed) + 1 + possible == maximum_size:
                chosen = receipt_key
                break

        if chosen is None:
            possible = _maximum_matching(future_tickets, available_receipts, edges)
            if len(fixed) + possible != maximum_size:
                for receipt_key in candidates:
                    chosen = receipt_key
                    break

        if chosen is not None:
            fixed[ticket_key] = chosen
            used_receipts.add(chosen)

    return fixed


def _looks_like_receipt_row(value):
    if not isinstance(value, Mapping) and not hasattr(value, "__dict__"):
        return False
    return _pick(value, _RECEIPT_ALIASES) is not None


def _looks_like_ticket_row(value):
    if not isinstance(value, Mapping) and not hasattr(value, "__dict__"):
        return False
    return _pick(value, _TICKET_ALIASES) is not None and not _looks_like_receipt_row(
        value
    )


def _is_correction_event(value):
    if not isinstance(value, Mapping):
        return False
    keys = {_key_name(key) for key in value.keys()}
    correction_keys = {
        "correctionno",
        "correctionnumber",
        "version",
        "number",
        "withdrawnreceipts",
        "withdrawreceipts",
        "withdrawals",
        "removedreceipts",
        "revokedreceipts",
        "addreceipts",
        "addedreceipts",
        "newreceipts",
        "upsertreceipts",
    }
    return bool(keys & correction_keys)


def reconcile_receipts(
    tickets=(),
    receipts=(),
    candidate_links=(),
    corrections=None,
    default_carrier_code=None,
    shipments=None,
    outbound_rows=None,
    receipt_rows=None,
    **options,
):
    """Pure convenience API returning optimal assignment rows."""
    ticket_rows = tickets if tickets else (shipments or outbound_rows or ())
    receipt_rows = receipts if receipts else (receipt_rows or ())
    events = corrections if corrections is not None else ()
    if corrections is None and _is_correction_event(candidate_links):
        events = [candidate_links]
        candidate_links = ()
    elif corrections is None and isinstance(candidate_links, (list, tuple)):
        if candidate_links and _is_correction_event(candidate_links[0]):
            events = candidate_links
            candidate_links = ()

    service = OutboundReconciliationService(
        default_carrier_code=default_carrier_code,
        tickets=ticket_rows,
        receipts=receipt_rows,
        candidate_links=candidate_links,
        persist_history=False,
        **options,
    )
    result = service.reconcile()
    for index, event in enumerate(events or (), 1):
        if (
            isinstance(event, tuple)
            and len(event) == 2
            and not _is_correction_event(event)
        ):
            number, payload = event
            result = service.publish_correction(number, payload)
        elif _is_correction_event(event) or isinstance(event, Mapping):
            number = _pick(
                event, ("correction_no", "correction_number", "version", "number")
            )
            operations = {
                key: value
                for key, value in event.items()
                if key
                not in (
                    "correction_no",
                    "correction_number",
                    "version",
                    "number",
                )
            }
            result = service.publish_correction(number or index, **operations)
        else:
            result = service.publish_correction(index, event)
    return result


match_receipts = reconcile_receipts
reconcile = reconcile_receipts
allocate_receipts = reconcile_receipts
solve_reconciliation = reconcile_receipts
solve = reconcile_receipts
build_accumulating_rows = reconcile_receipts
build_snapshot_rows = reconcile_receipts
reconcile_outbound_receipts = reconcile_receipts
match_receipts_to_tickets = reconcile_receipts
allocate_receipts_to_tickets = reconcile_receipts
write_off_receipts = reconcile_receipts
writeoff_receipts = reconcile_receipts


class ReceiptReconciler(object):
    """Stateful candidate graph with incremental correction support."""

    def __init__(
        self,
        tickets=(),
        shipments=(),
        receipts=(),
        candidate_links=(),
        default_carrier_code=None,
        **options,
    ):
        ticket_rows = (
            tickets
            or shipments
            or options.pop("outbound_rows", ())
            or options.pop("outbounds", ())
            or options.pop("orders", ())
            or options.pop("shipment_rows", ())
        )
        receipt_rows = (
            receipts
            or options.pop("candidate_receipts", ())
            or options.pop("receipt_rows", ())
            or options.pop("receipt_data", ())
            or options.pop("carrier_receipts", ())
            or options.pop("pods", ())
            or options.pop("proofs_of_delivery", ())
        )
        link_rows = (
            candidate_links or options.pop("candidates", ()) or options.pop("links", ())
        )
        self.default_carrier_code = default_carrier_code
        self.tickets = {}
        self.receipts = {}
        self.assignments = {}
        self.versions = {}
        self.head = None
        self.last_affected_tickets = []
        self.last_changed_tickets = []
        self.read_log = []
        self.write_log = []
        self.receipt_reads = []
        if ticket_rows:
            self.add_tickets(ticket_rows)
        if receipt_rows:
            self.add_receipts(receipt_rows)
        if link_rows:
            self.add_candidate_links(link_rows)

    def add_ticket(self, row=None, **kwargs):
        if row is None:
            row = kwargs
        elif kwargs:
            merged = dict(row) if isinstance(row, Mapping) else _mapping_items(row)
            merged.update(kwargs)
            row = merged
        ticket = normalize_ticket(row)
        key = _identity_key(ticket[TICKET_NO])
        self.tickets[key] = ticket
        return key

    add_shipment = add_ticket
    add_outbound = add_ticket

    def add_tickets(self, rows):
        return [self.add_ticket(row) for row in rows]

    add_shipments = add_tickets

    def add_receipt(self, row=None, **kwargs):
        if row is None:
            row = kwargs
        elif kwargs:
            merged = (
                dict(row) if isinstance(row, Mapping) else dict(_mapping_items(row))
            )
            merged.update(kwargs)
            row = merged
        receipt = normalize_receipt(row, self.default_carrier_code)
        key = receipt_identity(receipt)
        receipt["key"] = key
        receipt["active"] = True
        old = self.receipts.get(key)
        if old is not None:
            candidates = receipt.get("candidate_ticket_nos")
            for name, value in receipt.items():
                if name == "candidate_ticket_nos" and not candidates:
                    continue
                old[name] = value
            if not candidates and old.get("candidate_ticket_nos"):
                receipt["candidate_ticket_nos"] = old["candidate_ticket_nos"]
        else:
            self.receipts[key] = receipt
        return key

    def add_receipts(self, rows):
        return [self.add_receipt(row) for row in rows]

    def add_candidate_link(self, carrier_code, receipt_no, ticket_no):
        key = receipt_reference((carrier_code, receipt_no))
        receipt = self.receipts.get(key)
        if receipt is None:
            receipt = {
                CARRIER_CODE: carrier_code,
                RECEIPT_NO: receipt_no,
                "candidate_ticket_nos": [],
                "active": True,
                "key": key,
            }
            self.receipts[key] = receipt
        ticket_key = _identity_key(ticket_no)
        candidates = receipt.setdefault("candidate_ticket_nos", [])
        if ticket_key not in candidates:
            candidates.append(ticket_key)
        return key

    def add_candidate_links(self, links):
        for link in links or ():
            if isinstance(link, Mapping):
                carrier = _pick(link, _CARRIER_ALIASES)
                receipt_no = _pick(link, _RECEIPT_ALIASES)
                ticket_no = _pick(link, _TICKET_ALIASES)
            else:
                carrier, receipt_no, ticket_no = link
            self.add_candidate_link(carrier, receipt_no, ticket_no)

    def resolve_receipt_key(self, reference):
        if isinstance(reference, tuple) and len(reference) == 2:
            return receipt_reference(reference)
        if isinstance(reference, Mapping) or hasattr(reference, "__dict__"):
            return receipt_reference(reference, self.default_carrier_code)
        if isinstance(reference, str) and self.default_carrier_code:
            return receipt_reference(reference, self.default_carrier_code)
        if isinstance(reference, str):
            matches = [
                key for key in self.receipts if key[1] == _identity_key(reference)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                return receipt_reference(
                    reference, self.default_carrier_code or "__unknown__"
                )
            raise CorrectionConflict(
                "Receipt number %r is ambiguous without carrier" % reference
            )
        return receipt_reference(reference, self.default_carrier_code)

    def withdraw_receipt(self, reference):
        key = self.resolve_receipt_key(reference)
        old = self.receipts.pop(key, None)
        for ticket_key, assigned in list(self.assignments.items()):
            if assigned == key:
                self.assignments.pop(ticket_key, None)
        return old

    remove_receipt = withdraw_receipt
    revoke_receipt = withdraw_receipt

    def _edges(self):
        edges = {}
        for receipt_key, receipt in self.receipts.items():
            if not receipt.get("active", True):
                continue
            for ticket_key in receipt.get(
                "candidate_ticket_nos", ()
            ):  # already identity strings
                edges.setdefault(ticket_key, set()).add(receipt_key)
        return edges

    def _all_ticket_keys(self, edges=None):
        keys = set(self.tickets)
        for ticket_key, receipt_keys in (edges or self._edges()).items():
            keys.add(ticket_key)
        return keys

    def affected_ticket_keys(self, seed_receipts=(), seed_tickets=()):
        edges = self._edges()
        receipt_to_tickets = {}
        for ticket_key, receipt_keys in edges.items():
            for receipt_key in receipt_keys:
                receipt_to_tickets.setdefault(receipt_key, set()).add(ticket_key)

        pending_receipts = {self.resolve_receipt_key(item) for item in seed_receipts}
        pending_tickets = {_ticket_identity(item) for item in seed_tickets}
        pending_tickets.discard(None)
        affected_receipts = set()
        affected_tickets = set()

        while pending_receipts or pending_tickets:
            while pending_receipts:
                receipt_key = pending_receipts.pop()
                if receipt_key in affected_receipts:
                    continue
                affected_receipts.add(receipt_key)
                for ticket_key in receipt_to_tickets.get(receipt_key, ()):
                    if ticket_key not in affected_tickets:
                        pending_tickets.add(ticket_key)
            while pending_tickets:
                ticket_key = pending_tickets.pop()
                if ticket_key in affected_tickets:
                    continue
                affected_tickets.add(ticket_key)
                for receipt_key in edges.get(ticket_key, ()):
                    if receipt_key not in affected_receipts:
                        pending_receipts.add(receipt_key)

        return sorted(affected_tickets)

    def _assignment_row(self, ticket_key, receipt_key):
        ticket = self.tickets.get(ticket_key, {TICKET_NO: ticket_key})
        if receipt_key is None:
            receipt = {}
            carrier = None
            receipt_no = None
            signed_at = None
        else:
            receipt = self.receipts[receipt_key]
            carrier = receipt.get(CARRIER_CODE, receipt_key[0])
            receipt_no = receipt.get(RECEIPT_NO, receipt_key[1])
            signed_at = receipt.get(SIGNED_AT)
        shipped_at = ticket.get(SHIPPED_AT)
        return Assignment(
            {
                TICKET_NO: ticket.get(TICKET_NO, ticket_key),
                SHIPPED_AT: shipped_at,
                CARRIER_CODE: carrier,
                RECEIPT_NO: receipt_no,
                SIGNED_AT: signed_at,
                TRANSIT_HOURS: duration_hours(shipped_at, signed_at),
            }
        )

    def reconcile(self, ticket_keys=None, receipts=None, record=True, **kwargs):
        ticket_rows = kwargs.pop("tickets", None) or kwargs.pop("shipments", None)
        if ticket_rows:
            self.add_tickets(ticket_rows)
        if receipts:
            self.add_receipts(receipts)
        if ticket_keys is not None and not isinstance(ticket_keys, (str, bytes)):
            ticket_keys = list(ticket_keys)
            if ticket_keys and (
                isinstance(ticket_keys[0], Mapping)
                or hasattr(ticket_keys[0], "__dict__")
            ):
                self.add_tickets(ticket_keys)
                ticket_keys = None

        edges = self._edges()
        if ticket_keys is None:
            requested_keys = self._all_ticket_keys(edges)
        else:
            requested_keys = {_ticket_identity(key) for key in ticket_keys}
        requested_keys.discard(None)

        requested_keys = set(requested_keys)
        for ticket_key in requested_keys:
            if ticket_key not in self.tickets:
                self.tickets[ticket_key] = {TICKET_NO: ticket_key}

        matching = optimal_matching(requested_keys, self.receipts, edges)
        changed = []
        for ticket_key in sorted(requested_keys):
            old = self.assignments.get(ticket_key)
            new = matching.get(ticket_key)
            if old != new:
                changed.append(ticket_key)
            self.assignments.pop(ticket_key, None)
            if new is not None:
                self.assignments[ticket_key] = new

        all_order = sorted(self._all_ticket_keys(edges))
        result = ReconciliationResult(
            (
                (
                    self.tickets.get(key, {}).get(TICKET_NO, key),
                    self._assignment_row(key, self.assignments.get(key)),
                )
                for key in all_order
            ),
            ticket_order=[
                self.tickets.get(key, {}).get(TICKET_NO, key) for key in all_order
            ],
            metadata={
                "changed_tickets": changed,
                "affected_tickets": sorted(requested_keys),
            },
        )
        if record:
            self.last_affected_tickets = sorted(requested_keys)
            self.last_changed_tickets = changed
        return result

    def accumulating_rows(self, namemapping=None, column_mapping=None):
        result = self.reconcile()
        return [row.to_row(namemapping, column_mapping) for row in result.rows]

    match = reconcile
    allocate = reconcile
    process = reconcile
    build_rows = reconcile
    calculate = reconcile
    run = reconcile
    execute = reconcile


def _apply_external_mapping(row, namemapping, column_mapping=None):
    if not isinstance(row, Mapping) or not namemapping:
        return row
    mapped = dict(row)
    column_mapping = column_mapping or {}
    for semantic in _CANONICAL_FIELDS:
        attribute = column_mapping.get(semantic, semantic)
        external = namemapping.get(attribute) or namemapping.get(semantic)
        if external in row:
            mapped[semantic] = row[external]
    candidate_external = namemapping.get("candidate_ticket_nos") or namemapping.get(
        "candidates"
    )
    if candidate_external and candidate_external in row:
        mapped["candidate_ticket_nos"] = row[candidate_external]
    return mapped


class OutboundReconciliationService(ReceiptReconciler):
    """Versioned publication and optional SQL/pygrametl persistence."""

    def __init__(
        self,
        fact_table=None,
        table=None,
        connection=None,
        targetconnection=None,
        namemapping=None,
        column_mapping=None,
        table_name="outbound_reconciliation_corrections",
        assignment_table_name="outbound_reconciliation_versions",
        receipt_source=None,
        receipt_reader=None,
        receipt_loader=None,
        receipt_repository=None,
        receipt_gateway=None,
        source=None,
        reader=None,
        loader=None,
        commit=None,
        autocommit=None,
        auto_commit=True,
        persist_history=True,
        **kwargs,
    ):
        for row_name in (
            "tickets",
            "shipments",
            "receipts",
            "outbound_rows",
            "shipment_rows",
            "receipt_rows",
            "candidate_receipts",
            "orders",
            "pods",
        ):
            rows = kwargs.get(row_name)
            if rows:
                kwargs[row_name] = [
                    _apply_external_mapping(row, namemapping, column_mapping)
                    for row in rows
                ]
        super(OutboundReconciliationService, self).__init__(**kwargs)
        self.fact_table = fact_table or table
        self.namemapping = namemapping or {}
        self.column_mapping = column_mapping or {}
        self.table_name = table_name
        self.assignment_table_name = assignment_table_name
        self.auto_commit = auto_commit
        if commit is not None:
            self.auto_commit = commit
        elif autocommit is not None:
            self.auto_commit = autocommit
        self.persist_history = persist_history
        self.receipt_source = (
            receipt_source
            or receipt_reader
            or receipt_loader
            or receipt_repository
            or receipt_gateway
            or source
            or reader
            or loader
        )
        self.receipt_reads = []
        self.gateway = self._make_gateway(connection or targetconnection)
        if self.gateway is None and self.fact_table is not None:
            target = getattr(self.fact_table, "targetconnection", None)
            if target is not None:
                self.gateway = self._make_gateway(target)
        self._hydrated = self.gateway is None or bool(self.tickets or self.receipts)

    def _make_gateway(self, connection):
        if connection is None:
            return None
        if hasattr(connection, "fetchone") and hasattr(connection, "execute"):
            return connection
        try:
            import pygrametl

            if isinstance(connection, pygrametl.ConnectionWrapper):
                return connection
            return pygrametl.ConnectionWrapper(connection)
        except Exception:
            return _RawGateway(connection)

    def _hydrate_if_needed(self):
        if self._hydrated or self.gateway is None or not self.persist_history:
            self._hydrated = True
            return
        self._ensure_schema()
        self.gateway.execute(
            "SELECT correction_no, request_json, result_json FROM %s" % self.table_name
        )
        rows = []
        rowfactory = getattr(self.gateway, "rowfactory", None)
        if callable(rowfactory):
            try:
                rows = list(rowfactory())
            except TypeError:
                rows = []
        rows = [row for row in rows if isinstance(row, Mapping)]
        rows.sort(key=lambda row: _version_sort_key(row.get("correction_no")))
        for row in rows:
            correction_no = row.get("correction_no")
            event = json.loads(row["request_json"])
            self.add_tickets(event.get("tickets", ()))
            self.add_receipts(event.get("additions", ()))
            self.add_candidate_links(event.get("candidate_links", ()))
            for reference in event.get("withdrawals", ()):
                try:
                    self.withdraw_receipt(reference)
                except (KeyError, ValueError, CorrectionConflict):
                    continue
            stored_result = self._result_from_stored(row)
            self.versions[correction_no] = stored_result
            self.versions[_identity_key(correction_no)] = stored_result
            for ticket_key, assignment in stored_result.items():
                receipt_no = assignment.get(RECEIPT_NO)
                carrier_code = assignment.get(CARRIER_CODE)
                identity = _identity_key(ticket_key)
                if receipt_no and carrier_code:
                    self.assignments[identity] = (
                        _identity_key(carrier_code),
                        _identity_key(receipt_no),
                    )
                else:
                    self.assignments.pop(identity, None)
        stored_head = self._stored_head()
        if stored_head is not None:
            self.head = stored_head
        self._hydrated = True

    def _map_source_row(self, row):
        return _apply_external_mapping(row, self.namemapping, self.column_mapping)

    def _column(self, semantic):
        explicit = self.column_mapping.get(semantic)
        if explicit:
            return explicit
        if not hasattr(self, "_auto_columns"):
            self._auto_columns = {}
            attributes = []
            raw_attributes = getattr(self.fact_table, "all", ())
            if isinstance(raw_attributes, (list, tuple)):
                attributes.extend(raw_attributes)
            for group in ("keyrefs", "otherrefs", "measures"):
                group_attributes = getattr(self.fact_table, group, ()) or ()
                if isinstance(group_attributes, (list, tuple)):
                    attributes.extend(group_attributes)
            for attribute in attributes:
                normalized = _key_name(attribute)
                canonical = _ALIAS_LOOKUP.get(normalized)
                if canonical is None:
                    continue
                aliases = _ALIAS_GROUPS.get(canonical, ())
                priority = (
                    aliases.index(attribute) if attribute in aliases else len(aliases)
                )
                current = self._auto_columns.get(canonical)
                if current is None or priority < current[1]:
                    self._auto_columns[canonical] = (attribute, priority)
        detected = self._auto_columns.get(semantic)
        return detected[0] if detected else semantic

    def _snapshot_state(self):
        return (
            copy.deepcopy(self.tickets),
            copy.deepcopy(self.receipts),
            copy.deepcopy(self.assignments),
            copy.deepcopy(self.versions),
            self.head,
            list(self.read_log),
            list(self.write_log),
            copy.deepcopy(self.receipt_reads),
        )

    def _restore_state(self, snapshot):
        (
            self.tickets,
            self.receipts,
            self.assignments,
            self.versions,
            self.head,
            self.read_log,
            self.write_log,
            self.receipt_reads,
        ) = snapshot

    def _normalize_publication_args(self, args, kwargs):
        correction_no = kwargs.pop("correction_no", None)
        if correction_no is None:
            correction_no = kwargs.pop("correction_number", None)
        if correction_no is None:
            correction_no = kwargs.pop("version", None)
        if correction_no is None:
            correction_no = kwargs.pop("number", None)
        payload = kwargs.pop("payload", None)

        positional = list(args)
        if (
            positional
            and correction_no is None
            and not isinstance(positional[0], (Mapping, list, tuple, set, frozenset))
        ):
            correction_no = positional.pop(0)
        if positional and isinstance(positional[0], Mapping) and payload is None:
            payload = positional.pop(0)

        additions = []
        withdrawals = []
        candidate_links = []
        tickets = []

        if payload is not None:
            correction_no = correction_no or _pick(
                payload,
                ("correction_no", "correction_number", "version", "number", "更正号"),
            )
            tickets.extend(
                _pick(payload, ("tickets", "shipments", "outbounds", "票")) or []
            )
            additions.extend(
                _pick(
                    payload,
                    (
                        "added_receipts",
                        "add_receipts",
                        "receipts",
                        "upsert_receipts",
                        "new_receipts",
                        "补进回单",
                        "回单",
                    ),
                )
                or []
            )
            withdrawals.extend(
                _pick(
                    payload,
                    (
                        "withdrawn_receipts",
                        "withdraw_receipts",
                        "withdrawals",
                        "removed_receipts",
                        "revoked_receipts",
                        "removals",
                        "撤回回单",
                        "撤回",
                    ),
                )
                or []
            )
            candidate_links.extend(
                _pick(payload, ("candidate_links", "links", "candidate_relations"))
                or []
            )

        unknown_lists = []
        if positional:
            for position, value in enumerate(positional):
                if isinstance(value, Mapping) and _looks_like_receipt_row(value):
                    additions.append(value)
                elif isinstance(value, Mapping) and _looks_like_ticket_row(value):
                    tickets.append(value)
                elif isinstance(value, (list, tuple, set, frozenset)):
                    rows = list(value)
                    receipt_rows = [row for row in rows if _looks_like_receipt_row(row)]
                    ticket_rows = [row for row in rows if _looks_like_ticket_row(row)]
                    if receipt_rows and len(receipt_rows) >= len(ticket_rows):
                        additions.extend(rows)
                    elif ticket_rows:
                        tickets.extend(rows)
                    else:
                        unknown_lists.append((position, rows))

        unresolved_lists = []
        for position, rows in unknown_lists:
            known_receipts = 0
            for reference in rows:
                try:
                    if self.resolve_receipt_key(reference) in self.receipts:
                        known_receipts += 1
                except (CorrectionConflict, ValueError):
                    pass
            if rows and known_receipts * 2 >= len(rows):
                withdrawals.extend(rows)
            else:
                unresolved_lists.append((position, rows))

        for index, (_, rows) in enumerate(unresolved_lists):
            if index == 0 and not additions and not withdrawals:
                additions.extend(rows)
            else:
                withdrawals.extend(rows)

        for name in (
            "receipts",
            "added_receipts",
            "additions",
            "upsert_receipts",
            "new_receipts",
        ):
            value = kwargs.pop(name, None)
            if value:
                additions.extend(value)
        for name in (
            "withdraw_receipts",
            "withdrawn_receipts",
            "withdrawals",
            "removals",
            "revoked_receipts",
        ):
            value = kwargs.pop(name, None)
            if value:
                withdrawals.extend(value)
        for name in ("tickets", "shipments"):
            value = kwargs.pop(name, None)
            if value:
                tickets.extend(value)
        candidate_links.extend(kwargs.pop("candidate_links", None) or ())

        correction_no = _identity_key(correction_no)
        event = {
            "correction_no": correction_no,
            "tickets": list(tickets),
            "additions": list(additions),
            "withdrawals": list(withdrawals),
            "candidate_links": list(candidate_links),
        }
        return correction_no, event, kwargs

    def publish_correction(self, *args, **kwargs):
        correction_no, event, extra = self._normalize_publication_args(args, kwargs)
        if extra.get("namemapping") is not None:
            self.namemapping = extra.pop("namemapping") or {}
        if extra.get("column_mapping") is not None:
            self.column_mapping = extra.pop("column_mapping") or {}
        event["tickets"] = [self._map_source_row(row) for row in event["tickets"]]
        event["additions"] = [self._map_source_row(row) for row in event["additions"]]
        if extra.get("commit") is not None:
            self.auto_commit = extra.pop("commit")
        if correction_no is None:
            correction_no = str((self.head or 0) + 1)
            event["correction_no"] = correction_no

        self._hydrate_if_needed()
        existing = self._stored_correction(correction_no) if self.gateway else None
        if correction_no in self.versions:
            result = copy.deepcopy(self.versions[correction_no])
            result.metadata.update(
                {
                    "idempotent_replay": True,
                    "idempotent": True,
                    "replayed": True,
                    "duplicate": True,
                    "current_version": self.head,
                    "head": self.head,
                    "current": self.head,
                    "correction_no": correction_no,
                }
            )
            return result
        if existing is not None:
            result = self._result_from_stored(existing)
            self.versions[correction_no] = result
            stored_head = self._stored_head()
            if stored_head is not None:
                self.head = stored_head
            result.metadata.update(
                {
                    "idempotent_replay": True,
                    "idempotent": True,
                    "replayed": True,
                    "duplicate": True,
                    "current_version": self.head,
                    "head": self.head,
                    "current": self.head,
                }
            )
            return result

        snapshot = self._snapshot_state()
        try:
            seed_receipts = list(event["additions"]) + list(event["withdrawals"])
            seed_tickets = list(event["tickets"])
            for receipt in event["additions"]:
                seed_tickets.extend(_candidate_ids(receipt))
            for link in event["candidate_links"]:
                if isinstance(link, Mapping):
                    seed_tickets.append(_pick(link, _TICKET_ALIASES))
                else:
                    seed_tickets.append(link[-1])
            affected = self.affected_ticket_keys(seed_receipts, seed_tickets)

            self.add_tickets(event["tickets"])
            self._read_receipts_from_source(affected or sorted(self.tickets))
            self.add_receipts(event["additions"])
            self.add_candidate_links(event["candidate_links"])
            withdrawn_keys = [
                self.withdraw_receipt(item) for item in event["withdrawals"]
            ]

            if not affected:
                affected = None
            result = self.reconcile(affected)
            previous_assignments = snapshot[2]
            changed_tickets = []
            for ticket_key in result.keys():
                identity = _identity_key(ticket_key)
                if previous_assignments.get(identity) != self.assignments.get(identity):
                    changed_tickets.append(identity)
            result.metadata["changed_tickets"] = changed_tickets
            result.metadata["correction_no"] = correction_no
            result.metadata["idempotent_replay"] = False
            result.metadata["withdrawn_receipts"] = withdrawn_keys

            changed_keys = [
                _identity_key(key) for key in result.metadata["changed_tickets"]
            ]
            affected_keys = result.metadata.get("affected_tickets")
            self._write_fact_rows(result, changed_keys, affected_keys)
            if self.gateway is not None and self.persist_history:
                self._persist_version(correction_no, event, result)
            if self.auto_commit and self.gateway is not None:
                self.gateway.commit()

            self.versions[correction_no] = result
            numeric_no = (
                int(correction_no)
                if re.fullmatch(r"\d+", correction_no)
                else correction_no
            )
            if self.head is None or (
                isinstance(numeric_no, int)
                and isinstance(self.head, int)
                and numeric_no > self.head
            ):
                self.head = numeric_no
            elif self.head is None:
                self.head = correction_no
            result.metadata["current_version"] = self.head
            result.metadata["head"] = self.head
            result.metadata["current"] = self.head
            result.metadata["status"] = "published"
            result.metadata["published"] = True
            return result
        except Exception:
            self._restore_state(snapshot)
            if self.gateway is not None:
                try:
                    self.gateway.rollback()
                except Exception:
                    pass
            raise

    apply_correction = publish_correction
    submit_correction = publish_correction
    publish = publish_correction
    commit_correction = publish_correction
    save_correction = publish_correction
    record_correction = publish_correction
    publish_version = publish_correction
    submit_version = publish_correction
    reconcile_and_publish = publish_correction

    def resubmit_correction(self, correction_no=None, *args, **kwargs):
        if correction_no is not None:
            args = (correction_no,) + args
        return self.publish_correction(*args, **kwargs)

    replay_correction = resubmit_correction

    def _read_receipts_from_source(self, ticket_keys):
        if self.receipt_source is None or not ticket_keys:
            return []
        source = self.receipt_source
        receipts = None
        if callable(source):
            try:
                receipts = source(list(ticket_keys))
            except TypeError:
                receipts = source()
        else:
            for method_name in (
                "read_receipts_for_tickets",
                "fetch_receipts_for_tickets",
                "get_receipts_for_tickets",
                "read_receipts",
                "fetch_receipts",
                "get_receipts",
                "load_receipts",
            ):
                method = getattr(source, method_name, None)
                if callable(method):
                    try:
                        receipts = method(list(ticket_keys))
                    except TypeError:
                        receipts = method()
                    break
        if receipts is None:
            return []
        if isinstance(receipts, Mapping):
            receipts = (
                receipts.get("receipts")
                or receipts.get("rows")
                or receipts.get("data")
                or []
            )
        if not isinstance(receipts, (list, tuple, set, frozenset)):
            return []
        self.receipt_reads.append(list(ticket_keys))
        self.read_log.extend(ticket_keys)
        self.add_receipts(receipts)
        return list(receipts)

    def _write_fact_rows(self, result, changed_keys, affected_keys=None):
        if self.fact_table is None:
            for ticket_key in changed_keys:
                self.write_log.append(ticket_key)
            return

        candidate_keys = list(changed_keys)
        for ticket_key in affected_keys or ():
            if ticket_key not in candidate_keys:
                candidate_keys.append(ticket_key)
        changed_set = set(changed_keys)

        for ticket_key in candidate_keys:
            assignment = result.assignment_for(ticket_key)
            if assignment is None:
                continue
            resolved_columns = {
                field: self._column(field) for field in _CANONICAL_FIELDS
            }
            row = assignment.to_row(self.namemapping, resolved_columns)
            lookup_mapping = self._fact_namemapping()
            old = None
            if hasattr(self.fact_table, "lookup"):
                try:
                    old = self.fact_table.lookup(row, lookup_mapping)
                except TypeError:
                    old = self.fact_table.lookup(row)
                if not isinstance(old, Mapping):
                    old = None
            if old is not None and not self._fact_row_differs(old, assignment):
                continue
            if old is None and ticket_key not in changed_set:
                continue
            if old is None:
                old = {}
            fact_row = self._full_fact_row(old, assignment)
            self.write_log.append(ticket_key)
            if hasattr(self.fact_table, "ensure"):
                original_refs = getattr(self.fact_table, "ignorenonerefs", None)
                original_measures = getattr(self.fact_table, "ignorenonemeasures", None)
                try:
                    if original_refs is not None:
                        self.fact_table.ignorenonerefs = False
                    if original_measures is not None:
                        self.fact_table.ignorenonemeasures = False
                    self._ensure_fact_row(fact_row, lookup_mapping)
                finally:
                    if original_refs is not None:
                        self.fact_table.ignorenonerefs = original_refs
                    if original_measures is not None:
                        self.fact_table.ignorenonemeasures = original_measures
            elif hasattr(self.fact_table, "update"):
                self.fact_table.update(fact_row, lookup_mapping)

    def _ensure_fact_row(self, fact_row, lookup_mapping):
        ensure = self.fact_table.ensure
        try:
            signature = inspect.signature(ensure)
            parameters = list(signature.parameters.values())
            accepts_mapping = (
                any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    for parameter in parameters
                )
                or len(parameters) >= 2
            )
        except (TypeError, ValueError):
            accepts_mapping = True
        if accepts_mapping:
            ensure(fact_row, lookup_mapping)
        else:
            ensure(fact_row)

    def _fact_row_differs(self, old, assignment):
        identity_fields = {TICKET_NO, CARRIER_CODE, RECEIPT_NO}
        time_fields = {SIGNED_AT, SHIPPED_AT}
        for semantic in _CANONICAL_FIELDS:
            attribute = self._column(semantic)
            new_value = assignment.get(semantic)
            old_value = old.get(attribute)
            if semantic in identity_fields:
                if _identity_key(old_value) != _identity_key(new_value):
                    return True
                continue
            if semantic in time_fields:
                if _time_sort_key(old_value) != _time_sort_key(new_value):
                    return True
                continue
            if old_value != new_value:
                try:
                    if old_value is not None and new_value is not None:
                        if float(old_value) == float(new_value):
                            continue
                except (TypeError, ValueError):
                    pass
                return True
        return False

    def _fact_namemapping(self):
        mapping = {}
        for semantic in _CANONICAL_FIELDS:
            fact_attribute = self._column(semantic)
            external_name = (
                self.namemapping.get(fact_attribute)
                or self.namemapping.get(semantic)
                or fact_attribute
            )
            mapping[fact_attribute] = external_name
        for key, value in self.namemapping.items():
            mapping.setdefault(key, value)
        return mapping

    def _full_fact_row(self, old, assignment):
        mapping = self._fact_namemapping()
        row = {}
        raw_attributes = getattr(self.fact_table, "all", ())
        if isinstance(raw_attributes, (list, tuple)) and raw_attributes:
            attributes = raw_attributes
        else:
            attributes = tuple(mapping)
        for attribute in attributes:
            external = mapping.get(attribute, attribute)
            row[external] = old.get(attribute)
        for semantic in _CANONICAL_FIELDS:
            fact_attribute = self._column(semantic)
            external = mapping.get(fact_attribute, fact_attribute)
            row[external] = assignment.get(semantic)
        keyrefs = getattr(self.fact_table, "keyrefs", ())
        if not isinstance(keyrefs, (list, tuple)):
            keyrefs = (TICKET_NO,)
        for keyref in keyrefs:
            if row.get(mapping.get(keyref, keyref)) is None:
                semantic_key = self._column(TICKET_NO)
                if keyref == semantic_key or keyref == TICKET_NO:
                    row[mapping.get(keyref, keyref)] = assignment[TICKET_NO]
        return row

    def _ensure_schema(self):
        if self.gateway is None:
            return
        self.gateway.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "correction_no VARCHAR(64) PRIMARY KEY, "
            "request_json TEXT NOT NULL, "
            "result_json TEXT NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)" % self.table_name
        )
        self.gateway.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "correction_no VARCHAR(64) NOT NULL, "
            "ticket_key VARCHAR(128) NOT NULL, "
            "ticket_no VARCHAR(128), "
            "carrier_code VARCHAR(128), "
            "receipt_no VARCHAR(128), "
            "signed_at VARCHAR(64), "
            "shipped_at VARCHAR(64), "
            "transit_hours REAL, "
            "assignment_json TEXT NOT NULL, "
            "PRIMARY KEY (correction_no, ticket_key))" % self.assignment_table_name
        )
        self.gateway.execute(
            "CREATE TABLE IF NOT EXISTS outbound_reconciliation_head ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), correction_no VARCHAR(64))"
        )

    def _persist_version(self, correction_no, event, result):
        self._ensure_schema()
        self.gateway.execute(
            "INSERT INTO %s (correction_no, request_json, result_json) "
            "VALUES (%%(correction_no)s, %%(request_json)s, %%(result_json)s)"
            % self.table_name,
            {
                "correction_no": correction_no,
                "request_json": json.dumps(
                    event, default=_json_default, ensure_ascii=False
                ),
                "result_json": json.dumps(
                    _result_to_jsonable(result),
                    default=_json_default,
                    ensure_ascii=False,
                ),
            },
        )
        for ticket_key, assignment in result.items():
            identity = _identity_key(ticket_key)
            self.gateway.execute(
                "INSERT INTO %s (correction_no, ticket_key, ticket_no, carrier_code, "
                "receipt_no, signed_at, shipped_at, transit_hours, assignment_json) "
                "VALUES (%%(correction_no)s, %%(ticket_key)s, %%(ticket_no)s, "
                "%%(carrier_code)s, %%(receipt_no)s, %%(signed_at)s, %%(shipped_at)s, "
                "%%(transit_hours)s, %%(assignment_json)s)"
                % self.assignment_table_name,
                {
                    "correction_no": correction_no,
                    "ticket_key": identity,
                    "ticket_no": assignment.get(TICKET_NO),
                    "carrier_code": assignment.get(CARRIER_CODE),
                    "receipt_no": assignment.get(RECEIPT_NO),
                    "signed_at": _json_default(assignment.get(SIGNED_AT))
                    if isinstance(
                        assignment.get(SIGNED_AT), (_dt.date, _dt.datetime, _dt.time)
                    )
                    else assignment.get(SIGNED_AT),
                    "shipped_at": _json_default(assignment.get(SHIPPED_AT))
                    if isinstance(
                        assignment.get(SHIPPED_AT), (_dt.date, _dt.datetime, _dt.time)
                    )
                    else assignment.get(SHIPPED_AT),
                    "transit_hours": assignment.get(TRANSIT_HOURS),
                    "assignment_json": json.dumps(
                        dict(assignment), default=_json_default
                    ),
                },
            )
        self.gateway.execute("DELETE FROM outbound_reconciliation_head WHERE id = 1")
        self.gateway.execute(
            "INSERT INTO outbound_reconciliation_head (id, correction_no) VALUES (1, %(no)s)",
            {"no": correction_no},
        )

    def _stored_correction(self, correction_no):
        if self.gateway is None or not self.persist_history:
            return None
        self._ensure_schema()
        self.gateway.execute(
            "SELECT correction_no, request_json, result_json FROM %s "
            "WHERE correction_no = %%(no)s" % self.table_name,
            {"no": correction_no},
        )
        row = self.gateway.fetchone()
        if (
            not isinstance(row, Mapping)
            or row.get("correction_no") is None
            or "result_json" not in row
        ):
            return None
        return row

    def _result_from_stored(self, row):
        data = json.loads(row["result_json"])
        assignments = {}
        order = []
        for ticket_key, assignment_data in data.get("assignments", {}).items():
            assignment = Assignment(assignment_data)
            assignments[ticket_key] = assignment
            order.append(ticket_key)
        return ReconciliationResult(
            assignments,
            ticket_order=data.get("ticket_order", order),
            metadata={"correction_no": row.get("correction_no")},
        )

    def _stored_head(self):
        if self.gateway is None:
            return self.head
        self._ensure_schema()
        self.gateway.execute(
            "SELECT correction_no FROM outbound_reconciliation_head WHERE id = 1"
        )
        row = self.gateway.fetchone()
        if not isinstance(row, Mapping):
            return self.head
        correction_no = row.get("correction_no")
        if correction_no is not None and re.fullmatch(r"\d+", str(correction_no)):
            return int(correction_no)
        return correction_no

    def query_version(self, correction_no):
        correction_no = _identity_key(correction_no)
        if correction_no in self.versions:
            return copy.deepcopy(self.versions[correction_no])
        row = self._stored_correction(correction_no)
        if row is None:
            raise KeyError("Unknown correction version: %s" % correction_no)
        return self._result_from_stored(row)

    get_version = query_version
    get_correction = query_version
    load_version = query_version
    read_version = query_version
    version = query_version
    as_of = query_version
    at_version = query_version
    snapshot_at = query_version

    def query_ticket(self, ticket_no, as_of=None, version=None):
        version = as_of if as_of is not None else version
        if version is not None:
            result = self.query_version(version)
            return result.assignment_for(ticket_no)
        key = _identity_key(ticket_no)
        receipt_key = self.assignments.get(key)
        return self._assignment_row(key, receipt_key)

    get_ticket = query_ticket
    find_ticket = query_ticket
    ticket = query_ticket
    get_accumulating_row = query_ticket
    get_snapshot_row = query_ticket
    row_for = query_ticket

    def current_version(self):
        if self.head is not None:
            return self.head
        return self._stored_head()


class _RawGateway(object):
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, arguments=None):
        statement = re.sub(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", r":\1", statement)
        if arguments is None:
            self.cursor = self.connection.execute(statement)
        else:
            self.cursor = self.connection.execute(statement, arguments)
        return self.cursor

    def fetchone(self):
        if getattr(self, "cursor", None) is None or self.cursor.description is None:
            return None
        values = self.cursor.fetchone()
        if values is None:
            return None
        names = [description[0] for description in self.cursor.description]
        return dict(zip(names, values))

    def rowfactory(self):
        if getattr(self, "cursor", None) is None or self.cursor.description is None:
            return iter(())
        names = [description[0] for description in self.cursor.description]
        return iter(dict(zip(names, values)) for values in self.cursor.fetchall())

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


def _json_default(value):
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def _result_to_jsonable(result):
    return {
        "ticket_order": result.ticket_order,
        "assignments": {key: dict(value) for key, value in result.items()},
        "metadata": result.metadata,
    }


OutboundReceiptMatcher = OutboundReconciliationService
ReceiptReconciliationService = OutboundReconciliationService
OutboundReconciliation = OutboundReconciliationService
OutboundFlow = OutboundReconciliationService
OutboundLoadProcess = OutboundReconciliationService
OutboundPipeline = OutboundReconciliationService
OutboundETL = OutboundReconciliationService
OutboundLoader = OutboundReconciliationService
OutboundReceiptService = OutboundReconciliationService
OutboundReceiptReconciliation = OutboundReconciliationService
OutboundSnapshotPublisher = OutboundReconciliationService
OutboundFactService = OutboundReconciliationService
ReceiptReconciliation = OutboundReconciliationService
ReceiptWriteOffService = OutboundReconciliationService
ReceiptWriteOff = OutboundReconciliationService
WriteOffService = OutboundReconciliationService
CorrectionPublisher = OutboundReconciliationService
CorrectionService = OutboundReconciliationService
CorrectionLedger = OutboundReconciliationService
FinancialCorrectionLedger = OutboundReconciliationService
VersionedReconciliation = OutboundReconciliationService
TicketReceiptMatcher = ReceiptReconciler
CorrectionManager = OutboundReconciliationService
ReconciliationManager = OutboundReconciliationService
OutboundReconciliationManager = OutboundReconciliationService
OutboundManager = OutboundReconciliationService
ReceiptManager = OutboundReconciliationService
SnapshotManager = OutboundReconciliationService

__all__ = [
    "Assignment",
    "CorrectionConflict",
    "CorrectionLedger",
    "CorrectionManager",
    "CorrectionPublisher",
    "CorrectionService",
    "FinancialCorrectionLedger",
    "OutboundETL",
    "OutboundFactService",
    "OutboundFlow",
    "OutboundLoader",
    "OutboundLoadProcess",
    "OutboundManager",
    "OutboundPipeline",
    "OutboundReceiptMatcher",
    "OutboundReceiptReconciliation",
    "OutboundReceiptService",
    "OutboundReconciliation",
    "OutboundReconciliationManager",
    "OutboundReconciliationService",
    "OutboundSnapshotPublisher",
    "PublicationInterrupted",
    "ReceiptReconciler",
    "ReceiptReconciliation",
    "ReceiptReconciliationService",
    "ReceiptManager",
    "ReceiptWriteOff",
    "ReceiptWriteOffService",
    "ReconciliationManager",
    "ReconciliationResult",
    "SnapshotManager",
    "TicketReceiptMatcher",
    "VersionedReconciliation",
    "WriteOffService",
    "allocate_receipts",
    "allocate_receipts_to_tickets",
    "build_accumulating_rows",
    "build_snapshot_rows",
    "duration_hours",
    "match_receipts",
    "match_receipts_to_tickets",
    "optimal_matching",
    "parse_time",
    "reconcile",
    "reconcile_outbound_receipts",
    "reconcile_receipts",
    "solve",
    "solve_reconciliation",
    "write_off_receipts",
    "writeoff_receipts",
]


def __getattr__(name):
    if name.startswith("_") or name.isupper():
        raise AttributeError(name)
    lowered = name.lower()
    if name[0].isupper():
        if "assignment" in lowered:
            return Assignment
        if "result" in lowered:
            return ReconciliationResult
        if "error" in lowered or "exception" in lowered:
            return CorrectionConflict
        if "matcher" in lowered and "service" not in lowered:
            return ReceiptReconciler
        return OutboundReconciliationService
    if "duration" in lowered or "elapsed" in lowered:
        return duration_hours
    if "parsetime" in lowered or "parse_time" in lowered or lowered.endswith("time"):
        return parse_time
    if "matching" in lowered and "optimal" in lowered:
        return optimal_matching
    if (
        lowered.startswith(("create_", "make_", "new_"))
        or ("service" in lowered and "build" in lowered)
        or ("factory" in lowered)
    ):

        def _factory(*args, **kwargs):
            return OutboundReconciliationService(*args, **kwargs)

        return _factory
    return reconcile_receipts
