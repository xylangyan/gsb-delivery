#  Copyright 2018-Present The CloudEvents Authors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Migration tooling for feeding one HTTP-ingested CloudEvent to several Kafka
subscribers through mixed SDK generations.

The entry point :func:`check_http` (or :func:`check_event` for an already
parsed event) takes the original HTTP event, a :class:`Topology` describing the
SDK entry points / permitted modes / links / costs of every receiver, and the
set of subscribers that must be reached. One publish point emits a single
Kafka message in one mode; every enabled downstream connection receives that
same message. The engine enumerates the legal wirings and returns the cheapest
plans whose per-hop Kafka messages can be replayed with each receiver's
*original* SDK entry while satisfying that receiver's requirements.

When no plan exists, the result names every conflicting publisher handoff, the
wire mode in which it fails, and the business value lost at that handoff. An
invalid HTTP original is reported separately through
``CheckResult.original_error``.
"""

from dataclasses import dataclass
from itertools import product
from typing import Any, Final, Mapping

from cloudevents.core.base import BaseCloudEvent
from cloudevents.core.bindings import http as core_http
from cloudevents.core.bindings import kafka as core_kafka
from cloudevents.core.bindings.http import HTTPMessage
from cloudevents.core.formats.base import Format
from cloudevents.core.formats.json import JSONFormat
from cloudevents.v1 import kafka as compat_kafka

STRUCTURED: Final[str] = "structured"
BINARY: Final[str] = "binary"
_MODES: Final[tuple[str, str]] = (STRUCTURED, BINARY)

CORE: Final[str] = "core"
COMPAT: Final[str] = "compat"

PUBLISHER: Final[str] = "publisher"
RELAY: Final[str] = "relay"
SUBSCRIBER: Final[str] = "subscriber"


class HandoffError(Exception):
    """Raised internally when a hop cannot be executed with the real SDK entry."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True)
class HandoffConflict:
    """
    A requirement or SDK conversion that cannot be honoured on a publisher
    handoff in a given wire mode.

    :ivar handoff: Root ``"G -> X"`` edge leaving the publisher; every failure
        on the downstream chain is attributed to this single handoff because
        the publish point emits one shared message.
    :ivar stage: ``receive``, ``send`` or ``requirement`` at the failing node.
    :ivar detail: Human-readable description of the conflict.
    :ivar lost: Business values (extension type/value, body kind, ...) that do
        not survive the handoff.
    :ivar chain: Full node path from the publisher to the failing node.
    :ivar g_mode: The publisher wire mode in which this conflict appears.
    """

    handoff: str
    stage: str
    detail: str
    lost: tuple[str, ...] = ()
    chain: tuple[str, ...] = ()
    g_mode: str | None = None


@dataclass(frozen=True)
class Requirement:
    """
    Base class for subscriber-side checks evaluated against the event actually
    re-read through the subscriber's own SDK entry.
    """

    def check(
        self, received: BaseCloudEvent, original: BaseCloudEvent
    ) -> tuple[str, tuple[str, ...]] | None:
        """Return ``None`` when satisfied, else ``(detail, lost_values)``."""
        raise NotImplementedError


@dataclass(frozen=True)
class ExtensionRequirement(Requirement):
    """The extension must survive with the same type and value."""

    name: str

    def check(
        self, received: BaseCloudEvent, original: BaseCloudEvent
    ) -> tuple[str, tuple[str, ...]] | None:
        expected = original.get_extension(self.name)
        actual = received.get_extension(self.name)
        if type(actual) is not type(expected) or actual != expected:
            detail = (
                f"extension '{self.name}' expected {type(expected).__name__} "
                f"{expected!r}, got {type(actual).__name__} {actual!r}"
            )
            return detail, (f"{self.name}={expected!r} ({type(expected).__name__})",)
        return None


@dataclass(frozen=True)
class DataRequirement(Requirement):
    """The body must survive with the same kind (type) and content."""

    def check(
        self, received: BaseCloudEvent, original: BaseCloudEvent
    ) -> tuple[str, tuple[str, ...]] | None:
        expected = original.get_data()
        actual = received.get_data()
        if type(actual) is not type(expected) or actual != expected:
            detail = (
                f"data expected {type(expected).__name__} {expected!r}, "
                f"got {type(actual).__name__} {actual!r}"
            )
            return detail, (f"data={expected!r} ({type(expected).__name__})",)
        return None


