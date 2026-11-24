"""Manifest-licensed, auditable exports of rules and their dependencies.

The regular :func:`cloudpickle.dumps` makes by-value/by-name decisions from a
process-wide registry (``register_pickle_by_value``) and gives no feedback
about what was actually written into a payload. This module implements a
stricter workflow for teams that ship rules to offline machines:

1. A receiver job is first registered against an :class:`ExportManifest`,
   which records explicit *licenses* describing which module revisions may be
   embedded into the payload (pickled by value) and which must instead be
   loaded by name from a pre-installed library of the same version (pickled
   by reference).

2. An :class:`ExportPickler` performs the actual serialization later,
   possibly while other exports run concurrently. The pickler only honors the
   licenses of its manifest: the global by-value registry is ignored for the
   duration of the export, so a by-value module temporarily registered by
   another job can never leak into this payload.

3. Every distinct function, class or module that is actually written into the
   payload is observed exactly once while its reducer runs. Objects built
   lazily inside a custom ``__reduce__`` (and their own nested dependencies)
   are therefore discovered too. The resulting :class:`ExportReport` lists,
   item by item, the real by-value sources embedded in the payload and the
   module versions the receiver must have pre-installed for by-name items.

4. Before the payload is accepted, the pickler verifies that every written
   third-party or local dependency is covered by a license of the right kind
   and that two different revisions of a same-named class or module can never
   disagree on how they are resolved at load time. Any failure raises
   :class:`ExportError` carrying the offending dependencies; the partially
   written bytes are not a valid delivery and the in-progress payload is left
   untouched for review.

Standard library modules (including ``builtins``) and cloudpickle's own
reconstructors are always implicitly loaded by name: cloudpickle already
requires the same Python and cloudpickle version on both ends, so they need
no explicit license. They are still itemized in the report for full
auditability.
"""

import contextlib
import importlib.metadata
import io
import pickle
import sys
import threading
import types
import uuid
import weakref

from .cloudpickle import (
    Pickler,
    _add_object_observer,
    _get_pickle_by_value_override,
    _lookup_module_and_qualname,
    _remove_object_observer,
    _set_pickle_by_value_override,
    _should_pickle_by_reference,
)

# Serialization kinds used in reports.
BY_VALUE = "by_value"
BY_NAME = "by_name"

_STDLIB_NAMES = getattr(sys, "stdlib_module_names", frozenset())


def _is_stdlib_module(module):
    top_level = module.__name__.split(".", 1)[0]
    return top_level == "builtins" or top_level in _STDLIB_NAMES


def _is_cloudpickle_module(module):
    # cloudpickle's own reconstructors are always loaded by name from the
    # same cloudpickle revision, which is itself a requirement of any
    # cloudpickle payload. They are framework plumbing, not licensed content.
    name = module.__name__
    return name == "cloudpickle" or name.startswith("cloudpickle.")


def _is_stdlib_name(module_name):
    top_level = module_name.split(".", 1)[0]
    return top_level == "builtins" or top_level in _STDLIB_NAMES


def _is_cloudpickle_name(module_name):
    return module_name == "cloudpickle" or module_name.startswith(
        "cloudpickle."
    )


def _is_implicit_by_name(module):
    return _is_stdlib_module(module) or _is_cloudpickle_module(module)


def _is_rule_body(module_name, qualname):
    """Whether a symbol is part of the exported rule itself, not a dependency.

    Interactive code (``__main__``) and objects defined inside a function
    (a qualname containing ``<locals>``) make up the rule being shipped and
    are always embedded by value without needing a module license. Licensed
    third-party or local rule modules, by contrast, are defined at module
    level and must be covered by a license.
    """
    if module_name in ("__main__", "__mp_main__"):
        return True
    return bool(qualname) and "<locals>" in qualname


