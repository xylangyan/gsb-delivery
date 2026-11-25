"""Nightly top-N refresh for a fixed two-term OR query under a block-read budget.

Materials produced/consumed (the "given" inputs of the task):
  * last night's ranking receipt (old top-N, old global stats, generation),
  * the new Searcher's real global statistics (Whoosh BM25F: idf/avgfl from
    the top-level searcher, counting deleted docs, exactly as the code does),
  * the list of deleted permanent ids (compliance filter),
  * a cheap block catalog (per term-block summaries: ranges / tf totals /
    doc lists),
  * an itemized block-read budget.

The verifier publishes the longest confirmable prefix of the new top-N,
proves every unread block excluded, and emits two witnesses for the first
undetermined rank. Everything is checked against a ground-truth full search
on the real new index (true Whoosh BM25F, no index modification).
"""

import json
import shutil
import sys
from math import log
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whoosh import fields, index, query, scoring  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
IDX_OLD = OUT / "idx_old"
IDX_NEW = OUT / "idx_new"

FIELD = "body"
TERMS = ("alfa", "bravo")
TOPN = 4
READ_BUDGET = 3  # block reads allowed tonight

B, K1 = 0.75, 1.2  # whoosh.scoring.BM25F defaults

# docid -> body text. Old segment: every document byte-identical to last night.
OLD_DOCS = [
    ("P1", "alfa alfa"),
    ("P2", "bravo bravo bravo bravo"),  # compliance-deleted in the new version
    ("P3", "alfa bravo"),
    ("P4", "alfa"),
    ("P5", "bravo"),
    ("P6", "charlie"),
]
# New segment: a batch of arrivals, most carrying the common term "alfa".
NEW_DOCS = (
    [("Q1", "alfa alfa")]
    + [("Q%d" % i, "alfa") for i in range(2, 10)]
    + [("Q10", "bravo bravo bravo")]
)
DELETED_IDS = ["P2"]  # permanent ids filtered out for compliance


# --------------------------------------------------------------------------
# BM25F pieces replicated straight from whoosh.scoring (verified below against
# the real searcher, so the verifier provably uses the same numbers).

def bm25f_idf(doc_count_all, df):
    # whoosh.scoring.WeightingModel.idf: log(dc / (df + 1)) + 1
    return log(doc_count_all / (df + 1)) + 1


def bm25f_term_score(idf, tf, fl, avgfl):
    # whoosh.scoring.bm25
    return idf * ((tf * (K1 + 1)) / (tf + K1 * ((1 - B) + B * fl / avgfl)))


# --------------------------------------------------------------------------
def build_schema():
    return fields.Schema(
        docid=fields.ID(stored=True, unique=True),
        body=fields.TEXT(stored=True),
    )


def build_indexes():
    if OUT.exists():
        shutil.rmtree(OUT)
    IDX_OLD.mkdir(parents=True)

    ix = index.create_in(str(IDX_OLD), build_schema())
    with ix.writer() as w:
        for docid, body in OLD_DOCS:
            w.add_document(docid=docid, body=body)

    # The new version starts from a byte-copy of last night's index; the
    # original segment is never rewritten (verified by file hashes below).
    shutil.copytree(str(IDX_OLD), str(IDX_NEW))
    ix_new = index.open_dir(str(IDX_NEW))
    with ix_new.writer() as w:
        for docid in DELETED_IDS:
            w.delete_by_term("docid", docid)
        for docid, body in NEW_DOCS:
            w.add_document(docid=docid, body=body)

    old_files = {p.name: p.read_bytes() for p in IDX_OLD.iterdir() if p.is_file()}
    new_files = {p.name: p.read_bytes() for p in IDX_NEW.iterdir() if p.is_file()}
    preserved = sorted(n for n in old_files if new_files.get(n) == old_files[n])
    added = sorted(n for n in new_files if n not in old_files)
    return {"preserved_segment_files": preserved, "added_files": added}


def make_query():
    return query.Or([query.Term(FIELD, t) for t in TERMS])


