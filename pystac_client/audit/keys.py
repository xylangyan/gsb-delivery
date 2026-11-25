"""Sort keys and swept-key intervals for sealed-archive audits.

Catalog records are ordered by UTC observation time (newest first), then
collection id and item id (both ascending).  A sort key is therefore the
triple ``(datetime, collection, id)`` and every "fully swept" receipt claims
an inclusive interval of those triples.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)
MAX_DATETIME = datetime.max.replace(tzinfo=timezone.utc)


class SortKey(NamedTuple):
    """A point in the catalog sort order.

    ``datetime`` is always timezone-aware UTC.  The two sentinel values
    :data:`MIN_KEY` and :data:`MAX_KEY` bracket every real key.
    """

    dt: datetime
    collection: str
    id: str

    def is_sentinel(self) -> bool:
        return self is MIN_KEY or self is MAX_KEY

    def to_string(self) -> str:
        if self is MIN_KEY:
            return "min"
        if self is MAX_KEY:
            return "max"
        dt = self.dt.astimezone(timezone.utc)
        return f"{_format_datetime(dt)}|{self.collection}|{self.id}"


MIN_KEY = SortKey(MIN_DATETIME, "", "")
MAX_KEY = SortKey(MAX_DATETIME, "", "")


def _format_datetime(dt: datetime) -> str:
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_sort_key(value: Any) -> SortKey:
    """Parse a key from a string, mapping, or sentinel name."""
    if value is None:
        raise ValueError("cannot parse a missing sort key")
    if isinstance(value, SortKey):
        return value
    if isinstance(value, dict):
        name = value.get("key")
        if name in ("min", "max"):
            return MIN_KEY if name == "min" else MAX_KEY
        dt = parse_datetime(value["datetime"])
        return SortKey(dt, str(value["collection"]), str(value["id"]))
    text = str(value).strip()
    if text == "min":
        return MIN_KEY
    if text == "max":
        return MAX_KEY
    dt_text, collection, item_id = text.split("|", 2)
    return SortKey(parse_datetime(dt_text), collection, item_id)


def optional_sort_key(value: Any, *, default: SortKey) -> SortKey:
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return parse_sort_key(value)


def item_sort_key(feature: dict[str, Any]) -> SortKey:
    """Extract the sort key of a STAC Item feature dictionary."""
    properties = feature.get("properties") or {}
    dt_value = properties.get("datetime")
    if dt_value is None:
        dt_value = properties.get("start_datetime")
    if dt_value is None:
        raise ValueError(
            f"item {feature.get('id')!r} has neither datetime nor start_datetime"
        )
    collection = feature.get("collection") or properties.get("collection")
    if collection is None:
        raise ValueError(f"item {feature.get('id')!r} has no collection id")
    item_id = feature.get("id")
    if item_id is None:
        raise ValueError("item has no id")
    return SortKey(parse_datetime(dt_value), str(collection), str(item_id))


def natural_compare(left: SortKey, right: SortKey) -> int:
    """Compare keys in natural (ascending) order, sentinels included."""
    if left is right:
        return 0
    if left is MIN_KEY or right is MAX_KEY:
        return -1
    if left is MAX_KEY or right is MIN_KEY:
        return 1
    if left.dt != right.dt:
        return -1 if left.dt < right.dt else 1
    if left.collection != right.collection:
        return -1 if left.collection < right.collection else 1
    if left.id != right.id:
        return -1 if left.id < right.id else 1
    return 0


def sort_compare(left: SortKey, right: SortKey) -> int:
    """Compare keys in catalog order: time desc, collection asc, id asc."""
    if left is right:
        return 0
    if left is MIN_KEY or right is MAX_KEY:
        return 1
    if left is MAX_KEY or right is MIN_KEY:
        return -1
    if left.dt != right.dt:
        return 1 if left.dt > right.dt else -1
    if left.collection != right.collection:
        return -1 if left.collection < right.collection else 1
    if left.id != right.id:
        return -1 if left.id < right.id else 1
    return 0


@dataclass(frozen=True)
class KeyInterval:
    """Inclusive interval of sort keys, ordered naturally (low -> high)."""

    low: SortKey
    high: SortKey

    def __post_init__(self) -> None:
        if natural_compare(self.low, self.high) > 0:
            raise ValueError(
                f"key interval low {self.low} must not exceed high {self.high}"
            )

    @classmethod
    def from_sweep(cls, first: SortKey, last: SortKey) -> "KeyInterval":
        # A receipt names the newest (first) and oldest (last) swept key.
        return cls(last, first)

    @classmethod
    def parse(
        cls,
        first: Any,
        last: Any,
        *,
        default_first: SortKey = MAX_KEY,
        default_last: SortKey = MIN_KEY,
    ) -> "KeyInterval":
        high = optional_sort_key(first, default=default_first)
        low = optional_sort_key(last, default=default_last)
        return cls(low=low, high=high)

    @property
    def first(self) -> SortKey:
        return self.high

    @property
    def last(self) -> SortKey:
        return self.low

    def contains(self, key: SortKey) -> bool:
        return (
            natural_compare(self.low, key) <= 0
            and natural_compare(key, self.high) <= 0
        )

    def covers(self, other: "KeyInterval") -> bool:
        return (
            natural_compare(self.low, other.low) <= 0
            and natural_compare(self.high, other.high) >= 0
        )

    def overlaps(self, other: "KeyInterval") -> bool:
        return not (
            natural_compare(self.high, other.low) < 0
            or natural_compare(other.high, self.low) < 0
        )

    def intersection(self, other: "KeyInterval") -> "KeyInterval | None":
        low = self.low if natural_compare(self.low, other.low) >= 0 else other.low
        high = (
            self.high if natural_compare(self.high, other.high) <= 0 else other.high
        )
        if natural_compare(low, high) > 0:
            return None
        return KeyInterval(low, high)

    def __str__(self) -> str:
        return f"[{self.low.to_string()}, {self.high.to_string()}]"