def _module_version(module):
    """Best-effort revision identifier for a module.

    The module's own ``__version__`` is preferred; otherwise the version of
    the installed distribution that provides the top-level package is used.
    ``None`` is returned when no revision can be determined.
    """
    version = getattr(module, "__version__", None)
    if isinstance(version, str) and version:
        return version
    top_level = module.__name__.split(".", 1)[0]
    try:
        return importlib.metadata.version(top_level)
    except importlib.metadata.PackageNotFoundError:
        return None


class _License:
    """A single registered export license for one module revision."""

    def __init__(self, module, kind, license_id):
        self.module_name = module.__name__
        self.module_version = _module_version(module)
        self.module_file = getattr(module, "__file__", None)
        # Identity of the exact module object revision licensed. Two
        # same-named modules loaded from different locations have distinct
        # ids, which is how conflicting revisions are told apart.
        self.module_id = id(module)
        self.kind = kind
        self.license_id = license_id
        # Keep the module alive for the lifetime of the manifest without
        # preventing its normal garbage collection.
        self._module_ref = weakref.ref(module)

    def as_dict(self):
        return {
            "license_id": self.license_id,
            "kind": self.kind,
            "module_name": self.module_name,
            "module_version": self.module_version,
            "module_file": self.module_file,
        }


class ExportManifest:
    """Registry of the export licenses granted for one receiver job.

    Licenses are recorded when a job is accepted, while the actual
    serialization happens later. A manifest is only read by the picklers
    that export for its receiver; concurrent exports use their own manifest
    and their own report, so a failing export never mutates the state of
    another in-progress package.
    """

    def __init__(self, receiver=None):
        # Stable identifier of the registration, used to correlate reports
        # with the receiver job that accepted the licenses.
        self.manifest_id = uuid.uuid4().hex
        self.receiver = receiver
        self._lock = threading.Lock()
        self._licenses = []
        # Indexed by module object identity so that same-named modules from
        # different revisions are never confused.
        self._by_id = {}

    def _register(self, module, kind):
        if not isinstance(module, types.ModuleType):
            raise ValueError(
                f"Input should be a module object, got {module!r} instead"
            )
        # Note: unlike register_pickle_by_value, presence in sys.modules is
        # deliberately not required. Two groups can own distinct same-named
        # rule modules, only one of which can occupy sys.modules; licenses
        # are keyed by module object identity and defining namespace instead.
        with self._lock:
            existing = self._by_id.get(id(module))
            if existing is not None:
                if existing.kind != kind:
                    raise ValueError(
                        f"Module {module.__name__!r} is already licensed "
                        f"{existing.kind!r}, cannot also license it {kind!r}"
                    )
                return existing.license_id
            license_id = uuid.uuid4().hex
            license_ = _License(module, kind, license_id)
            self._licenses.append(license_)
            self._by_id[id(module)] = license_
            return license_id

    def license_by_value(self, module):
        """License embedding ``module``'s revision into payloads (by value)."""
        return self._register(module, BY_VALUE)

    def license_by_name(self, module):
        """License loading ``module``'s revision by name at the receiver."""
        return self._register(module, BY_NAME)

    def _entry_for_module(self, module):
        return self._by_id.get(id(module))

    def _entry_for_globals(self, globals_dict):
        """Find the license of the module whose namespace is ``globals_dict``.

        Functions only keep a reference to their defining module's namespace,
        not to the module object itself. Matching that namespace lets the
        pickler attribute a dynamic function to its exact module revision,
        even when another same-named module occupies ``sys.modules``.

        Exact namespace identity is preferred. When that fails (for example
        after a by-value class was reconstructed in-process, whose methods
        point at a rebuilt namespace), fall back to the module name and source
        file provenance recorded on the licenses. A unique match is required;
        an ambiguous or unknown namespace returns ``None`` so the dependency
        is conservatively reported as unlicensed instead of being attributed
        to the wrong revision.
        """
        for license_ in self._licenses:
            module = license_._module_ref()
            if module is not None and module.__dict__ is globals_dict:
                return license_

        name = globals_dict.get("__name__")
        file_ = globals_dict.get("__file__")
        if name is None or file_ is None:
            return None
        matches = [
            license_
            for license_ in self._licenses
            if license_.module_name == name and license_.module_file == file_
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _entry_for_name(self, module_name):
        """Return the unique license registered for ``module_name``.

        Used as a last-resort attribution for objects (e.g. a class with no
        method of its own) that only carry their defining module's name. A
        unique license is required; same-named distinct revisions make this
        ambiguous and therefore return ``None``.
        """
        with self._lock:
            matches = [
                license_
                for license_ in self._licenses
                if license_.module_name == module_name
            ]
        return matches[0] if len(matches) == 1 else None

    def is_licensed_by_value(self, module):
        entry = self._by_id.get(id(module))
        return entry is not None and entry.kind == BY_VALUE

    def licenses(self):
        """Return an immutable snapshot of the registered licenses."""
        with self._lock:
            return [license_.as_dict() for license_ in self._licenses]

    def __reduce__(self):
        # A manifest is an export-time control object. It must never be
        # embedded in a payload (it holds a lock and weakrefs), and a rule
        # capturing it in its globals would otherwise fail with an obscure
        # "cannot pickle '_thread.lock'" error.
        raise pickle.PicklingError(
            "ExportManifest objects cannot be serialized: they only govern "
            "an export and must not be captured by the objects being "
            "exported. Use the active manifest's licenses through the "
            "export entry points instead."
        )


class Dependency:
    """One function, class or module actually written into a payload."""

    def __init__(
        self,
        kind,
        module=None,
        qualname=None,
        license_id=None,
        *,
        module_name=None,
        module_version=None,
        module_file=None,
        module_id=None,
    ):
        if module is not None:
            module_name = module.__name__
            module_version = _module_version(module)
            module_file = getattr(module, "__file__", None)
            module_id = id(module)
        self.kind = kind
        self.module_name = module_name
        self.module_version = module_version
        self.module_file = module_file
        self.module_id = module_id
        self.qualname = qualname
        self.license_id = license_id

    @classmethod
    def from_namespace(cls, kind, namespace, qualname=None, license_id=None):
        """Build a dependency from a module namespace (no module object)."""
        return cls(
            kind,
            qualname=qualname,
            license_id=license_id,
            module_name=namespace.get("__name__"),
            module_version=namespace.get("__version__"),
            module_file=namespace.get("__file__"),
            module_id=id(namespace),
        )

    @property
    def symbol(self):
        if self.qualname is None:
            return self.module_name
        return f"{self.module_name}.{self.qualname}"

    @property
    def revision_key(self):
        """Revision identity stable across module and namespace views.

        A by-value module can be observed either as a module object or as the
        rebuilt namespace carried by a reconstructed class; their Python ids
        differ, so the source file (falling back to the recorded version) is
        used together with the module name to tell two same-named revisions
        apart.
        """
        return (self.module_name, self.module_file, self.module_version)

    def as_dict(self):
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "module_name": self.module_name,
            "module_version": self.module_version,
            "module_file": self.module_file,
            "qualname": self.qualname,
            "license_id": self.license_id,
        }

    def __repr__(self):
        return (
            f"Dependency({self.symbol!r}, kind={self.kind!r}, "
            f"version={self.module_version!r}, file={self.module_file!r})"
        )


