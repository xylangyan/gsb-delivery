"""Solver tests, including a brute-force oracle on random small instances."""

from __future__ import annotations

from itertools import product

import pytest

from deps_redaction.graph import Edge, EnhancedGraph
from deps_redaction.model import License, Query, SentenceInput
from deps_redaction.solver import _assignments, solve_sentence


def brute_force_optimum(data: SentenceInput):
    """Enumerate every assignment and every mapped-edge subset."""

    original_answers = EnhancedGraph(data.edges).answers(data.queries)
    roles = tuple(sorted(data.hidden_roles))
    options = {role: data.license.options_for(role) for role in roles}

    best = None
    for assignment in _assignments(roles, options):
        def mapped(node_id: str) -> str:
            if node_id == "0":
                return "0"
            if node_id in data.id_map:
                return data.id_map[node_id]
            return assignment[node_id]
        mapped_edges = {
            Edge(mapped(edge.src), mapped(edge.dst), edge.label) for edge in data.edges
        }
        mapped_edges = tuple(sorted(mapped_edges, key=lambda e: (e.src, e.dst, e.label)))
        budget = len(set(assignment.values()))
        translated = [
            Query(q.name, mapped(q.source), mapped(q.target), q.labels)
            for q in data.queries
        ]
        for mask in range(1 << len(mapped_edges)):
            chosen = {edge for index, edge in enumerate(mapped_edges) if mask >> index & 1}
            answers = EnhancedGraph(chosen).answers(translated)
            if answers != original_answers:
                continue
            score = (budget, -len(chosen))
            if best is None or score < best[0]:
                best = (score, budget, len(chosen))
    return best


def make_random_data(rng) -> SentenceInput:
    kept = [str(index) for index in range(1, rng.randint(3, 5))]
    hidden = [f"h{index}" for index in range(rng.randint(1, 3))]
    placeholders = [f"{index}.1" for index in range(1, rng.randint(2, 4))]
    license_map = {
        placeholder: tuple(
            role for role in hidden if rng.random() < 0.7
        )
        for placeholder in placeholders
    }
    for role in hidden:
        if not any(role in allowed for allowed in license_map.values()):
            license_map[placeholders[0]] = license_map[placeholders[0]] + (role,)

    nodes = kept + hidden
    labels = ["a", "b", "c"]
    edge_count = rng.randint(4, 8)
    edges = [
        Edge(
            rng.choice(["0"] + nodes),
            rng.choice(nodes),
            rng.choice(labels),
        )
        for _ in range(edge_count)
    ]
    queries = [
        Query(
            f"q{index}",
            rng.choice(kept),
            rng.choice(kept),
            tuple(rng.choice(labels) for _ in range(rng.randint(1, 2))),
        )
        for index in range(rng.randint(1, 3))
    ]
    return SentenceInput(
        sent_id="rand",
        id_map={token: token for token in kept},
        edges=edges,
        hidden_roles=set(hidden),
        license=License(license_map),
        queries=queries,
    )


def test_solver_matches_brute_force():
    rng = __import__("random").Random(20261124)
    for trial in range(60):
        data = make_random_data(rng)
        result = solve_sentence(data)
        oracle = brute_force_optimum(data)
        if oracle is None:
            assert not result.feasible, trial
            continue
        _, budget, kept_count = oracle
        assert result.feasible, trial
        assert result.placeholder_count == budget, (trial, result.placeholder_rows)
        assert len(result.output_edges) == kept_count, trial


