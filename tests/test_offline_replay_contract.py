from __future__ import annotations

import copy
import json
import socket
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = ROOT.parent
for path in (str(ROOT), str(WORKSPACE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

for module_name in list(sys.modules):
    if module_name == "yarl" or module_name.startswith("yarl."):
        del sys.modules[module_name]

import pytest  # noqa: E402
from yarl import URL  # noqa: E402

from offline_replay_contract import (  # noqa: E402
    HARD_MAX_CONCRETE_CASES,
    ConcreteBudget,
    StringDomain,
    _exhaustive_evaluate,
    evaluate_config,
    load_config,
    main,
    parse_config,
    replay_counterexample,
)

FIXTURE = ROOT / "fixtures" / "offline_replay_supplier.json"
Z24 = "z" * 24


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline contract test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture(scope="module")
def supplier_config():
    return load_config(FIXTURE)


def test_uses_yarl_snapshot_from_this_workspace() -> None:
    import yarl

    assert Path(yarl.__file__).resolve().is_relative_to(WORKSPACE_ROOT.resolve())


@pytest.fixture
def supplier_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_recorded_config_reports_exact_first_counterexample(supplier_config) -> None:
    report = evaluate_config(supplier_config)

    assert report["status"] == "divergent"
    divergence = report["first_divergence"]
    assert divergence["input"] == {"r": "", "k": Z24, "v": Z24 + "+"}
    assert divergence["r_index"] == 2
    assert divergence["order_index"] == report["input_space"]["total_inputs"] - 1
    assert divergence["first_different_step"] == "query_modification"
    assert report["coverage"]["concrete_cases_compared"] <= HARD_MAX_CONCRETE_CASES

    append_target = divergence["outer_raw_path_qs"]["append"]
    update_target = divergence["outer_raw_path_qs"]["update"]
    assert append_target == (
        "/replay?target=/catalog/index?"
        f"{Z24}%3Da%252Bb%26{Z24}%3Da%2Bb%26{Z24}%3D{Z24}%252B"
    )
    assert update_target == f"/replay?target=/catalog/index?{Z24}%3D{Z24}%252B"
    assert append_target != update_target

    assert divergence["stage_raw_path_qs"]["catalog"] == {
        "append": f"/catalog/index?{Z24}=a%2Bb&{Z24}=a+b",
        "update": f"/catalog/index?{Z24}=a%2Bb&{Z24}=a+b",
    }
    assert divergence["stage_raw_path_qs"]["query_modification"]["append"].endswith(
        f"&{Z24}={Z24}%2B"
    )
    assert "a%2Bb" not in divergence["stage_raw_path_qs"]["query_modification"]["update"]


def test_length_then_ascii_order_is_used_for_string_domains() -> None:
    domain = StringDomain(("a", "b"), 1, 2)
    assert list(domain.iter_strings()) == ["a", "b", "aa", "ab", "ba", "bb"]
    assert domain.rank("ab") == 3
    assert domain.at_rank(3) == "ab"


@pytest.mark.parametrize("reference", ["?", "leaf"])
def test_z24_still_shares_replay_for_query_resetting_references(reference: str) -> None:
    catalog = f"http://catalog.invalid/catalog/index?{Z24}=a%2Bb&{Z24}=a+b"
    base = URL(catalog, encoded=True).join(URL(reference, encoded=True))
    k = Z24
    v = Z24 + "+"
    append = base.extend_query([(k, v)])
    update = base.update_query([(k, v)])
    append_outer = URL("http://fixture.invalid/replay").with_query(
        [("target", append.raw_path_qs)]
    )
    update_outer = URL("http://fixture.invalid/replay").with_query(
        [("target", update.raw_path_qs)]
    )
    assert append_outer.raw_path_qs == update_outer.raw_path_qs


def test_directory_reference_independent_v_and_literal_templates() -> None:
    raw = {
        "replay_base_url": "http://fixture.invalid/replay",
        "input": {
            "references": ["dir/leaf"],
            "k": {"alphabet": "ab", "min_length": 1, "max_length": 1},
            "v": {
                "alphabet": "xy",
                "min_length": 1,
                "max_length": 1,
                "binding": {"kind": "independent"},
            },
        },
        "procedures": [
            {
                "id": "append",
                "catalog_url": "http://catalog.invalid/catalog/index",
                "encoded": False,
                "method": "extend_query",
                "pairs": [
                    [{"literal": "op"}, {"var": "v"}],
                    [{"var": "k"}, {"literal": "tag"}],
                ],
            },
            {
                "id": "update",
                "catalog_url": "http://catalog.invalid/catalog/index",
                "encoded": False,
                "method": "update_query",
                "pairs": [
                    [{"literal": "op"}, {"var": "v"}],
                    [{"var": "k"}, {"literal": "tag"}],
                ],
            },
        ],
    }
    config = parse_config(raw)
    report = evaluate_config(config)
    assert report["status"] == "same"
    assert report["coverage"]["total_inputs"] == 4
    assert report["coverage"]["concrete_cases_compared"] == 4

    rendered = URL(raw["procedures"][0]["catalog_url"], encoded=False).join(
        URL("dir/leaf", encoded=False)
    )
    rendered = rendered.extend_query([("op", "x"), ("a", "tag")])
    assert rendered.raw_path_qs == "/catalog/dir/leaf?op=x&a=tag"


def test_recorded_counterexample_replays_with_yarl(supplier_config) -> None:
    report = evaluate_config(supplier_config)
    replayed = replay_counterexample(supplier_config, report)
    assert replayed == report["first_divergence"]["outer_raw_path_qs"]

    case = report["first_divergence"]["input"]
    catalog = supplier_config.procedures[0].catalog_url
    joined = URL(catalog, encoded=True).join(URL(case["r"], encoded=True))
    inner = joined.extend_query([(case["k"], case["v"])]).raw_path_qs
    outer = URL(supplier_config.replay_base_url).with_query([("target", inner)])
    assert outer.raw_path_qs == replayed["append"]
    assert outer.query["target"] == inner
    append_outer_url = report["first_divergence"]["outer_urls"]["append"]
    assert URL(append_outer_url).query["target"] == report["first_divergence"][
        "stage_raw_path_qs"
    ]["query_modification"]["append"]


def test_actual_z24_catalog_small_k_slice_matches_yarl_every_case() -> None:
    catalog = (
        f"http://catalog.invalid/catalog/index?{Z24}=a%2Bb&{Z24}=a+b"
    )
    checked = 0
    for r in ("?", "leaf", ""):
        base = URL(catalog, encoded=True).join(URL(r, encoded=True))
        for k in "abcdefghijklmnopqrstuvwxyz":
            v = k + "+"
            append = base.extend_query([(k, v)])
            update = base.update_query([(k, v)])
            append_outer = URL("http://fixture.invalid/replay").with_query(
                [("target", append.raw_path_qs)]
            )
            update_outer = URL("http://fixture.invalid/replay").with_query(
                [("target", update.raw_path_qs)]
            )
            assert append_outer.raw_path_qs == update_outer.raw_path_qs
            checked += 1
    assert checked == 78


def test_small_domain_is_checked_one_by_one_against_yarl(supplier_raw) -> None:
    raw = copy.deepcopy(supplier_raw)
    aa = "aa"
    raw["input"]["k"].update(alphabet="ab", min_length=1, max_length=2)
    raw["input"]["v"].update(alphabet="ab+", min_length=2, max_length=3)
    raw["procedures"][0]["catalog_url"] = (
        f"http://catalog.invalid/catalog/index?{aa}=a%2Bb&{aa}=a+b"
    )
    raw["procedures"][1]["catalog_url"] = raw["procedures"][0]["catalog_url"]
    config = parse_config(raw)

    direct_first = None
    direct_checked = 0
    catalog = raw["procedures"][0]["catalog_url"]
    for r in raw["input"]["references"]:
        base = URL(catalog, encoded=True).join(URL(r, encoded=True))
        for length in range(1, 3):
            for chars in product("ab", repeat=length):
                k = "".join(chars)
                v = k + "+"
                append_inner = base.extend_query([(k, v)]).raw_path_qs
                update_inner = base.update_query([(k, v)]).raw_path_qs
                append_outer = URL(raw["replay_base_url"]).with_query(
                    [("target", append_inner)]
                ).raw_path_qs
                update_outer = URL(raw["replay_base_url"]).with_query(
                    [("target", update_inner)]
                ).raw_path_qs
                direct_checked += 1
                if append_outer != update_outer:
                    direct_first = {
                        "r": r,
                        "k": k,
                        "v": v,
                        "append": append_outer,
                        "update": update_outer,
                    }
                    break
            if direct_first is not None:
                break
        if direct_first is not None:
            break

    assert direct_checked == 15
    assert direct_first is not None
    assert direct_first["r"] == ""
    assert direct_first["k"] == "aa"

    report = evaluate_config(config)
    assert report["status"] == "divergent"
    assert report["coverage"]["method"] == "exhaustive_concrete"
    assert report["coverage"]["concrete_cases_compared"] == 15
    divergence = report["first_divergence"]
    assert divergence["input"] == {"r": "", "k": "aa", "v": "aa+"}
    assert divergence["outer_raw_path_qs"]["append"] == direct_first["append"]
    assert divergence["outer_raw_path_qs"]["update"] == direct_first["update"]
    replay_counterexample(config, report)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda raw: raw["input"]["k"].__setitem__("max_length", 23), id="k-too-short"),
        pytest.param(
            lambda raw: raw["input"].__setitem__("references", ["?", "leaf"]),
            id="drop-empty-reference",
        ),
        pytest.param(
            lambda raw: raw["input"]["k"].__setitem__(
                "alphabet", "abcdefghijklmnopqrstuvwxy"
            ),
            id="alphabet-without-z",
        ),
        pytest.param(
            lambda raw: raw["procedures"][1].__setitem__("method", "extend_query"),
            id="both-extend",
        ),
    ],
)
def test_changed_large_supplier_config_can_be_certified_same(supplier_raw, mutation) -> None:
    raw = copy.deepcopy(supplier_raw)
    mutation(raw)
    config = parse_config(raw)

    report = evaluate_config(config)

    assert report["status"] == "same", report
    assert report["first_divergence"] is None
    assert report["coverage"]["all_permitted_inputs_covered"] is True
    assert report["coverage"]["total_inputs"] > HARD_MAX_CONCRETE_CASES
    assert report["coverage"]["concrete_cases_generated"] <= HARD_MAX_CONCRETE_CASES
    assert report["coverage"]["concrete_cases_compared"] <= HARD_MAX_CONCRETE_CASES


