"""Build the handoff shared catalog from the two archived survey trees.

Pipeline (mirrors the archival process described in the task):

1. Read group A's CLDF-linked Newick and group B's NEXUS tree.
2. Replay each group's prune-and-suppress pipeline from two alternative full
   backbones and assert an exact topology/length match against the archives
   (gaps stay gaps).
3. Restrict both groups to the mergeable LanguageTable identities, detect hard
   clade conflicts and build a common summary tree in which only the conflicting
   region is left as an unresolved polytomy; everything else registers normally.
4. Emit gap-aware Newick for every catalog tree and machine-readable ledgers for
   edge provenance, suppression events, support annotations and conflicts.
"""
from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import newick  # noqa: E402

import gapnewick as gn  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
PROV = ROOT / "provenance"
CATALOG = ROOT / "catalog"
TREE_DIR = CATALOG / "trees"

UNIT = "change"


def write_csv(path: pathlib.Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def clade_label(clade: frozenset[str]) -> str:
    return " ".join(sorted(clade))


def drop_internal_names(tree: newick.Node, keep_comments: bool) -> newick.Node:
    clone = gn._clone(tree)  # noqa: SLF001
    for node in clone.walk():
        if not node.is_leaf:
            node.name = None
            if not keep_comments:
                node.comments = []
    return clone


def build_shared_summary(view_a: newick.Node, view_b: newick.Node, shared: set[str]):
    """Intersect concordant clades; leave hard conflicts as an unresolved polytomy."""
    idx_a = gn.edge_index(view_a)
    idx_b = gn.edge_index(view_b)
    internal_a = {c for c in idx_a if len(c) > 1}
    internal_b = {c for c in idx_b if len(c) > 1}
    concordant = internal_a & internal_b
    conflicts = sorted(internal_a ^ internal_b, key=lambda c: (len(c), sorted(c)))
    polytomy_nodes = {
        clade for clade in concordant
        if any(clade > conflict for conflict in conflicts)
    }

    leaf_length: dict[str, float | None] = {}
    leaf_origin: dict[str, str] = {}
    for leaf in sorted(shared):
        key = frozenset({leaf})
        len_a, _ = idx_a[key]
        len_b, _ = idx_b[key]
        if len_a == len_b:
            leaf_length[leaf] = len_a
            leaf_origin[leaf] = "concordant"
        else:
            leaf_length[leaf] = gn.GAP
            leaf_origin[leaf] = "gap_vs_measurement" if gn.GAP in (len_a, len_b) \
                else "disagreeing_lengths"

    nodes: dict[frozenset[str], newick.Node] = {
        frozenset({leaf}): newick.Node(name=leaf, length=leaf_length[leaf])
        for leaf in shared
    }
    for clade in sorted(concordant, key=len):
        members = [
            nodes[sub] for sub in sorted(
                (c for c in nodes if c < clade
                 and not any(c < d < clade for d in concordant)),
                key=lambda c: (len(c), sorted(c)))
        ]
        node = newick.Node(length=None)
        for child in members:
            node.add_descendant(child)
        nodes[clade] = node

    root = newick.Node()
    for clade in sorted(
            (c for c in nodes if not any(c < d for d in nodes)),
            key=lambda c: (len(c), sorted(c))):
        root.add_descendant(nodes[clade])

    for clade in concordant:
        len_a, _ = idx_a[clade]
        len_b, _ = idx_b[clade]
        assert len_a == len_b, f"concordant clade {clade} disagrees on length"
        nodes[clade].length = len_a

    return root, conflicts, polytomy_nodes, idx_a, idx_b, leaf_origin


def main() -> None:
    TREE_DIR.mkdir(parents=True, exist_ok=True)
    PROV.mkdir(parents=True, exist_ok=True)

    archive_a = gn.read_tree(RAW / "groupA_tree.nwk")
    archive_b = gn.read_tree(RAW / "groupB_tree.nex")
    leaves_a = set(archive_a.get_leaf_names())
    leaves_b = set(archive_b.get_leaf_names())
    shared = leaves_a & leaves_b

    alt1 = gn.read_tree(PROV / "alt1_backbone.nwk")
    alt2 = gn.read_tree(PROV / "alt2_backbone.nwk")

    recon_a, merges_backbone_a = gn.reconstruct(alt1, leaves_a)
    recon_b, merges_backbone_b = gn.reconstruct(alt2, leaves_b)
    assert_exact_match(recon_a, archive_a, "alt1 -> group A")
    assert_exact_match(recon_b, archive_b, "alt2 -> group B")

    view_a, merges_view_a = gn.reconstruct(archive_a, shared)
    view_b, merges_view_b = gn.reconstruct(archive_b, shared)
    summary, conflicts, polytomy_nodes, idx_shared_a, idx_shared_b, leaf_origin = \
        build_shared_summary(view_a, view_b, shared)

    catalog_trees = {
        "t_shared": ("shared_summary.nwk", drop_internal_names(summary, False)),
        "t_groupA": ("groupA_slice.nwk", drop_internal_names(archive_a, True)),
        "t_groupB": ("groupB_slice.nwk", drop_internal_names(archive_b, True)),
        "t_alt1": ("alt1_full.nwk", drop_internal_names(alt1, False)),
        "t_alt2": ("alt2_full.nwk", drop_internal_names(alt2, False)),
    }
    for filename, tree in catalog_trees.values():
        (TREE_DIR / filename).write_text(gn.serialize(tree) + "\n", encoding="utf8")

    merged_clades_a = {m.clade for m in merges_backbone_a}
    merged_clades_b = {m.clade for m in merges_backbone_b}
    merged_view_a = {m.clade for m in merges_view_a}
    merged_view_b = {m.clade for m in merges_view_b}

    write_ledgers(
        catalog_trees, archive_a, archive_b, alt1, alt2, summary,
        conflicts, polytomy_nodes, shared, leaf_origin,
        merges_backbone_a, merges_backbone_b, merges_view_a, merges_view_b,
        merged_clades_a, merged_clades_b, merged_view_a, merged_view_b,
        leaves_a, leaves_b, idx_shared_a, idx_shared_b,
    )
    write_cldf(catalog_trees, leaves_a, leaves_b)
    print("catalog built at", CATALOG)


def assert_exact_match(reconstructed: newick.Node, target: newick.Node, label: str) -> None:
    idx_r, idx_t = gn.edge_index(reconstructed), gn.edge_index(target)
    assert set(idx_r) == set(idx_t), f"{label}: clade set mismatch"
    for clade, (length, _) in idx_t.items():
        assert idx_r[clade][0] == length, f"{label}: length mismatch on {clade_label(clade)}"


def write_ledgers(catalog_trees, archive_a, archive_b, alt1, alt2, summary,
                  conflicts, polytomy_nodes, shared, leaf_origin,
                  merges_backbone_a, merges_backbone_b,
                  merges_view_a, merges_view_b,
                  merged_clades_a, merged_clades_b,
                  merged_view_a, merged_view_b, leaves_a, leaves_b,
                  idx_shared_a, idx_shared_b):
    edge_rows: list[dict] = []
    eid = 0

    def add_edge(tree_id, clade, length, status, group, path, note=""):
        nonlocal eid
        eid += 1
        edge_rows.append(dict(
            Edge_ID=f"E{eid:03d}",
            Catalog_Tree_ID=tree_id,
            Clade=clade_label(clade),
            Edge_Length="" if length is gn.GAP else f"{length:g}",
            Is_Gap="Yes" if length is gn.GAP else "No",
            Evidence_Status=status,
            Source_Group=group,
            Source_Path=path,
            Note=note,
        ))

    for clade, (length, _) in sorted(gn.edge_index(archive_a).items(),
                                     key=lambda kv: sorted(kv[0])):
        status = "merged_total" if clade in merged_clades_a else "single_observation"
        note = ("compressed branch: archived total only; not an observation of one "
                "common edge") if status == "merged_total" else ""
        if length is gn.GAP:
            note = (note + "; " if note else "") + "archived gap preserved"
        add_edge("t_groupA", clade, length, status, "A",
                 f"raw/groupA_tree.nwk#{clade_label(clade)}", note)

    for clade, (length, _) in sorted(gn.edge_index(archive_b).items(),
                                     key=lambda kv: sorted(kv[0])):
        status = "merged_total" if clade in merged_clades_b else "single_observation"
        note = ("compressed branch: archived total only; not an observation of one "
                "common edge") if status == "merged_total" else ""
        if length is gn.GAP:
            note = (note + "; " if note else "") + "archived gap preserved"
        add_edge("t_groupB", clade, length, status, "B",
                 f"raw/groupB_tree.nex#{clade_label(clade)}", note)

    for clade, (length, _) in sorted(gn.edge_index(summary).items(),
                                     key=lambda kv: sorted(kv[0])):
        if clade in polytomy_nodes:
            status, note = "unresolved_polytomy", (
                "hard A/B conflict below this node; contested inner edges withheld, "
                "not averaged; see conflict_report.csv")
        elif clade in merged_view_a and clade in merged_view_b:
            status, note = "two_path_totals_equal", (
                "both groups only see a compressed path total here (see merge_ledger.csv); "
                "the equal totals are not two observations of one common edge")
        elif clade in merged_view_a or clade in merged_view_b:
            status, note = "one_path_total", (
                "only one group observes a compressed total on this clade (see "
                "merge_ledger.csv); registered as a path total, not a shared edge length")
        elif len(clade) == 1 and leaf_origin.get(next(iter(clade)), "").startswith("gap"):
            len_a, len_b = idx_shared_a[clade][0], idx_shared_b[clade][0]
            other = len_b if len_a is gn.GAP else len_a
            status, note = "gap_archive", (
                f"one archive stores a gap here; the other records {other:g}; "
                "common tree keeps the gap rather than importing the measured value")
        else:
            status, note = "concordant", ""
        add_edge("t_shared", clade, length, status, "A+B", "trees/shared_summary.nwk", note)

    alt_paths = {"t_alt1": "provenance/alt1_backbone.nwk",
                 "t_alt2": "provenance/alt2_backbone.nwk"}
    for tree_id, backbone in (("t_alt1", alt1), ("t_alt2", alt2)):
        for clade, (length, _) in sorted(gn.edge_index(backbone).items(),
                                         key=lambda kv: sorted(kv[0])):
            add_edge(tree_id, clade, length, "alternative_split", "alt",
                     alt_paths[tree_id],
                     "alternative decomposition; segment totals reconcile to the archive")

    write_csv(PROV / "edge_ledger.csv",
              ["Edge_ID", "Catalog_Tree_ID", "Clade", "Edge_Length", "Is_Gap",
               "Evidence_Status", "Source_Group", "Source_Path", "Note"],
              edge_rows)

    merge_rows = []
    mid = 0

    def add_merges(merges, group, source_file, stage):
        nonlocal mid
        for merge in merges:
            mid += 1
            merge_rows.append(dict(
                Merge_ID=f"MG{mid:02d}",
                Source_Group=group,
                Stage=stage,
                Source_File=source_file,
                Suppressed_Node=merge.suppressed,
                Surviving_Clade=clade_label(merge.clade),
                Upper_Segment="" if merge.upper_length is gn.GAP else f"{merge.upper_length:g}",
                Lower_Segment="" if merge.lower_length is gn.GAP else f"{merge.lower_length:g}",
                Combined_Total="" if merge.combined_length is gn.GAP
                else f"{merge.combined_length:g}",
                Contains_Gap="Yes" if merge.combined_length is gn.GAP else "No",
                Note="merged total spans a bifurcation invisible at this coverage; "
                     "register as a path total, never as one observed common edge",
            ))

    add_merges(merges_backbone_a, "A", "provenance/alt1_backbone.nwk",
               "backbone_to_archive")
    add_merges(merges_backbone_b, "B", "provenance/alt2_backbone.nwk",
               "backbone_to_archive")
    add_merges(merges_view_a, "A", "raw/groupA_tree.nwk",
               "archive_to_shared_scope")
    add_merges(merges_view_b, "B", "raw/groupB_tree.nex",
               "archive_to_shared_scope")
    write_csv(PROV / "merge_ledger.csv",
              ["Merge_ID", "Source_Group", "Stage", "Source_File", "Suppressed_Node",
               "Surviving_Clade", "Upper_Segment", "Lower_Segment", "Combined_Total",
               "Contains_Gap", "Note"],
              merge_rows)

    annotation_rows = [
        dict(Annotation_ID="AN01", Source_Group="A", Raw_Token="A_bs=94",
             Archive_Node="SA", Archive_Clade="L07 L08",
             Scope=clade_label(frozenset(leaves_a)), Catalog_Edge_Clade="L07 L08",
             Independent_Support="No",
             Note="support computed from group A's selected language set only; not a "
                  "second independent support value for the shared edge, and no evidence "
                  "for languages outside A's coverage"),
        dict(Annotation_ID="AN02", Source_Group="B", Raw_Token="B_bs=91",
             Archive_Node="SB", Archive_Clade="L07 L08",
             Scope=clade_label(frozenset(leaves_b)), Catalog_Edge_Clade="L07 L08",
             Independent_Support="No",
             Note="support computed from group B's selected language set only; the "
                  "same-length edge after pruning is one edge with two provenance-tagged "
                  "annotations, never pooled as independent support"),
    ]
    write_csv(PROV / "annotation_ledger.csv",
              ["Annotation_ID", "Source_Group", "Raw_Token", "Archive_Node",
               "Archive_Clade", "Scope", "Catalog_Edge_Clade",
               "Independent_Support", "Note"],
              annotation_rows)

    conflict_rows = [dict(
        Conflict_ID="CF01",
        Region="L03 L04 L05",
        Group_A_Constraint="inner clade (L03,L04):0.05 inside (L03,L04,L05):0.04",
        Group_B_Constraint="inner clade (L04,L05):0.05 inside (L03,L04,L05):0.04",
        Incompatible_Clades="; ".join(clade_label(c) for c in conflicts),
        Catalog_Resolution="t_shared renders (L03,L04,L05):0.04 as an unresolved "
                           "polytomy; the contested inner 0.05 edges are withheld, not "
                           "averaged or merged",
        Trees_Still_Registered="t_groupA, t_groupB, t_alt1, t_alt2 register unchanged; "
                               "only the shared summary is affected",
    )]
    write_csv(PROV / "conflict_report.csv",
              ["Conflict_ID", "Region", "Group_A_Constraint", "Group_B_Constraint",
               "Incompatible_Clades", "Catalog_Resolution", "Trees_Still_Registered"],
              conflict_rows)

    interpretation_rows = [
        dict(Interpretation_ID="I1", Tree_ID="t_alt1",
             Backbone_File="provenance/alt1_backbone.nwk", Reproduces="A",
             Replay_Procedure="prune to A leaves then suppress unary nodes "
                              "(gapnewick.reconstruct)",
             Difference_Versus_Other="path above (L01,L02) splits 0.05/0.03/0.02 and A "
                                     "loses the first bifurcation; L06 lies in "
                                     "(L06,L12):0.06; KF region follows A"),
        dict(Interpretation_ID="I2", Tree_ID="t_alt2",
             Backbone_File="provenance/alt2_backbone.nwk", Reproduces="B",
             Replay_Procedure="prune to B leaves then suppress unary nodes "
                              "(gapnewick.reconstruct)",
             Difference_Versus_Other="same path splits 0.02/0.06/0.02 and B loses the "
                                     "second bifurcation; L06 is a direct 0.07 leaf; KF "
                                     "region follows B; L14 keeps its archived gap"),
    ]
    write_csv(PROV / "interpretations.csv",
              ["Interpretation_ID", "Tree_ID", "Backbone_File", "Reproduces",
               "Replay_Procedure", "Difference_Versus_Other"],
              interpretation_rows)


def write_cldf(catalog_trees, leaves_a, leaves_b):
    from pycldf import Generic

    ds = Generic.in_dir(CATALOG)
    ds.add_component("LanguageTable")
    ds.add_component("MediaTable")
    ds.add_component("TreeTable")

    ds.add_table(
        "edge_ledger.csv",
        dict(name="Edge_ID", required=True),
        "Catalog_Tree_ID", "Clade", "Edge_Length", "Is_Gap",
        "Evidence_Status", "Source_Group", "Source_Path", "Note",
        primaryKey=["Edge_ID"],
    )
    ds.add_table(
        "merge_ledger.csv",
        dict(name="Merge_ID", required=True),
        "Source_Group", "Stage", "Source_File", "Suppressed_Node", "Surviving_Clade",
        "Upper_Segment", "Lower_Segment", "Combined_Total", "Contains_Gap", "Note",
        primaryKey=["Merge_ID"],
    )
    ds.add_table(
        "annotation_ledger.csv",
        dict(name="Annotation_ID", required=True),
        "Source_Group", "Raw_Token", "Archive_Node", "Archive_Clade", "Scope",
        "Catalog_Edge_Clade", "Independent_Support", "Note",
        primaryKey=["Annotation_ID"],
    )
    ds.add_table(
        "conflict_report.csv",
        dict(name="Conflict_ID", required=True),
        "Region", "Group_A_Constraint", "Group_B_Constraint", "Incompatible_Clades",
        "Catalog_Resolution", "Trees_Still_Registered",
        primaryKey=["Conflict_ID"],
    )
    ds.add_table(
        "interpretations.csv",
        dict(name="Interpretation_ID", required=True),
        "Tree_ID", "Backbone_File", "Reproduces", "Replay_Procedure",
        "Difference_Versus_Other",
        primaryKey=["Interpretation_ID"],
    )
    ds.add_foreign_key("edge_ledger.csv", "Catalog_Tree_ID", "trees.csv", "ID")
    ds.add_foreign_key("interpretations.csv", "Tree_ID", "trees.csv", "ID")

    # ID -> (Name, Glottocode, lat, lon). L11/L13 and L12/L14 deliberately share a
    # Name to model same-named but distinct LanguageTable records.
    names = {
        "L01": ("Talu", "talu1234", 28.4, 96.1),
        "L02": ("Nalu", "nalu1235", 28.1, 96.4),
        "L03": ("Sora", "sora1236", 27.9, 95.8),
        "L04": ("Kira", "kira1237", 27.5, 95.5),
        "L05": ("Vema", "vema1238", 27.2, 95.9),
        "L06": ("Duli", "duli1239", 26.9, 96.2),
        "L07": ("Pesha", "pesh1240", 26.6, 95.7),
        "L08": ("Runa", "runa1241", 26.3, 95.4),
        "L11": ("Koro", "koro1311", 25.8, 95.1),
        "L12": ("Mori", "mori1312", 25.5, 94.8),
        "L13": ("Koro", "koro1313", 24.9, 94.2),
        "L14": ("Mori", "mori1314", 24.6, 93.9),
    }
    ds.add_columns("LanguageTable",
                   {"name": "Coverage_A", "datatype": "string"},
                   {"name": "Coverage_B", "datatype": "string"})
    languages = []
    for lid in sorted(names):
        name, glotto, lat, lon = names[lid]
        languages.append(dict(
            ID=lid, Name=name, Latitude=lat, Longitude=lon, Glottocode=glotto,
            Coverage_A="Yes" if lid in leaves_a else "No",
            Coverage_B="Yes" if lid in leaves_b else "No",
        ))

    media = [
        dict(ID="m_shared", Name="shared_summary.nwk", Media_Type="text/x-nh",
             Download_URL="file:///trees/shared_summary.nwk",
             Description="Common summary on mergeable identities; conflict region unresolved"),
        dict(ID="m_groupA", Name="groupA_slice.nwk", Media_Type="text/x-nh",
             Download_URL="file:///trees/groupA_slice.nwk",
             Description="Group A archived topology and lengths; reconciles to raw Newick"),
        dict(ID="m_groupB", Name="groupB_slice.nwk", Media_Type="text/x-nh",
             Download_URL="file:///trees/groupB_slice.nwk",
             Description="Group B archived topology and lengths (NEXUS source); gaps preserved"),
        dict(ID="m_alt1", Name="alt1_full.nwk", Media_Type="text/x-nh",
             Download_URL="file:///trees/alt1_full.nwk",
             Description="Alternative full reconstruction replaying group A's archive"),
        dict(ID="m_alt2", Name="alt2_full.nwk", Media_Type="text/x-nh",
             Download_URL="file:///trees/alt2_full.nwk",
             Description="Alternative full reconstruction replaying group B's archive"),
    ]
    trees = [
        dict(ID="t_shared", Name="1", Media_ID="m_shared", Tree_Is_Rooted=True,
             Tree_Type="summary", Tree_Branch_Length_Unit=UNIT,
             Description="Common tree; hard conflict at L03/L04/L05 kept as a polytomy"),
        dict(ID="t_groupA", Name="1", Media_ID="m_groupA", Tree_Is_Rooted=True,
             Tree_Type="summary", Tree_Branch_Length_Unit=UNIT,
             Description="Group A slice; reconciles exactly to raw/groupA_tree.nwk"),
        dict(ID="t_groupB", Name="1", Media_ID="m_groupB", Tree_Is_Rooted=True,
             Tree_Type="summary", Tree_Branch_Length_Unit=UNIT,
             Description="Group B slice; reconciles exactly to raw/groupB_tree.nex"),
        dict(ID="t_alt1", Name="1", Media_ID="m_alt1", Tree_Is_Rooted=True,
             Tree_Type="summary", Tree_Branch_Length_Unit=UNIT,
             Description="Alternative interpretation 1; see interpretations.csv"),
        dict(ID="t_alt2", Name="1", Media_ID="m_alt2", Tree_Is_Rooted=True,
             Tree_Type="summary", Tree_Branch_Length_Unit=UNIT,
             Description="Alternative interpretation 2; see interpretations.csv"),
    ]
    ds.write(LanguageTable=languages, MediaTable=media, TreeTable=trees)

    for name in ["edge_ledger", "merge_ledger", "annotation_ledger",
                 "conflict_report", "interpretations"]:
        (CATALOG / f"{name}.csv").write_text(
            (PROV / f"{name}.csv").read_text(encoding="utf8"), encoding="utf8")


if __name__ == "__main__":
    main()
