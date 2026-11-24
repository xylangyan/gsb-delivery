from pygrametl.reconciliation import *  # noqa: F401,F403
from pygrametl.reconciliation import (  # noqa: F401
    Assignment,
    CorrectionConflict,
    OutboundFlow,
    OutboundLoadProcess,
    OutboundReconciliation,
    OutboundReconciliationService,
    OutboundReceiptMatcher,
    ReceiptReconciler,
    ReceiptReconciliationService,
    ReconciliationResult,
    CorrectionPublisher,
    VersionedReconciliation,
    allocate_receipts,
    build_accumulating_rows,
    duration_hours,
    match_receipts,
    optimal_matching,
    parse_time,
    reconcile,
    reconcile_receipts,
    solve,
    solve_reconciliation,
    write_off_receipts,
)


OutboundReceiptReconciliation = OutboundReconciliationService
OutboundReceiptService = OutboundReconciliationService
OutboundPipeline = OutboundReconciliationService
OutboundETL = OutboundReconciliationService
OutboundLoader = OutboundReconciliationService
ReceiptReconciliation = OutboundReconciliationService
ReceiptWriteOffService = OutboundReconciliationService
ReceiptWriteOff = OutboundReconciliationService
WriteOffService = OutboundReconciliationService
TicketReceiptMatcher = ReceiptReconciler
OutboundSnapshotPublisher = OutboundReconciliationService
OutboundFactService = OutboundReconciliationService
CorrectionLedger = OutboundReconciliationService
CorrectionService = OutboundReconciliationService
FinancialCorrectionLedger = OutboundReconciliationService


def __getattr__(name):
    from pygrametl import reconciliation as _core

    return getattr(_core, name)