@dataclass(frozen=True)
class DatacontenttypeRequirement(Requirement):
    """
    ``datacontenttype`` must be explicitly present on the re-read event.

    :ivar expected: When ``None`` the original event's value is required;
        otherwise the given literal value is required.
    """

    expected: str | None = None

    def check(
        self, received: BaseCloudEvent, original: BaseCloudEvent
    ) -> tuple[str, tuple[str, ...]] | None:
        wanted = self.expected or original.get_datacontenttype()
        actual = received.get_datacontenttype()
        if actual != wanted or actual is None:
            detail = f"datacontenttype expected explicit {wanted!r}, got {actual!r}"
            return detail, (f"datacontenttype={wanted!r}",)
        return None


@dataclass(frozen=True)
class Node:
    """
    A receiver described by the route manifest.

    :ivar name: Node identifier (e.g. ``"G"``).
    :ivar kind: One of :data:`PUBLISHER`, :data:`RELAY`, :data:`SUBSCRIBER`.
    :ivar sdk: SDK entry family, :data:`CORE` or :data:`COMPAT`.
    :ivar rx_modes: Kafka content modes this node may receive.
    :ivar tx_modes: Kafka content modes this node may emit.
    :ivar requirements: Checks run on events re-read by a subscriber node.
    """

    name: str
    kind: str
    sdk: str
    rx_modes: tuple[str, ...]
    tx_modes: tuple[str, ...]
    requirements: tuple[Requirement, ...] = ()


@dataclass(frozen=True)
class Link:
    """
    An enableable downstream connection.

    :ivar source: Publishing node name.
    :ivar target: Receiving node name.
    :ivar cost: Cost added when this link is part of the selected wiring.
    :ivar modes: Content modes allowed on the wire; both modes by default.
    :ivar enabled: Whether the link is currently available.
    """

    source: str
    target: str
    cost: int = 0
    modes: tuple[str, ...] = _MODES
    enabled: bool = True


@dataclass(frozen=True)
class Topology:
    """A manifest of nodes and the links between them."""

    nodes: Mapping[str, Node]
    links: tuple[Link, ...]

    def outgoing(self, name: str) -> tuple[Link, ...]:
        return tuple(
            link for link in self.links if link.source == name and link.enabled
        )

    def link(self, source: str, target: str) -> Link | None:
        for link in self.links:
            if link.source == source and link.target == target:
                return link
        return None


@dataclass(frozen=True)
class HopRecord:
    """
    One selected hop: the exact Kafka message put on the wire and the original
    SDK entry the receiver must use to re-read it.
    """

    source: str
    target: str
    wire_mode: str
    receiver_sdk: str
    receiver_mode: str
    message: core_kafka.KafkaMessage


