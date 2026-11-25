"""Compatibility entry point for indexed moving-window rank reissue queries."""

from __future__ import annotations

from bottleneck.reissue import (  # noqa: F401
    CorrectionArchive,
    CorrectionIndex,
    CorrectionPlan,
    CorrectionQueryResult,
    CorrectionSlip,
    DailyRankArchive,
    IntervalResult,
    MoveRankCorrectionIndex,
    MoveRankIndex,
    RankCorrectionIndex,
    RankReissueIndex,
    ReissueIndex,
    ReissuePlan,
    ReissueQueryResult,
    ReissueSlip,
    build_correction_index,
    build_index,
    count_changed_ranks,
    count_reissues,
    earliest_changed_rank,
    earliest_reissue,
    index_move_rank,
    index_ranks,
    move_rank_correction_index,
    query_correction,
    query_reissue,
)

solve = query_correction
count = count_reissues
earliest = earliest_reissue
index = build_index
