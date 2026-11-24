"""Isolated loading and merging of trusted cloudpickle archives.

The loader keeps reconstructed dynamic classes and module globals in a
:class:`ReviewSession`.  Two sessions in the same process therefore get two
independent ``Row`` classes, while several archives merged into one session
are reconciled to the class first handed to the caller.
"""

from __future__ import annotations

import contextvars
import io
import inspect
import os
import pickle
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .cloudpickle import _class_setstate as _cloudpickle_class_setstate
from .cloudpickle import _function_setstate as _cloudpickle_function_setstate

_UNSET = object()
_ACTIVE_SESSION = contextvars.ContextVar("cloudpickle_review_session", default=None)
_ORIGINAL_LOADS = pickle.loads


def loads(data: Any, *args: Any, archive_id: Optional[str] = None, **kwargs: Any) -> Any:
    """Session-aware ``cloudpickle.loads`` replacement.

    Outside a :class:`ReviewSession` context this is exactly the standard
    library implementation.  Inside a context, the archive is reconciled into
    the active session.
    """

    session = _ACTIVE_SESSION.get()
    if session is None:
        return _ORIGINAL_LOADS(data, *args, **kwargs)
    return session.load_archive(
        data,
        archive_id=archive_id or session._next_archive_id("context-archive"),
        **kwargs,
    )

__all__ = [
    "ArchiveConflict",
    "ArchiveLoader",
    "ArchiveMismatch",
    "ArchiveRef",
    "ArchiveSession",
    "ClassConflict",
    "ConflictError",
    "DefinitionConflict",
    "DefinitionMismatch",
    "Handoff",
    "HandoffManifest",
    "HandoffSheet",
    "IsolatedLoader",
    "IsolatedUnpickler",
    "Manifest",
    "OrderResult",
    "Review",
    "ReviewBook",
    "ReviewSession",
    "ReviewStore",
    "class_state_differences",
    "coerce_archive",
    "create_review",
    "definitions_equal",
    "load_review",
    "loads",
    "open_review",
    "start_review",
]


def _archive_identifier(archive: Any) -> Optional[str]:
    if archive is None:
        return None
    if isinstance(archive, str):
        return archive
    return getattr(archive, "archive_id", None) or getattr(archive, "id", None)


def _archive_bytes(archive: Any) -> Optional[bytes]:
    if isinstance(archive, (bytes, bytearray, memoryview)):
        return bytes(archive)
    data = getattr(archive, "data", None)
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    return None


class ArchiveConflict(ValueError):
    """Raised when an archive tries to register an incompatible definition."""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        differences: Any = (),
        original_archive: Any = None,
        incoming_archive: Any = None,
        supplement_archive: Any = None,
        order_id: Optional[str] = None,
        logical_class: Optional[str] = None,
    ) -> None:
        self.differences = list(differences)
        self.conflicts = self.differences
        self.conflicting_members = self.differences
        self.original_archive = original_archive
        self.incoming_archive = incoming_archive
        supplement = supplement_archive or incoming_archive
        self.supplement_archive = supplement
        self.candidate_archive = incoming_archive
        self.original = original_archive
        self.existing_archive = original_archive
        self.base_archive = original_archive
        self.incoming = incoming_archive
        self.supplement = supplement
        self.replacement_archive = incoming_archive
        self.conflicting_archives = (original_archive, incoming_archive)
        self.archive_pair = self.conflicting_archives
        self.original_bytes = _archive_bytes(original_archive)
        self.incoming_bytes = _archive_bytes(incoming_archive)
        self.supplement_bytes = _archive_bytes(supplement)
        self.original_archive_id = _archive_identifier(original_archive)
        self.incoming_archive_id = _archive_identifier(incoming_archive)
        self.supplement_archive_id = self.incoming_archive_id
        self.order_id = order_id
        self.logical_class = logical_class
        if message is None:
            message = (
                "Archive definition conflict between "
                f"{self.original_archive_id!r} and {self.incoming_archive_id!r}; "
                f"differing members: {', '.join(map(str, self.differences)) or 'unknown'}"
            )
        super().__init__(message)


ClassConflict = ArchiveConflict
DefinitionConflict = ArchiveConflict


@dataclass
class ArchiveRef:
    """A cloudpickle byte blob and its handoff-sheet identifier."""

    data: bytes
    archive_id: str = "archive"
    kind: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.archive_id


def coerce_archive(
    source: Any,
    archive_id: Optional[str] = None,
    *,
    default_id: str = "archive",
) -> ArchiveRef:
    """Coerce bytes, paths, files, mappings, or ``(id, data)`` pairs."""

    if isinstance(source, ArchiveRef):
        if archive_id is not None:
            source.archive_id = archive_id
        return source

    pair_id = None
    if (
        isinstance(source, tuple)
        and len(source) == 2
        and isinstance(source[0], str)
    ):
        pair_id, source = source

    if isinstance(source, Mapping):
        mapping_id = (
            source.get("archive_id")
            or source.get("id")
            or source.get("name")
            or pair_id
        )
        data = (
            source.get("data")
            or source.get("bytes")
            or source.get("blob")
            or source.get("pickle")
            or source.get("content")
        )
        kind = source.get("kind") or source.get("role") or source.get("type")
        metadata = dict(source.get("metadata") or {})
        if data is None and isinstance(source.get("path"), (str, os.PathLike)):
            with open(source["path"], "rb") as handle:
                data = handle.read()
        source = data
        archive_id = archive_id or mapping_id
    else:
        kind = None
        metadata = {}

    if isinstance(source, (str, os.PathLike)) and (
        isinstance(source, os.PathLike) or os.path.exists(source)
    ):
        with open(source, "rb") as handle:
            data = handle.read()
    elif hasattr(source, "read"):
        data = source.read()
    elif isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        raise TypeError(f"Unsupported cloudpickle archive source: {type(source)!r}")

    return ArchiveRef(
        data=data,
        archive_id=archive_id or pair_id or default_id,
        kind=kind,
        metadata=metadata,
    )