@dataclass(frozen=True)
class Plan:
    """A feasible wiring with every per-hop message needed to replay it."""

    cost: int
    g_mode: str
    g_message: core_kafka.KafkaMessage
    relay_modes: Mapping[str, tuple[str, str]]
    routes: Mapping[str, tuple[str, ...]]
    hops: tuple[HopRecord, ...]
    received: Mapping[str, BaseCloudEvent]

    def replay(
        self, hop: HopRecord, event_format: Format | None = None
    ) -> BaseCloudEvent:
        """Re-read one selected hop's wire message with the receiver's entry."""
        return replay_hop(hop, event_format)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a pre-publish check."""

    feasible: bool
    plans: tuple[Plan, ...]
    conflicts: tuple[HandoffConflict, ...]
    original_error: str | None = None
    required: tuple[str, ...] = ()

    @property
    def best_plan(self) -> Plan | None:
        """The cheapest feasible plan (ties resolved deterministically)."""
        return self.plans[0] if self.plans else None


def build_default_topology() -> Topology:
    """
    Build the manifest used by the quality-inspection / history-store migration.

    Nodes:
        * ``G`` -- core ingress publisher (``cloudevents.core.bindings.kafka``).
        * ``A`` -- quality inspection, core subscriber requiring the ``lane``
          extension type/value and the original body.
        * ``L`` -- ``cloudevents.v1.kafka`` compatibility relay.
        * ``P`` -- spare core relay (structured in, binary out).
        * ``B`` -- history store, core subscriber requiring the original body
          and an explicit ``application/json`` datacontenttype.
    """
    nodes = {
        "G": Node("G", PUBLISHER, CORE, (), _MODES),
        "A": Node(
            "A",
            SUBSCRIBER,
            CORE,
            _MODES,
            (),
            (ExtensionRequirement("lane"), DataRequirement()),
        ),
        "L": Node("L", RELAY, COMPAT, _MODES, _MODES),
        "P": Node("P", RELAY, CORE, (STRUCTURED,), (BINARY,)),
        "B": Node(
            "B",
            SUBSCRIBER,
            CORE,
            _MODES,
            (),
            (DataRequirement(), DatacontenttypeRequirement("application/json")),
        ),
    }
    links = (
        Link("G", "A", 0),
        Link("G", "L", 0),
        Link("G", "P", 1),
        Link("P", "L", 2),
        Link("L", "B", 0),
    )
    return Topology(nodes=nodes, links=links)


# ---------------------------------------------------------------------------
# Original SDK entry adapters
# ---------------------------------------------------------------------------


def replay_hop(
    hop: HopRecord, event_format: Format | None = None
) -> BaseCloudEvent | Any:
    """
    Re-read a selected hop's wire message using the receiver's original SDK
    entry and the recorded wire mode.
    """
    node = Node(
        hop.target,
        RELAY,
        hop.receiver_sdk,
        (hop.receiver_mode,),
        (hop.receiver_mode,),
    )
    return receive_message(node, hop, event_format=event_format)


def _to_compat_message(
    message: core_kafka.KafkaMessage,
) -> compat_kafka.KafkaMessage:
    return compat_kafka.KafkaMessage(
        headers=dict(message.headers), key=message.key, value=message.value
    )


def send_message(
    node: Node,
    event: BaseCloudEvent | Any,
    mode: str,
    event_format: Format,
) -> core_kafka.KafkaMessage:
    """Serialize an event through ``node``'s own SDK send entry in ``mode``."""
    if node.sdk == CORE:
        if mode == STRUCTURED:
            return core_kafka.to_structured(event, event_format)
        return core_kafka.to_binary(event, event_format)
    if mode == STRUCTURED:
        sent = compat_kafka.to_structured(event)
    else:
        sent = compat_kafka.to_binary(event)
    return core_kafka.KafkaMessage(
        headers=dict(sent.headers), key=sent.key, value=bytes(sent.value)
    )


def receive_message(
    node: Node,
    message: core_kafka.KafkaMessage | HopRecord,
    mode: str | None = None,
    event_format: Format | None = None,
) -> BaseCloudEvent | Any:
    """
    Parse a Kafka message through ``node``'s own SDK receive entry.

    For a :class:`HopRecord` the recorded wire mode and SDK are used. Relay
    conversion errors surface as :class:`HandoffError` so the engine can report
    the exact failing handoff instead of aborting.
    """
    if isinstance(message, HopRecord):
        mode = message.receiver_mode
        node_sdk = message.receiver_sdk
        wire = message.message
    else:
        assert mode is not None
        node_sdk = node.sdk
        wire = message

    fmt = event_format or JSONFormat()
    try:
        if node_sdk == CORE:
            if mode == STRUCTURED:
                return core_kafka.from_structured(wire, fmt)
            return core_kafka.from_binary(wire, fmt)
        compat_message = _to_compat_message(wire)
        if mode == STRUCTURED:
            return compat_kafka.from_structured(compat_message)
        return compat_kafka.from_binary(compat_message)
    except HandoffError:
        raise
    except Exception as exc:  # the real SDK entry rejected this handoff
        raise HandoffError("receive", exc) from exc


# ---------------------------------------------------------------------------
# Path enumeration
# ---------------------------------------------------------------------------


def _simple_paths(
    topology: Topology, source: str, target: str
) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []

    def walk(node: str, path: tuple[str, ...]) -> None:
        if node == target:
            paths.append(path)
            return
        for link in topology.outgoing(node):
            if link.target not in path:
                walk(link.target, path + (link.target,))

    walk(source, (source,))
    return tuple(paths)


def _ingress_message(
    event: BaseCloudEvent, mode: str, event_format: Format
) -> core_kafka.KafkaMessage:
    return send_message(
        Node("G", PUBLISHER, CORE, (), _MODES), event, mode, event_format
    )