def test_merge_conflict_requires_two_placeholder_rows():
    edges = [
        Edge("0", "1", "root"),
        Edge("1", "2", "r1"),
        Edge("2", "3", "r2"),
        Edge("0", "4", "root"),
        Edge("4", "5", "r3"),
        Edge("5", "6", "r4"),
    ]
    queries = [
        Query("qAB", "1", "3", ("r1", "r2")),
        Query("qCD", "4", "6", ("r3", "r4")),
        Query("qAD", "1", "6", ("r1", "r4")),
        Query("qCB", "4", "3", ("r3", "r2")),
    ]
    base = dict(
        sent_id="s",
        id_map={"1": "1", "3": "2", "4": "3", "6": "4"},
        edges=edges,
        hidden_roles={"2", "5"},
        queries=queries,
    )
    wide = SentenceInput(
        license=License({"2.1": ("2", "5"), "4.1": ("2", "5")}), **base
    )
    result = solve_sentence(wide)
    assert result.feasible
    assert result.placeholder_count == 2
    assert result.query_answers == {
        "qAB": True,
        "qCD": True,
        "qAD": False,
        "qCB": False,
    }

    strict = SentenceInput(license=License({"2.1": ("2", "5")}), **base)
    infeasible = solve_sentence(strict)
    assert not infeasible.feasible
    joined = " ".join(infeasible.diagnostics)
    assert "qAD" in joined and "qCB" in joined


def test_yes_query_forces_retention():
    data = SentenceInput(
        sent_id="keep",
        id_map={"1": "1", "2": "2"},
        edges=[
            Edge("0", "1", "root"),
            Edge("1", "3", "a"),
            Edge("3", "2", "b"),
        ],
        hidden_roles={"3"},
        license=License({"1.1": ("3",), "2.1": ("3",)}),
        queries=[
            Query("via", "1", "2", ("a", "b")),
            Query("direct", "1", "2", ("b",)),
        ],
    )
    result = solve_sentence(data)
    assert result.feasible
    assert len(result.output_edges) == 3
    assert result.query_answers == {"via": True, "direct": False}
    assert result.dropped_edge_ids == ()


def test_no_invented_edges_and_traceability():
    data = SentenceInput(
        sent_id="trace",
        id_map={"1": "1", "3": "2"},
        edges=[Edge("1", "2", "u"), Edge("2", "3", "v"), Edge("0", "1", "root")],
        hidden_roles={"2"},
        license=License({"1.1": ("2",), "2.1": ("2",)}),
        queries=[Query("q", "1", "3", ("u", "v"))],
    )
    result = solve_sentence(data)
    assert result.feasible
    assignment = {role: ph for ph, roles in result.placeholder_rows.items() for role in roles}

    def mapped(node_id: str) -> str:
        if node_id == "0":
            return "0"
        if node_id in data.id_map:
            return data.id_map[node_id]
        return assignment[node_id]

    images = {Edge(mapped(e.src), mapped(e.dst), e.label) for e in data.edges}
    assert set(result.output_edges) <= images
    assert len(result.ties) == 1


def test_validation_rejects_unknown_endpoint():
    data = SentenceInput(
        sent_id="bad",
        id_map={"1": "1"},
        edges=[Edge("1", "9", "x")],
        hidden_roles=set(),
        license=License({}),
        queries=[],
    )
    result = solve_sentence(data)
    assert not result.feasible
    assert any("9" in line for line in result.diagnostics)


def test_search_cap_is_reported_as_diagnostic(monkeypatch):
    from deps_redaction import solver as solver_module

    monkeypatch.setattr(solver_module._AssignmentSolver, "node_cap", 0)
    data = SentenceInput(
        sent_id="cap",
        id_map={"1": "1", "3": "2"},
        edges=[
            Edge("0", "1", "root"),
            Edge("1", "h1", "a"),
            Edge("h2", "3", "b"),
            Edge("1", "3", "a"),
        ],
        hidden_roles={"h1", "h2"},
        license=License({"1.1": ("h1", "h2")}),
        # Originally false (a leaves through h1, b leaves through h2); merging
        # both roles into one placeholder makes it reachable, forcing the DFS.
        queries=[Query("no", "1", "3", ("a", "b"))],
    )
    result = solve_sentence(data)
    assert not result.feasible
    assert any("cap" in line for line in result.diagnostics)
