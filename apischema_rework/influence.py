__all__ = ["FieldInfluence", "InfluenceMap", "build_influence"]

import collections.abc
import typing
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apischema.objects import ObjectField, object_fields
from apischema.types import Undefined

from .model import Path

WILDCARD = "*"


@dataclass(frozen=True)
class FieldInfluence:
    """How one warehouse field is fed by the incoming JSON document."""

    attr_path: Path
    alias_path: Path
    required: bool
    default: Any = Undefined
    flattened: bool = False
    source_alias_paths: Tuple[Path, ...] = ()


@dataclass
class InfluenceMap:
    fields: Tuple[FieldInfluence, ...]
    alias_to_fields: Dict[Path, List[FieldInfluence]] = field(default_factory=dict)
    attr_to_alias: Dict[Path, Path] = field(default_factory=dict)
    alias_to_attrs: Dict[Path, List[Path]] = field(default_factory=dict)

    def resolve_path(self, path: Path) -> Optional[Path]:
        """Resolve a warehouse field path onto its source JSON alias path.

        Audit sheets may name either imported object attributes or JSON
        aliases; flattened fields collapse several attributes onto one alias.
        """

        return self.resolve_both(path)[1]

    def resolve_both(self, path: Path) -> Optional[Tuple[Path, Path]]:
        """Return ``(concrete attribute path, concrete alias path)``."""

        wildcard = tuple(WILDCARD if isinstance(part, int) else part for part in path)
        if wildcard in self.attr_to_alias:
            wildcard_alias = self.attr_to_alias[wildcard]
            return path, _replace_wildcards(wildcard_alias, path)
        if path in self.attr_to_alias:
            return path, self.attr_to_alias[path]
        if wildcard in self.alias_to_fields:
            attrs = self.alias_to_attrs.get(wildcard, ())
            attr_path = attrs[0] if attrs else wildcard
            return attr_path, _replace_wildcards(wildcard, path)
        if path in self.alias_to_fields:
            attrs = self.alias_to_attrs.get(path, ())
            return (attrs[0] if attrs else path), path
        attr_prefix = _longest_prefix(self.attr_to_alias, wildcard)
        if attr_prefix is not None:
            suffix = wildcard[len(attr_prefix) :]
            return path, self.attr_to_alias[attr_prefix] + suffix
        alias_prefix = _longest_prefix(self.alias_to_fields, wildcard)
        if alias_prefix is not None:
            attrs = self.alias_to_attrs.get(alias_prefix, ())
            attr_prefix_path = attrs[0] if attrs else alias_prefix
            return (
                attr_prefix_path + wildcard[len(alias_prefix) :],
                alias_prefix + wildcard[len(alias_prefix) :],
            )
        return None

    def influence_key(self, path: Path) -> Path:
        """Wildcard-normalized path used to look up influencing source paths."""

        return tuple(WILDCARD if isinstance(part, int) else part for part in path)

    def influencers(self, path: Path) -> Tuple[Path, ...]:
        """Source JSON paths driving the warehouse field named by ``path``.

        A flat key shared by two inner objects feeds both fields, so the whole
        shared source path is reported for each of them. Concrete list
        indices from the query replace wildcard slots so distinct detail
        lines stay independent.
        """

        resolved = self.resolve_both(path)
        if resolved is None:
            return ()
        _, alias_path = resolved
        key = self.influence_key(alias_path)
        touched = self.alias_to_fields.get(key)
        if touched is None:
            prefix = _longest_prefix(self.alias_to_fields, key)
            if prefix is None:
                return ()
            touched = self.alias_to_fields[prefix]
        seen: Dict[Path, None] = {}
        for influenced in touched:
            paths = influenced.source_alias_paths or (influenced.alias_path,)
            for source_path in paths:
                seen.setdefault(_replace_wildcards(source_path, alias_path), None)
        return tuple(seen)


def _longest_prefix(mapping: Dict[Path, Any], path: Path) -> Optional[Path]:
    match: Optional[Path] = None
    for key in mapping:
        if len(key) <= len(path) and path[: len(key)] == key and (
            match is None or len(key) > len(match)
        ):
            match = key
    return match


def _replace_wildcards(wildcard_path: Path, concrete_path: Path) -> Path:
    result = list(wildcard_path)
    for index, part in enumerate(wildcard_path):
        if part == WILDCARD and index < len(concrete_path):
            result[index] = concrete_path[index]
    return tuple(result)


def _object_fields_of(tp: Any) -> Optional[Tuple[ObjectField, ...]]:
    try:
        return tuple(object_fields(tp).values())
    except (TypeError, NotImplementedError, ValueError):
        return None


def build_influence(target_type: Any) -> InfluenceMap:
    """Walk the apischema object graph of ``target_type``.

    Flattened fields are inlined: their inner aliases are emitted at the same
    JSON prefix, which is how one flat key can feed two warehouse fields.
    Generic records are visited after substitution, so each parametrization
    keeps its own aliases and its own field conversions.
    """

    collected: List[FieldInfluence] = []
    _visit(target_type, (), (), False, collected)
    alias_to_fields: Dict[Path, List[FieldInfluence]] = {}
    attr_to_alias: Dict[Path, Path] = {}
    alias_to_attrs: Dict[Path, List[Path]] = {}
    for item in collected:
        alias_to_fields.setdefault(item.alias_path, []).append(item)
        attr_to_alias.setdefault(item.attr_path, item.alias_path)
        alias_to_attrs.setdefault(item.alias_path, []).append(item.attr_path)
    return InfluenceMap(
        tuple(collected), alias_to_fields, attr_to_alias, alias_to_attrs
    )


def _visit(
    tp: Any,
    attr_prefix: Path,
    emit_prefix: Path,
    inside_flattened: bool,
    collected: List[FieldInfluence],
) -> None:
    fields = _object_fields_of(tp)
    if fields is None:
        return
    for obj_field in fields:
        attr_path = attr_prefix + (obj_field.name,)
        if obj_field.flattened:
            # Inner aliases are read at the current level instead of under
            # the flattened field's own alias.
            _visit(
                obj_field.type,
                attr_path,
                emit_prefix,
                True,
                collected,
            )
            continue
        alias_path = emit_prefix + (obj_field.alias,)
        influence = FieldInfluence(
            attr_path=attr_path,
            alias_path=alias_path,
            required=obj_field.required,
            default=Undefined if obj_field.required else obj_field.get_default(),
            flattened=inside_flattened,
            source_alias_paths=(alias_path,),
        )
        collected.append(influence)
        item_type = _collection_item_type(obj_field.type)
        if item_type is not None:
            _visit(
                item_type,
                attr_path + (WILDCARD,),
                alias_path + (WILDCARD,),
                False,
                collected,
            )
        elif _object_fields_of(obj_field.type) is not None:
            _visit(
                obj_field.type,
                attr_path,
                alias_path,
                False,
                collected,
            )


def _collection_item_type(tp: Any) -> Optional[Any]:
    origin = typing.get_origin(tp)
    if origin is None:
        return None
    if not (
        isinstance(origin, type)
        and issubclass(origin, (list, tuple, collections.abc.Collection))
    ):
        return None
    args = typing.get_args(tp)
    if not args:
        return None
    item = args[0]
    return item if _object_fields_of(item) is not None else None