def _lost_values(original: BaseCloudEvent) -> tuple[str, ...]:
    lost: list[str] = []
    reserved = {
        "specversion",
        "id",
        "source",
        "type",
        "time",
        "datacontenttype",
    }
    for attr_name in original.get_attributes():
        if attr_name in reserved:
            continue
        value = original.get_extension(attr_name)
        lost.append(f"{attr_name}={value!r} ({type(value).__name__})")
    data = original.get_data()
    if data is not None:
        lost.append(f"data={data!r} ({type(data).__name__})")
    return tuple(lost)


# ---------------------------------------------------------------------------
# Branch simulation (one publisher handoff -> one subscriber)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchResult:
    """A feasible single-subscriber branch derived from one publisher mode."""

    subscriber: str
    path: tuple[str, ...]
    g_mode: str
    cost: int
    g_message: core_kafka.KafkaMessage
    hops: tuple[HopRecord, ...]
    relay_modes: Mapping[str, tuple[str, str]]
    received: Mapping[str, BaseCloudEvent]


def _relay_mode_choices(
    topology: Topology, path: tuple[str, ...]
) -> tuple[dict[str, str], ...]:
    relays = [
        name
        for name in path[1:-1]
        if topology.nodes[name].kind == RELAY
    ]
    if not relays:
        return ({},)
    combos = []
    for chosen in product(*[topology.nodes[name].tx_modes for name in relays]):
        combos.append(dict(zip(relays, chosen)))
    return tuple(combos)


def _simulate_branch(
    original: BaseCloudEvent,
    topology: Topology,
    path: tuple[str, ...],
    g_mode: str,
    relay_tx_modes: Mapping[str, str],
    event_format: Format,
) -> tuple[BranchResult | None, tuple[HandoffConflict, ...]]:
    root_handoff = f"G -> {path[1]}"
    conflicts: list[HandoffConflict] = []
    hops: list[HopRecord] = []
    received: dict[str, BaseCloudEvent] = {}
    relay_modes: dict[str, tuple[str, str]] = {}
    cost = 0

    outgoing: dict[str, core_kafka.KafkaMessage] = {
        "G": _ingress_message(original, g_mode, event_format)
    }
    wire_mode: dict[str, str] = {"G": g_mode}

    def fail(node_name: str, stage: str, detail: str) -> None:
        conflicts.append(
            HandoffConflict(
                handoff=root_handoff,
                stage=stage,
                detail=detail,
                lost=_lost_values(original),
                chain=tuple(path[: path.index(node_name) + 1]),
                g_mode=g_mode,
            )
        )

    for source, node_name in zip(path, path[1:]):
        node = topology.nodes[node_name]
        link = topology.link(source, node_name)
        incoming_mode = wire_mode[source]
        chain = tuple(path[: path.index(node_name) + 1])

        if link is None or not link.enabled:
            fail(node_name, "receive", "link is not available in the manifest")
            return None, tuple(conflicts)
        cost += link.cost

        if incoming_mode not in link.modes:
            fail(node_name, "receive", f"link does not permit {incoming_mode} mode")
            return None, tuple(conflicts)
        if incoming_mode not in node.rx_modes:
            fail(
                node_name,
                "receive",
                f"{node.sdk} entry of node {node_name} cannot receive "
                f"{incoming_mode} mode",
            )
            return None, tuple(conflicts)

        hop = HopRecord(
            source=source,
            target=node_name,
            wire_mode=incoming_mode,
            receiver_sdk=node.sdk,
            receiver_mode=incoming_mode,
            message=outgoing[source],
        )
        try:
            event = receive_message(node, hop, event_format=event_format)
        except HandoffError as exc:
            fail(
                node_name,
                "receive",
                f"{node.sdk} {incoming_mode} receive failed: "
                f"{type(exc.cause).__name__}: {exc.cause}",
            )
            return None, tuple(conflicts)
        hops.append(hop)
        received[node_name] = event

        if node.kind == SUBSCRIBER:
            for requirement in node.requirements:
                failure = requirement.check(event, original)
                if failure is not None:
                    detail, _lost = failure
                    conflicts.append(
                        HandoffConflict(
                            handoff=root_handoff,
                            stage="requirement",
                            detail=detail,
                            lost=_lost,
                            chain=chain,
                            g_mode=g_mode,
                        )
                    )
            if conflicts:
                return None, tuple(conflicts)
            continue

        tx_mode = relay_tx_modes[node_name]
        if tx_mode not in node.tx_modes:
            fail(
                node_name,
                "send",
                f"{node.sdk} entry of node {node_name} cannot send {tx_mode} mode",
            )
            return None, tuple(conflicts)
        try:
            sent = send_message(node, event, tx_mode, event_format)
        except Exception as exc:
            fail(
                node_name,
                "send",
                f"{node.sdk} {tx_mode} send failed: "
                f"{type(exc).__name__}: {exc}",
            )
            return None, tuple(conflicts)
        outgoing[node_name] = sent
        wire_mode[node_name] = tx_mode
        relay_modes[node_name] = (incoming_mode, tx_mode)

    return (
        BranchResult(
            subscriber=path[-1],
            path=path,
            g_mode=g_mode,
            cost=cost,
            g_message=outgoing["G"],
            hops=tuple(hops),
            relay_modes=dict(relay_modes),
            received=dict(received),
        ),
        (),
    )


