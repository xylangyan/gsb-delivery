"""End-to-end tests for bundle loading and CoNLL-U deliverable writing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pyconll.conllu import conllu

from deps_redaction.graph import Edge, EnhancedGraph
from deps_redaction.io_pipeline import load_bundle, run_bundle


DEMO = Path(__file__).resolve().parents[2] / "demo" / "bundle.json"


def test_demo_pipeline(tmp_path: Path):
    deliverable = tmp_path / "out.conllu"
    manifest = tmp_path / "manifest.json"
    results = run_bundle(DEMO, deliverable, manifest)

    by_id = {result.sent_id: result for result in results}
    assert by_id["s_merge"].feasible
    assert by_id["s_merge"].placeholder_count == 2
    assert not by_id["s_merge_strict"].feasible
    assert "qAD" in " ".join(by_id["s_merge_strict"].diagnostics)

    reparsed = conllu.load_from_string(deliverable.read_text(encoding="utf-8"))
    ids = [sentence.meta.get("sent_id") for sentence in reparsed]
    assert ids == ["s_merge", "s_merge_strict", "s_keep", "s_tie"]

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    bundle = json.loads(DEMO.read_text(encoding="utf-8"))
    records = {item["sent_id"]: item for item in manifest_data["sentences"]}
    for sentence, item in zip(reparsed, bundle["sentences"]):
        record = records[item["sent_id"]]
        if not record["feasible"]:
            continue
        edges = [
            Edge(head, token.id, relation[0])
            for token in sentence.tokens
            if not token.is_multiword()
            for head, relation in token.deps.items()
        ]
        graph = EnhancedGraph(edges)
        for query in item["queries"]:
            answer = graph.query_yes(
                item["id_map"][query["source"]],
                item["id_map"][query["target"]],
                query["labels"],
            )
            assert answer == record["original_answers"][query["name"]]


def test_edge_numbering_and_manifest_origin(tmp_path: Path):
    loaded = load_bundle(DEMO)
    merge = next(item for item in loaded if item.data.sent_id == "s_merge")
    assert list(merge.edge_numbers.values()) == list(range(1, 7))
    results = run_bundle(
        DEMO, tmp_path / "out.conllu", tmp_path / "manifest.json"
    )
    merge_result = next(result for result in results if result.sent_id == "s_merge")
    assert merge_result.kept_edge_ids == tuple(range(6))


def test_relation_numbering_follows_token_order(tmp_path: Path):
    bundle = {
        "sentences": [
            {
                "sent_id": "dup",
                "conllu": (
                    "# sent_id = dup\n"
                    "1\tw\t_\t_\t_\t_\t0\troot\t0:root\t_\n"
                    "2\tx\t_\t_\t_\t_\t1\tdep\t1:a\t_\n"
                    "3\ty\t_\t_\t_\t_\t2\tdep\t2:b\t_\n\n"
                ),
                "id_map": {"1": "1", "3": "2"},
                "hidden_roles": ["2"],
                "license": {"1.1": ["2"]},
                "queries": [
                    {"name": "q", "source": "1", "target": "3", "labels": ["a", "b"]}
                ],
            }
        ]
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    loaded = load_bundle(bundle_path)
    assert loaded[0].edge_numbers == {0: 1, 1: 2, 2: 3}

    manifest_path = tmp_path / "manifest.json"
    run_bundle(bundle_path, tmp_path / "out.conllu", manifest_path)
    record = json.loads(manifest_path.read_text(encoding="utf-8"))["sentences"][0]
    assert record["adopted_original_relation_numbers"] == [1, 2, 3]


def test_dangling_basic_head_is_reported_not_emitted(tmp_path: Path):
    bundle = {
        "sentences": [
            {
                "sent_id": "dangle",
                # Token 3's basic head is the hidden token 2.
                "conllu": (
                    "# sent_id = dangle\n"
                    "1\tw\t_\t_\t_\t_\t0\troot\t0:root\t_\n"
                    "2\tx\t_\t_\t_\t_\t1\tdep\t1:a\t_\n"
                    "3\ty\t_\t_\t_\t_\t2\tdep\t2:b\t_\n\n"
                ),
                "id_map": {"1": "1", "3": "3"},
                "hidden_roles": ["2"],
                "license": {"1.1": ["2"]},
                "queries": [
                    {"name": "q", "source": "1", "target": "3", "labels": ["a", "b"]}
                ],
            }
        ]
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    results = run_bundle(bundle_path, tmp_path / "out.conllu", manifest_path)
    assert not results[0].feasible
    record = json.loads(manifest_path.read_text(encoding="utf-8"))["sentences"][0]
    assert any("basic HEAD" in line for line in record["diagnostics"])


def test_cli_exit_code(tmp_path: Path):
    deliverable = tmp_path / "out.conllu"
    manifest = tmp_path / "manifest.json"
    completed = subprocess.run(
        [sys.executable, "-m", "deps_redaction.cli", str(DEMO), str(deliverable), str(manifest)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "3/4 sentences deliverable" in completed.stdout
