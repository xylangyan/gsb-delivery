__all__ = ["ImportEntry", "ImportResult", "expectation_holds", "import_document"]

from dataclasses import dataclass
from typing import Any, Optional

from apischema import ValidationError, deserialize

from .documents import get_path, has_path
from .model import Missing, Null, TargetExpectation

_FORWARDED_KWARGS = (
    "additional_properties",
    "aliaser",
    "coerce",
    "conversion",
    "default_conversion",
    "fall_back_on_default",
    "no_copy",
    "pass_through",
    "schema",
)


@dataclass(frozen=True)
class ImportEntry:
    """The original import gateway and its local rules.

    Only rules handed by the caller are ever used; the rework engine never
    invents conversions, coercions or validators. ``validators`` are local
    callables run on the imported object (raising
    :class:`apischema.ValidationError` to reject it), which keeps them
    independent of apischema's per-call validator wrapping.
    """

    target_type: Any
    additional_properties: Optional[bool] = None
    aliaser: Any = None
    coerce: Any = None
    conversion: Any = None
    default_conversion: Any = None
    fall_back_on_default: Optional[bool] = None
    no_copy: Optional[bool] = None
    pass_through: Any = None
    schema: Any = None
    validators: tuple = ()

    def _kwargs(self) -> dict:
        return {
            name: value
            for name in _FORWARDED_KWARGS
            if (value := getattr(self, name)) is not None
        }


@dataclass(frozen=True)
class ImportResult:
    imported: Any
    document: Any
    errors: tuple = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _run_validators(imported: Any, validators: tuple) -> tuple:
    errors = []
    for rule in validators:
        try:
            validate = getattr(rule, "validate", rule)
            validate(imported)
        except ValidationError as error:
            errors.extend(error.errors)
        except (TypeError, ValueError) as error:
            errors.append({"err": str(error)})
    return tuple(errors)


def import_document(entry: ImportEntry, document: Any) -> ImportResult:
    """Re-feed a document through the original apischema import entry."""

    try:
        imported = deserialize(entry.target_type, document, **entry._kwargs())
    except ValidationError as error:
        return ImportResult(None, document, tuple(error.errors))
    except (TypeError, ValueError) as error:
        return ImportResult(None, document, ({"err": str(error)},))
    errors = _run_validators(imported, entry.validators)
    if errors:
        return ImportResult(None, document, errors)
    return ImportResult(imported, document)


def expectation_holds(document: Any, expectation: TargetExpectation) -> bool:
    """Check a target value, keeping explicit null and absence distinct."""

    if expectation.value is Null:
        return has_path(document, expectation.path) and get_path(
            document, expectation.path
        ) is None
    if expectation.value is Missing:
        return not has_path(document, expectation.path)
    if not has_path(document, expectation.path):
        return False
    return get_path(document, expectation.path) == expectation.value
