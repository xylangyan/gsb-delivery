"""Handover mode: let two shifts share one cassette without leaking owner IDs.

A *handover* sits next to an ordinary VCR cassette (the "original recording").
Each shift opens the cassette with its own :class:`~vcr.handover.ledger.HandoverLedger`,
which keeps a separate playback cursor, remembers which original interactions the
shift has actually used (each only once), which ones the duty officer has
withdrawn, and which new interactions the shift has recorded.

When a shift closes, its progress is written to a sidecar handover book
(``<cassette>.handover.yaml`` by default) without waiting for any other shift.
The original cassette file is never modified by the handover machinery and stays
readable by ordinary VCR.

Finally :func:`~vcr.handover.builder.build_handover_cassette` produces a cassette
for another team: only the used original interactions and the newly recorded
ones, with the configured owner IDs replaced. The handover book is a separate
file and never becomes a request field, so matchers don't see it.
"""

from .builder import HandoverBuildError, build_handover_cassette
from .config import (
    HandoverConfig,
    HandoverConfigError,
    Location,
    OwnerIdRules,
    Permissions,
    Permit,
)
from .ledger import HandoverBookError, HandoverLedger
from .rewrite import RewriteError
from .script import OfflineScript

__all__ = [
    "HandoverBookError",
    "HandoverBuildError",
    "HandoverConfig",
    "HandoverConfigError",
    "HandoverLedger",
    "Location",
    "OfflineScript",
    "OwnerIdRules",
    "Permissions",
    "Permit",
    "RewriteError",
    "build_handover_cassette",
]
