#!/usr/bin/env python3
"""盘点对账时点核销:版本化目录 + 盘点出现,基于 PyPika 对 SQLite 做时点匹配。

语义规则:
- 每条目录版本有独立记录号(record_no)与生效时刻(effective_at),按仓内业务键(仓号, 货号)核对。
- 更正记录只写"上一版本记录号"(prev_record_no),沿链继承仓号、货号。
- 空货号仅当行声明公共库存(is_public=1)时代表公共;明确未知的空值不参与确定匹配。
- 批次可乱序送达(arrival_seq 只记录到达顺序,不影响效力),可撤销已生效条目。
- 正常参照 / 撤销 / 恢复各从自己的生效时刻起生效。
- 撤销后旧参照不算命中;只有可能相关的未知记录可报告待定。
- 恢复后沿完整继承链找到原键,报告实际命中的新版本与来源链。
- 断链必须能定位,不得借另一仓、后一版本或公共记录补身份。
- 每次库存出现恰好一条结果:明确匹配 / 未匹配 / 待定,各附证据。
- 无版本声明(prev_record_no 为空)的行按原有普通键比较。
"""

import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional

from pypika import Column, Order, Query, Table

CATALOG = Table("catalog_version")
OCCURRENCE = Table("inventory_occurrence")

MATCHED = "MATCHED"      # 明确匹配
UNMATCHED = "UNMATCHED"  # 未匹配
PENDING = "PENDING"      # 待定


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class Version:
    record_no: str
    prev_record_no: Optional[str]
    warehouse_no: Optional[str]
    item_no: Optional[str]
    is_public: bool
    is_revoked: bool
    effective_at: str


@dataclass
class Resolved:
    """一条版本沿 prev 链解析的结果。"""
    record_no: str
    chain: list = field(default_factory=list)        # 从本版本到根的记录号路径
    warehouse_no: Optional[str] = None               # 继承得到的仓号
    item_no: Optional[str] = None                    # 继承得到的货号
    is_public: bool = False
    broken_at: Optional[str] = None                  # 断链发生的记录号
    missing_prev: Optional[str] = None               # 缺失的上一版本记录号

    @property
    def broken(self):
        return self.missing_prev is not None


# ---------------------------------------------------------------- 建库 / 装载(全部 SQL 由 PyPika 生成)

def create_schema(conn):
    for q in (
        Query.create_table(CATALOG).columns(
            Column("record_no", "TEXT PRIMARY KEY"),
            Column("prev_record_no", "TEXT"),
            Column("warehouse_no", "TEXT"),
            Column("item_no", "TEXT"),
            Column("is_public", "INTEGER NOT NULL"),
            Column("is_revoked", "INTEGER NOT NULL"),
            Column("effective_at", "TEXT NOT NULL"),
            Column("arrival_seq", "INTEGER NOT NULL"),
        ),
        Query.create_table(OCCURRENCE).columns(
            Column("occ_id", "TEXT PRIMARY KEY"),
            Column("warehouse_no", "TEXT NOT NULL"),
            Column("item_no", "TEXT"),
            Column("counted_at", "TEXT NOT NULL"),
        ),
    ):
        conn.execute(str(q))


def load_catalog(conn, rows_in_arrival_order):
    """按送达顺序写入目录批次;arrival_seq 仅记录到达先后,不参与效力判断。"""
    c = CATALOG
    for seq, row in enumerate(rows_in_arrival_order):
        q = (
            Query.into(c)
            .columns("record_no", "prev_record_no", "warehouse_no", "item_no",
                     "is_public", "is_revoked", "effective_at", "arrival_seq")
            .insert(row.record_no, row.prev_record_no, row.warehouse_no, row.item_no,
                    int(row.is_public), int(row.is_revoked), row.effective_at, seq)
        )
        conn.execute(str(q))


