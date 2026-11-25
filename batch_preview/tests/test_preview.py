"""End-to-end and solver tests for the batch preview."""

from __future__ import annotations

import json
import time

import marko

from batch_preview import load_spec, run_batch
from batch_preview.cli import main as cli_main
from batch_preview.manual import Manual
from batch_preview.plan import RefReq, ReleaseModel
from batch_preview.solve import Solver, Variable

# ------------------------------------------------------------------ example

M1 = """查[手册]，见附注[^核]。

[^核]: 对照[手册]。

[手册]: /official
"""
M2 = "参考 [手册] 与 [提示][a] 即可。\n"
M3 = "参考 [手册] 与 [提示][b] 即可。\n"


def example_spec(candidates, releases):
    return {
        "manuals": {"M1": M1, "M2": M2, "M3": M3},
        "segments": {
            "S0": {"manual": "M1", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M2", "start_line": 1, "end_line": 1},
            "S3": {"manual": "M3", "start_line": 1, "end_line": 1},
        },
        "releases": releases,
        "marks": [
            {"manual": "M1", "line": 1, "col": 2, "candidates": candidates},
            {"manual": "M1", "line": 3, "col": 9, "candidates": candidates},
            {"manual": "M1", "line": 5, "col": 1, "candidates": candidates},
        ],
    }


def link_dests(text):
    """(display, dest) pairs of all links in the rendered document."""
    doc = marko.Markdown(extensions=["footnote"]).parse(text)
    found = []

    def walk(el):
        if type(el).__name__ == "Link":
            children = getattr(el, "children", [])
            display = "".join(
                getattr(c, "children", "") for c in children if isinstance(c.children, str)
            )
            found.append((display, el.dest))
            return
        children = getattr(el, "children", None)
        if isinstance(children, list):
            for child in children:
                walk(child)

    walk(doc)
    return sorted(found)


def test_single_release_jia_uses_b():
    result = run_batch(load_spec(example_spec(["a", "b"], {"甲": ["S0", "S2"]})))
    assert result.status == "ok"
    text = result.releases["甲"].text
    assert "[手册][b]" in text
    assert "[b]: /official" in text
    assert "[提示][a]" in text  # literal text untouched
    assert {c["new_label"] for c in result.report["changes"]} == {"b"}


def test_single_release_yi_uses_a():
    result = run_batch(load_spec(example_spec(["a", "b"], {"乙": ["S0", "S3"]})))
    assert result.status == "ok"
    assert "[手册][a]" in result.releases["乙"].text


def test_joint_batch_blocked_and_names_blockers():
    result = run_batch(
        load_spec(example_spec(["a", "b"], {"甲": ["S0", "S2"], "乙": ["S0", "S3"]}))
    )
    assert result.status == "blocked"
    assert result.release_status == {"甲": "blocked", "乙": "blocked"}
    blockers = result.report["blockers"]
    texts = {(b["release"], b["text"]) for b in blockers}
    assert ("甲", "[提示][a]") in texts
    assert ("乙", "[提示][b]") in texts
    # the blocking texts are reported with their original positions
    for b in blockers:
        if b["text"] == "[提示][a]":
            assert b["manual"] == "M2"
        if b["text"] == "[提示][b]":
            assert b["manual"] == "M3"


def test_supplementary_candidate_unblocks_both():
    result = run_batch(
        load_spec(
            example_spec(["a", "b", "c"], {"甲": ["S0", "S2"], "乙": ["S0", "S3"]})
        )
    )
    assert result.status == "ok"
    for name in ("甲", "乙"):
        text = result.releases[name].text
        assert "[手册][c]" in text
        assert "[c]: /official" in text
        # body and footnote references still open /official
        assert ("手册", "/official") in link_dests(text)
        assert link_dests(text).count(("手册", "/official")) == 2
        # literal bracket texts stay visible
        assert "[手册]" in text
        assert all(c.ok for c in result.releases[name].checks)
    assert "[提示][a]" in result.releases["甲"].text
    assert "[提示][b]" in result.releases["乙"].text
    # one signed change per original position, shared by both releases
    changes = result.report["changes"]
    assert len(changes) == 3
    for change in changes:
        assert change["new_label"] == "c"
        assert sorted(change["releases"]) == ["乙", "甲"]


# --------------------------------------------------------------- more cases


def test_no_change_when_nothing_conflicts():
    spec = {
        "manuals": {"M": "见[手册]。\n\n[手册]: /official\n"},
        "segments": {"S": {"manual": "M", "start_line": 1, "end_line": 1}},
        "releases": {"R": ["S"]},
        "marks": [{"manual": "M", "line": 1, "col": 2, "candidates": ["x"]}],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    assert result.report["changes"] == []
    assert "[手册]" in result.releases["R"].text
    assert "[手册][x]" not in result.releases["R"].text


def test_duplicate_url_definitions_tracked_by_position():
    spec = {
        "manuals": {"M": "见[甲] 与 [乙]。\n\n[甲]: /same\n[乙]: /same\n"},
        "segments": {"S": {"manual": "M", "start_line": 1, "end_line": 1}},
        "releases": {"R": ["S"]},
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    defs = result.report["releases"]["R"]["definitions"]
    assert {d["dest"] for d in defs} == {"/same"}
    assert len(defs) == 2
    # same address, different original positions
    assert {(d["manual"], d["line"]) for d in defs} == {("M", 3), ("M", 4)}
    assert {d["label"] for d in defs} == {"甲", "乙"}


def test_footnote_renumbering_avoids_literal_labels():
    spec = {
        "manuals": {
            "M1": "正文[^核]。\n\n[^核]: 注。\n",
            "M2": "此处 [^fn1] 只是文字。\n",
        },
        "segments": {
            "S1": {"manual": "M1", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M2", "start_line": 1, "end_line": 1},
        },
        "releases": {"R": ["S1", "S2"]},
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    text = result.releases["R"].text
    assert "[^fn1] 只是文字" in text  # literal untouched
    assert "[^fn2]" in text  # renumbered footnote skips the taken label
    doc = marko.Markdown(extensions=["footnote"]).parse(text)
    assert "fn2" in doc.footnotes


def test_tiebreak_prefers_fewer_brought_in_definitions():
    # Two physical definition lines share label x; exactly one must move.
    # Moving the one without extra references avoids an appended definition.
    spec = {
        "manuals": {
            "A": "见[x] 说明。\n\n[x]: /a\n",
            "B": "[x]: /b\n",
        },
        "segments": {
            "SA": {"manual": "A", "start_line": 1, "end_line": 3},
            "SB": {"manual": "B", "start_line": 1, "end_line": 1},
        },
        "releases": {"R": ["SA", "SB"]},
        "marks": [
            {"manual": "A", "line": 3, "col": 1, "candidates": ["y"]},
            {"manual": "B", "line": 1, "col": 1, "candidates": ["y"]},
        ],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    changes = result.report["changes"]
    assert len(changes) == 1
    assert changes[0]["manual"] == "B"  # moving B's definition needs no appendix
    defs = result.report["releases"]["R"]["definitions"]
    assert all(d["origin"] == "segment" for d in defs)


def test_unresolvable_shared_label_reports_both_sides():
    result = run_batch(
        load_spec(example_spec(["a", "b"], {"甲": ["S0", "S2"], "乙": ["S0", "S3"]}))
    )
    labels = {(b["release"], b["label"]) for b in result.report["blockers"]}
    assert ("甲", "a") in labels
    assert ("乙", "b") in labels


def test_reference_forms_and_multiparagraph_footnote():
    # CRLF, image/collapsed/full references, titled definition, and a
    # multi-paragraph footnote whose body contains a marked shortcut.
    manual = (
        "正文[^a] 与 ![图][p] 与 [折][] 与 [全][p]。\r\n\r\n"
        "[^a]: 首段 [p]。\r\n\r\n    续段 [q]。\r\n\r\n"
        '[p]: /img "标题"\r\n[折]: /zhe\r\n[q]: /que\r\n'
    )
    spec = {
        "manuals": {"M": manual, "M2": "另一册的 [p] 与 [折] 只是文字。\n"},
        "segments": {
            "S": {"manual": "M", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M2", "start_line": 1, "end_line": 1},
        },
        "releases": {"R": ["S", "S2"]},
        "marks": [
            {"manual": "M", "line": 1, "col": 10, "candidates": ["i"]},
            {"manual": "M", "line": 1, "col": 20, "candidates": ["z"]},
            {"manual": "M", "line": 1, "col": 28, "candidates": ["i"]},
            {"manual": "M", "line": 3, "col": 10, "candidates": ["i"]},
            {"manual": "M", "line": 7, "col": 1, "candidates": ["i"]},
            {"manual": "M", "line": 8, "col": 1, "candidates": ["z"]},
        ],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    text = result.releases["R"].text
    assert "![图][i]" in text
    assert "[折][z]" in text
    assert "[全][i]" in text
    assert "[p][i]" in text  # shortcut inside the footnote keeps its display
    assert '[i]: /img "标题"' in text
    assert "[z]: /zhe" in text
    assert "续段 [q]" in text  # continuation paragraph preserved
    assert "[p] 与 [折] 只是文字" in text  # literals untouched
    assert all(c.ok for c in result.releases["R"].checks)


def test_relabelled_definition_not_introduced_in_untouched_release():
    # R1 relabels the shared definition to L; R2 only keeps original-label
    # references, so the literal [L] there must not block the shared decision.
    spec = {
        "manuals": {
            "M1": "甲段用 [x]。\n\n乙段也用 [x]。\n\n[x]: /dest\n",
            "M2": "另一册 [L] 文字。\n",
            "M3": "甲册的 [x] 只是文字。\n",
        },
        "segments": {
            "S1": {"manual": "M1", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M1", "start_line": 3, "end_line": 3},
            "S3": {"manual": "M2", "start_line": 1, "end_line": 1},
            "S4": {"manual": "M3", "start_line": 1, "end_line": 1},
        },
        "releases": {"R1": ["S1", "S4"], "R2": ["S2", "S3"]},
        "marks": [
            {"manual": "M1", "line": 1, "col": 5, "candidates": ["L"]},
            {"manual": "M1", "line": 5, "col": 1, "candidates": ["L"]},
        ],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    assert "[x][L]" in result.releases["R1"].text
    assert "[L]: /dest" in result.releases["R1"].text
    r2 = result.releases["R2"].text
    assert "乙段也用 [x]。" in r2  # unmarked reference kept as shortcut
    assert "[x]: /dest" in r2
    assert "[L] 文字" in r2  # literal untouched


def test_fixed_conflict_does_not_doom_sibling_release():
    spec = {
        "manuals": {
            "M1": "共享 [x]。\n\n[x]: /d\n",
            "M2": "[z]: /1\n[z]: /2\n",
            "M3": "干净的一段。\n",
        },
        "segments": {
            "S1": {"manual": "M1", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M2", "start_line": 1, "end_line": 2},
            "S3": {"manual": "M3", "start_line": 1, "end_line": 1},
        },
        "releases": {"A": ["S1", "S2"], "B": ["S1", "S3"]},
        "marks": [
            {"manual": "M1", "line": 1, "col": 4, "candidates": ["y"]},
            {"manual": "M1", "line": 3, "col": 1, "candidates": ["y"]},
        ],
    }
    result = run_batch(load_spec(spec))
    # A has a decision-independent clash (two unmarked definitions share z)
    assert result.release_status["A"] == "blocked"
    assert result.report["fixed_conflicts"]["A"]
    # B shares the marked positions but is fine on its own
    assert result.release_status["B"] == "ok"
    assert result.report["changes"] == []


def test_unmarked_reference_blocks_release_as_fixed_conflict():
    manual = "正文[^a]。\n\n[^a]: 见 [p]。\n\n[p]: /x\n"
    spec = {
        "manuals": {"M": manual, "M2": "另一册的 [p] 只是文字。\n"},
        "segments": {
            "S": {"manual": "M", "start_line": 1, "end_line": 1},
            "S2": {"manual": "M2", "start_line": 1, "end_line": 1},
        },
        "releases": {"R": ["S", "S2"]},
    }
    result = run_batch(load_spec(spec))
    assert result.status == "blocked"
    conflicts = result.report["fixed_conflicts"]["R"]
    assert any("[p]" in c for c in conflicts)


def test_mark_on_footnote_ref_is_declined():
    spec = {
        "manuals": {"M": "正文[^核]。\n\n[^核]: 注。\n"},
        "segments": {"S": {"manual": "M", "start_line": 1, "end_line": 1}},
        "releases": {"R": ["S"]},
        "marks": [{"manual": "M", "line": 1, "col": 3, "candidates": ["x"]}],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    assert any("脚注" in w for w in result.report["warnings"])


def test_unknown_mark_position_warns():
    spec = {
        "manuals": {"M": "没有引用。\n"},
        "segments": {"S": {"manual": "M", "start_line": 1, "end_line": 1}},
        "releases": {"R": ["S"]},
        "marks": [{"manual": "M", "line": 1, "col": 2, "candidates": ["x"]}],
    }
    result = run_batch(load_spec(spec))
    assert result.status == "ok"
    assert any("未指向" in w for w in result.report["warnings"])


# ------------------------------------------------------------------ solver


def _solver_setup(count):
    """One release, `count` marked ref/def pairs, no conflicts."""
    model = ReleaseModel("R")
    variables = {}
    for i in range(count):
        def_key = ("M", i)
        ref_mark = f"M@{1000 + i}"
        def_mark = f"M@{2000 + i}"
        model.ref_reqs.append(
            RefReq(ref=None, def_key=def_key, orig_label=f"l{i}", mark_id=ref_mark)
        )
        variables[ref_mark] = Variable(
            mark_id=ref_mark,
            manual="M",
            offset=1000 + i,
            kind="ref",
            orig_label=f"l{i}",
            candidates=["x"],
            candidate_raw={"x": "x"},
            def_key=def_key,
            releases={"R"},
        )
        variables[def_mark] = Variable(
            mark_id=def_mark,
            manual="M",
            offset=2000 + i,
            kind="def",
            orig_label=f"l{i}",
            candidates=["x"],
            candidate_raw={"x": "x"},
            def_key=def_key,
            releases={"R"},
        )
    return variables, {"R": model}


def test_solver_timeout_reports_pending_not_blocked():
    variables, models = _solver_setup(30)
    solver = Solver(variables, models, {}, budget=0.0, check_interval=1)
    result = solver.solve()
    assert result.release_status["R"] == "pending"


def test_solver_same_instance_solves_with_budget():
    variables, models = _solver_setup(30)
    solver = Solver(variables, models, {}, budget=10.0)
    result = solver.solve()
    assert result.release_status["R"] == "ok"
    assert result.assignment == {}  # keeping originals is feasible and cheapest


# ------------------------------------------------------------- performance


def perf_spec(groups=10, releases_per_group=4, pairs=10):
    manuals, segments, releases, marks = {}, {}, {}, []
    for g in range(groups):
        shared = f"G{g}S"
        body_lines = [f"段{i}见[手册{g}_{i}]。" for i in range(pairs)]
        def_lines = [f"[手册{g}_{i}]: /g{g}/{i}" for i in range(pairs)]
        manuals[shared] = "\n\n".join(body_lines + def_lines) + "\n"
        segments[f"{shared}0"] = {
            "manual": shared,
            "start_line": 1,
            "end_line": 2 * pairs - 1,
        }
        for i in range(pairs):
            marks.append(
                {
                    "manual": shared,
                    "line": 2 * i + 1,
                    "col": 4,
                    "candidates": [f"a{g}_{i}", f"b{g}_{i}", f"c{g}_{i}"],
                }
            )
            marks.append(
                {
                    "manual": shared,
                    "line": 2 * pairs + 1 + 2 * i,
                    "col": 1,
                    "candidates": [f"a{g}_{i}", f"b{g}_{i}", f"c{g}_{i}"],
                }
            )
        for r in range(releases_per_group):
            own = f"G{g}R{r}"
            blocker = f"a{g}" if r % 2 == 0 else f"b{g}"
            hints = "、".join(f"[提示][{blocker}_{i}]" for i in range(pairs))
            literals = "、".join(f"[手册{g}_{i}]" for i in range(pairs))
            manuals[own] = f"说明 {hints} 与 {literals}。\n"
            segments[f"{own}1"] = {"manual": own, "start_line": 1, "end_line": 1}
            releases[f"G{g}版{r}"] = [f"{shared}0", f"{own}1"]
    return {
        "manuals": manuals,
        "segments": segments,
        "releases": releases,
        "marks": marks,
        "time_budget_seconds": 10.0,
    }


def test_daily_batch_within_budget():
    spec = load_spec(perf_spec())
    assert len(spec.releases) == 40
    assert len(spec.marks) == 200
    started = time.monotonic()
    result = run_batch(spec)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0
    assert result.status == "ok"
    # every shared position uses the same change in all four releases
    for change in result.report["changes"]:
        assert len(change["releases"]) == 4
    for rel in result.releases.values():
        assert rel.ok


def test_oversized_batch_shows_pending(monkeypatch):
    from batch_preview import pipeline

    class ImpatientSolver(Solver):
        def __init__(self, *args, **kwargs):
            kwargs["check_interval"] = 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pipeline, "Solver", ImpatientSolver)
    spec = perf_spec(groups=1, releases_per_group=2, pairs=3)
    spec["time_budget_seconds"] = 0.0
    result = run_batch(load_spec(spec))
    assert result.status == "pending"
    assert all(s == "pending" for s in result.release_status.values())


# --------------------------------------------------------------------- cli


def test_cli_roundtrip(tmp_path):
    spec = example_spec(["a", "b", "c"], {"甲": ["S0", "S2"], "乙": ["S0", "S3"]})
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    outdir = tmp_path / "out"
    assert cli_main([str(spec_path), "-o", str(outdir)]) == 0
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert (outdir / "甲.md").exists()
    assert (outdir / "乙.md").exists()


def test_cli_blocked_exit_code(tmp_path):
    spec = example_spec(["a", "b"], {"甲": ["S0", "S2"], "乙": ["S0", "S3"]})
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    assert cli_main([str(spec_path), "-o", str(tmp_path / "out")]) == 1
