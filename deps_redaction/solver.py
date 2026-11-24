"""Branch-and-bound solver for one redaction sentence.

The search has two layers:

1. Assign every hidden intermediate role to one of its permitted placeholder
   empty-node ids.  Assignments are tried in order of the number of distinct
   placeholder rows they consume, so the first feasible budget is optimal.
2. For a fixed assignment, every original edge maps to a deterministic output
   edge.  We choose a subset of those mapped edges with a DFS, pruning as soon
   as a query whose original answer is "no" gains a matching path, or a "yes"
   query loses every possible witness path.

No output edge is ever invented: each candidate is the renamed image of one or
more original edges, and the manifest keeps the original indices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from .graph import Edge, EnhancedGraph
from .model import Query, SentenceInput


class SearchCapExceeded(RuntimeError):
    pass


@dataclass
class TieAlternative:
    """An equally economical solution that was not delivered."""

    assignment: dict[str, str]
    edge_indices: tuple[int, ...]


@dataclass
class SentenceResult:
    sent_id: str
    feasible: bool
    placeholder_rows: dict[str, tuple[str, ...]] = field(default_factory=dict)
    kept_edge_ids: tuple[int, ...] = ()
    dropped_edge_ids: tuple[int, ...] = ()
    output_edges: tuple[Edge, ...] = ()
    query_answers: dict[str, bool] = field(default_factory=dict)
    original_answers: dict[str, bool] = field(default_factory=dict)
    ties: list[TieAlternative] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def placeholder_count(self) -> int:
        return len(self.placeholder_rows)


def _assignments(roles: tuple[str, ...], options: dict[str, tuple[str, ...]]):
    """Yield role->placeholder maps, cheapest (fewest distinct rows) first."""

    choices = [options[role] for role in roles]
    raw = [dict(zip(roles, picked)) for picked in product(*choices)] if roles else [{}]
    raw.sort(key=lambda mapping: (len(set(mapping.values())), tuple(sorted(mapping.items()))))
    return raw


class _AssignmentSolver:
    node_cap = 500_000

    def __init__(self, data: SentenceInput, assignment: dict[str, str]) -> None:
        self.data = data
        self.assignment = assignment
        self.original_graph = EnhancedGraph(data.edges)
        self.original_answers = self.original_graph.answers(data.queries)

        # Each candidate output edge records every original edge index that
        # maps onto it (duplicates collapse into one deliverable relation).
        self.candidate_support: dict[Edge, list[int]] = {}
        for index, edge in enumerate(data.edges):
            mapped = Edge(self._map(edge.src), self._map(edge.dst), edge.label)
            self.candidate_support.setdefault(mapped, []).append(index)
        self.candidates = tuple(self.candidate_support)

        # Original witness paths, translated to candidate edges, drive the
        # yes-query feasibility pruning.
        self.yes_witness_sets: dict[str, list[frozenset[Edge]]] = {}
        for query in data.queries:
            if not self.original_answers[query.name]:
                continue
            witnesses = []
            for path in self._all_witness_paths(self.original_graph, query):
                witnesses.append(
                    frozenset(Edge(self._map(e.src), self._map(e.dst), e.label) for e in path)
                )
            self.yes_witness_sets[query.name] = witnesses

        self.no_queries = [q for q in data.queries if not self.original_answers[q.name]]
        self.no_query_edges = self._edges_touching_no_queries()
        self.best: frozenset[Edge] | None = None
        self.visited = 0

    def _map(self, node_id: str) -> str:
        if node_id == "0":
            return "0"
        if node_id in self.data.id_map:
            return self.data.id_map[node_id]
        return self.assignment[node_id]

    def _all_witness_paths(self, graph: EnhancedGraph, query) -> list[tuple[Edge, ...]]:
        adj = graph.adjacency()
        results: list[tuple[Edge, ...]] = []

        def dfs(node: str, depth: int, trail: tuple[Edge, ...]) -> None:
            if depth == len(query.labels):
                if node == query.target:
                    results.append(trail)
                return
            for nxt, edge_label, edge in adj.get(node, ()):
                if edge_label == query.labels[depth]:
                    dfs(nxt, depth + 1, trail + (edge,))

        dfs(query.source, 0, ())
        return results

    def _edges_touching_no_queries(self) -> frozenset[Edge]:
        full_graph = EnhancedGraph(self.candidates)
        touched: set[Edge] = set()
        for query in self.no_queries:
            source = self.data.id_map[query.source]
            target = self.data.id_map[query.target]
            for path in self._witness_paths_on(full_graph, source, target, query.labels):
                touched.update(path)
        return frozenset(touched)

    def _witness_paths_on(self, graph: EnhancedGraph, source: str, target: str, labels):
        adj = graph.adjacency()
        results: list[tuple[Edge, ...]] = []

        def dfs(node: str, depth: int, trail: tuple[Edge, ...]) -> None:
            if depth == len(labels):
                if node == target:
                    results.append(trail)
                return
            for nxt, edge_label, edge in adj.get(node, ()):
                if edge_label == labels[depth]:
                    dfs(nxt, depth + 1, trail + (edge,))

        dfs(source, 0, ())
        return results

    def _no_violation(self, chosen: frozenset[Edge]) -> bool:
        graph = EnhancedGraph(chosen)
        for query in self.no_queries:
            if graph.query_yes(
                self.data.id_map[query.source],
                self.data.id_map[query.target],
                query.labels,
            ):
                return True
        return False

    def _yes_feasible(self, chosen: frozenset[Edge], remaining: frozenset[Edge]) -> bool:
        available = chosen | remaining
        for witnesses in self.yes_witness_sets.values():
            if not any(witness <= available for witness in witnesses):
                return False
        return True

    def search(self) -> frozenset[Edge] | None:
        full = frozenset(self.candidates)
        # Keeping every mapped edge retains the most relations, so it is the
        # optimum whenever it still witnesses every yes-query and answers no
        # no-query that was originally "no".
        if not self._no_violation(full) and self._yes_feasible(full, frozenset()):
            return full
        # Edges on no-query witness paths branch first: the keep/drop decision
        # on them is where pruning happens.
        ordered = tuple(
            sorted(self.candidates, key=lambda edge: edge not in self.no_query_edges)
        )
        self._dfs(0, frozenset(), frozenset(ordered), ordered)
        return self.best

    def _dfs(
        self,
        index: int,
        chosen: frozenset[Edge],
        remaining: frozenset[Edge],
        ordered: tuple[Edge, ...],
    ) -> None:
        self.visited += 1
        if self.visited > self.node_cap:
            raise SearchCapExceeded()
        if self.best is not None and len(chosen) + len(remaining) <= len(self.best):
            return
        if self._no_violation(chosen):
            return
        if not self._yes_feasible(chosen, remaining):
            return
        if index == len(ordered):
            if self.best is None or len(chosen) > len(self.best):
                self.best = chosen
            return

        edge = ordered[index]
        rest = remaining - {edge}
        self._dfs(index + 1, chosen | {edge}, rest, ordered)
        self._dfs(index + 1, chosen, rest, ordered)


def solve_sentence(data: SentenceInput) -> SentenceResult:
    result = SentenceResult(sent_id=data.sent_id, feasible=False)
    original_graph = EnhancedGraph(data.edges)
    result.original_answers = original_graph.answers(data.queries)

    problems = data.validate()
    if problems:
        result.diagnostics.extend(problems)
        return result

    roles = tuple(sorted(data.hidden_roles))
    options = {role: data.license.options_for(role) for role in roles}

    feasible_at_budget: list[tuple[dict[str, str], frozenset[Edge], _AssignmentSolver]] = []
    current_budget: int | None = None
    failures: list[str] = []

    for assignment in _assignments(roles, options):
        budget = len(set(assignment.values()))
        if current_budget is not None and budget > current_budget and feasible_at_budget:
            break
        current_budget = budget
        solver = _AssignmentSolver(data, assignment)
        try:
            chosen = solver.search()
        except SearchCapExceeded:
            failures.append(f"assignment {assignment}: search node cap reached")
            continue
        if chosen is None:
            failures.append(f"assignment {assignment}: no edge subset preserves all queries")
            continue
        feasible_at_budget.append((assignment, chosen, solver))

    if not feasible_at_budget:
        result.diagnostics.append(
            "no assignment of permitted placeholder nodes can preserve every checker query"
        )
        result.diagnostics.extend(_conflict_report(data, roles, options))
        result.diagnostics.extend(failures)
        return result

    max_kept = max(len(chosen) for _, chosen, _ in feasible_at_budget)
    winners = [item for item in feasible_at_budget if len(item[1]) == max_kept]
    assignment, chosen, solver = winners[0]

    kept_edge_ids: list[int] = []
    dropped_edge_ids: list[int] = []
    for index, edge in enumerate(data.edges):
        mapped = Edge(solver._map(edge.src), solver._map(edge.dst), edge.label)
        if mapped in chosen:
            kept_edge_ids.append(index)
        else:
            dropped_edge_ids.append(index)

    rows: dict[str, list[str]] = {}
    for role, placeholder in assignment.items():
        rows.setdefault(placeholder, []).append(role)

    result.feasible = True
    result.placeholder_rows = {key: tuple(sorted(value)) for key, value in sorted(rows.items())}
    result.kept_edge_ids = tuple(kept_edge_ids)
    result.dropped_edge_ids = tuple(dropped_edge_ids)
    result.output_edges = tuple(sorted(chosen, key=lambda e: (e.src, e.dst, e.label)))
    translated_queries = [
        Query(query.name, solver._map(query.source), solver._map(query.target), query.labels)
        for query in data.queries
    ]
    result.query_answers = EnhancedGraph(chosen).answers(translated_queries)
    result.ties = [
        TieAlternative(
            assignment=dict(alt_assignment),
            edge_indices=tuple(
                sorted(
                    original_index
                    for edge in alt_chosen
                    for original_index in alt_solver.candidate_support[edge]
                )
            ),
        )
        for alt_assignment, alt_chosen, alt_solver in winners[1:]
    ]
    if failures:
        result.diagnostics.append("rejected assignments at this budget: " + " | ".join(failures))
    return result


def _conflict_report(
    data: SentenceInput,
    roles: tuple[str, ...],
    options: dict[str, tuple[str, ...]],
) -> list[str]:
    """Name the queries and placeholders that cannot be reconciled."""

    lines: list[str] = []
    original_graph = EnhancedGraph(data.edges)
    original_answers = original_graph.answers(data.queries)
    for assignment in _assignments(roles, options):
        def mapped(node_id: str) -> str:
            if node_id == "0":
                return "0"
            if node_id in data.id_map:
                return data.id_map[node_id]
            return assignment[node_id]

        collapsed = [
            query.name
            for query in data.queries
            if original_answers[query.name] and mapped(query.source) == mapped(query.target)
        ]
        candidate_edges = [
            Edge(mapped(edge.src), mapped(edge.dst), edge.label) for edge in data.edges
        ]
        full_graph = EnhancedGraph(candidate_edges)
        violated = [
            query.name
            for query in data.queries
            if not original_answers[query.name]
            and full_graph.query_yes(
                data.id_map[query.source], data.id_map[query.target], query.labels
            )
        ]
        if collapsed or violated:
            sharing = ", ".join(
                f"{ph}<-[{'/'.join(role for role, value in assignment.items() if value == ph)}]"
                for ph in sorted(set(assignment.values()))
            )
            lines.append(
                f"assignment {{{sharing}}}: "
                f"collapsed yes-queries={collapsed or '-'}, "
                f"newly reachable no-queries={violated or '-'}"
            )
    return lines
