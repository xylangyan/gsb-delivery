"""给验收人的可开写计划：把排程结论与顺序构建台账整理成一份报告。

直接运行：

    .venv/bin/python -m seqwrite.acceptance_report

报告会回答移交/验收最关心的四件事：
1. 需要多少暂存（拆成输出侧已写未交付 + 原件侧已取未落笔，并给峰值）；
2. 先送哪一段已经不会再变的连续前缀（带水线区间与放行依据）；
3. 选了哪种取件先后，以及其它次序为何峰值更高；
4. 若配额不足，哪两个区域必须同时放下、还差多少（动笔前判定）。
"""

from dataclasses import asdict

from . import (
    BuildPlanner,
    OneshotSource,
    QuotaExceeded,
    RangeSource,
    build_demo_work_order,
)
from .declaration import LEN_A, LEN_B, standard_inputs

LONG_LEN = 40
CHUNK = 4
BLOCK = 16


def _seed():
    return {
        "orig_a": bytes(range(LEN_A)),
        "orig_b": bytes((i * 3) & 0xFF for i in range(LEN_B)),
        "orig_c": (0x0A0B0C0D).to_bytes(4, "big"),
        "orig_long": bytes((i * 7) & 0xFF for i in range(LONG_LEN)),
    }


def _build(staging_quota, attach_registry=True):
    seed = _seed()
    order = build_demo_work_order(
        staging_quota=staging_quota, long_len=LONG_LEN,
        chunk_size=CHUNK, attach_registry=attach_registry,
    )
    sources = {
        "orig_a": OneshotSource(order.spec("orig_a"), seed["orig_a"]),
        "orig_b": OneshotSource(order.spec("orig_b"), seed["orig_b"]),
        "orig_c": OneshotSource(order.spec("orig_c"), seed["orig_c"]),
        "orig_long": RangeSource(order.spec("orig_long"), seed["orig_long"]),
    }
    context = standard_inputs(
        long_len=LONG_LEN, attach_registry=attach_registry,
        registry_value=0x52454731,
        short_header=b"SH" + b"\x00" * 6,
        orig_a=seed["orig_a"], orig_b=seed["orig_b"],
        orig_c=seed["orig_c"], orig_long=seed["orig_long"],
    )
    planner = BuildPlanner(order, sources, block_size=BLOCK)
    planner.analyze_layout(context)
    return planner, order, seed


def build_acceptance_report(staging_quota=10 ** 9):
    planner, order, seed = _build(staging_quota)
    best, reports = planner.choose_best()

    report = {
        "order_id": order.order_id,
        "total_volume_bytes": planner.total_length,
        "staging": {
            "chosen_order": list(best.fetch_order),
            "combined_peak_bytes": best.combined_peak,
            "output_staged_peak_bytes": best.output_peak,
            "original_held_peak_bytes": best.held_peak,
            "all_orders": [
                {
                    "fetch_order": list(r.fetch_order),
                    "combined_peak_bytes": r.combined_peak,
                    "output_peak_bytes": r.output_peak,
                    "held_peak_bytes": r.held_peak,
                }
                for r in reports
            ],
        },
        "backfill_sites": [asdict(s) for s in planner.backfill_sites],
    }

    try:
        planner.preflight(best)
    except QuotaExceeded as exc:
        report["quota"] = {
            "quota_bytes": exc.quota,
            "required_bytes": exc.required,
            "shortfall_bytes": exc.shortfall,
            "must_hold_together": exc.regions,
        }
        return report  # 配额不足：不动笔，不产生半成品

    result = planner.execute()
    report["quota"] = {"quota_bytes": order.staging_quota, "sufficient": True}
    report["safe_prefix_delivery"] = [
        {
            "step": r.step,
            "watermark_before": r.watermark_before,
            "watermark_after": r.watermark_after,
            "delivered_ranges": list(r.delivered_ranges),
            "held_originals": r.held_originals,
            "held_bytes": r.held_bytes,
            "open_backfills": r.open_backfills,
        }
        for r in result["progress"]
        if r.watermark_after != r.watermark_before or r.delivered_ranges
    ]
    report["byte_identical_to_reference"] = (
        result["bytes"] == planner.reference_bytes
    )
    report["media_delivered_bytes"] = result["stream"].sink.delivered
    return report


def _print_report():
    report = build_acceptance_report()
    print("工单 %s  卷总长 %d 字节" % (report["order_id"],
                                  report["total_volume_bytes"]))
    stg = report["staging"]
    print("\n[暂存需求] 选中取件次序 %s" % stg["chosen_order"])
    print("  组合峰值 %d 字节（同一时刻：已写未交付 + 已取未落笔 之和的最大值）"
          % stg["combined_peak_bytes"])
    print("  其中各侧单独峰值：输出侧 %d、原件侧 %d（峰值时刻不同，不可直接相加）"
          % (stg["output_staged_peak_bytes"],
             stg["original_held_peak_bytes"]))
    for cand in stg["all_orders"]:
        print("  候选 %-28r 峰值 %d"
              % (cand["fetch_order"], cand["combined_peak_bytes"]))

    print("\n[回填站点]（坐标来源可回溯到声明）")
    for site in report["backfill_sites"]:
        flag = "（条件）" if site["conditional"] else ""
        print("  %-13s [%d,%d) 基准=%s%s —— %s"
              % (site["key"], site["start"], site["start"] + site["length"],
                 site["coordinate_basis"], flag, site["reason"]))

    print("\n[安全前缀交付] 只送保证不再覆盖的连续前缀")
    for item in report["safe_prefix_delivery"]:
        if item["delivered_ranges"]:
            print("  水线 %3d -> %3d  交付 %-14s 暂存原件=%s 未决回填=%s"
                  % (item["watermark_before"], item["watermark_after"],
                     item["delivered_ranges"], item["held_originals"],
                     item["open_backfills"]))

    print("\n[一致性] 顺序产物与本机可定位构建逐字节一致：%s；介质已交付 %d 字节"
          % (report["byte_identical_to_reference"],
             report["media_delivered_bytes"]))

    print("\n[配额不足示例] 把配额压到 50 字节：")
    tight = build_acceptance_report(staging_quota=50)
    q = tight["quota"]
    print("  需要 %d，配额 %d，还差 %d"
          % (q["required_bytes"], q["quota_bytes"], q["shortfall_bytes"]))
    for region in q["must_hold_together"]:
        print("  必须同时放下：%s" % region)


if __name__ == "__main__":
    _print_report()