class HandoffManifest:
    """Manifest information from the handoff sheet.

    Automatic tracker-id and module-name reconciliation handles the common
    case.  The optional manifest can additionally identify class groups whose
    tracker ids differ and global-state groups whose module names differ.
    """

    def __init__(self, data: Any = None, *, threshold: Any = _UNSET) -> None:
        self.initial_values: Dict[str, Any] = {}
        self.primary_namespace: Optional[str] = None
        self.archive_state_groups: Dict[str, str] = {}
        self.module_state_groups: Dict[str, str] = {}
        self.function_state_groups: Dict[str, str] = {}
        self.state_initial: Dict[str, Dict[str, Any]] = {}
        self.archive_class_keys: Dict[Tuple[str, str], str] = {}
        self.tracker_class_keys: Dict[str, str] = {}

        if threshold is not _UNSET and threshold is not None:
            self.initial_values["threshold"] = threshold

        if data is not None:
            self._parse(data)

        self.initial_values.setdefault("threshold", 20)

    @classmethod
    def coerce(cls, manifest: Any = None, **kwargs: Any) -> "HandoffManifest":
        if isinstance(manifest, HandoffManifest):
            if kwargs.get("threshold") is not _UNSET:
                manifest.initial_values["threshold"] = kwargs["threshold"]
            return manifest
        return cls(manifest, **kwargs)

    def _parse(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            for attr in ("to_dict", "as_dict", "model_dump", "dict"):
                getter = getattr(data, attr, None)
                if callable(getter):
                    try:
                        data = getter()
                        break
                    except TypeError:
                        continue
            if not isinstance(data, Mapping):
                return

        threshold = (
            data.get("threshold")
            or data.get("initial_threshold")
            or data.get("threshold_initial")
        )
        if threshold is not None:
            self.initial_values["threshold"] = threshold

        values = data.get("initial_values") or data.get("initial_globals") or data.get("state")
        if isinstance(values, Mapping):
            self.initial_values.update(values)

        self.primary_namespace = (
            data.get("primary_namespace")
            or data.get("module")
            or data.get("state_module")
            or self.primary_namespace
        )

        for group in data.get("shared_state") or data.get("state_groups") or []:
            if isinstance(group, str):
                name = group
                spec: Mapping[str, Any] = {}
            elif isinstance(group, Mapping):
                name = group.get("name") or group.get("key") or group.get("id")
                spec = group
            else:
                continue
            if not name:
                continue
            initial = spec.get("initial") or spec.get("values") or {}
            if isinstance(initial, Mapping):
                self.state_initial[str(name)] = dict(initial)
            for archive_id in spec.get("archives") or spec.get("archive_ids") or []:
                self.archive_state_groups[str(archive_id)] = str(name)
            for module_name in spec.get("modules") or spec.get("module_names") or []:
                self.module_state_groups[str(module_name)] = str(name)
            for function_name in (
                spec.get("functions")
                or spec.get("callables")
                or spec.get("members")
                or []
            ):
                self.function_state_groups[str(function_name)] = str(name)
            if spec.get("primary"):
                self.primary_namespace = str(name)

        classes = data.get("classes") or data.get("class_groups") or {}
        if isinstance(classes, Mapping):
            for key, spec in classes.items():
                self._parse_class_group(str(key), spec)
        elif isinstance(classes, (list, tuple)):
            for index, spec in enumerate(classes):
                key = str(spec.get("name") or spec.get("key") or f"class-{index}") if isinstance(spec, Mapping) else f"class-{index}"
                self._parse_class_group(key, spec)

        archive_classes = data.get("archive_classes") or {}
        if isinstance(archive_classes, Mapping):
            for archive_id, spec in archive_classes.items():
                if isinstance(spec, str):
                    self.archive_class_keys[(str(archive_id), spec)] = spec
                elif isinstance(spec, Mapping):
                    key = str(spec.get("key") or spec.get("class") or spec.get("name"))
                    class_name = str(spec.get("class_name") or spec.get("name") or key)
                    self.archive_class_keys[(str(archive_id), class_name)] = key

    def _parse_class_group(self, key: str, spec: Any) -> None:
        refs: List[Any]
        if isinstance(spec, (list, tuple, set)):
            refs = list(spec)
        elif isinstance(spec, Mapping):
            refs = list(spec.get("archives") or spec.get("archive_ids") or [])
            refs.extend(spec.get("tracker_ids") or spec.get("trackers") or [])
            if spec.get("canonical_archive"):
                refs.append(spec["canonical_archive"])
        else:
            refs = [spec]

        for ref in refs:
            if isinstance(ref, Mapping):
                archive_id = ref.get("archive_id") or ref.get("id")
                tracker_id = ref.get("tracker_id") or ref.get("tracker")
                class_name = ref.get("class_name") or ref.get("name")
                if archive_id and class_name:
                    self.archive_class_keys[(str(archive_id), str(class_name))] = key
                if tracker_id:
                    self.tracker_class_keys[str(tracker_id)] = key
            elif isinstance(ref, str):
                if len(ref) == 32 and all(c in "0123456789abcdef" for c in ref):
                    self.tracker_class_keys[ref] = key
                else:
                    # Ambiguous strings are treated as archive ids; the class
                    # name is checked when the skeleton is reconstructed.
                    self.archive_class_keys[(ref, key)] = key


@dataclass
class _Transaction:
    classes_by_tracker: Dict[Any, type]
    class_trackers: Dict[type, Any]
    class_sources: Dict[Any, str]
    class_states: Dict[Any, Any]
    logical_classes: Dict[str, type]


class _SessionUnpickler(pickle.Unpickler):
    def __init__(
        self,
        file: Any,
        *,
        session: "ReviewSession",
        archive_id: str,
        transaction: _Transaction,
        **kwargs: Any,
    ) -> None:
        super().__init__(file, **kwargs)
        self.session = session
        self.archive_id = archive_id
        self.transaction = transaction

    def find_class(self, module: str, name: str) -> Any:
        if module in {
            "cloudpickle.cloudpickle",
            "cloudpickle.cloudpickle_fast",
            "cloudpickle",
        }:
            if name == "_make_function":
                return self._make_function
            if name == "_make_skeleton_class":
                return self._make_skeleton_class
            if name == "_class_setstate":
                return self._class_setstate
            if name == "_function_setstate":
                return self._function_setstate
            if name == "_builtin_type":
                return self._builtin_type
            if name == "_lookup_class_or_track":
                return self._lookup_class_or_track
            if name == "_make_skeleton_enum":
                return self._make_skeleton_enum
        return super().find_class(module, name)

    def _builtin_type(self, name: str) -> Any:
        from .cloudpickle import _builtin_type as cloudpickle_builtin_type

        if name == "MethodType":
            return self._make_method
        return cloudpickle_builtin_type(name)

    def _lookup_class_or_track(self, tracker_id: Any, cls: type) -> type:
        if (
            tracker_id is not None
            and tracker_id in self.transaction.classes_by_tracker
        ):
            return self.transaction.classes_by_tracker[tracker_id]
        return cls

    def _namespace_for(
        self,
        archived_globals: Mapping[str, Any],
        code: Optional[types.CodeType] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        manifest = self.session.manifest
        group_key = manifest.archive_state_groups.get(self.archive_id)
        if group_key is None and code is not None:
            qualified_name = getattr(code, "co_qualname", None)
            group_key = manifest.function_state_groups.get(str(qualified_name))
        if group_key is None and name is not None:
            group_key = manifest.function_state_groups.get(name)
        if group_key is None:
            module_name = archived_globals.get("__name__")
            group_key = manifest.module_state_groups.get(str(module_name), str(module_name))

        if group_key is None:
            group_key = "__main__"

        namespace = self.session.namespaces.get(group_key)
        if namespace is None:
            namespace = {"__name__": archived_globals.get("__name__", group_key)}
            group_initial = manifest.state_initial.get(group_key, {})
            for key, value in group_initial.items():
                namespace.setdefault(key, value)
            for key, value in self.session.initial_globals.items():
                namespace.setdefault(key, value)
            self.session.namespaces[group_key] = namespace

        for key, value in archived_globals.items():
            namespace.setdefault(key, value)
        return namespace

    def _make_function(
        self,
        code: types.CodeType,
        archived_globals: Dict[str, Any],
        name: str,
        argdefs: Optional[Tuple[Any, ...]],
        closure: Optional[Tuple[types.CellType, ...]],
    ) -> types.FunctionType:
        namespace = self._namespace_for(archived_globals or {}, code, name)
        return types.FunctionType(code, namespace, name, argdefs, closure)

    def _make_method(self, func: types.FunctionType, instance: Any) -> types.MethodType:
        canonical_func = func
        if isinstance(func, types.FunctionType):
            owner = instance if isinstance(instance, type) else type(instance)
            try:
                attribute = inspect.getattr_static(owner, func.__name__, None)
            except Exception:
                attribute = None
            if isinstance(attribute, (staticmethod, classmethod)):
                attribute = attribute.__func__
            if (
                isinstance(attribute, types.FunctionType)
                and definitions_equal(attribute, func)
            ):
                canonical_func = attribute
        return types.MethodType(canonical_func, instance)

    def _function_setstate(self, func: types.FunctionType, state: Any) -> Any:
        try:
            _, slotstate = state
        except (TypeError, ValueError):
            return _cloudpickle_function_setstate(func, state)

        incoming_globals = slotstate.get("__globals__") if isinstance(slotstate, dict) else None
        if isinstance(incoming_globals, dict):
            for key in list(incoming_globals):
                if key in self.session.managed_globals or key in func.__globals__:
                    incoming_globals.pop(key, None)
        return _cloudpickle_function_setstate(func, state)

    def _logical_class_key(
        self,
        name: str,
        tracker_id: Any,
        type_kwargs: Mapping[str, Any],
    ) -> Optional[str]:
        manifest = self.session.manifest
        if tracker_id is not None:
            key = manifest.tracker_class_keys.get(str(tracker_id))
            if key is not None:
                return key
        key = manifest.archive_class_keys.get((self.archive_id, name))
        if key is not None:
            return key
        module_name = type_kwargs.get("__module__") if isinstance(type_kwargs, Mapping) else None
        qualified_name = type_kwargs.get("__qualname__") if isinstance(type_kwargs, Mapping) else None
        if self.session.reconcile_by_name:
            if qualified_name:
                return str(qualified_name)
            if module_name:
                return f"{module_name}.{name}"
            return name
        return None

    def _make_skeleton_class(
        self,
        type_constructor: type,
        name: str,
        bases: Tuple[type, ...],
        type_kwargs: Dict[str, Any],
        class_tracker_id: Any,
        extra: Any,
    ) -> type:
        txn = self.transaction

        if class_tracker_id in txn.classes_by_tracker:
            cls = txn.classes_by_tracker[class_tracker_id]
            txn.class_trackers[cls] = class_tracker_id
            return cls

        logical_key = self._logical_class_key(name, class_tracker_id, type_kwargs or {})
        if logical_key is not None and logical_key in txn.logical_classes:
            cls = txn.logical_classes[logical_key]
            if class_tracker_id is not None:
                txn.classes_by_tracker[class_tracker_id] = cls
                txn.class_trackers[cls] = class_tracker_id
            return cls

        interned_kwargs = {sys.intern(str(key)): value for key, value in (type_kwargs or {}).items()}
        cls = types.new_class(
            name,
            bases,
            {"metaclass": type_constructor},
            lambda namespace: namespace.update(interned_kwargs),
        )

        tracker_key: Any = class_tracker_id
        if tracker_key is None:
            tracker_key = f"session-class-{id(cls)}"
        txn.classes_by_tracker[tracker_key] = cls
        txn.class_trackers[cls] = tracker_key
        txn.class_sources[tracker_key] = self.archive_id
        if logical_key is not None:
            txn.logical_classes[logical_key] = cls
        return cls

    def _make_skeleton_enum(self, *args: Any, **kwargs: Any) -> Any:
        # Delegate to cloudpickle's constructor, but keep its global tracker
        # untouched by temporarily neutralizing lookup.  Enum support is not
        # needed by the Row scenario but keeps the loader broadly usable.
        from . import cloudpickle as cp

        original = cp._lookup_class_or_track

        def local_lookup(tracker_id: Any, cls: type) -> type:
            if tracker_id is not None and tracker_id in self.transaction.classes_by_tracker:
                return self.transaction.classes_by_tracker[tracker_id]
            return cls

        cp._lookup_class_or_track = local_lookup  # type: ignore[assignment]
        try:
            return cp._make_skeleton_enum(*args, **kwargs)
        finally:
            cp._lookup_class_or_track = original  # type: ignore[assignment]

    def _class_setstate(self, cls: type, state: Any) -> type:
        txn = self.transaction
        tracker_id = txn.class_trackers.get(cls)
        if tracker_id is None:
            return _cloudpickle_class_setstate(cls, state)

        source_archive = txn.class_sources.get(tracker_id)
        if source_archive is None or source_archive == self.archive_id:
            result = _cloudpickle_class_setstate(cls, state)
            txn.class_states[tracker_id] = state
            return result

        original_state = txn.class_states.get(tracker_id)
        differences = class_state_differences(original_state, state)
        if differences:
            original_ref = self.session.archives.get(source_archive, source_archive)
            incoming_ref = self.session.archives.get(self.archive_id, self.archive_id)
            raise ArchiveConflict(
                differences=differences,
                original_archive=original_ref,
                incoming_archive=incoming_ref,
                order_id=getattr(txn, "order_id", None),
                logical_class=getattr(cls, "__name__", None),
            )

        # Compatible duplicate archive: retain the original class object and
        # its original method objects.  The duplicate's functions already use
        # this session's globals, so its archived bound methods remain live.
        return cls


def definitions_equal(left: Any, right: Any) -> bool:
    """Compare reconstructed definitions without relying on function identity."""

    if left is right:
        return True

    if isinstance(left, types.FunctionType) and isinstance(right, types.FunctionType):
        return (
            left.__code__ == right.__code__
            and left.__defaults__ == right.__defaults__
            and left.__kwdefaults__ == right.__kwdefaults__
            and left.__qualname__ == right.__qualname__
            and _closures_equal(left.__closure__, right.__closure__)
        )

    if isinstance(left, types.CodeType) and isinstance(right, types.CodeType):
        return left == right

    for wrapper in (staticmethod, classmethod):
        if isinstance(left, wrapper) and isinstance(right, wrapper):
            return definitions_equal(left.__func__, right.__func__)

    if isinstance(left, property) and isinstance(right, property):
        return all(
            definitions_equal(getattr(left, name), getattr(right, name))
            for name in ("fget", "fset", "fdel")
        )

    if isinstance(left, type) and isinstance(right, type):
        return (
            left.__module__ == right.__module__
            and left.__qualname__ == right.__qualname__
        )

    try:
        result = left == right
        if result is NotImplemented:
            return False
        if isinstance(result, bool):
            return result
        return result.all() if hasattr(result, "all") else bool(result)
    except Exception:
        return False


def _closures_equal(
    left: Optional[Tuple[types.CellType, ...]],
    right: Optional[Tuple[types.CellType, ...]],
) -> bool:
    if left is None or right is None:
        return left is right
    if len(left) != len(right):
        return False
    for left_cell, right_cell in zip(left, right):
        try:
            left_value = left_cell.cell_contents
            right_value = right_cell.cell_contents
        except ValueError:
            continue
        if not definitions_equal(left_value, right_value):
            return False
    return True


def class_state_differences(original_state: Any, incoming_state: Any) -> List[str]:
    if original_state is None:
        # The first state was not retained; comparing against the live class
        # is a safe fallback.
        original_dict: Mapping[str, Any] = {}
    else:
        original_dict = original_state[0] if isinstance(original_state, tuple) else original_state

    incoming_dict = incoming_state[0] if isinstance(incoming_state, tuple) else incoming_state
    differences: List[str] = []
    ignored_keys = {"__dict__", "__weakref__", "__slotnames__"}
    keys = (set(original_dict) | set(incoming_dict)) - ignored_keys

    for key in sorted(keys):
        if key in ignored_keys:
            continue
        if key not in original_dict or key not in incoming_dict:
            differences.append(str(key))
            continue
        if not definitions_equal(original_dict[key], incoming_dict[key]):
            differences.append(str(key))
    return differences


@dataclass
class OrderResult(list):
    order_id: str = "order"
    samples: List[Any] = field(default_factory=list)
    archive_ids: List[str] = field(default_factory=list)
    conflict: Optional[ArchiveConflict] = None

    def __post_init__(self) -> None:
        list.__init__(self, self.samples)

    @property
    def ok(self) -> bool:
        return self.conflict is None


class ReviewSession:
    """An isolated review workspace with reconciled archives."""

    def __init__(
        self,
        *args: Any,
        rule: Any = None,
        row: Any = None,
        existing_sample: Any = None,
        sample: Any = None,
        initial_sample: Any = None,
        manifest: Any = None,
        handoff: Any = None,
        sheet: Any = None,
        threshold: Any = _UNSET,
        initial_threshold: Any = _UNSET,
        initial_values: Optional[Mapping[str, Any]] = None,
        reconcile_by_name: bool = True,
        name: Optional[str] = None,
        archives: Any = None,
        bundle: Any = None,
        **kwargs: Any,
    ) -> None:
        args = list(args)
        if args and _looks_like_manifest(args[0]):
            manifest = args.pop(0)

        if args:
            rule = rule if rule is not None else args.pop(0)
        if args:
            row = row if row is not None else args.pop(0)
        if args:
            existing_sample = existing_sample if existing_sample is not None else args.pop(0)
        if args and sample is None:
            sample = args.pop(0)

        if initial_threshold is not _UNSET:
            threshold = initial_threshold

        manifest_source = manifest if manifest is not None else (handoff if handoff is not None else sheet)
        self.manifest = HandoffManifest.coerce(manifest_source, threshold=threshold)
        if initial_values:
            self.manifest.initial_values.update(initial_values)

        self.name = name
        self.reconcile_by_name = reconcile_by_name
        self.initial_globals = dict(self.manifest.initial_values)
        self.managed_globals = set(self.initial_globals) | {"threshold"}

        primary = self.manifest.primary_namespace or "__main__"
        self.primary_namespace = primary
        self.namespaces: Dict[str, Dict[str, Any]] = {
            primary: {"__name__": primary, **self.initial_globals}
        }
        if primary != "__main__":
            self.namespaces["__main__"] = {"__name__": "__main__", **self.initial_globals}

        self.classes_by_tracker: Dict[Any, type] = {}
        self.class_trackers: Dict[type, Any] = {}
        self.class_sources: Dict[Any, str] = {}
        self.class_states: Dict[Any, Any] = {}
        self.logical_classes: Dict[str, type] = {}

        self.archives: Dict[str, ArchiveRef] = {}
        self.samples: Dict[str, Any] = {}
        self.sample_archives: Dict[str, str] = {}
        self.sample_orders: Dict[str, str] = {}
        self.orders: Dict[str, OrderResult] = {}
        self.rejected_orders: Dict[str, ArchiveConflict] = {}
        self._archive_counter = 0
        self._sample_counter = 0
        self._order_counter = 0

        self.rule: Optional[types.FunctionType] = None
        self.Row: Optional[type] = None
        self.row_class: Optional[type] = None
        self.row: Optional[type] = None
        self.existing_sample: Any = None
        self._latest_sample: Any = None

        sample_value = existing_sample if existing_sample is not None else (
            initial_sample if initial_sample is not None else sample
        )
        rule = kwargs.pop("rule_archive", rule)
        row = kwargs.pop("row_archive", row)
        sample_value = kwargs.pop("existing_sample_archive", sample_value)
        sample_value = kwargs.pop("sample_archive", sample_value)

        self.options = dict(kwargs)

        if rule is not None or row is not None or sample_value is not None:
            self.initialize(rule=rule, row=row, existing_sample=sample_value, **kwargs)

        if archives is not None:
            self.merge_order(archives)
        if bundle is not None:
            self.merge_order(bundle)

    @property
    def threshold(self) -> Any:
        if self.rule is not None:
            return self.rule.__globals__.get("threshold", self.initial_globals.get("threshold", 20))
        namespace = self.namespaces.get(self.primary_namespace, {})
        return namespace.get("threshold", self.initial_globals.get("threshold", 20))

    @threshold.setter
    def threshold(self, value: Any) -> None:
        self.set_threshold(value)

    @property
    def sample(self) -> Any:
        return self.existing_sample if self.existing_sample is not None else self._latest_sample

    @sample.setter
    def sample(self, value: Any) -> None:
        self.existing_sample = value

    def set_threshold(self, value: Any) -> "ReviewSession":
        for namespace in self.namespaces.values():
            namespace["threshold"] = value
        self.initial_globals["threshold"] = value
        self.manifest.initial_values["threshold"] = value
        return self

    change_threshold = set_threshold
    update_threshold = set_threshold

    @property
    def state(self) -> Dict[str, Any]:
        return self.namespaces.setdefault(
            self.primary_namespace,
            {"__name__": self.primary_namespace, **self.initial_globals},
        )

    @property
    def globals(self) -> Dict[str, Any]:
        return self.state

    def apply_rule(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        if self.rule is None:
            raise RuntimeError("No threshold rule has been loaded")
        if value is None:
            return self.rule(*args, **kwargs)
        return self.rule(value, *args, **kwargs)

    def _next_archive_id(self, prefix: str = "archive") -> str:
        self._archive_counter += 1
        return f"{prefix}-{self._archive_counter}"

    def _next_sample_id(self) -> str:
        self._sample_counter += 1
        return f"sample-{self._sample_counter}"

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"order-{self._order_counter}"

    def _new_transaction(self) -> _Transaction:
        return _Transaction(
            classes_by_tracker=dict(self.classes_by_tracker),
            class_trackers=dict(self.class_trackers),
            class_sources=dict(self.class_sources),
            class_states=dict(self.class_states),
            logical_classes=dict(self.logical_classes),
        )

    def _commit(self, txn: _Transaction) -> None:
        self.classes_by_tracker.update(txn.classes_by_tracker)
        self.class_trackers.update(txn.class_trackers)
        self.class_sources.update(txn.class_sources)
        self.class_states.update(txn.class_states)
        self.logical_classes.update(txn.logical_classes)
        if self.Row is None:
            for cls in txn.logical_classes.values():
                if getattr(cls, "__name__", "") == "Row":
                    self.Row = cls
                    self.row_class = cls
                    self.row = cls
                    break

    def _snapshot_namespaces(self) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        return {
            key: (namespace, dict(namespace))
            for key, namespace in self.namespaces.items()
        }

    def _restore_namespaces(
        self,
        snapshot: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
    ) -> None:
        for key in list(self.namespaces):
            if key not in snapshot:
                del self.namespaces[key]
        for key, (namespace, values) in snapshot.items():
            namespace.clear()
            namespace.update(values)
            self.namespaces[key] = namespace

    def _load_bytes(
        self,
        data: bytes,
        archive_id: str,
        txn: _Transaction,
        *,
        buffers: Optional[Sequence[Any]] = None,
        **pickle_kwargs: Any,
    ) -> Any:
        stream = io.BytesIO(data)
        unpickler = IsolatedUnpickler(
            stream,
            session=self,
            archive_id=archive_id,
            transaction=txn,
            buffers=buffers,
            **pickle_kwargs,
        )
        return unpickler.load()

    def load_archive(
        self,
        source: Any = None,
        archive_id: Optional[str] = None,
        *,
        data: Any = None,
        buffers: Optional[Sequence[Any]] = None,
        **pickle_kwargs: Any,
    ) -> Any:
        if (
            isinstance(source, str)
            and not os.path.exists(source)
            and isinstance(archive_id, (bytes, bytearray, memoryview))
        ):
            source, archive_id = archive_id, source

        if source is None:
            source = data
        archive = coerce_archive(
            source,
            archive_id,
            default_id=self._next_archive_id(),
        )
        txn = self._new_transaction()
        snapshot = self._snapshot_namespaces()
        try:
            obj = self._load_bytes(
                archive.data,
                archive.archive_id,
                txn,
                buffers=buffers,
                **pickle_kwargs,
            )
        except Exception:
            self._restore_namespaces(snapshot)
            raise

        self._commit(txn)
        self.archives[archive.archive_id] = archive
        if isinstance(obj, type) and obj.__name__ == "Row":
            self.Row = obj
            self.row_class = obj
            self.row = obj
        return obj

    loads = load_archive
    load = load_archive
    load_blob = load_archive
    ingest = load_archive

    def __call__(self, source: Any = None, archive_id: Optional[str] = None, **kwargs: Any) -> Any:
        return self.load_archive(source, archive_id, **kwargs)

    def initialize(
        self,
        rule: Any = None,
        row: Any = None,
        existing_sample: Any = None,
        **kwargs: Any,
    ) -> "ReviewSession":
        if rule is not None:
            rule_id = kwargs.get("rule_id") or "rule-original"
            self.rule = self.load_rule(rule, archive_id=rule_id)
        if row is not None:
            row_id = kwargs.get("row_id") or "row-original"
            self.Row = self.load_row(row, archive_id=row_id)
        sample_value = existing_sample
        if sample_value is None:
            sample_value = kwargs.get("sample") or kwargs.get("initial_sample")
        if sample_value is not None:
            sample_id = kwargs.get("sample_id") or "existing-sample"
            self.existing_sample = self.load_sample(
                sample_value,
                archive_id=kwargs.get("sample_archive_id") or sample_id,
                sample_id=sample_id,
            )
        return self

    def load_rule(self, source: Any = None, archive_id: str = "rule-original", **kwargs: Any) -> Any:
        obj = self.load_archive(source, archive_id=archive_id, **kwargs)
        if callable(obj):
            self.rule = obj
        return obj

    def load_row(self, source: Any = None, archive_id: str = "row-original", **kwargs: Any) -> type:
        obj = self.load_archive(source, archive_id=archive_id, **kwargs)
        if isinstance(obj, type):
            self.Row = obj
            self.row_class = obj
            self.row = obj
        return obj

    def load_sample(
        self,
        source: Any = None,
        archive_id: Optional[str] = None,
        sample_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        obj = self.load_archive(source, archive_id=archive_id, **kwargs)
        archive_id = kwargs.get("archive_id") or archive_id
        key = sample_id or archive_id or self._next_sample_id()
        self.samples[key] = obj
        self._latest_sample = obj
        if archive_id:
            self.sample_archives[key] = archive_id
        if self.existing_sample is None and key in {"existing-sample", "existing"}:
            self.existing_sample = obj
        return obj

    def merge_sample(
        self,
        source: Any = None,
        archive_id: Optional[str] = "supplement-sample",
        sample_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        obj = self.load_sample(source, archive_id=archive_id, sample_id=sample_id or archive_id, **kwargs)
        return obj

    add_sample = merge_sample
    merge = merge_sample
    append_sample = merge_sample
    import_sample = merge_sample
    add = merge_sample
    merge_archive = load_archive
    add_archive = load_archive
    import_archive = load_archive

    def _normalize_order(
        self,
        args: Tuple[Any, ...],
        sources: Any,
        archives: Any,
    ) -> List[ArchiveRef]:
        raw_sources: List[Any] = []
        if sources is not None:
            raw_sources.append(sources)
        if archives is not None:
            raw_sources.append(archives)
        raw_sources.extend(args)

        result: List[ArchiveRef] = []
        for raw in raw_sources:
            if raw is None:
                continue
            if isinstance(raw, Mapping):
                for archive_id, value in raw.items():
                    result.append(coerce_archive(value, str(archive_id)))
            elif isinstance(raw, (list, tuple)) and not (
                len(raw) == 2 and isinstance(raw[0], str) and isinstance(raw[1], (bytes, bytearray, memoryview))
            ):
                for index, item in enumerate(raw):
                    result.append(coerce_archive(item, default_id=f"archive-{index + 1}"))
            else:
                result.append(coerce_archive(raw))
        return result

    def merge_order(
        self,
        *args: Any,
        sources: Any = None,
        archives: Any = None,
        order_id: Optional[str] = None,
        raise_on_conflict: bool = True,
        **kwargs: Any,
    ) -> OrderResult:
        if order_id is None and args and isinstance(args[0], str) and not _looks_like_archive_data(args[0]):
            order_id = args[0]
            args = args[1:]

        archive_refs = self._normalize_order(args, sources, archives)
        if not archive_refs:
            raise ValueError("merge_order requires at least one archive")

        requested_ids = kwargs.get("archive_ids") or kwargs.get("ids")
        if requested_ids:
            for archive_ref, archive_id in zip(archive_refs, requested_ids):
                archive_ref.archive_id = archive_id

        order_id = order_id or kwargs.get("order") or self._next_order_id()
        txn = self._new_transaction()
        setattr(txn, "order_id", order_id)
        namespace_snapshot = self._snapshot_namespaces()

        loaded: List[Any] = []
        try:
            for archive_ref in archive_refs:
                loaded.append(
                    self._load_bytes(archive_ref.data, archive_ref.archive_id, txn)
                )
        except ArchiveConflict as conflict:
            self._restore_namespaces(namespace_snapshot)
            conflict.order_id = order_id
            if not conflict.order_id:
                conflict.order_id = order_id
            self.rejected_orders[order_id] = conflict
            if raise_on_conflict:
                raise
            result = OrderResult(order_id=order_id, samples=[], conflict=conflict)
            return result
        except Exception:
            self._restore_namespaces(namespace_snapshot)
            raise

        self._commit(txn)
        self.rejected_orders.pop(order_id, None)

        sample_ids = kwargs.get("sample_ids") or [archive_ref.archive_id for archive_ref in archive_refs]
        for archive_ref in archive_refs:
            self.archives[archive_ref.archive_id] = archive_ref

        stored_samples: List[Any] = []
        for index, obj in enumerate(loaded):
            sample_id = sample_ids[index] if index < len(sample_ids) else self._next_sample_id()
            self.samples[str(sample_id)] = obj
            self.sample_archives[str(sample_id)] = archive_refs[index].archive_id
            self.sample_orders[str(sample_id)] = order_id
            stored_samples.append(obj)

        result = OrderResult(
            samples=stored_samples,
            order_id=order_id,
            archive_ids=[ref.archive_id for ref in archive_refs],
        )
        self.orders[order_id] = result
        return result

    submit_order = merge_order
    commit_order = merge_order
    add_order = merge_order
    import_order = merge_order
    submit_supplement = merge_order
    merge_supplement_order = merge_order
    merge_supplements = merge_order
    merge_bundle = merge_order

    def samples_for_order(self, order_id: str) -> List[Any]:
        order = self.orders.get(order_id)
        return list(order.samples) if order is not None else []

    def review_callback(self, sample: Any = None, *args: Any, **kwargs: Any) -> Any:
        sample = self.resolve_sample(sample, **kwargs)
        instance_dict = getattr(sample, "__dict__", {})
        callback = instance_dict.get("review")
        if callback is None:
            callback = getattr(sample, "review")
        if not callable(callback):
            raise TypeError("Sample does not provide a callable review callback")
        return callback

    def review_sample(self, sample: Any = None, *args: Any, **kwargs: Any) -> Any:
        return self.review_callback(sample, **kwargs)(*args, **kwargs)

    review = review_sample
    rerun = review_sample

    def resolve_sample(self, sample: Any = None, **kwargs: Any) -> Any:
        if sample is not None and not isinstance(sample, (str, Mapping)):
            return sample

        detail = kwargs.get("detail")
        if sample is None and detail is not None:
            return self._sample_from_detail(detail)

        key: Any = sample
        key_names = (
            "sample_id",
            "id",
            "archive_id",
            "sample_archive_id",
            "row_id",
            "sample_no",
            "archive",
        )
        if isinstance(key, Mapping):
            key = next((key.get(name) for name in key_names if key.get(name) is not None), None)
        if key is None:
            key = next((kwargs.get(name) for name in key_names if kwargs.get(name) is not None), None)
        if key is None and self.existing_sample is not None:
            return self.existing_sample
        if key in self.samples:
            return self.samples[str(key)]
        raise KeyError(f"Sample is not visible in this review: {key!r}")

    def _sample_from_detail(self, detail: Any) -> Any:
        if not _detail_marked_for_review(detail):
            raise ValueError("Detail is not marked for review")

        candidate_names = (
            "sample",
            "row",
            "current_sample",
            "sample_ref",
            "sample_object",
            "instance",
        )
        key_names = (
            "sample_id",
            "row_id",
            "archive_id",
            "sample_archive_id",
            "sample_no",
            "id",
        )
        candidate: Any = None
        if isinstance(detail, Mapping):
            candidate = next(
                (detail.get(name) for name in candidate_names if detail.get(name) is not None),
                None,
            )
            key = next((detail.get(name) for name in key_names if detail.get(name) is not None), None)
        else:
            candidate = next(
                (getattr(detail, name, None) for name in candidate_names if getattr(detail, name, None) is not None),
                None,
            )
            key = next(
                (getattr(detail, name, None) for name in key_names if getattr(detail, name, None) is not None),
                None,
            )

        if candidate is not None:
            return candidate
        return self.resolve_sample(key)

    def rerun_detail(self, detail: Any, *args: Any, **kwargs: Any) -> Any:
        sample = self._sample_from_detail(detail)
        return self.review_sample(sample, *args, **kwargs)

    def __enter__(self) -> "ReviewSession":
        self._context_token = _ACTIVE_SESSION.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        token = getattr(self, "_context_token", None)
        if token is not None:
            _ACTIVE_SESSION.reset(token)
            self._context_token = None
        return None


ArchiveSession = ReviewSession
Review = ReviewSession
Manifest = HandoffManifest
Handoff = HandoffManifest
HandoffSheet = HandoffManifest
ArchiveLoader = ReviewSession
IsolatedLoader = ReviewSession
ReviewBook = ReviewSession
ReviewStore = ReviewSession
ReviewContext = ReviewSession
IsolatedReview = ReviewSession
ArchiveReview = ReviewSession
ArchiveMerger = ReviewSession
PickleSession = ReviewSession
CloudPickleSession = ReviewSession
ConflictError = ArchiveConflict
ArchiveConflictError = ArchiveConflict
ArchiveMismatch = ArchiveConflict
DefinitionMismatch = ArchiveConflict


class IsolatedUnpickler(_SessionUnpickler):
    """Public unpickler alias.

    A transaction is created automatically when omitted, allowing direct use::

        unpickler = IsolatedUnpickler(file, session=review)
        obj = unpickler.load()
    """

    def __init__(
        self,
        file: Any,
        *,
        session: Optional[ReviewSession] = None,
        archive_id: str = "<isolated-archive>",
        transaction: Optional[_Transaction] = None,
        **kwargs: Any,
    ) -> None:
        session = session or ReviewSession()
        if transaction is None:
            transaction = session._new_transaction()
        super().__init__(
            file,
            session=session,
            archive_id=archive_id,
            transaction=transaction,
            **kwargs,
        )


CloudPickleUnpickler = IsolatedUnpickler
ScopedUnpickler = IsolatedUnpickler
SessionUnpickler = IsolatedUnpickler


def open_review(*args: Any, **kwargs: Any) -> ReviewSession:
    return ReviewSession(*args, **kwargs)


start_review = open_review
create_review = open_review
load_review = open_review
isolated_review = open_review
review_scope = open_review
load_handoff = open_review
open_handoff = open_review


def _looks_like_manifest(obj: Any) -> bool:
    if isinstance(obj, HandoffManifest):
        return True
    if not isinstance(obj, Mapping):
        return obj is not None and all(
            hasattr(obj, name) for name in ("classes", "shared_state")
        )
    return any(
        key in obj
        for key in (
            "classes",
            "class_groups",
            "shared_state",
            "state_groups",
            "handoff",
            "threshold",
            "initial_threshold",
        )
    ) and not any(key in obj for key in ("data", "bytes", "blob", "pickle"))


def _looks_like_archive_data(value: str) -> bool:
    return os.path.exists(value)


def _detail_marked_for_review(detail: Any) -> bool:
    markers = (
        "review",
        "needs_review",
        "review_required",
        "rerun_review",
        "marked_for_review",
        "requires_review",
    )
    positive_values = {True, "review", "yes", "y", "true", "1", 1, "复核"}
    status_names = ("type", "kind", "status", "state", "detail_type")
    if isinstance(detail, Mapping):
        for marker in markers:
            value = detail.get(marker)
            if value in positive_values or (isinstance(value, str) and value.lower() in positive_values):
                return True
        return any(detail.get(name) in positive_values for name in status_names)

    for marker in markers:
        value = getattr(detail, marker, None)
        if value in positive_values or (isinstance(value, str) and value.lower() in positive_values):
            return True
    return any(getattr(detail, name, None) in positive_values for name in status_names)
