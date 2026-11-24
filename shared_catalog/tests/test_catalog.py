"""End-to-end verification of the handoff shared catalog.

Run from the repository root with:

    .venv/bin/pytest shared_catalog/tests/test_catalog.py

Everything is reconciled against the two original archives under
``shared_catalog/raw`` - nothing here trusts the build script's own assertions.
"""
from __future__ import annotations

import csv
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gapnewick as gn  # noqa: E402

RAW_A = ROOT / "raw" / "groupA_tree.nwk"
RAW_B = ROOT / "raw" / "groupB_tree.nex"
ALT1 = ROOT / "provenance" / "alt1_backbone.nwk"
ALT2 = ROOT / "provenance" / "alt2_backbone.nwk"
CATALOG = ROOT / "catalog"


@pytest.fixture(scope="module")
def data():
    archive_a = gn.read_tree(RAW_A)
    archive_b = gn.read_tree(RAW_B)
    alt1 = gn.read_tree(ALT1)
    alt2 = gn.read_tree(ALT2)
    return archive_a, archive_b, alt1, alt2


def read_catalog_tree(filename: str):
    return gn.read_tree(CATALOG / "trees" / filename)


def read_ledger(name: str) -> list[dict]:
    with (CATALOG / f"{name}.csv").open(encoding="utf8") as handle:
        return list(csv.DictReader(handle))


def assert_same_edges(left, right):
    idx_l, idx_r = gn.edge_index(left), gn.edge_index(right)
    assert set(idx_l) == set(idx_r), "clade sets differ"
    for clade in idx_r:
        assert idx_l[clade][0] == idx_r[clade][0], (
            f"length mismatch on {sorted(clade)}: {idx_l[clade][0]} != {idx_r[clade][0]}")


def test_backbones_replay_archives_exactly(data):
    archive_a, archive_b, alt1, alt2 = data
    leaves_a = set(archive_a.get_leaf_names())
    leaves_b = set(archive_b.get_leaf_names())

    recon_a, merges_a = gn.reconstruct(alt1, leaves_a)
    recon_b, merges_b = gn.reconstruct(alt2, leaves_b)
    assert_same_edges(recon_a, archive_a)
    assert_same_edges(recon_b, archive_b)
    # Each group loses one of the two bifurcations flanking the compressed path.
    assert {m.suppressed for m in merges_a} == {"F1"}
    assert {m.suppressed for m in merges_b} == {"F2"}


def test_gaps_survive_every_pipeline_stage(data):
    archive_a, archive_b, alt1, alt2 = data
    # Group A: L02 terminal has no length. Group B: L14 terminal has no length.
    assert gn.edge_index(archive_a)[frozenset({"L02"})][0] is gn.GAP
    assert gn.edge_index(archive_b)[frozenset({"L14"})][0] is gn.GAP
    # Gaps are still gaps in the catalog trees and never serialized as ":0".
    catalog_a = read_catalog_tree("groupA_slice.nwk")
    catalog_b = read_catalog_tree("groupB_slice.nwk")
    assert gn.edge_index(catalog_a)[frozenset({"L02"})][0] is gn.GAP
    assert gn.edge_index(catalog_b)[frozenset({"L14"})][0] is gn.GAP
    assert "L02:" not in (CATALOG / "trees" / "groupA_slice.nwk").read_text()
    assert "L14:" not in (CATALOG / "trees" / "groupB_slice.nwk").read_text()
    # Gap + measured segment keeps the measured value, gap + gap stays a gap.
    assert gn.add_lengths(gn.GAP, 0.06) == 0.06
    assert gn.add_lengths(0.06, gn.GAP) == 0.06
    assert gn.add_lengths(gn.GAP, gn.GAP) is gn.GAP


def test_catalog_slices_reconcile_to_raw_archives(data):
    archive_a, archive_b, _, _ = data
    assert_same_edges(read_catalog_tree("groupA_slice.nwk"), archive_a)
    assert_same_edges(read_catalog_tree("groupB_slice.nwk"), archive_b)
    # Support comments are retained on the slice but their semantics live in the ledger.
    comments_a = {n.comment for n in read_catalog_tree("groupA_slice.nwk").walk() if n.comment}
    comments_b = {n.comment for n in read_catalog_tree("groupB_slice.nwk").walk() if n.comment}
    assert comments_a == {"&A_bs=94"}
    assert comments_b == {"&B_bs=91"}


def test_root_to_leaf_path_totals_reconcile(data):
    archive_a, archive_b, alt1, alt2 = data

    def path_totals(tree, leaves):
        view, _ = gn.reconstruct(tree, leaves)
        totals: dict[str, float | None] = {}
        for leaf in leaves:
            node = next(n for n in view.walk() if n.name == leaf)
            total: float | None = 0.0
            while node.ancestor is not None:
                seg = gn.length(node)
                total = gn.add_lengths(total, seg)
                node = node.ancestor
            totals[leaf] = total
        return totals

    leaves_a = set(archive_a.get_leaf_names())
    leaves_b = set(archive_b.get_leaf_names())
    assert path_totals(alt1, leaves_a) == path_totals(archive_a, leaves_a)
    assert path_totals(alt2, leaves_b) == path_totals(archive_b, leaves_b)