def search_receipt(ixdir):
    """Run the fixed query and capture a receipt: ranking + global stats."""
    ix = index.open_dir(str(ixdir))
    searcher = ix.searcher(weighting=scoring.BM25F())
    results = searcher.search(make_query(), limit=None)
    ranking = [
        {"rank": hit.rank + 1, "docnum": hit.docnum, "docid": hit["docid"],
         "score": hit.score}
        for hit in results
    ]
    reader = searcher.reader()
    id_map = {}
    for docnum in range(reader.doc_count_all()):
        if not reader.is_deleted(docnum):
            id_map[docnum] = reader.stored_fields(docnum)["docid"]
    receipt = {
        "generation": ix.latest_generation(),
        "query": " OR ".join("%s:%s" % (FIELD, t) for t in TERMS),
        "weighting": "BM25F(B=0.75, K1=1.2)",
        "global_stats": global_stats(searcher),
        "docid_by_docnum": id_map,
        "top%d" % TOPN: ranking[:TOPN],
        "full_ranking": ranking,
    }
    searcher.close()
    return receipt


def global_stats(searcher):
    """The global statistics the new Searcher actually uses (top-level,
    deleted documents included, per whoosh.searching.Searcher)."""
    stats = {
        "doc_count_all": searcher.doc_count_all(),
        "doc_count_live": searcher.doc_count(),
        "field_length": searcher.field_length(FIELD),
        "avg_field_length": searcher.avg_field_length(FIELD),
        "terms": {},
    }
    for text in TERMS:
        df = searcher.doc_frequency(FIELD, text)
        stats["terms"][text] = {
            "df": df,
            "idf": bm25f_idf(stats["doc_count_all"], df),
        }
    return stats


# --------------------------------------------------------------------------
# Block catalog. A "block" is one term's posting run inside one segment.
# Summaries are the cheap catalog data; only an explicit read_block() (charged
# against the budget) reveals per-document tf/length detail.

def segment_readers(searcher):
    reader = searcher.reader()
    if hasattr(reader, "readers"):  # MultiReader
        return list(zip(reader.readers, reader.doc_offsets))
    return [(reader, 0)]


def block_ground_truth(searcher, keep_seg):
    """Full raw posting detail per (term, segment), as stored on disk --
    deletion filters are NOT applied (a raw block read still sees the
    deleted doc's posting; the deletion list is a separate material).
    Global docnums = segment offset + local docnum."""
    blocks = {}
    for segreader, offset in segment_readers(searcher):
        seg = "seg%d" % (offset and 1 or 0)  # seg0 = old, seg1 = new
        if seg != keep_seg:
            continue
        for text in TERMS:
            if (FIELD, text) not in segreader:
                continue
            postings = {}
            m = segreader.postings(FIELD, text)
            while m.is_active():
                local = m.id()
                tf = m.value_as("frequency")
                dl = segreader.doc_field_length(local, FIELD, 0)
                postings[offset + local] = (tf, dl)
                m.next()
            blocks[(text, seg)] = postings
    return blocks


def deleted_docnums_of(searcher):
    deleted = set()
    for segreader, offset in segment_readers(searcher):
        deleted.update(offset + d for d in segreader.deleted_docs_set)
    return sorted(deleted)


def mint_catalog(blocks):
    """Cheap summaries, deliberately of mixed granularity (the catalog may
    only certify ranges, tf totals, or the doc set a block can land on)."""
    catalog = {}
    for (text, seg), postings in blocks.items():
        docnums = sorted(postings)
        tfs = [tf for tf, _ in postings.values()]
        dls = [dl for _, dl in postings.values()]
        key = "%s@%s" % (text, seg)
        if key == "alfa@seg0":
            # fine-grained: the catalog says exactly which docs carry it
            catalog[key] = {"kind": "doclist", "docs": docnums, "df": len(docnums),
                            "max_tf": max(tfs), "sum_tf": sum(tfs),
                            "min_dl": min(dls), "max_dl": max(dls)}
        elif key == "bravo@seg0":
            # coarse: only a range and totals -- the deleted doc may hold max_tf
            catalog[key] = {"kind": "range", "range": [min(docnums), max(docnums)],
                            "df": len(docnums), "max_tf": max(tfs),
                            "sum_tf": sum(tfs),
                            "min_dl": min(dls), "max_dl": max(dls)}
        elif key == "alfa@seg1":
            # range + totals + a (loose) certified length bracket
            catalog[key] = {"kind": "range", "range": [min(docnums), max(docnums)],
                            "df": len(docnums), "max_tf": max(tfs),
                            "sum_tf": sum(tfs),
                            "min_dl": min(dls), "max_dl": max(dls) + 1}
        else:  # bravo@seg1
            # doc list but no lengths
            catalog[key] = {"kind": "doclist", "docs": docnums, "df": len(docnums),
                            "max_tf": max(tfs), "sum_tf": sum(tfs)}
    return catalog