def load_occurrences(conn, rows):
    o = OCCURRENCE
    for occ_id, warehouse_no, item_no, counted_at in rows:
        q = (Query.into(o).columns("occ_id", "warehouse_no", "item_no", "counted_at")
             .insert(occ_id, warehouse_no, item_no, counted_at))
        conn.execute(str(q))


def fetch_versions_at(conn, time_point):
    """时点查询:只取生效时刻 <= time_point 的版本(尚未生效的版本不可见)。"""
    c = CATALOG
    q = (
        Query.from_(c)
        .select(c.record_no, c.prev_record_no, c.warehouse_no, c.item_no,
                c.is_public, c.is_revoked, c.effective_at)
        .where(c.effective_at <= time_point)
        .orderby(c.effective_at, order=Order.asc)
        .orderby(c.record_no, order=Order.asc)
    )
    return [Version(*row) for row in conn.execute(str(q)).fetchall()]


def fetch_occurrences_at(conn, time_point):
    o = OCCURRENCE
    q = (
        Query.from_(o)
        .select(o.occ_id, o.warehouse_no, o.item_no, o.counted_at)
        .where(o.counted_at == time_point)
        .orderby(o.occ_id, order=Order.asc)
    )
    return conn.execute(str(q)).fetchall()


# ---------------------------------------------------------------- 继承链解析

def resolve_chains(versions):
    """对每条版本沿 prev_record_no 走到根,继承仓号/货号;断链可定位。"""
    by_no = {v.record_no: v for v in versions}
    resolved = {}
    for v in versions:
        path, seen, cur = [], set(), v.record_no
        missing = None
        while cur is not None:
            if cur in seen:  # 防御:成环视为断链
                missing = cur
                break
            seen.add(cur)
            row = by_no.get(cur)
            if row is None:
                missing = cur
                break
            path.append(cur)
            cur = row.prev_record_no
        chain_rows = [by_no[no] for no in path]
        warehouse_no = next((r.warehouse_no for r in chain_rows if r.warehouse_no is not None), None)
        item_no = next((r.item_no for r in chain_rows if r.item_no is not None), None)
        resolved[v.record_no] = Resolved(
            record_no=v.record_no,
            chain=path,
            warehouse_no=warehouse_no,
            item_no=item_no,
            is_public=any(r.is_public for r in chain_rows),
            broken_at=path[-1] if (missing and path) else v.record_no,
            missing_prev=missing,
        )
    return resolved


def key_state(events):
    """一组同业务键的版本事件(已按生效时刻排序)在时点上的状态。

    返回 (state, hit_record_no):state 为 'valid' / 'revoked';撤销时 hit 为 None。
    """
    latest = events[-1]
    if latest.is_revoked:
        return "revoked", None
    return "valid", latest.record_no


# ---------------------------------------------------------------- 时点核销

def reconcile_at(conn, time_point):
    versions = fetch_versions_at(conn, time_point)
    by_no = {v.record_no: v for v in versions}
    resolved = resolve_chains(versions)

    broken = {no: r for no, r in resolved.items() if r.broken}
    usable = {no: r for no, r in resolved.items() if not r.broken}

    # 按仓内业务键分组(公共行货号为空,键为 (仓号, None) 且仅匹配公共语义)
    groups = {}
    for r in usable.values():
        groups.setdefault((r.warehouse_no, r.item_no), []).append(by_no[r.record_no])
    for ev in groups.values():
        ev.sort(key=lambda v: (v.effective_at, v.record_no))  # 效力只认生效时刻,与到达顺序无关

    results = []
    for occ_id, warehouse_no, item_no, counted_at in fetch_occurrences_at(conn, time_point):
        if item_no is not None:
            results.append(match_known(occ_id, warehouse_no, item_no, counted_at, groups, resolved))
        else:
            results.append(match_unknown(occ_id, warehouse_no, counted_at, groups, resolved))
    return results, broken


