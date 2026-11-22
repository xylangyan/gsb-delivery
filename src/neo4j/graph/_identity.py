# Copyright (c) "Neo4j"
# Neo4j Sweden AB [https://neo4j.com]
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Identity types shared by graph snapshots and entities."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DatabaseIdentity",
    "GraphBatch",
    "Provenance",
]


@dataclass(frozen=True, order=True)
class DatabaseIdentity:
    """
    The physical database that produced a graph response.

    A database name alone is not necessarily globally unique.  ``address``
    can be used to distinguish equal database names on different servers.
    """

    name: str
    address: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("database name must be a non-empty string")
        if self.address is not None and not isinstance(self.address, str):
            raise TypeError("database address must be a string or None")

    def __str__(self) -> str:
        if self.address:
            return f"{self.name}@{self.address}"
        return self.name

    @classmethod
    def coerce(cls, value: DatabaseIdentity | str) -> DatabaseIdentity:
        if isinstance(value, cls):
            return value
        return cls(value)


@dataclass(frozen=True, order=True)
class GraphBatch:
    """
    The identity and batch in which an ``element_id`` is valid.

    Responses from different batches must not share an element cache, even
    when they are produced by the same database.
    """

    database: DatabaseIdentity
    batch: str

    def __post_init__(self) -> None:
        if not isinstance(self.database, DatabaseIdentity):
            object.__setattr__(
                self, "database", DatabaseIdentity.coerce(self.database)
            )
        if not isinstance(self.batch, str) or not self.batch:
            raise ValueError("graph batch must be a non-empty string")

    def __str__(self) -> str:
        return f"{self.database}/{self.batch}"


@dataclass(frozen=True, order=True)
class Provenance:
    """The physical origin of one occurrence of a graph entity."""

    database: DatabaseIdentity
    batch: str
    element_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.database, DatabaseIdentity):
            object.__setattr__(
                self, "database", DatabaseIdentity.coerce(self.database)
            )
        if not isinstance(self.batch, str) or not self.batch:
            raise ValueError("graph batch must be a non-empty string")
        if not isinstance(self.element_id, str):
            raise TypeError("element_id must be a string")

    @property
    def graph_batch(self) -> GraphBatch:
        return GraphBatch(self.database, self.batch)

    def __str__(self) -> str:
        return f"{self.database}/{self.batch}/{self.element_id}"