def test_hard_conflict_is_a_polytomy_and_reported(data):
    archive_a, archive_b, _, _ = data
    shared = set(archive_a.get_leaf_names()) & set(archive_b.get_leaf_names())
    view_a, _ = gn.reconstruct(archive_a, shared)
    view_b, _ = gn.reconstruct(archive_b, shared)

    inner_a = frozenset({"L03", "L04"})
    inner_b = frozenset({"L04", "L05"})
    assert inner_a in gn.edge_index(view_a) and inner_a not in gn.edge_index(view_b)
    assert inner_b in gn.edge_index(view_b) and inner_b not in gn.edge_index(view_a)

    summary = read_catalog_tree("shared_summary.nwk")
    kf = next(n for n in summary.walk()
              if frozenset(n.get_leaf_names()) == frozenset({"L03", "L04", "L05"}))
    assert len(kf.descendants) == 3  # unresolved polytomy, contested inner edges withheld
    assert all(child.is_leaf for child in kf.descendants)

    conflicts = read_ledger("conflict_report")
    assert len(conflicts) == 1 and conflicts[0]["Conflict_ID"] == "CF01"
    assert "L03 L04" in conflicts[0]["Incompatible_Clades"]
    assert "L04 L05" in conflicts[0]["Incompatible_Clades"]
    assert "t_groupA, t_groupB, t_alt1, t_alt2" in conflicts[0]["Trees_Still_Registered"]


def test_compressed_totals_are_not_registered_as_shared_edges():
    edges = read_ledger("edge_ledger")
    shared = {(e["Clade"], e["Evidence_Status"]) for e in edges
              if e["Catalog_Tree_ID"] == "t_shared"}
    assert ("L01 L02", "two_path_totals_equal") in shared
    assert ("L06", "one_path_total") in shared
    # No shared edge in the compressed region is silently labelled a plain concordant edge.
    assert ("L01 L02", "concordant") not in shared
    assert ("L06", "concordant") not in shared

    merges = read_ledger("merge_ledger")
    by_stage = {}
    for row in merges:
        by_stage.setdefault(row["Stage"], []).append(row)
    assert {r["Surviving_Clade"] for r in by_stage["backbone_to_archive"]} == {
        "L01 L02", "L01 L02 L13"}
    # Archive -> shared scope suppression events carry the two equal 0.10 totals.
    view_rows = by_stage["archive_to_shared_scope"]
    z_rows = [r for r in view_rows if r["Surviving_Clade"] == "L01 L02"]
    assert {r["Source_Group"] for r in z_rows} == {"A", "B"}
    assert all(r["Combined_Total"] == "0.1" for r in z_rows)


def test_support_annotations_are_provenance_tagged_not_independent():
    annotations = read_ledger("annotation_ledger")
    assert len(annotations) == 2
    assert {a["Raw_Token"] for a in annotations} == {"A_bs=94", "B_bs=91"}
    assert all(a["Catalog_Edge_Clade"] == "L07 L08" for a in annotations)
    assert all(a["Independent_Support"] == "No" for a in annotations)
    # Each annotation is scoped to its own group's languages, never the other group's.
    scopes = {a["Source_Group"]: set(a["Scope"].split()) for a in annotations}
    assert "L13" not in scopes["A"] and "L11" not in scopes["B"]
    # The two source edges really are equally long (0.07) but carry distinct tokens.
    edges = read_ledger("edge_ledger")
    s_edges = [e for e in edges if e["Clade"] == "L07 L08"
               and e["Catalog_Tree_ID"] in {"t_groupA", "t_groupB"}]
    assert {e["Edge_Length"] for e in s_edges} == {"0.07"}


def test_same_named_languages_are_distinct_records():
    languages = read_ledger("languages")
    by_name: dict[str, list[str]] = {}
    for row in languages:
        by_name.setdefault(row["Name"], []).append(row["ID"])
    assert by_name["Koro"] == ["L11", "L13"]
    assert by_name["Mori"] == ["L12", "L14"]
    ids_glotto = {(row["ID"], row["Glottocode"]) for row in languages}
    assert ("L11", "koro1311") in ids_glotto and ("L13", "koro1313") in ids_glotto
    coverage = {row["ID"]: (row["Coverage_A"], row["Coverage_B"]) for row in languages}
    assert coverage["L11"] == ("Yes", "No")
    assert coverage["L13"] == ("No", "Yes")


def test_two_alternatives_are_reproducible_and_differ(data):
    _, _, alt1, alt2 = data
    idx1, idx2 = gn.edge_index(alt1), gn.edge_index(alt2)
    # Same leaf universe, different edge decomposition around the ambiguous path.
    assert set().union(*idx1) == set().union(*idx2)
    differing = sorted(set(idx1) ^ set(idx2), key=lambda c: sorted(c))
    assert frozenset({"L03", "L04"}) in differing
    assert frozenset({"L04", "L05"}) in differing
    # The flanking path has identical total but different internal splits.
    assert idx1[frozenset({"L01", "L02", "L13"})][0] == 0.03
    assert idx2[frozenset({"L01", "L02", "L13"})][0] == 0.06
    interpretations = read_ledger("interpretations")
    assert {i["Interpretation_ID"] for i in interpretations} == {"I1", "I2"}
    assert all(i["Replay_Procedure"] for i in interpretations)


def test_catalog_reads_back_through_pycldf_treetable():
    from pycldf import Dataset
    from pycldf.trees import TreeTable

    ds = Dataset.from_metadata(CATALOG / "Generic-metadata.json")
    tree_ids = {tree.id for tree in TreeTable(ds)}
    assert tree_ids == {"t_shared", "t_groupA", "t_groupB", "t_alt1", "t_alt2"}
    for tree in TreeTable(ds):
        node = tree.newick()
        assert node.get_leaf_names()
        assert tree.tree_branch_length_unit == "change"