def match_known(occ_id, warehouse_no, item_no, counted_at, groups, resolved):
    key = (warehouse_no, item_no)
    events = groups.get(key)
    if not events:
        return {"occ_id": occ_id, "counted_at": counted_at, "key": key, "status": UNMATCHED,
                "evidence": {"reason": "仓内无该业务键的目录记录"}}
    state, hit = key_state(events)
    if state == "revoked":
        revoked_by = events[-1]
        last_valid = next((v for v in reversed(events) if not v.is_revoked), None)
        return {"occ_id": occ_id, "counted_at": counted_at, "key": key, "status": UNMATCHED,
                "evidence": {
                    "reason": "该时点目录已撤销,旧参照存在也不算命中",
                    "revoked_by": revoked_by.record_no, "revoked_at": revoked_by.effective_at,
                    "stale_reference": last_valid.record_no if last_valid else None}}
    return {"occ_id": occ_id, "counted_at": counted_at, "key": key, "status": MATCHED,
            "evidence": {"hit_record_no": hit, "hit_effective_at": by_no_eff(events, hit),
                         "source_chain": resolved[hit].chain}}


def by_no_eff(events, record_no):
    return next(v.effective_at for v in events if v.record_no == record_no)


def match_unknown(occ_id, warehouse_no, counted_at, groups, resolved):
    """货号明确未知:不参与确定匹配,只就可能相关的同仓记录报告待定。"""
    candidates = []
    for (wh, _item), events in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if wh != warehouse_no:
            continue  # 不借另一仓补身份
        state, hit = key_state(events)
        tip = events[-1]
        r = resolved[tip.record_no]
        candidates.append({
            "record_no": tip.record_no,
            "resolved_item_no": r.item_no,
            "is_public": r.is_public,
            "state_at_point": state,
            "source_chain": r.chain,
        })
    if not candidates:
        return {"occ_id": occ_id, "counted_at": counted_at, "key": (warehouse_no, None),
                "status": UNMATCHED, "evidence": {"reason": "仓内无任何可能相关的目录记录"}}
    return {"occ_id": occ_id, "counted_at": counted_at, "key": (warehouse_no, None),
            "status": PENDING,
            "evidence": {"reason": "货号未知,不能确定匹配;存在可能相关的同仓记录",
                         "candidates": candidates}}


# ---------------------------------------------------------------- 场景数据

T1, T2, T3 = "2026-11-19T08:30:00", "2026-11-19T09:30:00", "2026-11-19T10:30:00"

V1 = Version("V1", None, "W1", "I1", False, False, "2026-11-19T08:00:00")   # 正常参照
V2 = Version("V2", "V1", None, None, False, True, "2026-11-19T09:00:00")    # 只写上一版本号 + 撤销
V3 = Version("V3", "V2", None, None, False, False, "2026-11-19T10:00:00")   # 继承被撤销记录号并恢复有效
B1 = Version("B1", "V-NOT-EXIST", None, None, False, False, "2026-11-19T08:15:00")  # 断链:前版不存在
W2 = Version("V4", None, "W2", "I1", False, False, "2026-11-19T08:00:00")   # 同货号另一仓
PUB = Version("V5", None, "W1", None, True, False, "2026-11-19T08:00:00")   # 明确的公共参照

OCCURRENCES = [
    ("O1", "W1", "I1", T1),   # 真实出现 1(与 O2 值相同)
    ("O2", "W1", "I1", T1),   # 真实出现 2(与 O1 值相同)
    ("O3", "W1", "I1", T2),   # 撤销后、恢复前
    ("O4", "W1", None, T2),   # 同仓货号未知,撤销与恢复之间可能匹配
    ("O5", "W1", "I1", T3),   # 恢复后
    ("O6", "W2", "I1", T1),   # 另一仓同货号
]

# 后两条(V2、V3)在第一条(V1)之前送到
ARRIVAL_ORDER_A = [V2, V3, V1, B1, W2, PUB]
# 改变输入顺序,验证结果与送达顺序无关
ARRIVAL_ORDER_B = [PUB, W2, B1, V3, V1, V2]