class ExportReport:
    """Itemized, reviewable record of what one export actually wrote."""

    def __init__(self, manifest):
        self.manifest_id = manifest.manifest_id
        self.receiver = manifest.receiver
        self.by_value = []
        self.by_name = []
        # Dependencies covered by no published license at all.
        self.missing = []
        # Dependencies whose actual serialization disagrees with the only
        # license available (e.g. a by-name-only licensed module that is not
        # importable and therefore has to be embedded by value).
        self.mismatches = []
        # Dependencies involved in a same-name/different-revision conflict.
        self.conflicts = []
        self.valid = True

    def as_dict(self):
        return {
            "manifest_id": self.manifest_id,
            "receiver": self.receiver,
            "valid": self.valid,
            "by_value": [dep.as_dict() for dep in self.by_value],
            "by_name": [dep.as_dict() for dep in self.by_name],
            "missing": [dep.as_dict() for dep in self.missing],
            "mismatches": [dep.as_dict() for dep in self.mismatches],
            "conflicts": [dep.as_dict() for dep in self.conflicts],
        }

    def __repr__(self):
        return (
            f"ExportReport(manifest_id={self.manifest_id!r}, "
            f"valid={self.valid}, by_value={len(self.by_value)}, "
            f"by_name={len(self.by_name)}, missing={len(self.missing)}, "
            f"mismatches={len(self.mismatches)}, "
            f"conflicts={len(self.conflicts)})"
        )


