"""Plan upstream packet merges without changing warehouse results.

The mental model
================

Packets arrive at a warehouse in *original order*.  The warehouse historically
computed::

    state = blueprint
    for packet in packets:
        state = deepmerge(state, packet)

A transit station may merge a run of adjacent packets into one package before
forwarding it.  The warehouse then computes ``deepmerge(state, package)``.
Because :mod:`deepmerge` strategies are not associative in general -- a field
may change type from mapping to list and back, for example -- a perfectly
reasonable upstream merge can change the final state.

This module answers, for a concrete work order:

* which adjacent runs are *safe* to merge (same value, same array order and
  same original packet for every retained field as the serial work order);
* which packages to send, in which order, at each of two station levels;
* how to stay non-anticipatory when one of two blueprint versions is published
  only after some packets have already been sent;
* the smallest worst-case number of sends, with ties broken by dispatch permit.

Safety is decided by *execution*, not by a syntactic rule: the package is
actually merged into the true serial prefix and compared with the true serial
suffix, including per-node origin ids.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Iterable

from .trace import MergePolicy, Node, TrackedMerger

_MISSING = object()
UNSET = _MISSING
_INF = 10**18


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Packet:
    """One original packet.

    ``origin`` is the true original packet number that must survive into the
    warehouse result.  ``receiver`` is the level-1 station that physically
    received the packet.  ``window`` is the inclusive ``[earliest, latest]``
    staging window in which the packet may be held or sent.  ``permit`` is the
    dispatch-permit rank from the work order and is used only to break ties
    between equally cheap plans.
    """

    origin: str
    receiver: str
    window: tuple[int, int]
    payload: Any
    permit: int = 0

    def __post_init__(self) -> None:
        if len(self.window) != 2:
            raise ValueError("window must be a (earliest, latest) pair")
        if self.window[0] > self.window[1]:
            raise ValueError(f"packet {self.origin!r} has an empty staging window")


@dataclass(frozen=True)
class Station:
    """A station that may merge packets it genuinely received."""

    name: str
    level: int

    def __post_init__(self) -> None:
        if self.level not in (1, 2):
            raise ValueError("station level must be 1 or 2")


@dataclass(frozen=True)
class ReadEvent:
    """A receiver read that happened after ``after`` packets arrived.

    Such a read pins a package boundary: a merged package may end at the read
    but may not swallow it.  If ``expected`` is given, the serial state at that
    point must expose it.  ``expected_origin`` additionally pins the original
    packet that supplied the value.
    """

    after: int
    path: tuple[Any, ...] = ()
    expected: Any = _MISSING
    expected_origin: str | None = None
    reader: str = ""


@dataclass(frozen=True)
class Difference:
    """One observed difference between the plan and the serial work order."""

    path: tuple[Any, ...]
    kind: str
    serial: Any = None
    planned: Any = None
    serial_origins: tuple[str, ...] = ()
    planned_origins: tuple[str, ...] = ()

    def describe(self) -> str:
        where = "/".join(str(part) for part in self.path) or "<root>"
        if self.kind == "extra":
            return (
                f"{where}: 仓端会多看见 {self.planned!r}"
                f"（原件 {_origins(self.planned_origins)}）"
            )
        if self.kind == "missing":
            return (
                f"{where}: 仓端会少看见 {self.serial!r}"
                f"（原件 {_origins(self.serial_origins)}）"
            )
        if self.kind == "type":
            return (
                f"{where}: 类型由 {type(self.serial).__name__} 变为 "
                f"{type(self.planned).__name__}，值 {self.serial!r} -> {self.planned!r}"
            )
        if self.kind == "origin":
            return (
                f"{where}: 值仍是 {self.serial!r}，但原件号由 "
                f"{_origins(self.serial_origins)} 变为 {_origins(self.planned_origins)}"
            )
        if self.kind == "order":
            return f"{where}: 数组先后不同 {self.serial!r} -> {self.planned!r}"
        return f"{where}: {self.serial!r} -> {self.planned!r}"


def _origins(origins: Iterable[str]) -> str:
    values = tuple(origins)
    if not values:
        return "无"
    return "{" + ", ".join(values) + "}"


def _first_send(plan: "Plan") -> int | None:
    times = [
        package.send_at
        for package in plan.packages()
        if package.send_at is not None
    ]
    return min(times) if times else None


def _last_send(plan: "Plan") -> int:
    times = [
        package.send_at
        for package in plan.packages()
        if package.send_at is not None
    ]
    return max(times) if times else -_INF


@dataclass(frozen=True)
class IntervalReport:
    """Why an interval may or may not be packed as one upstream package.

    ``left_group`` is the packet group before the interval (the serial prefix
    state) and ``right_group`` is the packet group inside the interval.  The
    per-path ``differences`` say what the warehouse would see extra or miss.
    """

    scenario: str
    start: int
    end: int
    valid: bool
    reason: str = ""
    differences: tuple[Difference, ...] = ()
    left_group: tuple[str, ...] = ()
    right_group: tuple[str, ...] = ()
    left_origins: tuple[str, ...] = ()
    right_origins: tuple[str, ...] = ()

    def describe(self) -> str:
        head = f"[{self.start}..{self.end}] ({self.scenario})"
        if self.valid:
            return f"{head}: 可以合并"
        lines = [f"{head}: 不能合并 — {self.reason}"]
        lines.append(f"  前缀一组原件: {_origins(self.left_group)}")
        lines.append(f"  合并一组原件: {_origins(self.right_group)}")
        for difference in self.differences:
            lines.append("  " + difference.describe())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    """One dispatch: a contiguous run ``start..end`` sent from ``station``."""

    start: int
    end: int
    level: int
    station: str
    window: tuple[int, int]
    payload: Any
    origins: tuple[str, ...]
    permit: int
    send_at: int | None = None
    node: Node | None = None

    @property
    def size(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class StagePlan:
    level: int
    station: str
    packages: tuple[Package, ...]


@dataclass(frozen=True)
class Plan:
    """An executable plan.  ``sends`` counts every dispatch at both levels."""

    scenario: str
    stages: tuple[StagePlan, ...]
    sends: int
    start: int = 1
    end: int = 0

    def packages(self, level: int | None = None) -> tuple[Package, ...]:
        return tuple(
            package
            for stage in self.stages
            if level is None or stage.level == level
            for package in stage.packages
        )

    def dispatch_order(self) -> tuple[Package, ...]:
        """Final (level-2) packages in the order the warehouse receives them."""
        return self.packages(level=2)

    def permit_key(self) -> tuple[Any, ...]:
        """Dispatch-permit ordering used to break equal-cost ties.

        Plans with the same send count are compared by the dispatch-permit
        rank of each package in the order the warehouse receives them; a lower
        permit wins.  Position only breaks remaining ties.
        """
        final = tuple(
            (p.permit, p.start, p.end) for p in self.dispatch_order()
        )
        first = tuple((p.permit, p.start, p.end) for p in self.packages(level=1))
        return final + first

    def describe(self, compressed: bool = False) -> str:
        lines = [
            f"计划 {self.scenario}: 发送 {self.sends} 次 "
            f"(一级 {len(self.packages(1))} 次, 二级 {len(self.packages(2))} 次)"
        ]
        for stage in self.stages:
            lines.append(f"  站 {stage.station} (第{stage.level}级):")
            if compressed:
                for run in chapter_runs(stage.packages):
                    lines.append("    " + run.describe())
            else:
                for package in stage.packages:
                    send = "" if package.send_at is None else f" @{package.send_at}"
                    lines.append(
                        f"    第{package.start}–{package.end}包 -> 第{stage.level + 1}站"
                        f"{send} 原件{_origins(package.origins)}"
                    )
        return "\n".join(lines)


@dataclass(frozen=True)
class PackageRun:
    """A short-form, run-length representation of consecutive packages."""

    start: int
    end: int
    count: int
    group_size: int
    level: int
    station: str
    repeated_value: bool = False

    def describe(self) -> str:
        if self.start == self.end:
            span = f"第{self.start}包"
        else:
            span = f"第{self.start}–{self.end}包"
        repeat = "，同式重复" if self.repeated_value else ""
        return f"{span}: {self.count}次，每包{self.group_size}件{repeat}"


def chapter_runs(packages: Sequence[Package]) -> list[PackageRun]:
    """Compress consecutive packages into chapter/index runs.

    This is a *run-length* view of the already chosen plan.  It never stores one
    entry per alternative grouping, so a long shift stays linear in the number
    of sends.
    """
    runs: list[PackageRun] = []
    previous: Package | None = None
    for package in packages:
        if (
            runs
            and previous is not None
            and runs[-1].level == package.level
            and runs[-1].station == package.station
            and package.start == runs[-1].end + 1
            and runs[-1].group_size == package.size
        ):
            last = runs[-1]
            repeated = previous is not None and canonical_value(
                previous.payload
            ) == canonical_value(package.payload)
            runs[-1] = PackageRun(
                last.start,
                package.end,
                last.count + 1,
                last.group_size,
                last.level,
                last.station,
                repeated,
            )
        else:
            runs.append(
                PackageRun(
                    package.start,
                    package.end,
                    1,
                    package.size,
                    package.level,
                    package.station,
                    False,
                )
            )
        previous = package
    return runs


def describe_chapters(runs: Sequence[PackageRun]) -> str:
    """Render runs as numbered chapters without enumerating alternatives."""
    lines = []
    for index, run in enumerate(runs, start=1):
        lines.append(f"  第{index}章 {run.describe()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Value comparison helpers
# ---------------------------------------------------------------------------


def canonical_value(value: Any) -> Any:
    """A type-sensitive, order-sensitive canonical form used for comparison."""
    if isinstance(value, dict):
        items = sorted(
            ((canonical_value(k), canonical_value(v)) for k, v in value.items()),
            key=repr,
        )
        return ("dict", tuple(items))
    if isinstance(value, list):
        return ("list", tuple(canonical_value(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(canonical_value(item) for item in value))
    if isinstance(value, set):
        return ("set", tuple(sorted((canonical_value(item) for item in value), key=repr)))
    if isinstance(value, frozenset):
        return ("frozenset", tuple(sorted((canonical_value(item) for item in value), key=repr)))
    return ("scalar", type(value).__name__, repr(value))


def _diff_values(serial: Any, planned: Any, path: tuple[Any, ...] = ()) -> list[Difference]:
    differences: list[Difference] = []
    if type(serial) is not type(planned):
        differences.append(Difference(path, "type", serial, planned))
        if isinstance(serial, dict) and isinstance(planned, (list, set, tuple)):
            for key, value in serial.items():
                if not isinstance(planned, dict):
                    differences.append(Difference(path + (key,), "missing", value, None))
        if isinstance(planned, dict) and isinstance(serial, (list, set, tuple)):
            for key, value in planned.items():
                differences.append(Difference(path + (key,), "extra", None, value))
        return differences
    if isinstance(serial, dict):
        for key in serial:
            if key not in planned:
                differences.append(Difference(path + (key,), "missing", serial[key], None))
        for key in planned:
            if key not in serial:
                differences.append(Difference(path + (key,), "extra", None, planned[key]))
        for key in serial:
            if key in planned:
                differences.extend(_diff_values(serial[key], planned[key], path + (key,)))
        return differences
    if isinstance(serial, list):
        if len(serial) != len(planned):
            serial_counts = _count_values(serial)
            planned_counts = _count_values(planned)
            for value, count in serial_counts.items():
                extra_in_serial = count - planned_counts.get(value, 0)
                if extra_in_serial > 0:
                    differences.append(
                        Difference(
                            path,
                            "missing",
                            _representative(serial, value),
                            None,
                        )
                    )
            for value, count in planned_counts.items():
                extra_in_planned = count - serial_counts.get(value, 0)
                if extra_in_planned > 0:
                    differences.append(
                        Difference(
                            path,
                            "extra",
                            None,
                            _representative(planned, value),
                        )
                    )
            return differences
        element_differences: list[Difference] = []
        for index, (left, right) in enumerate(zip(serial, planned)):
            element_differences.extend(_diff_values(left, right, path + (index,)))
        if element_differences:
            if _count_values(serial) == _count_values(planned):
                differences.append(Difference(path, "order", serial, planned))
            else:
                differences.extend(element_differences)
        return differences
    if isinstance(serial, (set, frozenset)):
        missing = serial - planned
        extra = planned - serial
        for item in sorted(missing, key=repr):
            differences.append(Difference(path, "missing", item, None))
        for item in sorted(extra, key=repr):
            differences.append(Difference(path, "extra", None, item))
        return differences
    if canonical_value(serial) != canonical_value(planned):
        differences.append(Difference(path, "value", serial, planned))
    return differences


def _count_values(values: Sequence[Any]) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for value in values:
        key = canonical_value(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _representative(values: Sequence[Any], key: Any) -> Any:
    for value in values:
        if canonical_value(value) == key:
            return value
    return key


def _origin_at(node: Node, path: Sequence[Any]) -> tuple[str, ...]:
    current = node
    for part in path:
        if current.kind == "dict":
            if part not in current.children:
                return ()
            current = current.children[part]
        elif current.kind in ("list", "set") and isinstance(part, int):
            if not 0 <= part < len(current.children):
                return ()
            current = current.children[part]
        else:
            return ()
    return current.sources


def _diff_nodes(
    serial: Node, planned: Node, selected_fields: Sequence[Sequence[Any]] = ()
) -> list[Difference]:
    raw = _diff_values(serial.to_value(), planned.to_value())
    differences: list[Difference] = []
    for difference in raw:
        differences.append(
            Difference(
                difference.path,
                difference.kind,
                difference.serial,
                difference.planned,
                _origin_at(serial, difference.path),
                _origin_at(planned, difference.path),
            )
        )
    if differences:
        return differences
    # Values and order match; now require the true original packet per node.
    serial_origins = serial.origin_map()
    planned_origins = planned.origin_map()
    for path, origins in serial_origins.items():
        if origins != planned_origins.get(path):
            differences.append(
                Difference(
                    tuple(path),
                    "origin",
                    _value_at(serial, path),
                    _value_at(planned, path),
                    origins,
                    planned_origins.get(path, ()),
                )
            )
    for path, origins in planned_origins.items():
        if path not in serial_origins:
            differences.append(
                Difference(
                    tuple(path),
                    "origin",
                    None,
                    _value_at(planned, path),
                    (),
                    origins,
                )
            )
    differences.extend(_diff_element_origins(serial, planned))
    for path in selected_fields:
        serial_origins_at = serial.origins_under(path)
        planned_origins_at = planned.origins_under(path)
        if serial_origins_at != planned_origins_at:
            differences.append(
                Difference(
                    tuple(path),
                    "origin",
                    _value_at(serial, path),
                    _value_at(planned, path),
                    serial_origins_at,
                    planned_origins_at,
                )
            )
    return differences


def _diff_element_origins(
    serial: Node, planned: Node, path: tuple[Any, ...] = ()
) -> list[Difference]:
    """Compare list-element origins position by position.

    Equal values supplied by different original packets can otherwise hide
    behind a value-only comparison, which is exactly the failure mode the work
    order forbids.
    """
    differences: list[Difference] = []
    if serial.kind == "dict" and planned.kind == "dict":
        for key in serial.children:
            if key in planned.children:
                differences.extend(
                    _diff_element_origins(
                        serial.children[key], planned.children[key], path + (key,)
                    )
                )
        return differences
    if serial.kind == "list" and planned.kind == "list":
        for index, (left, right) in enumerate(zip(serial.children, planned.children)):
            if left.sources != right.sources and left.kind == right.kind:
                differences.append(
                    Difference(
                        path + (index,),
                        "origin",
                        left.to_value(),
                        right.to_value(),
                        left.sources,
                        right.sources,
                    )
                )
            differences.extend(
                _diff_element_origins(left, right, path + (index,))
            )
        return differences
    return differences


def _value_at(node: Node, path: Sequence[Any]) -> Any:
    current = node
    for part in path:
        if current.kind == "dict":
            current = current.children[part]
        else:
            current = current.children[part]
    return current.to_value()


# ---------------------------------------------------------------------------
# Work order and planner
# ---------------------------------------------------------------------------


class PlanningError(ValueError):
    """Raised when no plan satisfies the work order."""


@dataclass(frozen=True)
class _SendConflict(Exception):
    start: int
    end: int
    at: int
    high: int
    level: int
    station: str

    def __str__(self) -> str:
        return (
            f"第{self.start}–{self.end}包在第{self.level}级站{self.station} "
            f"最早只能在 {self.at} 发出，超过合法窗上限 {self.high}"
        )


class WorkOrder:
    """A concrete shift: packets, stations, blueprint versions and reads."""

    def __init__(
        self,
        packets: Sequence[Packet],
        blueprints: Mapping[str, Any],
        policy: MergePolicy | None = None,
        stations: Sequence[Station] | None = None,
        reads: Sequence[ReadEvent] = (),
        selected_fields: Sequence[Sequence[Any]] = (),
        reveal_after: int | None = None,
        initial_config: Mapping[str, Any] | None = None,
        hub: str = "hub",
    ) -> None:
        if not packets:
            raise ValueError("at least one packet is required")
        if not blueprints:
            raise ValueError("at least one blueprint version is required")
        origins = [packet.origin for packet in packets]
        if len(set(origins)) != len(origins):
            raise ValueError("packet origins must be unique")
        self.packets = list(packets)
        self.blueprints = dict(blueprints)
        self.policy = policy or MergePolicy.always()
        self.tracker = TrackedMerger(self.policy)
        self.reads = list(reads)
        self.selected_fields = tuple(tuple(path) for path in selected_fields)
        self.initial_config = dict(initial_config or {})
        self.hub = hub

        if stations is None:
            level1_names = sorted({packet.receiver for packet in packets})
            stations = [Station(name, 1) for name in level1_names] + [Station(hub, 2)]
        self.stations = list(stations)
        self._station_level = {station.name: station.level for station in self.stations}
        if hub not in self._station_level:
            raise ValueError(f"hub station {hub!r} is not declared")
        if self._station_level[hub] != 2:
            raise ValueError("the hub station must be level 2")
        for packet in packets:
            if self._station_level.get(packet.receiver) != 1:
                raise ValueError(
                    f"packet {packet.origin!r} is received by {packet.receiver!r}, "
                    "which is not a declared level-1 station"
                )
        if reveal_after is None:
            reveal_after = len(self.packets)
        if not 0 <= reveal_after <= len(self.packets):
            raise ValueError("reveal_after is outside the packet range")
        self.reveal_after = reveal_after
        self._serial_cache: dict[str, list[Node]] = {}
        self._interval_cache: dict[tuple[str, int, int], IntervalReport] = {}
        self._build_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self._validate_reads()

    # -- basic inspection -------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.packets)

    def window(self, start: int, end: int) -> tuple[int, int] | None:
        low = max(self.packets[index - 1].window[0] for index in range(start, end + 1))
        high = min(self.packets[index - 1].window[1] for index in range(start, end + 1))
        return (low, high) if low <= high else None

    def _validate_reads(self) -> None:
        last = 0
        for event in self.reads:
            if not 0 <= event.after <= self.size:
                raise ValueError("read position is outside the packet range")
            if event.after < last:
                raise ValueError("read positions must be non-decreasing")
            last = event.after
        for name in self.blueprints:
            states = self.serial(name)
            for event in self.reads:
                if event.expected is _MISSING and event.expected_origin is None:
                    continue
                node = states[event.after]
                if event.expected is not _MISSING:
                    actual = self._value_in_node(node, event.path)
                    if canonical_value(actual) != canonical_value(event.expected):
                        raise ValueError(
                            f"read at packet {event.after} for {event.path!r}: "
                            f"serial value {actual!r} != expected {event.expected!r}"
                        )
                if event.expected_origin is not None:
                    actual_origins = node.origins_under(event.path)
                    if event.expected_origin not in actual_origins:
                        raise ValueError(
                            f"read at packet {event.after} for {event.path!r}: "
                            f"origin {event.expected_origin!r} not in {actual_origins!r}"
                        )

    @staticmethod
    def _value_in_node(node: Node, path: Sequence[Any]) -> Any:
        try:
            return _value_at(node, path)
        except (KeyError, IndexError, TypeError):
            return _MISSING

    # -- serial baseline --------------------------------------------------
    def serial(self, scenario: str) -> list[Node]:
        if scenario not in self.blueprints:
            raise KeyError(f"unknown blueprint version {scenario!r}")
        if scenario not in self._serial_cache:
            configured = Node.from_value(
                self.blueprints[scenario], f"blueprint:{scenario}"
            )
            if self.initial_config:
                configured = self.tracker.merge(
                    configured,
                    Node.from_value(self.initial_config, "initial-config"),
                )
            state = configured
            states = [state]
            for packet in self.packets:
                state = self.tracker.merge(
                    state, Node.from_value(packet.payload, packet.origin)
                )
                states.append(state)
            self._serial_cache[scenario] = states
        return self._serial_cache[scenario]

    # -- package construction --------------------------------------------
    def _fold_packets(self, start: int, end: int) -> Node:
        node = Node.from_value(self.packets[start - 1].payload, self.packets[start - 1].origin)
        for index in range(start + 1, end + 1):
            packet = self.packets[index - 1]
            node = self.tracker.merge(node, Node.from_value(packet.payload, packet.origin))
        return node

    def _origin_order(self, start: int, end: int) -> tuple[str, ...]:
        return tuple(packet.origin for packet in self.packets[start - 1 : end])

    def _l1_blocks(self, start: int, end: int) -> list[tuple[int, int, str]]:
        blocks: list[tuple[int, int, str]] = []
        block_start = start
        receiver = self.packets[start - 1].receiver
        for index in range(start + 1, end + 1):
            current = self.packets[index - 1].receiver
            if current != receiver:
                blocks.append((block_start, index - 1, receiver))
                block_start = index
                receiver = current
        blocks.append((block_start, end, receiver))
        return blocks

    def l1_count(self, start: int, end: int) -> int:
        """Minimum level-1 sends for a final run: one per same-station block."""
        return len(self._l1_blocks(start, end))

    def _build_package(self, start: int, end: int) -> dict[str, Any]:
        cached = self._build_cache.get((start, end))
        if cached is not None:
            return cached
        window = self.window(start, end)
        if window is None:
            result: dict[str, Any] = {"window": None}
            self._build_cache[(start, end)] = result
            return result
        l1_packages: list[Package] = []
        for block_start, block_end, station in self._l1_blocks(start, end):
            node = self._fold_packets(block_start, block_end)
            l1_packages.append(
                Package(
                    start=block_start,
                    end=block_end,
                    level=1,
                    station=station,
                    window=self.window(block_start, block_end),
                    payload=node.to_value(),
                    origins=node.retained_origins(self._origin_order(block_start, block_end)),
                    permit=self.packets[block_start - 1].permit,
                    node=node,
                )
            )
        l2_node = self._fold_packages([package.node for package in l1_packages])
        result = {
            "window": window,
            "l1_packages": tuple(l1_packages),
            "l2_node": l2_node,
            "l2_package": Package(
                start=start,
                end=end,
                level=2,
                station=self.hub,
                window=window,
                payload=l2_node.to_value(),
                origins=l2_node.retained_origins(self._origin_order(start, end)),
                permit=self.packets[start - 1].permit,
                node=l2_node,
            ),
        }
        self._build_cache[(start, end)] = result
        return result

    def _fold_packages(self, nodes: Sequence[Node]) -> Node:
        result = nodes[0].clone()
        for node in nodes[1:]:
            result = self.tracker.merge(result, node.clone())
        return result

    # -- interval validation ---------------------------------------------
    def interval_report(self, scenario: str, start: int, end: int) -> IntervalReport:
        key = (scenario, start, end)
        if key in self._interval_cache:
            return self._interval_cache[key]
        if not (1 <= start <= end <= self.size):
            raise ValueError("interval is outside the packet range")
        if any(start <= event.after < end for event in self.reads):
            report = IntervalReport(
                scenario,
                start,
                end,
                False,
                "中间有接收方读取，必须在该次到达前形成边界",
                left_group=(self.packets[start - 1].origin,),
                right_group=tuple(packet.origin for packet in self.packets[start - 1 : end]),
            )
            self._interval_cache[key] = report
            return report
        built = self._build_package(start, end)
        if built["window"] is None:
            report = IntervalReport(
                scenario,
                start,
                end,
                False,
                "暂存窗没有交集，不能同包暂存",
                left_group=(),
                right_group=tuple(packet.origin for packet in self.packets[start - 1 : end]),
            )
            self._interval_cache[key] = report
            return report
        states = self.serial(scenario)
        planned = self.tracker.merge(states[start - 1].clone(), built["l2_node"].clone())
        differences = _diff_nodes(states[end], planned, self.selected_fields)
        left_group = states[start - 1].sources
        right_group = tuple(packet.origin for packet in self.packets[start - 1 : end])
        if differences:
            first = differences[0]
            report = IntervalReport(
                scenario,
                start,
                end,
                False,
                "合并结果与串行工单不同",
                tuple(differences),
                left_group,
                right_group,
                _origin_at(states[start - 1], first.path),
                _origin_at(built["l2_node"], first.path),
            )
        else:
            report = IntervalReport(
                scenario, start, end, True, left_group=left_group, right_group=right_group
            )
        self._interval_cache[key] = report
        return report

    def explain(self, scenario: str, start: int, end: int) -> IntervalReport:
        """Explain one candidate package, including a rejected one."""
        return self.interval_report(scenario, start, end)

    def safe_intervals(self, scenario: str) -> list[IntervalReport]:
        """All adjacent runs that can be packed without changing the serial result."""
        return [
            self.interval_report(scenario, start, end)
            for start in range(1, self.size + 1)
            for end in range(start, self.size + 1)
            if self.interval_report(scenario, start, end).valid
        ]

    def is_valid(self, interval: tuple[int, int], scenarios: Sequence[str]) -> bool:
        return all(
            self.interval_report(scenario, interval[0], interval[1]).valid
            for scenario in scenarios
        )

    # -- dynamic program --------------------------------------------------
    def _solve(
        self, scenarios: Sequence[str], start: int, end: int, forbidden: frozenset
    ) -> list[tuple[int, int]]:
        size = end - start + 1
        best_cost: list[int | None] = [None] * (size + 1)
        best_key: list[tuple[Any, ...] | None] = [None] * (size + 1)
        best_from = [0] * (size + 1)
        best_cost[0] = 0
        best_key[0] = ()
        for position in range(start, end + 1):
            j = position - start + 1
            for begin in range(start, position + 1):
                i = begin - start
                if best_cost[i] is None:
                    continue
                if (begin, position) in forbidden:
                    continue
                if not self.is_valid((begin, position), scenarios):
                    continue
                blocks = self._l1_blocks(begin, position)
                cost = best_cost[i] + 1 + len(blocks)
                component = (
                    self.packets[begin - 1].permit,
                    begin,
                    position,
                    tuple((self.packets[b[0] - 1].permit, b[0], b[1]) for b in blocks),
                )
                key = best_key[i] + (component,)
                if best_cost[j] is None or (cost, key) < (best_cost[j], best_key[j]):
                    best_cost[j] = cost
                    best_key[j] = key
                    best_from[j] = i
        if best_cost[size] is None:
            return []
        intervals: list[tuple[int, int]] = []
        position = size
        while position > 0:
            previous = best_from[position]
            intervals.append((start + previous, start + position - 1))
            position = previous
        intervals.reverse()
        return intervals

    def _schedule(
        self,
        l1_packages: Sequence[Package],
        l2_packages: Sequence[Package],
        interval_blocks: Mapping[tuple[int, int], Sequence[tuple[int, int]]],
        not_before: int = -_INF,
    ) -> tuple[list[Package], list[Package]]:
        station_previous: dict[str, int] = {}
        block_send: dict[tuple[int, int], int] = {}
        l1_scheduled: list[Package] = []
        for package in sorted(l1_packages, key=lambda p: (p.start, p.end)):
            low, high = package.window
            at = max(low, station_previous.get(package.station, not_before) + 1)
            if at > high:
                interval = next(
                    interval
                    for interval, blocks in interval_blocks.items()
                    if (package.start, package.end) in blocks
                )
                raise _SendConflict(interval[0], interval[1], at, high, 1, package.station)
            station_previous[package.station] = at
            block_send[(package.start, package.end)] = at
            l1_scheduled.append(
                Package(
                    package.start,
                    package.end,
                    package.level,
                    package.station,
                    package.window,
                    package.payload,
                    package.origins,
                    package.permit,
                    at,
                    package.node,
                )
            )
        previous = -_INF
        l2_scheduled: list[Package] = []
        for package in sorted(l2_packages, key=lambda p: (p.start, p.end)):
            blocks = interval_blocks[(package.start, package.end)]
            ready = max(block_send[block] for block in blocks)
            low, high = package.window
            at = max(low, previous + 1, ready + 1, not_before + 1)
            if at > high:
                raise _SendConflict(package.start, package.end, at, high, 2, package.station)
            previous = at
            l2_scheduled.append(
                Package(
                    package.start,
                    package.end,
                    package.level,
                    package.station,
                    package.window,
                    package.payload,
                    package.origins,
                    package.permit,
                    at,
                    package.node,
                )
            )
        return l1_scheduled, l2_scheduled

    def _plan_range(
        self,
        scenarios: Sequence[str],
        start: int,
        end: int,
        label: str,
        not_before: int = -_INF,
    ) -> Plan:
        if start > end:
            return Plan(label, (), 0, start, end)
        forbidden: frozenset[tuple[int, int]] = frozenset()
        while True:
            intervals = self._solve(scenarios, start, end, forbidden)
            if not intervals:
                raise PlanningError(
                    f"{label}: 无法在第{start}–{end}包"
                    "形成满足暂存窗与读取边界的计划"
                )
            l1_packages: list[Package] = []
            l2_packages: list[Package] = []
            interval_blocks: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {}
            for begin, finish in intervals:
                built = self._build_package(begin, finish)
                l1_packages.extend(built["l1_packages"])
                l2_packages.append(built["l2_package"])
                interval_blocks[(begin, finish)] = tuple(
                    (package.start, package.end) for package in built["l1_packages"]
                )
            try:
                l1_scheduled, l2_scheduled = self._schedule(
                    l1_packages, l2_packages, interval_blocks, not_before
                )
            except _SendConflict as conflict:
                interval = (conflict.start, conflict.end)
                if interval in forbidden:
                    raise PlanningError(f"{label}: {conflict}") from None
                forbidden = forbidden | {interval}
                continue
            by_station: dict[str, list[Package]] = {}
            for package in l1_scheduled:
                by_station.setdefault(package.station, []).append(package)
            stages = tuple(
                StagePlan(1, station, tuple(by_station[station]))
                for station in sorted(by_station)
            ) + (StagePlan(2, self.hub, tuple(l2_scheduled)),)
            return Plan(
                label,
                stages,
                len(l1_scheduled) + len(l2_scheduled),
                start,
                end,
            )

    # -- public plans -----------------------------------------------------
    def plan(self, scenario: str, start: int = 1, end: int | None = None) -> Plan:
        if end is None:
            end = self.size
        return self._plan_range([scenario], start, end, scenario)

    def package_each(self, scenario: str, start: int = 1, end: int | None = None) -> Plan:
        """The conservative baseline: one packet per send at both levels."""
        if end is None:
            end = self.size
        l1_packages: list[Package] = []
        l2_packages: list[Package] = []
        interval_blocks: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {}
        for index in range(start, end + 1):
            built = self._build_package(index, index)
            l1_packages.extend(built["l1_packages"])
            l2_packages.append(built["l2_package"])
            interval_blocks[(index, index)] = ((index, index),)
        l1_scheduled, l2_scheduled = self._schedule(l1_packages, l2_packages, interval_blocks)
        by_station: dict[str, list[Package]] = {}
        for package in l1_scheduled:
            by_station.setdefault(package.station, []).append(package)
        stages = tuple(
            StagePlan(1, station, tuple(by_station[station]))
            for station in sorted(by_station)
        ) + (StagePlan(2, self.hub, tuple(l2_scheduled)),)
        return Plan(
            f"{scenario}:single", stages, len(l1_scheduled) + len(l2_scheduled), start, end
        )

    def robust_plan(self, scenarios: Sequence[str] | None = None) -> "RobustPlan":
        """Plan the non-anticipatory batch and its continuations.

        The plan may not see the published blueprint before ``reveal_after``,
        so packets ``1..reveal_after`` must be planned with every candidate
        version.  Two details make the result genuinely optimal:

        * the pre-publication batch may stop *before* ``reveal_after`` if a
          packet's staging window has not opened yet;
        * among all such cutoffs it picks the one with the smallest worst-case
          total send count, breaking ties by dispatch permit.
        """
        names = tuple(scenarios) if scenarios is not None else tuple(self.blueprints)
        for name in names:
            if name not in self.blueprints:
                raise KeyError(f"unknown blueprint version {name!r}")
        if self.reveal_after == 0:
            branches = {
                name: self._plan_range([name], 1, self.size, name) for name in names
            }
            worst = max(plan.sends for plan in branches.values())
            empty = Plan("common", (), 0, 1, 0)
            return RobustPlan(0, names, empty, branches, worst)
        candidates: list[tuple[Any, RobustPlan]] = []
        for cutoff in range(self.reveal_after, 0, -1):
            try:
                common = self._plan_range(names, 1, cutoff, "common")
            except PlanningError:
                continue
            branches: dict[str, Plan] = {}
            feasible = True
            common_last = _last_send(common)
            for name in names:
                try:
                    branches[name] = self._plan_range(
                        [name], cutoff + 1, self.size, name, common_last
                    )
                except PlanningError:
                    feasible = False
                    break
            if not feasible:
                continue
            if any(
                _first_send(plan) is not None and _first_send(plan) <= common_last
                for plan in branches.values()
            ):
                continue
            worst = common.sends + max(
                (plan.sends for plan in branches.values()), default=0
            )
            key = (
                worst,
                common.permit_key(),
                tuple((name, branches[name].permit_key()) for name in names),
            )
            candidates.append((key, RobustPlan(cutoff, names, common, branches, worst)))
        if not candidates:
            raise PlanningError("无法在公布前形成两种版本都适用的公共批次")
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    # -- verification -----------------------------------------------------
    def verify(self, plan: Plan, scenario: str) -> list[str]:
        """Replay a plan as the warehouse would and compare every boundary."""
        if plan.end < plan.start:
            return []
        states = self.serial(scenario)
        state = states[plan.start - 1].clone()
        problems: list[str] = []
        expected_start = plan.start
        for package in plan.dispatch_order():
            if package.start != expected_start:
                problems.append(
                    f"计划缺包或乱序: 期望从第{expected_start}包开始，"
                    f"实际第{package.start}包"
                )
            state = self.tracker.merge(state, package.node.clone())
            differences = _diff_nodes(states[package.end], state, self.selected_fields)
            if differences:
                problems.append(
                    f"第{package.end}包到达后与串行不一致: "
                    + "; ".join(item.describe() for item in differences[:5])
                )
            expected_start = package.end + 1
        if expected_start != plan.end + 1:
            problems.append(f"计划未覆盖到第{plan.end}包")
        for event in self.reads:
            if not (plan.start <= event.after <= plan.end):
                continue
            if not any(package.end == event.after for package in plan.dispatch_order()):
                problems.append(f"第{event.after}包处的读取没有形成边界")
        return problems


@dataclass(frozen=True)
class RobustPlan:
    """A common pre-publication batch plus one continuation per version."""

    reveal_after: int
    scenarios: tuple[str, ...]
    common: Plan
    branches: Mapping[str, Plan]
    worst_case_sends: int

    def continuation(self, scenario: str) -> Plan:
        return self.branches[scenario]

    def total_sends(self, scenario: str) -> int:
        return self.common.sends + self.branches[scenario].sends

    def verify(self, scenario: str, order: WorkOrder) -> list[str]:
        problems = order.verify(self.common, scenario)
        branch = self.branches[scenario]
        problems.extend(order.verify(branch, scenario))
        if self.common.end + 1 != branch.start and branch.end >= branch.start:
            problems.append("公布后的续派没有从已发批次之后开始")
        return problems

    def describe(self, compressed: bool = False) -> str:
        lines = [
            f"公布点: 第{self.reveal_after}包之后；"
            f"公布前公共批次发送 {self.common.sends} 次；"
            f"整班最坏发送 {self.worst_case_sends} 次"
        ]
        lines.append("公布前公共批次:")
        lines.append(self.common.describe(compressed=compressed))
        for name in self.scenarios:
            lines.append(
                f"公布为 {name} 后: 追加 {self.branches[name].sends} 次，"
                f"整班 {self.total_sends(name)} 次"
            )
            lines.append(self.branches[name].describe(compressed=compressed))
        return "\n".join(lines)
