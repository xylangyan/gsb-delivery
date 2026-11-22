# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Resolve message definitions for individual connections."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

from rosbags.interfaces import MessageDefinitionFormat, Nodetype
from rosbags.typesys import TypesysError, get_types_from_idl, get_types_from_msg

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from rosbags.interfaces import Connection, FieldDesc
    from rosbags.interfaces.typing import Constdefs, Fielddefs, Typesdict
    from rosbags.typesys.store import Typestore

SEPARATOR = '=' * 80


class DefinitionError(Exception):
    """Message definition could not be resolved."""


class MissingDefinitionError(DefinitionError):
    """Message definitions are incomplete and no matching fallback exists."""

    connections: list[Connection]

    def __init__(self, connections: Sequence[Connection]) -> None:
        """Initialize with the affected connections."""
        self.connections = list(connections)
        topics = ', '.join(sorted({x.topic for x in self.connections}))
        msg = f'Missing message definitions for connections: {topics}'
        super().__init__(msg)


def parse_definition(
    fmt: MessageDefinitionFormat,
    data: str,
    msgtype: str,
) -> Typesdict:
    """Parse the definition attached to a single connection.

    Args:
        fmt: Definition format.
        data: Definition text.
        msgtype: Message type carried by the connection.

    Returns:
        Parsed type definitions.

    Raises:
        DefinitionError: The definition cannot be parsed.

    """
    try:
        if fmt == MessageDefinitionFormat.MSG:
            return get_types_from_msg(data, msgtype)

        assert fmt == MessageDefinitionFormat.IDL
        if not data.startswith(f'{SEPARATOR}\nIDL: '):
            return get_types_from_idl(data)

        types: Typesdict = {}
        for part in data.split(f'{SEPARATOR}\n')[1:]:
            header, idl = part.split('\n', 1)
            assert header.startswith('IDL: ')
            types.update(get_types_from_idl(idl))
    except TypesysError as err:
        msg = f'Could not parse definition of {msgtype!r}: {err}'
        raise DefinitionError(msg) from err
    else:
        return types


def reference_name(desc: FieldDesc) -> str | None:
    """Return the referenced message type of a field, if any."""
    base = desc if desc[0] in {Nodetype.BASE, Nodetype.NAME} else desc[1][0]
    if base[0] != Nodetype.NAME:
        return None
    assert isinstance(base[1], str)
    return base[1]


def closure_types(types: Mapping[str, object], msgtype: str) -> dict[str, object]:
    """Return every type reachable from msgtype.

    Args:
        types: Parsed type definitions.
        msgtype: Root type.

    Returns:
        Mapping from type name to its type definition.

    Raises:
        DefinitionError: A referenced type is not declared.

    """
    closure: dict[str, object] = {}
    pending = [msgtype]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in types:
            msg = f'Definition of {name!r} is missing.'
            raise DefinitionError(msg)
        closure[name] = types[name]
        _, fields = types[name]  # type: ignore[misc]
        for _, desc in fields:  # type: ignore[union-attr]
            if (ref := reference_name(desc)) is not None:
                pending.append(ref)
    return closure


def canonical_type(entry: object) -> str:
    """Render a type definition in a stable, comparable form."""
    consts, fields = entry  # type: ignore[misc]
    consttext = '\n'.join(f'{typ} {name}={value!r}' for name, typ, value in consts)
    fieldtext = '\n'.join(f'{name} {desc!r}' for name, desc in fields)
    return f'{consttext}\n{fieldtext}'


@dataclass
class TypeNode:
    """One semantic variant of a message type."""

    name: str
    entry: tuple[Constdefs, Fielddefs]
    deps: dict[str, str]
    key: str
    output: str = ''


@dataclass
class DefinitionRegistry:
    """Collect definitions and assign output names to semantic variants."""

    nodes: dict[str, TypeNode] = field(default_factory=dict)
    keys_by_name: dict[str, list[str]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)

    def add(self, types: Mapping[str, object], msgtype: str) -> str:
        """Add one connection closure and return the root variant key."""
        keys: dict[str, str] = {}
        visiting: set[str] = set()

        def visit(name: str) -> str:
            if name in keys:
                return keys[name]
            if name in visiting:
                msg = f'Definition of {name!r} is recursive.'
                raise DefinitionError(msg)
            if name not in types:
                msg = f'Definition of {name!r} is missing.'
                raise DefinitionError(msg)
            visiting.add(name)
            entry = types[name]
            _, fields = entry  # type: ignore[misc]
            deps = {
                ref: visit(ref)
                for _, desc in fields  # type: ignore[union-attr]
                if (ref := reference_name(desc)) is not None
            }
            digest = sha256(
                '\n'.join(
                    [
                        name,
                        canonical_type(entry),
                        *[f'{ref}:{key}' for ref, key in sorted(deps.items())],
                    ],
                ).encode(),
            ).hexdigest()
            if digest not in self.nodes:
                self.nodes[digest] = TypeNode(
                    name,
                    entry,  # type: ignore[arg-type]
                    deps,
                    digest,
                )
                self.keys_by_name.setdefault(name, []).append(digest)
                self.used.add(name)
            visiting.discard(name)
            keys[name] = digest
            return digest

        return visit(msgtype)

    def assign_names(self, scope: str | None = None) -> None:
        """Assign a unique output type name to every variant.

        Args:
            scope: Package (or package/msg) prefix for variants after the
                first one. When omitted, a numeric suffix is appended in the
                original package.

        """
        prefix = ''
        if scope:
            prefix = scope.rstrip('/')
            if not prefix.endswith('/msg'):
                prefix = f'{prefix}/msg'

        for name, keys in self.keys_by_name.items():
            for index, key in enumerate(keys):
                node = self.nodes[key]
                if index == 0:
                    node.output = name
                    continue
                pkg, _, short = name.partition('/msg/')
                base = f'{prefix}/{short}' if prefix else f'{pkg}/msg/{short}'
                counter = index
                candidate = base if counter == 0 else f'{base}_{counter}'
                while candidate in self.used:
                    counter += 1
                    candidate = f'{base}_{counter}'
                node.output = candidate
                self.used.add(candidate)

    def _rename_desc(self, node: TypeNode, desc: FieldDesc) -> FieldDesc:
        if desc[0] == Nodetype.NAME:
            assert isinstance(desc[1], str)
            return (Nodetype.NAME, self.nodes[node.deps[desc[1]]].output)
        if desc[0] in {Nodetype.ARRAY, Nodetype.SEQUENCE}:
            sub, size = desc[1]
            if sub[0] == Nodetype.NAME:
                assert isinstance(sub[1], str)
                sub = (Nodetype.NAME, self.nodes[node.deps[sub[1]]].output)
            return (desc[0], (sub, size))
        return desc

    def typesdict(self) -> Typesdict:
        """Return all variants under their assigned output names."""
        result: Typesdict = {}
        for node in self.nodes.values():
            consts, fields = node.entry
            result[node.output] = (
                list(consts),
                [(fname, self._rename_desc(node, desc)) for fname, desc in fields],
            )
        return result

    def store(self) -> Typestore:
        """Create a type store for all variants."""
        store = builtin_store()
        store.register(self.typesdict())
        return store

    def output_name(self, key: str) -> str:
        """Return the assigned output name for a variant key."""
        return self.nodes[key].output