# --------------------------------------------------------------------------
class BudgetedVerifier:
    def __init__(self, stats, catalog, blocks, deleted_docnums, budget):
        self.stats = stats
        self.catalog = catalog
        self.blocks = blocks  # ground truth, only touched via read_block()
        self.deleted = set(deleted_docnums)
        self.budget = budget
        self.reads = []  # charged block reads
        self.exact = {}  # docnum -> {term: (tf, dl)}

    def read_block(self, key):
        assert len(self.reads) < self.budget, "read budget exhausted"
        text, seg = key.split("@")
        self.reads.append(key)
        for docnum, (tf, dl) in self.blocks[(text, seg)].items():
            self.exact.setdefault(docnum, {})[text] = (tf, dl)

    def term_bound(self, key, docnum):
        """Upper bound of one term's contribution for a doc, from catalog
        data only (no read). 0 if the summary proves the doc carries no
        posting for the term."""
        text, seg = key.split("@")
        summary = self.catalog[key]
        if summary["kind"] == "doclist":
            if docnum not in summary["docs"]:
                return 0.0
        else:
            lo, hi = summary["range"]
            if not (lo <= docnum <= hi):
                return 0.0
            # a range summary with df < range size does not prove presence;
            # the bound below is still valid for absent docs (they score 0).
        max_tf = summary["max_tf"]
        min_dl = summary.get("min_dl", max_tf)
        # field length is at least the term frequency
        best_dl = max(max_tf, min_dl)
        idf = self.stats["terms"][text]["idf"]
        return bm25f_term_score(idf, max_tf, best_dl, self.stats["avg_field_length"])

    def doc_bound(self, docnum):
        if docnum in self.deleted:
            return 0.0
        total = 0.0
        for key in self.catalog:
            text = key.split("@")[0]
            if docnum in self.exact and text in self.exact[docnum]:
                tf, dl = self.exact[docnum][text]
                idf = self.stats["terms"][text]["idf"]
                total += bm25f_term_score(idf, tf, dl, self.stats["avg_field_length"])
            elif docnum in self.exact and self._block_read(key):
                continue  # read block proves absence
            else:
                total += self.term_bound(key, docnum)
        return total

    def _block_read(self, key):
        return key in self.reads

    def exact_score(self, docnum):
        total = 0.0
        for text, (tf, dl) in self.exact.get(docnum, {}).items():
            idf = self.stats["terms"][text]["idf"]
            total += bm25f_term_score(idf, tf, dl, self.stats["avg_field_length"])
        return total


def confirm_prefix(verifier, candidate_docnums, topn):
    """Longest prefix of the new top-N that the evidence confirms.

    A doc with exact score s takes the next rank iff every other live doc d
    has bound(d) < s, or bound(d) == s with docnum(d) > docnum(candidate)
    (Whoosh orders equal scores by ascending docnum in the new generation).
    """
    confirmed = []
    remaining = set(candidate_docnums) - verifier.deleted
    exact_pool = {d for d in remaining if d in verifier.exact}
    while exact_pool and len(confirmed) < topn:
        best = max(exact_pool, key=lambda d: (verifier.exact_score(d), -d))
        score = verifier.exact_score(best)
        exact_pool.discard(best)
        ok = True
        for other in remaining:
            if other == best:
                continue
            bound = (verifier.exact_score(other)
                     if other in verifier.exact and other in exact_pool
                     else verifier.doc_bound(other))
            if other in verifier.exact and other not in exact_pool:
                bound = verifier.exact_score(other)
            if bound > score + 1e-12:
                ok = False
                break
            if abs(bound - score) <= 1e-12 and other < best:
                ok = False
                break
        if not ok:
            break
        confirmed.append((best, score))
        remaining.discard(best)
    return confirmed