def run_scenario(arrival_order):
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    load_catalog(conn, arrival_order)
    load_occurrences(conn, OCCURRENCES)
    out = {}
    for tp in (T1, T2, T3):
        results, broken = reconcile_at(conn, tp)
        occ_ids = [r["occ_id"] for r in results]
        expected = [o[0] for o in OCCURRENCES if o[3] == tp]
        assert sorted(occ_ids) == sorted(expected), (tp, occ_ids, expected)
        assert len(occ_ids) == len(set(occ_ids)), "每次库存出现必须恰好一条结果"
        out[tp] = (results, broken)
    conn.close()
    return out


def check(results_by_tp):
    def result(tp, occ_id):
        return next(r for r in results_by_tp[tp][0] if r["occ_id"] == occ_id)

    r1, r2 = result(T1, "O1"), result(T1, "O2")
    assert r1["status"] == MATCHED and r1["evidence"]["hit_record_no"] == "V1"
    assert r2["status"] == MATCHED and r2["evidence"]["hit_record_no"] == "V1"  # 同值两次出现各自一条结果

    r3 = result(T2, "O3")
    assert r3["status"] == UNMATCHED and r3["evidence"]["revoked_by"] == "V2"
    assert r3["evidence"]["stale_reference"] == "V1"  # 旧参照存在也不算命中

    r4 = result(T2, "O4")
    assert r4["status"] == PENDING
    cand = {c["record_no"] for c in r4["evidence"]["candidates"]}
    assert "V2" in cand and "V5" in cand and "V4" not in cand  # 可能相关才待定;不借另一仓

    r5 = result(T3, "O5")
    assert r5["status"] == MATCHED and r5["evidence"]["hit_record_no"] == "V3"
    assert r5["evidence"]["source_chain"] == ["V3", "V2", "V1"]  # 沿完整继承链找到原键

    r6 = result(T1, "O6")
    assert r6["status"] == MATCHED and r6["evidence"]["hit_record_no"] == "V4"

    for tp in (T1, T2, T3):
        broken = results_by_tp[tp][1]
        assert broken["B1"].missing_prev == "V-NOT-EXIST"  # 断链可定位
        assert broken["B1"].warehouse_no is None and broken["B1"].item_no is None  # 未借身份
        for r in results_by_tp[tp][0]:
            assert "B1" not in str(r["evidence"])  # 断链记录不参与任何匹配


def report(results_by_tp):
    name = {MATCHED: "明确匹配", UNMATCHED: "未匹配", PENDING: "待定"}
    for tp in (T1, T2, T3):
        results, broken = results_by_tp[tp]
        print(f"\n== 盘点时点 {tp} ==")
        for r in results:
            ev = r["evidence"]
            print(f"  {r['occ_id']} 键={r['key']} -> {name[r['status']]}")
            if r["status"] == MATCHED:
                print(f"      命中版本 {ev['hit_record_no']} (生效 {ev['hit_effective_at']}),来源链 {' -> '.join(ev['source_chain'])}")
            elif r["status"] == PENDING:
                for c in ev["candidates"]:
                    print(f"      候选 {c['record_no']} 货号={c['resolved_item_no']} 公共={c['is_public']} 时点状态={c['state_at_point']}")
            else:
                print(f"      {ev}")
        for no, b in sorted(broken.items()):
            print(f"  断链: {no} 的上一版本 {b.missing_prev} 不存在(于 {b.broken_at} 处定位),不参与匹配")


def main():
    res_a = run_scenario(ARRIVAL_ORDER_A)
    res_b = run_scenario(ARRIVAL_ORDER_B)
    assert res_a == res_b, "结果不得依赖批次送达顺序"
    check(res_a)
    report(res_a)
    print("\n全部断言通过:两种送达顺序结果一致,每次出现恰好一条结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
