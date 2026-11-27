"""Configuration for handover mode.

Owner IDs may appear in three places of an interaction:

* the request (URI, headers and body),
* the response (headers and body),
* the next-page cursor (a query parameter of a request URI, or a field inside
  a JSON response body).

Each *group* gives one original owner ID together with the replacement IDs that
are allowed to take its place. Keeping the original value is always a possible
group choice, but an issued cassette may not carry any original owner ID, so a
group whose value cannot be changed makes the build impossible at that location.

In addition, a few locations may hold *individual permits*: a specific original
value at a specific location may be changed, and each permit used counts towards
the "fewest individual permits" tie-break.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class HandoverConfigError(ValueError):
    """Raised when a handover configuration is inconsistent."""


@dataclass(frozen=True)
class Location:
    """Where an owner ID lives.

    ``kind`` is one of ``"request"``, ``"response"`` or ``"cursor"``.

    * request/response: ``part`` is ``"uri"``, ``"header"`` or ``"body"``;
      ``name`` is the header name or the dotted body path (``None`` for the
      whole URI).
    * cursor: ``part`` is ``"query"`` (a request URI query parameter) or
      ``"body"`` (a dotted path inside a JSON response body); ``name`` is the
      parameter name or body path.
    """

    kind: str
    part: str
    name: str | None = None

    def __str__(self):
        if self.name is None:
            return f"{self.kind}.{self.part}"
        return f"{self.kind}.{self.part}.{self.name}"

    @classmethod
    def parse(cls, text):
        bits = text.split(".")
        if len(bits) < 2 or bits[0] not in ("request", "response", "cursor"):
            raise HandoverConfigError(
                f"{text!r} is not a valid location; expected e.g. 'request.uri', "
                "'request.header.X-Owner', 'response.body.data.owner' or 'cursor.query.page'",
            )
        kind, part = bits[0], bits[1]
        if kind in ("request", "response") and part not in ("uri", "header", "body"):
            raise HandoverConfigError(f"{text!r}: {kind} locations must target uri, header or body")
        if kind == "cursor" and part not in ("query", "body"):
            raise HandoverConfigError(f"{text!r}: cursor locations must target query or body")
        name = ".".join(bits[2:]) if len(bits) > 2 else None
        return cls(kind, part, name)


@dataclass(frozen=True)
class Permit:
    """An individual permit to change the value at one location.

    ``location`` identifies the place; ``original`` is the value the place must
    hold for the permit to apply; ``replacement`` is the value to write. A
    permit is only "used" when its location actually holds ``original``.
    """

    location: Location
    original: str
    replacement: str

    def __post_init__(self):
        if not self.replacement:
            raise HandoverConfigError(f"Permit at {self.location} needs a non-empty replacement value")


@dataclass(frozen=True)
class OwnerIdRules:
    """The owner-ID rewrite rules for one handover configuration.

    ``groups`` maps each original owner ID to the tuple of values it may be
    replaced with. The original value itself is always an implicit candidate.
    Group order is declaration order and drives deterministic tie-breaks.

    ``locations`` maps each location to the original ID of the group that owns
    it: every declared location gets the group-wise replacement there.

    ``permits`` are individual, extra replacement permissions at locations that
    have no group rule.

    ``group_match_on`` optionally gives each group (keyed by its original ID)
    the matcher functions the receiving team will use for that group; the
    builder verifies occurrence identity under each of them.
    """

    groups: OrderedDict[str, tuple[str, ...]]
    locations: dict[Location, str]
    permits: tuple[Permit, ...] = ()
    group_match_on: dict[str, tuple] = field(default_factory=dict)

    def __post_init__(self):
        for location, original in self.locations.items():
            if original not in self.groups:
                raise HandoverConfigError(
                    f"Location {location} references owner ID {original!r}, which has no replacement group",
                )
        for permit in self.permits:
            if permit.location in self.locations:
                raise HandoverConfigError(
                    f"Location {permit.location} already has a group rule; individual permits can only "
                    "target locations without one",
                )
        for original in self.group_match_on:
            if original not in self.groups:
                raise HandoverConfigError(
                    f"group_match_on references owner ID {original!r}, which has no replacement group",
                )

    @property
    def group_order(self):
        return tuple(self.groups)

    def alternatives_for(self, original):
        """Values the given original ID may take, including itself."""
        return (original, *self.groups[original])

    def permits_at(self, location):
        return tuple(permit for permit in self.permits if permit.location == location)


@dataclass
class Permissions:
    """Builder permissions handed to a shift alongside the rules.

    ``receive_config`` names the receiving configuration (the set of rules the
    other team will match with) under which the handover is built. It is echoed
    back in any failure report so an operator can tell which configuration and
    which page turn could not be handed over safely.

    ``page_turns``/``retries`` are the finite, declared script paths. The fake
    request script refuses anything outside them, so no external service is
    ever reached.
    """

    rules: OwnerIdRules
    receive_config: str = "default"
    page_turns: tuple[str, ...] = ()
    retries: tuple[str, ...] = ()

    def validate_page_turn(self, page_turn):
        if self.page_turns and page_turn not in self.page_turns:
            raise HandoverConfigError(
                f"Page turn {page_turn!r} is not in the declared script {list(self.page_turns)!r}; "
                "handover scripts may not reach external services",
            )

    def validate_retry(self, retry):
        if self.retries and retry not in self.retries:
            raise HandoverConfigError(
                f"Retry path {retry!r} is not in the declared script {list(self.retries)!r}; "
                "handover scripts may not reach external services",
            )


@dataclass
class HandoverConfig:
    """Everything a shift needs to work in handover mode.

    ``shift_id`` distinguishes the two shifts' ledgers. ``book_path`` is where
    the handover book is stored; it defaults to a sibling of the cassette.
    """

    shift_id: str
    rules: OwnerIdRules
    book_path: str | None = None
    permissions: Permissions | None = None
    receive_config: str = "default"
    page_turns: tuple[str, ...] = field(default_factory=tuple)
    retries: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.shift_id:
            raise HandoverConfigError("shift_id must be a non-empty string")
        if self.permissions is None:
            self.permissions = Permissions(
                rules=self.rules,
                receive_config=self.receive_config,
                page_turns=self.page_turns,
                retries=self.retries,
            )

    def book_path_for(self, cassette_path):
        if self.book_path:
            return self.book_path
        return f"{cassette_path}.handover.yaml"


def replace_query_parameter(uri, name, value):
    """Return ``uri`` with query parameter ``name`` set to ``value`` in place."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == name for key, _ in query):
        query = [(key, value if key == name else val) for key, val in query]
    else:
        query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def query_parameter(uri, name):
    for key, value in parse_qsl(urlsplit(uri).query, keep_blank_values=True):
        if key == name:
            return value
    return None
