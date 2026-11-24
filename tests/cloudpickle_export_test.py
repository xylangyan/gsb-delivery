"""Tests for the manifest-licensed export workflow (cloudpickle.exporting)."""

import importlib.util
import io
import pickle
import sys

import pytest

import cloudpickle
from cloudpickle import (
    BY_NAME,
    BY_VALUE,
    ExportError,
    ExportManifest,
    export_dump,
    export_dumps,
)
from cloudpickle.cloudpickle import (
    _get_pickle_by_value_override,
    register_pickle_by_value,
    unregister_pickle_by_value,
)


def _load_module(path, name="rules", version=None, body=None):
    if body is not None:
        path.write_text(body)
    spec = importlib.util.spec_from_file_location(
        name, str(path), submodule_search_locations=[]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if version is not None:
        module.__version__ = version
    return module


@pytest.fixture
def two_rules(tmp_path):
    """Two distinct, same-named ``rules`` modules from separate revisions."""
    dir_a = tmp_path / "group_a"
    dir_b = tmp_path / "group_b"
    dir_a.mkdir()
    dir_b.mkdir()
    rules_a = _load_module(
        dir_a / "rules.py",
        version="1.0",
        body=(
            "__version__ = '1.0'\n"
            "class Converter:\n"
            "    def convert(self, x):\n"
            "        return x * 2\n"
            "def rule(x):\n"
            "    return Converter().convert(x)\n"
        ),
    )
    rules_b = _load_module(
        dir_b / "rules.py",
        version="2.0",
        body=(
            "__version__ = '2.0'\n"
            "class Converter:\n"
            "    def convert(self, x):\n"
            "        return x * 3\n"
            "def rule(x):\n"
            "    return Converter().convert(x)\n"
        ),
    )
    assert rules_a is not rules_b
    return rules_a, rules_b


def _symbols(deps):
    return {dep.symbol for dep in deps}


def test_manifest_records_licenses_and_snapshot(two_rules):
    rules_a, _ = two_rules
    manifest = ExportManifest(receiver="offline-1")
    license_id = manifest.license_by_value(rules_a)

    snapshot = manifest.licenses()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["license_id"] == license_id
    assert entry["kind"] == BY_VALUE
    assert entry["module_name"] == "rules"
    assert entry["module_version"] == "1.0"
    assert entry["module_file"] == rules_a.__file__

    # Re-registering the same module with the same kind is idempotent.
    assert manifest.license_by_value(rules_a) == license_id

    # Conflicting kinds on the same revision are rejected.
    with pytest.raises(ValueError, match="already licensed"):
        manifest.license_by_name(rules_a)

    # Non-module objects cannot be licensed.
    with pytest.raises(ValueError, match="module object"):
        manifest.license_by_value("not a module")


def test_by_value_export_loads_and_itemizes_sources(two_rules):
    rules_a, _ = two_rules
    manifest = ExportManifest(receiver="offline-1")
    manifest.license_by_value(rules_a)

    payload, report = export_dumps(rules_a.rule, manifest)
    assert report.valid
    assert pickle.loads(payload)(21) == 42

    by_value = _symbols(report.by_value)
    assert {"rules.rule", "rules.Converter"}.issubset(by_value)
    assert all(dep.module_version == "1.0" for dep in report.by_value)
    # Stdlib and cloudpickle reconstructors are implicit by-name items.
    assert all(dep.kind == BY_NAME for dep in report.by_name)
    assert any(dep.module_name == "builtins" for dep in report.by_name)
    assert report.missing == [] and report.conflicts == []


def test_by_name_export_is_loaded_by_reference(tmp_path, two_rules):
    _, rules_b = two_rules
    # A by-name license requires the module to be importable, hence it must
    # occupy sys.modules like an installed library.
    previous = sys.modules.get("rules")
    sys.modules["rules"] = rules_b
    try:
        manifest = ExportManifest(receiver="offline-2")
        manifest.license_by_name(rules_b)
        payload, report = export_dumps(rules_b.rule, manifest)
    finally:
        if previous is None:
            sys.modules.pop("rules", None)
        else:
            sys.modules["rules"] = previous

    assert report.valid
    assert any(
        dep.symbol == "rules.rule" and dep.kind == BY_NAME
        for dep in report.by_name
    )
    # A by-reference payload only carries the module/attribute names; it does
    # not embed the module's bytecode.
    assert b"rules" in payload and b"rule" in payload
    assert b"<dynamic-code>" not in payload
    sys.modules["rules"] = rules_b
    try:
        assert pickle.loads(payload)(14) == 42
    finally:
        if previous is None:
            sys.modules.pop("rules", None)
        else:
            sys.modules["rules"] = previous


def test_global_registry_cannot_leak_into_licensed_export(two_rules):
    rules_a, rules_b = two_rules
    manifest = ExportManifest(receiver="A")
    manifest.license_by_value(rules_a)

    # Another job temporarily registers its own same-named module globally.
    previous = sys.modules.get("rules")
    sys.modules["rules"] = rules_b
    register_pickle_by_value(rules_b)
    try:
        payload, report = export_dumps(rules_a.rule, manifest)
    finally:
        unregister_pickle_by_value(rules_b)
        if previous is None:
            sys.modules.pop("rules", None)
        else:
            sys.modules["rules"] = previous

    # The thread-local override is removed once the export completes.
    assert _get_pickle_by_value_override() is None
    assert pickle.loads(payload)(21) == 42
    assert all(dep.module_file != rules_b.__file__ for dep in report.by_value)
    assert all(dep.module_file == rules_a.__file__ for dep in report.by_value)


def test_lazy_nested_dependency_built_inside_reduce_is_discovered(two_rules):
    rules_a, _ = two_rules
    manifest = ExportManifest(receiver="offline-1")
    manifest.license_by_value(rules_a)

    class UnitConversion:
        def __init__(self, factor):
            self.factor = factor

        def apply(self, x):
            return x * self.factor

        def __reduce__(self):
            # The nested dump runs within the outer export context, so it
            # inherits the same licenses and feeds the same report.
            inner = cloudpickle.dumps(rules_a.Converter)
            return (UnitConversion, (self.factor,), {"inner": inner})

    class LazyRule:
        def __reduce__(self):
            unit = UnitConversion(7)  # constructed only at reduce time
            return (LazyRule, (), {"unit": unit})

    payload, report = export_dumps(LazyRule(), manifest)
    reloaded = pickle.loads(payload)
    assert reloaded.unit.apply(6) == 42
    assert pickle.loads(reloaded.unit.inner)().convert(21) == 42
    # The dependency hidden behind the lazy custom reduce is still itemized.
    assert "rules.Converter" in _symbols(report.by_value)


def test_each_distinct_dependency_is_accounted_once(two_rules):
    rules_a, _ = two_rules
    manifest = ExportManifest(receiver="offline-1")
    manifest.license_by_value(rules_a)

    converter = rules_a.Converter
    payload, report = export_dumps([converter, converter, converter], manifest)
    pickle.loads(payload)
    symbols = [dep.symbol for dep in report.by_value]
    assert symbols.count("rules.Converter") == 1


def test_missing_license_blocks_delivery_with_reviewable_dependency(tmp_path):
    rules = _load_module(
        tmp_path / "rules.py",
        version="9.9",
        body=(
            "__version__ = '9.9'\n"
            "class Widget:\n"
            "    def ping(self):\n"
            "        return 'pong'\n"
        ),
    )
    manifest = ExportManifest(receiver="fresh")
    with pytest.raises(ExportError) as exc_info:
        export_dumps(rules.Widget, manifest)

    report = exc_info.value.report
    assert report.valid is False
    dep = next(d for d in report.missing if d.symbol == "rules.Widget")
    assert dep.module_version == "9.9"
    assert dep.module_file == rules.__file__
    # The report is also available as plain data for review tooling.
    dumped_report = report.as_dict()
    assert dumped_report["valid"] is False
    assert any(d["symbol"] == "rules.Widget" for d in dumped_report["missing"])


def test_methodless_class_uses_name_level_fallback(tmp_path):
    rules = _load_module(
        tmp_path / "rules.py", version="1.0", body="class Marker:\n    pass\n"
    )
    manifest = ExportManifest(receiver="fresh")
    with pytest.raises(ExportError) as exc_info:
        export_dumps(rules.Marker, manifest)
    dep = exc_info.value.report.missing[0]
    assert dep.symbol == "rules.Marker"
    assert dep.module_name == "rules"


def test_same_name_two_revisions_conflict_when_one_is_by_name(two_rules):
    rules_a, rules_b = two_rules
    # The importable revision (by name) occupies sys.modules; the shadowed
    # revision can only be embedded by value.
    previous = sys.modules.get("rules")
    sys.modules["rules"] = rules_b
    try:
        manifest = ExportManifest(receiver="dual")
        manifest.license_by_value(rules_a)
        manifest.license_by_name(rules_b)
        with pytest.raises(ExportError) as exc_info:
            export_dumps([rules_a.Converter, rules_b.Converter], manifest)
    finally:
        if previous is None:
            sys.modules.pop("rules", None)
        else:
            sys.modules["rules"] = previous

    conflicts = exc_info.value.report.conflicts
    assert {dep.symbol for dep in conflicts} == {"rules.Converter"}
    assert {dep.module_version for dep in conflicts} == {"1.0", "2.0"}
    assert {dep.kind for dep in conflicts} == {BY_VALUE, BY_NAME}


def test_same_name_two_revisions_accepted_when_both_by_value(two_rules):
    rules_a, rules_b = two_rules
    manifest = ExportManifest(receiver="dual")
    manifest.license_by_value(rules_a)
    manifest.license_by_value(rules_b)

    payload, report = export_dumps(
        [rules_a.Converter, rules_b.Converter], manifest
    )
    assert report.valid
    assert report.conflicts == []
    reloaded_a, reloaded_b = pickle.loads(payload)
    assert reloaded_a().convert(21) == 42
    assert reloaded_b().convert(14) == 42


def test_failing_export_does_not_affect_another_in_flight_export(tmp_path):
    good_rules = _load_module(
        tmp_path / "good.py",
        name="good_rules",
        version="1.0",
        body="class Safe:\n    pass\n",
    )
    good_manifest = ExportManifest(receiver="good")
    good_manifest.license_by_value(good_rules)
    bad_manifest = ExportManifest(receiver="bad")

    class Boom:
        def __reduce__(self):
            raise RuntimeError("reduce exploded")

    with pytest.raises(RuntimeError, match="reduce exploded"):
        export_dumps(Boom(), bad_manifest)

    # The unrelated, still-in-flight export is fully usable.
    payload, report = export_dumps(good_rules.Safe, good_manifest)
    assert report.valid
    assert pickle.loads(payload).__name__ == "Safe"


def test_manifest_is_not_serializable(two_rules):
    rules_a, _ = two_rules
    manifest = ExportManifest()
    manifest.license_by_value(rules_a)
    with pytest.raises(pickle.PicklingError, match="cannot be serialized"):
        cloudpickle.dumps(manifest)


def test_streaming_export_keeps_bytes_for_review_on_failure(tmp_path):
    rules = _load_module(
        tmp_path / "rules.py", version="1.0", body="class Widget:\n    pass\n"
    )
    manifest = ExportManifest(receiver="stream")
    stream = io.BytesIO()
    with pytest.raises(ExportError):
        export_dump(rules.Widget, stream, manifest)
    # Partial bytes remain inspectable but are not handed back as a delivery.
    assert stream.tell() > 0


def test_plain_dumps_workflow_remains_supported(tmp_path):
    # The ordinary workflow of registering one module then a single dumps
    # keeps working unchanged.
    rules = _load_module(
        tmp_path / "plain.py",
        name="plain_rules",
        version="1.0",
        body="def plain_rule():\n    return 7 * 6\n",
    )
    sys.modules["plain_rules"] = rules
    register_pickle_by_value(rules)
    try:
        payload = cloudpickle.dumps(rules.plain_rule)
    finally:
        unregister_pickle_by_value(rules)
        sys.modules.pop("plain_rules", None)
    assert pickle.loads(payload)() == 42


def test_concurrent_exports_use_isolated_manifests(two_rules):
    import threading

    rules_a, rules_b = two_rules
    manifest_a = ExportManifest(receiver="A")
    manifest_a.license_by_value(rules_a)
    manifest_b = ExportManifest(receiver="B")
    manifest_b.license_by_value(rules_b)

    results = {}

    def worker(manifest, rules, key):
        payload, report = export_dumps(rules.rule, manifest)
        results[key] = (payload, report)

    threads = [
        threading.Thread(target=worker, args=(manifest_a, rules_a, "a")),
        threading.Thread(target=worker, args=(manifest_b, rules_b, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    report_a = results["a"][1]
    report_b = results["b"][1]
    assert report_a.valid and report_b.valid
    assert all(dep.module_file == rules_a.__file__ for dep in report_a.by_value)
    assert all(dep.module_file == rules_b.__file__ for dep in report_b.by_value)
    assert _get_pickle_by_value_override() is None