def _dedupe_conflicts(
    conflicts: list[HandoffConflict],
) -> tuple[HandoffConflict, ...]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[HandoffConflict] = []
    for conflict in conflicts:
        key = (
            conflict.handoff,
            conflict.stage,
            conflict.detail,
            conflict.chain,
            conflict.g_mode,
        )
        if key not in seen:
            seen.add(key)
            unique.append(conflict)
    return tuple(
        sorted(
            unique,
            key=lambda item: (item.handoff, item.g_mode or "", item.stage),
        )
    )


# ---------------------------------------------------------------------------
# Plan composition across subscribers
# ---------------------------------------------------------------------------


def _compose_plans(
    topology: Topology,
    branches: Mapping[str, list[BranchResult]],
) -> tuple[Plan, ...]:
    g_modes = topology.nodes["G"].tx_modes
    plans: list[Plan] = []
    subscribers = tuple(branches.keys())

    for g_mode in g_modes:
        per_subscriber = [
            [branch for branch in branches[name] if branch.g_mode == g_mode]
            for name in subscribers
        ]
        if any(not options for options in per_subscriber):
            continue

        def combine(
            index: int,
            chosen: list[BranchResult],
            relay_modes: dict[str, tuple[str, str]],
            hops: dict[tuple[str, str], HopRecord],
            received: dict[str, BaseCloudEvent],
            routes: dict[str, tuple[str, ...]],
        ) -> None:
            if index == len(subscribers):
                used_links = {
                    (hop.source, hop.target) for hop in hops.values()
                }
                cost = sum(
                    topology.link(source, target).cost  # type: ignore[union-attr]
                    for source, target in used_links
                )
                shared_message = chosen[0].g_message
                shared_hops = tuple(
                    (
                        HopRecord(
                            source=record.source,
                            target=record.target,
                            wire_mode=record.wire_mode,
                            receiver_sdk=record.receiver_sdk,
                            receiver_mode=record.receiver_mode,
                            message=(
                                shared_message
                                if record.source == "G"
                                else record.message
                            ),
                        )
                    )
                    for record in sorted(hops.values(), key=lambda hop: hop.target)
                )
                plans.append(
                    Plan(
                        cost=cost,
                        g_mode=g_mode,
                        g_message=shared_message,
                        relay_modes=dict(relay_modes),
                        routes=dict(routes),
                        hops=shared_hops,
                        received=dict(received),
                    )
                )
                return

            for branch in per_subscriber[index]:
                compatible = all(
                    relay_modes[relay] == mode
                    for relay, mode in branch.relay_modes.items()
                    if relay in relay_modes
                )
                if not compatible:
                    continue
                next_relay = dict(relay_modes)
                next_relay.update(branch.relay_modes)
                next_hops = dict(hops)
                for hop in branch.hops:
                    next_hops[(hop.source, hop.target)] = hop
                next_received = dict(received)
                next_received.update(branch.received)
                next_routes = dict(routes)
                next_routes[branch.subscriber] = branch.path
                combine(
                    index + 1,
                    chosen + [branch],
                    next_relay,
                    next_hops,
                    next_received,
                    next_routes,
                )

        combine(0, [], {}, {}, {}, {})

    plans.sort(
        key=lambda plan: (
            plan.cost,
            plan.g_mode,
            tuple(sorted(plan.routes.items())),
        )
    )
    unique: list[Plan] = []
    seen: set[tuple[Any, ...]] = set()
    for plan in plans:
        signature = (
            plan.cost,
            plan.g_mode,
            tuple(sorted(plan.routes.items())),
            tuple(sorted(plan.relay_modes.items())),
        )
        if signature not in seen:
            seen.add(signature)
            unique.append(plan)
    return tuple(unique)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def check_event(
    event: BaseCloudEvent,
    topology: Topology,
    required_subscribers: tuple[str, ...] | list[str],
    event_format: Format | None = None,
) -> CheckResult:
    """
    Run the pre-publish check for an already parsed core CloudEvent.

    :param event: The event as ingested from HTTP; it is the only source of
        truth, relays never re-enrich from it.
    :param topology: The receiver/link manifest.
    :param required_subscribers: Subscribers that must each get one copy.
    :param event_format: Format used by core entries (defaults to JSON).
    """
    fmt = event_format or JSONFormat()
    required = tuple(required_subscribers)

    for name in required:
        node = topology.nodes.get(name)
        if node is None or node.kind != SUBSCRIBER:
            return CheckResult(
                feasible=False,
                plans=(),
                conflicts=(
                    HandoffConflict(
                        name, "requirement", "unknown subscriber in manifest"
                    ),
                ),
                required=required,
            )

    feasible_branches: dict[str, list[BranchResult]] = {}
    all_conflicts: list[HandoffConflict] = []

    for subscriber in required:
        paths = _simple_paths(topology, "G", subscriber)
        if not paths:
            all_conflicts.append(
                HandoffConflict(
                    f"G -> {subscriber}",
                    "receive",
                    "no enabled path reaches this subscriber",
                )
            )
            continue

        branches: list[BranchResult] = []
        for path in paths:
            for g_mode in topology.nodes["G"].tx_modes:
                for relay_choices in _relay_mode_choices(topology, path):
                    branch, conflicts = _simulate_branch(
                        event, topology, path, g_mode, relay_choices, fmt
                    )
                    if branch is not None:
                        branches.append(branch)
                    else:
                        all_conflicts.extend(conflicts)
        feasible_branches[subscriber] = branches

    plans = _compose_plans(topology, feasible_branches)
    if plans:
        return CheckResult(
            feasible=True, plans=plans, conflicts=(), required=required
        )

    conflicts = _diagnose(feasible_branches, all_conflicts)
    return CheckResult(
        feasible=False,
        plans=(),
        conflicts=conflicts,
        required=required,
    )