# --------------------------------------------------------------------------
def main():
    file_report = build_indexes()

    # -- material 1: last night's receipt (old generation still opens) -------
    old_receipt = search_receipt(IDX_OLD)

    # -- materials 2-4: new searcher stats, deletions, block catalog ---------
    ix_new = index.open_dir(str(IDX_NEW))
    searcher = ix_new.searcher(weighting=scoring.BM25F())
    new_stats = global_stats(searcher)
    # Raw blocks: seg0 read from last night's index files (byte-identical in
    # the new version, deletions not yet applied), seg1 from the new segment.
    ix_old = index.open_dir(str(IDX_OLD))
    old_searcher = ix_old.searcher(weighting=scoring.BM25F())
    blocks = block_ground_truth(old_searcher, "seg0")
    blocks.update(block_ground_truth(searcher, "seg1"))
    deleted_docnums = deleted_docnums_of(searcher)
    catalog = mint_catalog(blocks)

    # permanent-id deletion list -> new-generation docnums, via the receipt's
    # docid<->docnum map of the untouched old segment
    id_to_docnum = {v: k for k, v in old_receipt["docid_by_docnum"].items()}
    deleted_resolution = {docid: id_to_docnum[docid] for docid in DELETED_IDS}
    assert set(deleted_resolution.values()) == set(deleted_docnums)

    # ground truth: full real search on the new index (no modification)
    truth = searcher.search(make_query(), limit=None)
    truth_ranking = [
        {"rank": hit.rank + 1, "docnum": hit.docnum, "docid": hit["docid"],
         "score": hit.score}
        for hit in truth
    ]

    # -- budgeted verification ------------------------------------------------
    verifier = BudgetedVerifier(new_stats, catalog, blocks, deleted_docnums,
                                READ_BUDGET)
    # Read decisions (3 of 4 blocks, exactly the budget):
    #  * bravo@seg0: coarse summary says max_tf=4 somewhere in docs 0-5 and
    #    the deletion list removed docnum 1 -- the max may be gone; must read.
    #  * alfa@seg0: exact scores of the old candidates P1/P3/P4 to publish.
    #  * bravo@seg1: doc list says doc 15 holds max_tf=3, but no length is
    #    certified; must read to publish Q10's exact score.
    #  * alfa@seg1 is NOT read: its certified bound cannot beat the rank-4
    #    score, and equal scores lose on docnum (see report).
    for key in ("bravo@seg0", "alfa@seg0", "bravo@seg1"):
        verifier.read_block(key)

    all_docnums = set()
    for postings in blocks.values():
        all_docnums.update(postings)
    confirmed = confirm_prefix(verifier, all_docnums, TOPN)

    docid_of = {}
    for docnum in all_docnums:
        for segreader, offset in segment_readers(searcher):
            if offset <= docnum < offset + segreader.doc_count_all():
                docid_of[docnum] = segreader.stored_fields(
                    docnum - offset)["docid"]
                break

    published = [
        {"rank": i + 1, "docnum": d, "docid": docid_of[d], "score": s}
        for i, (d, s) in enumerate(confirmed)
    ]

    # verify the verifier: recomputed exact scores must match real BM25F hits
    truth_score = {t["docnum"]: t["score"] for t in truth_ranking}
    for docnum, score in confirmed:
        assert abs(score - truth_score[docnum]) < 1e-9, (docnum, score)
    # published prefix must equal the ground-truth prefix
    truth_prefix = [t["docid"] for t in truth_ranking[: len(published)]]
    assert [p["docid"] for p in published] == truth_prefix, (
        published, truth_prefix)

    # -- the fabricated-document instability ----------------------------------
    avgfl = new_stats["avg_field_length"]
    idf_a = new_stats["terms"]["alfa"]["idf"]
    idf_b = new_stats["terms"]["bravo"]["idf"]
    fab_pre_delete = (
        bm25f_term_score(idf_a, catalog["alfa@seg0"]["max_tf"], 2, avgfl)
        + bm25f_term_score(idf_b, catalog["bravo@seg0"]["max_tf"], 4, avgfl)
    )
    fab_post_delete = (
        bm25f_term_score(idf_a, catalog["alfa@seg0"]["max_tf"], 2, avgfl)
        + bm25f_term_score(idf_b, catalog["bravo@seg1"]["max_tf"], 3, avgfl)
    )

    # what the deleted candidate would have scored tonight (its posting is
    # still intact in the raw seg0 block; only the deletion list removes it)
    p2_tf, p2_dl = blocks[("bravo", "seg0")][deleted_resolution["P2"]]
    p2_would_be = bm25f_term_score(idf_b, p2_tf, p2_dl, avgfl)

    # -- witnesses for the first undetermined rank (rank TOPN+1) --------------
    def rank5_under(filling):
        """filling: docnum -> (tf_alfa, dl) hypothesis for the unread block
        alfa@seg1. Returns the rank-(TOPN+1) (docid, score)."""
        scores = {}
        for docnum in all_docnums:
            if docnum in verifier.deleted:
                continue
            total = verifier.exact_score(docnum)
            if docnum in filling:
                tf, dl = filling[docnum]
                total += bm25f_term_score(idf_a, tf, dl, avgfl)
            scores[docnum] = total
        order = sorted(scores, key=lambda d: (-scores[d], d))
        d = order[TOPN]  # 0-based rank TOPN+1
        return docid_of[d], scores[d]

    summary = catalog["alfa@seg1"]
    lo, hi = summary["range"]
    witness_a = {lo: (2, 2)}
    for d in range(lo + 1, hi + 1):
        witness_a[d] = (1, 1)
    witness_b = dict(witness_a)
    witness_b[lo] = (2, 3)

    def consistent(filling):
        tfs = [tf for tf, _ in filling.values()]
        dls = [dl for _, dl in filling.values()]
        return (
            len(filling) == summary["df"]
            and max(tfs) == summary["max_tf"]
            and sum(tfs) == summary["sum_tf"]
            and min(dls) >= summary["min_dl"]
            and max(dls) <= summary["max_dl"]
            and all(dl >= tf for tf, dl in filling.values())
        )

    assert consistent(witness_a) and consistent(witness_b)
    rank5_a = rank5_under(witness_a)
    rank5_b = rank5_under(witness_b)
    assert rank5_a[0] != rank5_b[0] or abs(rank5_a[1] - rank5_b[1]) > 1e-12
    # witness A is the truth: unread block really is Q1=(2,2), Q2..=(1,1)
    truth_rank5 = truth_ranking[TOPN]
    assert rank5_a[0] == truth_rank5["docid"]
    assert abs(rank5_a[1] - truth_rank5["score"]) < 1e-9

    searcher.close()
    old_searcher.close()

    # -- artifacts -------------------------------------------------------------
    artifacts = {
        "files": file_report,
        "receipt_lastnight": old_receipt,
        "new_global_stats": new_stats,
        "deleted_permanent_ids": DELETED_IDS,
        "deleted_resolution": deleted_resolution,
        "block_catalog": catalog,
        "read_budget": READ_BUDGET,
        "blocks_read": verifier.reads,
        "blocks_not_read": sorted(set(catalog) - set(verifier.reads)),
        "published_top%d" % TOPN: published,
        "fabricated_doc_bound": {
            "pre_deletion": fab_pre_delete,
            "post_deletion": fab_post_delete,
            "confirmed_top1": published[0]["score"],
        },
        "deleted_candidate_would_be": {
            "docid": "P2", "score": p2_would_be,
        },
        "rank5_witnesses": {
            "W1": {"filling_head": "Q1=(tf2,dl2), Q2..Q9=(tf1,dl1)",
                   "rank5": {"docid": rank5_a[0], "score": rank5_a[1]}},
            "W2": {"filling_head": "Q1=(tf2,dl3), Q2..Q9=(tf1,dl1)",
                   "rank5": {"docid": rank5_b[0], "score": rank5_b[1]}},
        },
        "ground_truth_top6": truth_ranking[: TOPN + 2],
    }
    (OUT / "verification.json").write_text(
        json.dumps(artifacts, indent=2, ensure_ascii=False))
    print(json.dumps({"published": published,
                      "rank5_W1": rank5_a, "rank5_W2": rank5_b,
                      "reads": verifier.reads}, indent=2))
    return artifacts


if __name__ == "__main__":
    main()