class ExportError(pickle.PicklingError):
    """Raised when a payload cannot be accepted against its manifest.

    The partially written bytes are never returned: callers keep the report
    (and the underlying stream position) to review the dependencies that
    blocked acceptance.
    """

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report

    @staticmethod
    def _format(deps):
        return "; ".join(
            f"{dep.symbol} (version={dep.module_version!r}, "
            f"file={dep.module_file!r}, license_id={dep.license_id!r})"
            for dep in deps
        )


class ExportPickler(Pickler):
    """Pickler that enforces an :class:`ExportManifest` while serializing.

    Use :meth:`export` (or the module-level :func:`export_dumps` helper)
    instead of the raw :meth:`dump` method: the export context installs a
    per-thread by-value decision hook that makes the pickler ignore the
    global ``register_pickle_by_value`` registry for the duration of the
    export.
    """

    def __init__(self, file, manifest, protocol=None, buffer_callback=None):
        super().__init__(file, protocol=protocol, buffer_callback=buffer_callback)
        if not isinstance(manifest, ExportManifest):
            raise TypeError(
                f"Expected an ExportManifest, got {type(manifest).__name__}"
            )
        self.manifest = manifest
        self.report = ExportReport(manifest)
        # Identity of every object already accounted for. Each distinct
        # object reaches reducer_override exactly once thanks to the pickle
        # memo, so this set also defines the "actually written" basis for
        # sign-off. A reducer is itself executed only once.
        self._tracked = set()

    # -- dependency observation -----------------------------------------

    def _track(self, obj):
        obj_id = id(obj)
        if obj_id in self._tracked:
            return
        self._tracked.add(obj_id)

        if isinstance(obj, types.ModuleType):
            self._record_module(obj)
        elif isinstance(obj, (types.FunctionType, type)):
            self._record_symbol(obj)

    def _record_module(self, module):
        if _is_implicit_by_name(module):
            # Same Python/cloudpickle version is assumed at the receiver:
            # stdlib modules and cloudpickle reconstructors are pre-installed
            # and always loaded by name.
            self.report.by_name.append(Dependency(BY_NAME, module))
            return
        entry = self.manifest._entry_for_module(module)
        self._file_module(module, entry)

    def _record_symbol(self, obj):
        module_and_name = _lookup_module_and_qualname(obj)
        if module_and_name is not None:
            module, qualname = module_and_name
            if _is_implicit_by_name(module):
                self.report.by_name.append(Dependency(BY_NAME, module, qualname))
                return
            entry = self.manifest._entry_for_module(module)
            self._file_symbol(obj, module, qualname, entry)
            return

        # The symbol is not importable by name (interactive/locally defined,
        # or its module is shadowed in sys.modules). It is therefore written
        # by value; attribute it to the exact module namespace that defines it.
        namespace, qualname = self._attribute_dynamic(obj)
        if namespace is None:
            # No extractable namespace (e.g. a class with no method of its
            # own) and the object is not importable by reference. Resolving
            # its bare name through sys.modules would be unsafe here: the
            # name may resolve to a different, same-named revision. Only a
            # uniquely licensed module of that name may be used.
            module_name = getattr(obj, "__module__", None)
            if not module_name or _is_rule_body(module_name, qualname):
                return
            if _is_stdlib_name(module_name) or _is_cloudpickle_name(
                module_name
            ):
                self.report.by_name.append(
                    Dependency(BY_NAME, qualname=qualname, module_name=module_name)
                )
                return
            self._record_named_symbol(obj, None, module_name, qualname)
            return

        module_name = namespace.get("__name__")
        if _is_rule_body(module_name, qualname):
            return
        if _is_stdlib_name(module_name) or _is_cloudpickle_name(module_name):
            self.report.by_name.append(
                Dependency.from_namespace(BY_NAME, namespace, qualname)
            )
            return
        self._record_named_symbol(obj, namespace, module_name, qualname)

    def _record_named_symbol(self, obj, namespace, module_name, qualname):
        entry = None
        if namespace is not None:
            entry = self.manifest._entry_for_globals(namespace)
        if entry is None:
            entry = self.manifest._entry_for_name(module_name)

        if entry is None:
            # A named, non-stdlib module revision with no published license:
            # leave a reviewable entry instead of embedding it silently.
            if namespace is not None:
                self.report.missing.append(
                    Dependency.from_namespace(None, namespace, qualname)
                )
            else:
                self.report.missing.append(
                    Dependency(
                        None,
                        qualname=qualname,
                        module_name=module_name,
                    )
                )
            return

        module = entry._module_ref()
        if module is None:
            # The module object was garbage-collected after registration; the
            # license still records its name, version and source file.
            dep = Dependency(
                entry.kind,
                qualname=qualname,
                license_id=entry.license_id,
                module_name=entry.module_name,
                module_version=entry.module_version,
                module_file=entry.module_file,
                module_id=entry.module_id,
            )
            self._file(dep)
            return
        if entry.kind == BY_VALUE:
            self.report.by_value.append(
                Dependency(BY_VALUE, module, qualname, entry.license_id)
            )
        else:
            # A by-name license cannot be honored for a symbol that is not
            # importable by reference: cloudpickle embeds it by value.
            self.report.mismatches.append(
                Dependency(BY_VALUE, module, qualname, entry.license_id)
            )

    def _attribute_dynamic(self, obj):
        """Return the defining namespace and qualname of a by-value symbol.

        Returns ``(None, qualname)`` when the object has no module namespace
        (a genuinely interactive object such as a lambda in ``__main__``).
        """
        qualname = getattr(obj, "__qualname__", None) or getattr(
            obj, "__name__", None
        )
        namespaces = []
        if isinstance(obj, types.FunctionType):
            namespaces.append(obj.__globals__)
        elif isinstance(obj, type):
            # A class keeps no direct reference to its module, but every
            # method defined in that class shares the module's namespace.
            for value in obj.__dict__.values():
                if isinstance(value, types.FunctionType):
                    namespaces.append(value.__globals__)
        for namespace in namespaces:
            if isinstance(namespace, dict) and namespace.get("__name__"):
                return namespace, qualname
        return None, qualname

    def _file_module(self, module, entry):
        if entry is None:
            self.report.missing.append(Dependency(None, module))
            return
        dep = Dependency(entry.kind, module, license_id=entry.license_id)
        self._file_with_consistency(module, None, entry, dep)

    def _file_symbol(self, obj, module, qualname, entry):
        if entry is None:
            self.report.missing.append(Dependency(None, module, qualname))
            return
        dep = Dependency(entry.kind, module, qualname, entry.license_id)
        self._file_with_consistency(module, qualname, entry, dep, obj)

    def _file_with_consistency(self, module, qualname, entry, dep, obj=None):
        # The kind declared by the license must match how cloudpickle actually
        # writes the dependency. A by-name license is only honored when the
        # object is importable by reference; otherwise cloudpickle embeds it
        # by value, which violates the published by-name contract.
        if entry.kind == BY_NAME:
            referenceable = (
                _should_pickle_by_reference(obj)
                if obj is not None
                else module.__name__ in sys.modules
            )
            if not referenceable:
                mismatch = Dependency(BY_VALUE, module, qualname, entry.license_id)
                self.report.mismatches.append(mismatch)
                return
        self._file(dep)

    def _file(self, dep):
        if dep.kind == BY_VALUE:
            self.report.by_value.append(dep)
        else:
            self.report.by_name.append(dep)

    # -- by-value decision hook -----------------------------------------

    def _allows_by_value(self, module):
        return self.manifest.is_licensed_by_value(module)

    @contextlib.contextmanager
    def _export_context(self):
        previous = _get_pickle_by_value_override()
        _set_pickle_by_value_override(self._allows_by_value)
        _add_object_observer(self._track)
        try:
            yield
        finally:
            _remove_object_observer(self._track)
            _set_pickle_by_value_override(previous)

    # -- orchestration and sign-off -------------------------------------

    def export(self, obj):
        """Serialize ``obj`` and validate the payload before sign-off.

        Returns the :class:`ExportReport` on success. Raises
        :class:`ExportError` if unlicensed dependencies or same-name
        revision conflicts were written; the report attached to the
        exception keeps every offending dependency reviewable.
        """
        try:
            with self._export_context():
                self.dump(obj)
        except BaseException:
            # A reducer failed, possibly the reducer of an object built
            # lazily inside another custom reduce: choices made while
            # writing this payload are not a delivery, and the manifest is
            # left untouched so concurrent exports keep their own course.
            self.report.valid = False
            raise
        self._validate()
        return self.report

    def _validate(self):
        self._detect_conflicts()
        if (
            self.report.missing
            or self.report.mismatches
            or self.report.conflicts
        ):
            self.report.valid = False
            raise ExportError(self._build_error_message(), self.report)

    def _detect_conflicts(self):
        # Group every recorded dependency by its importable symbol name.
        # Two distinct module revisions sharing a name (or a same-named
        # class) conflict whenever at least one copy is not embedded by
        # value: the receiver would resolve that name to whichever revision
        # it imported first, so both revisions must be published by value.
        groups = {}
        all_deps = (
            self.report.by_value
            + self.report.by_name
            + self.report.missing
            + self.report.mismatches
        )
        for dep in all_deps:
            groups.setdefault(dep.symbol, []).append(dep)

        conflicts = []
        for deps in groups.values():
            distinct_revisions = {dep.revision_key for dep in deps}
            if len(distinct_revisions) > 1 and any(
                dep.kind != BY_VALUE for dep in deps
            ):
                conflicts.extend(deps)
        self.report.conflicts = conflicts

    def _build_error_message(self):
        lines = ["export cannot be accepted against its manifest:"]
        if self.report.missing:
            lines.append(
                "  dependencies without a published license: "
                + ExportError._format(self.report.missing)
            )
        if self.report.mismatches:
            lines.append(
                "  dependencies licensed by name but not importable, hence "
                "embedded by value: "
                + ExportError._format(self.report.mismatches)
            )
        if self.report.conflicts:
            lines.append(
                "  same-name dependencies from distinct revisions require a "
                "by-value license for every revision: "
                + ExportError._format(self.report.conflicts)
            )
        return "\n".join(lines)


def export_dumps(obj, manifest, protocol=None, buffer_callback=None):
    """Serialize ``obj`` under a licensed manifest, in memory.

    Returns a ``(payload, report)`` tuple. On failure, :class:`ExportError`
    is raised and no payload is returned; the report on the exception lists
    every dependency that must be reviewed before retrying.
    """
    with io.BytesIO() as file:
        pickler = ExportPickler(
            file,
            manifest,
            protocol=protocol,
            buffer_callback=buffer_callback,
        )
        report = pickler.export(obj)
        return file.getvalue(), report


def export_dump(obj, file, manifest, protocol=None, buffer_callback=None):
    """Serialize ``obj`` under a licensed manifest, streaming into ``file``.

    Behaves like :func:`export_dumps` but writes into an open binary file
    and only returns the :class:`ExportReport`. When sign-off fails, the
    bytes already streamed into ``file`` remain at their current position
    for inspection and must not be treated as a valid delivery.
    """
    pickler = ExportPickler(
        file,
        manifest,
        protocol=protocol,
        buffer_callback=buffer_callback,
    )
    return pickler.export(obj)