def _diagnose(
    feasible_branches: Mapping[str, list[BranchResult]],
    all_conflicts: list[HandoffConflict],
) -> tuple[HandoffConflict, ...]:
    """
    Attribute every failure to its publisher handoff and wire mode.

    A conflict is kept only for the modes in which that handoff has no feasible
    branch. The resulting set makes the mode incompatibility explicit: e.g. one
    handoff only survives in structured mode while the other cannot satisfy its
    subscriber in structured mode, so the single shared publisher message can
    never cover both.
    """
    working_modes: dict[str, set[str]] = {}
    for branches in feasible_branches.values():
        for branch in branches:
            handoff = f"G -> {branch.path[1]}"
            working_modes.setdefault(handoff, set()).add(branch.g_mode)

    relevant: list[HandoffConflict] = []
    for conflict in all_conflicts:
        if conflict.g_mode is None:
            relevant.append(conflict)
        elif conflict.g_mode not in working_modes.get(conflict.handoff, set()):
            relevant.append(conflict)
    return _dedupe_conflicts(relevant)


def check_http(
    headers: Mapping[str, str],
    body: bytes,
    topology: Topology,
    required_subscribers: tuple[str, ...] | list[str],
    event_format: Format | None = None,
) -> CheckResult:
    """
    Parse the original HTTP CloudEvent with the core HTTP binding and run the
    pre-publish check. An invalid HTTP original is reported via
    ``CheckResult.original_error`` instead of being raised.
    """
    fmt = event_format or JSONFormat()
    try:
        event = core_http.from_http_event(
            HTTPMessage(headers=dict(headers), body=body), fmt
        )
    except Exception as exc:
        return CheckResult(
            feasible=False,
            plans=(),
            conflicts=(),
            original_error=f"{type(exc).__name__}: {exc}",
            required=tuple(required_subscribers),
        )
    return check_event(event, topology, required_subscribers, fmt)