def builtin_store() -> Typestore:
    """Return an empty store carrying the builtin interface types."""
    from rosbags.typesys import Stores, get_typestore  # noqa: PLC0415

    return get_typestore(Stores.EMPTY)


def config_digest(store: Typestore, msgtype: str, *, is2: bool) -> str:
    """Return the digest a connection would carry for a type."""
    if is2:
        return store.hash_rihs01(msgtype)
    return store.generate_msgdef(msgtype, ros_version=1)[1]


def types_from_typestore(store: Typestore) -> Typesdict:
    """Extract a types dictionary from a typestore."""
    return {
        name: (list(consts), list(fields)) for name, (consts, fields) in store.fielddefs.items()
    }


def detach_types(types: Mapping[str, object]) -> Typesdict:
    """Copy a type mapping into a plain types dictionary."""
    result: Typesdict = {}
    for name, entry in types.items():
        consts, fields = entry  # type: ignore[misc]
        result[name] = (list(consts), list(fields))  # type: ignore[arg-type]
    return result


def resolve_connection(
    connection: Connection,
    fallback: Typestore | None = None,
    *,
    is2: bool = True,
) -> tuple[Typesdict, str]:
    """Resolve definitions for a connection.

    A complete definition embedded in the connection is used as is. Otherwise
    the fallback store is consulted, but only definitions whose digest matches
    the connection's own digest are accepted. Definitions from other
    connections are never substituted, even when they share a type name.

    Args:
        connection: Connection to resolve.
        fallback: Optional store with fallback definitions.
        is2: Whether the connection belongs to a rosbag2 recording.

    Returns:
        Detached definitions and the root message type name.

    Raises:
        MissingDefinitionError: Definitions are incomplete and no matching
            fallback exists.

    """
    candidates = None
    if fallback is not None:
        candidates = [types_from_typestore(fallback)]
    return resolve_from_candidates(connection, candidates, is2=is2)


def resolve_from_candidates(
    connection: Connection,
    candidates: Sequence[Mapping[str, object]] | None = None,
    *,
    is2: bool = True,
) -> tuple[Typesdict, str]:
    """Resolve a connection against a sequence of candidate definitions.

    A candidate is only accepted when the resulting digest matches the
    connection's digest, so same-named definitions from other connections are
    never substituted.

    Args:
        connection: Connection to resolve.
        candidates: Candidate type mappings.
        is2: Whether the connection belongs to a rosbag2 recording.

    Returns:
        Detached definitions and the root message type name.

    Raises:
        MissingDefinitionError: Definitions are incomplete and no candidate
            matches.

    """
    msgtype = connection.msgtype
    errors: list[Exception] = []

    embedded: Typesdict = {}
    if connection.msgdef.format != MessageDefinitionFormat.NONE:
        try:
            embedded = parse_definition(
                connection.msgdef.format,
                connection.msgdef.data,
                msgtype,
            )
            _ = closure_types(embedded, msgtype)
        except DefinitionError as err:
            errors.append(err)
        else:
            return detach_types(embedded), msgtype

    merged_candidates = list(candidates or [])
    for candidate in merged_candidates:
        for types in (detach_types(candidate), {**candidate, **embedded}):
            try:
                _ = closure_types(types, msgtype)
            except DefinitionError as err:
                errors.append(err)
                continue

            if not connection.digest:
                return types, msgtype

            store = builtin_store()
            store.register(types)
            try:
                digest = config_digest(store, msgtype, is2=is2)
            except (KeyError, TypesysError):
                digest = ''
            if digest == connection.digest:
                return types, msgtype

    if merged_candidates:
        errors.append(
            DefinitionError(
                f'Definition of {msgtype!r} does not match connection digest.',
            ),
        )

    raise MissingDefinitionError([connection]) from (errors[0] if errors else None)


def definition_for(store: Typestore, msgtype: str) -> str:
    """Generate a self-contained definition for a written connection."""
    return store.generate_msgdef(msgtype, ros_version=2)[0]