def test_changed_small_supplier_config_reports_exhaustive_same(supplier_raw) -> None:
    raw = copy.deepcopy(supplier_raw)
    raw["input"]["k"].update(alphabet="ab", min_length=1, max_length=2)
    raw["input"]["v"].update(alphabet="ab+", min_length=2, max_length=3)
    raw["input"]["references"] = ["?", "leaf"]
    config = parse_config(raw)

    report = evaluate_config(config)

    assert report["status"] == "same"
    assert report["coverage"]["method"] == "exhaustive_concrete"
    assert report["coverage"]["total_inputs"] == 12
    assert report["coverage"]["concrete_cases_compared"] == 12


def test_concrete_budget_is_hard_capped_at_512() -> None:
    budget = ConcreteBudget(HARD_MAX_CONCRETE_CASES)
    for _ in range(HARD_MAX_CONCRETE_CASES):
        budget.record_generated()
    assert budget.generated == 512
    with pytest.raises(Exception):
        budget.record_generated()


def test_large_exhaustive_helper_stops_after_512_lazy_cases(supplier_raw) -> None:
    raw = copy.deepcopy(supplier_raw)
    raw["input"]["references"] = ["a", "b"]
    raw["input"]["k"].update(alphabet="ab", min_length=1, max_length=8)
    raw["input"]["v"].update(alphabet="ab+", min_length=2, max_length=9)
    raw["procedures"][1]["method"] = "extend_query"
    config = parse_config(raw)
    assert config.input_space.total_cases() > 512

    budget = ConcreteBudget(512)
    assert _exhaustive_evaluate(config, budget) is None
    assert budget.generated == 512
    assert budget.compared == 512


def test_cli_returns_divergent_status_and_writes_json(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    exit_code = main([str(FIXTURE), "--write-report", str(output)])
    assert exit_code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "divergent"
    assert report["first_divergence"]["input"]["k"] == Z24


def test_checked_in_report_matches_current_contract() -> None:
    report_path = ROOT / "fixtures" / "offline_replay_report.json"
    checked = json.loads(report_path.read_text(encoding="utf-8"))
    current = evaluate_config(FIXTURE)
    assert checked["status"] == current["status"]
    assert checked["first_divergence"]["input"] == current["first_divergence"]["input"]
    assert (
        checked["first_divergence"]["outer_raw_path_qs"]
        == current["first_divergence"]["outer_raw_path_qs"]
    )
