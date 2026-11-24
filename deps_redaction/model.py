"""Input data model for one redaction sentence."""

from __future__ import annotations

from dataclasses import dataclass, field

from .graph import Edge, QuerySpec


@dataclass(frozen=True)
class Query(QuerySpec):
    """A fixed checker query.

    ``source`` and ``target`` are original ids of the two quasi-delivered
    ordinary words; they are translated to confirmed new ids via
    :attr:`SentenceInput.id_map`.
    """


@dataclass
class License:
    """Which empty-node placeholder rows may stand in for which originals.

    ``placeholders`` maps a permitted CoNLL-U empty-node id (e.g. ``"2.1"``)
    to the original intermediate ids it is allowed to host.  A role may be
    listed under several placeholders; an original empty node that loses its
    own row is handled as a hidden role exactly like a removed ordinary word.
    """

    placeholders: dict[str, tuple[str, ...]]

    def options_for(self, role: str) -> tuple[str, ...]:
        result = [p for p, roles in self.placeholders.items() if role in roles]
        return tuple(sorted(result))


@dataclass
class SentenceInput:
    sent_id: str
    id_map: dict[str, str]
    edges: list[Edge]
    hidden_roles: set[str]
    license: License
    queries: list[Query] = field(default_factory=list)

    def validate(self) -> list[str]:
        problems: list[str] = []
        known = set(self.id_map) | self.hidden_roles | {"0"}
        for edge in self.edges:
            for endpoint in (edge.src, edge.dst):
                if endpoint not in known:
                    problems.append(
                        f"{self.sent_id}: edge endpoint {endpoint} is neither a kept "
                        "token nor a declared hidden role"
                    )
        for query in self.queries:
            for endpoint in (query.source, query.target):
                if endpoint not in self.id_map:
                    problems.append(
                        f"{self.sent_id}: query {query.name} endpoint {endpoint} is "
                        "not a delivered ordinary token"
                    )
        for role in self.hidden_roles:
            if not self.license.options_for(role):
                problems.append(
                    f"{self.sent_id}: hidden role {role} has no permitted placeholder"
                )
        return problems
