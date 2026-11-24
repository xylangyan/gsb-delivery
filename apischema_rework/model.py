__all__ = [
    "ConflictPair",
    "ConflictReport",
    "Missing",
    "MissingType",
    "Null",
    "NullType",
    "Remove",
    "RemoveType",
    "ReworkPatch",
    "SourceChange",
    "TargetExpectation",
]

from dataclasses import dataclass, field
from typing import Any, Tuple


class NullType:
    """Sentinel describing an explicit JSON ``null`` after re-import."""

    _instance: "NullType | None" = None

    def __new__(cls) -> "NullType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Null"

    def __bool__(self) -> bool:
        return False


Null = NullType()


class MissingType:
    """Sentinel describing a property that is absent after re-import.

    Absence is distinct from an explicit ``null``: absent properties fall back
    to the field default while explicit ``null`` overrides it.
    """

    _instance: "MissingType | None" = None

    def __new__(cls) -> "MissingType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Missing"

    def __bool__(self) -> bool:
        return False


Missing = MissingType()


class RemoveType:
    """Candidate value meaning the source property must be removed."""

    _instance: "RemoveType | None" = None

    def __new__(cls) -> "RemoveType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Remove"

    def __bool__(self) -> bool:
        return False


Remove = RemoveType()


Path = Tuple[Any, ...]


@dataclass(frozen=True)
class SourceChange:
    """One source-side property the supplier accepts to touch.

    ``candidates`` holds every value the property may take. It may contain
    :data:`Remove` to allow deletion; every other entry is a JSON value
    (``None`` is an explicit null and stays distinct from :data:`Remove`).
    A property absent from this list must be forwarded untouched.
    """

    path: Path
    candidates: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("source change path cannot be empty")
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True)
class TargetExpectation:
    """One warehouse field governed by the audit sheet.

    ``value`` is the required imported value; use :data:`Null` for an explicit
    null and :data:`Missing` for a property that must stay absent.
    """

    path: Path
    value: Any

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("target expectation path cannot be empty")


@dataclass(frozen=True)
class ReworkPatch:
    """A returnable amendment: the original document plus minimal source edits."""

    target_type: Any
    operations: Tuple["PatchOperation", ...]
    document: Any
    changed_paths: Tuple[Path, ...]
    imported: Any = None

    @property
    def size(self) -> int:
        """Number of source paths actually modified by this patch."""

        return len(self.changed_paths)


@dataclass(frozen=True)
class PatchOperation:
    path: Path
    action: str
    value: Any = None


@dataclass(frozen=True)
class ConflictPair:
    """Two warehouse fields that cannot be satisfied together.

    ``source_paths`` are the original supplier paths pinning them, with the
    options each side is allowed to take.
    """

    first: TargetExpectation
    second: TargetExpectation
    source_paths: Tuple[Path, ...]
    options: dict


@dataclass(frozen=True)
class ConflictReport:
    """Answer sent back to audit when no returnable patch exists."""

    target_type: Any
    blocked: Tuple[TargetExpectation, ...] = ()
    conflicts: Tuple[ConflictPair, ...] = ()
    influences: dict = field(default_factory=dict)
    errors: Tuple[dict, ...] = ()

    @property
    def feasible(self) -> bool:
        return not self.blocked and not self.conflicts
