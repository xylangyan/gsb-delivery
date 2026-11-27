"""Build the cassette issued to the other team.

The build is journaled in the handover book: every finished step is recorded
and saved before the next one starts, so a process stopped after the book is
written but before the issued cassette is swapped in can resume and finish the
already-recorded work without recording anything twice or deleting a later
shift's submitted pages.
"""

import copy
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from vcr.matchers import requests_match
from vcr.serialize import serialize

from .config import HandoverConfig
from .ledger import HandoverLedger
from .rewrite import IssuedInteraction, RewriteError, rewrite_for_issuance


class HandoverBuildError(RuntimeError):
    """The issued cassette could not be built safely.

    Attributes name the receiving configuration and the page turn at which the
    unsafe assignment was detected, plus the reason. The previous good issued
    file (if any) is left in place.
    """

    def __init__(self, receive_config, page_turn, reason, target_path, last_good):
        self.receive_config = receive_config
        self.page_turn = page_turn
        self.reason = reason
        self.target_path = target_path
        self.last_good = last_good
        kept = f"the last usable file {last_good!r} was kept" if last_good else "no issued file was written"
        super().__init__(
            f"handover build for receive config {receive_config!r} cannot safely hand over page turn "
            f"{page_turn!r}: {reason}. Shift progress was saved and {kept}.",
        )


def _load_original_interactions(cassette_path, persister, serializer):
    requests, responses, *_ = persister.load_cassette(cassette_path, serializer=serializer)
    return list(zip(requests, responses, strict=True))


def build_handover_cassette(
    config,
    cassette_path,
    target_path,
    persister,
    serializer,
    page_turn="<final>",
    target_serializer=None,
):
    """Build the rewritten, minimized cassette for another team.

    ``serializer`` reads the original cassette; ``target_serializer`` writes
    the issued one and defaults to ``serializer``, so an issued cassette may
    use another format than its source.

    Resumable: reopening after a stop continues from the last journaled step.
    Raises :class:`HandoverBuildError` when no safe rewrite exists; the current
    shift's progress is saved first and the previous good file is retained.
    """
    target_serializer = target_serializer or serializer
    if not isinstance(config, HandoverConfig):
        raise TypeError("build_handover_cassette needs a HandoverConfig")
    ledger = HandoverLedger.open(config, cassette_path)
    permissions = config.permissions
    permissions.validate_page_turn(page_turn)
    build = ledger.begin_build(target_path)
    last_good = build.get("last_good")

    # Step 1: gather. Idempotent - the ledger is the source of truth and dedupes
    # both used originals (once each) and recorded interactions.
    if not ledger.build_step_done("gather"):
        ledger.mark_build_step("gather")

    original = _load_original_interactions(cassette_path, persister, serializer)
    withdrawn = set(ledger.withdrawn_occurrences)
    used = set(ledger.used_original_occurrences())
    interactions = []
    for occurrence_index, (request, response) in enumerate(original):
        if occurrence_index in used and occurrence_index not in withdrawn:
            interactions.append(
                IssuedInteraction(
                    request=copy.deepcopy(request),
                    response=copy.deepcopy(response),
                    source=("original", occurrence_index),
                ),
            )
    for shift_id, sequence, (request, response) in ledger.all_recorded_interactions():
        interactions.append(
            IssuedInteraction(
                request=copy.deepcopy(request),
                response=copy.deepcopy(response),
                source=("recorded", shift_id, sequence),
            ),
        )

    # Step 2: choose a safe owner-ID assignment.
    try:
        interactions, assignment = rewrite_for_issuance(config.rules, interactions, page_turn=page_turn)
    except RewriteError as error:
        ledger.fail_build(permissions.receive_config, page_turn, str(error))
        raise HandoverBuildError(
            permissions.receive_config,
            page_turn,
            str(error),
            os.fspath(target_path),
            last_good,
        ) from error
    if not ledger.build_step_done("rewrite"):
        ledger.mark_build_step(
            "rewrite",
            detail={
                "group_values": assignment.group_values,
                "permits": [str(permit.location) for permit in assignment.permits_used],
            },
        )

    # Step 3: verify occurrence identity under each group's specified matchers.
    if not ledger.build_step_done("verify"):
        _verify_occurrence_order(config, original, interactions)
        ledger.mark_build_step("verify")

    # Step 4: atomically swap in the issued cassette, keeping the last good file
    # until the new one is fully written.
    if not ledger.build_step_done("write"):
        _atomic_write(target_path, interactions, target_serializer)
        ledger.mark_build_step("write")

    ledger.complete_build(target_path)
    return interactions, assignment


def _verify_occurrence_order(config, original, issued):
    """The issued cassette must keep every group's occurrence identity in order.

    Under each group's specified matchers, two retained original occurrences
    that were distinct must stay distinct (so playback still reaches the same
    occurrence number at each turn), and two that matched must still match and
    keep their order. Newly recorded pages are checked separately by the
    collision search in :mod:`vcr.handover.rewrite`.
    """
    retained = [interaction for interaction in issued if interaction.source[0] == "original"]
    for group_original, matcher_tuple in config.rules.group_match_on.items():
        for position, left in enumerate(retained):
            for right in retained[position + 1 :]:
                left_index, right_index = left.source[1], right.source[1]
                matched_before = requests_match(
                    original[left_index][0], original[right_index][0], matcher_tuple
                )
                matched_after = requests_match(left.request, right.request, matcher_tuple)
                if matched_before != matched_after:
                    raise RewriteError(
                        f"group {group_original!r}: occurrences #{left_index} and #{right_index} "
                        "change match identity under the group's specified matchers after rewriting, "
                        "so playback would no longer reach them in handover order",
                    )


def _atomic_write(target_path, interactions, serializer):
    cassette_dict = {
        "requests": [interaction.request for interaction in interactions],
        "responses": [interaction.response for interaction in interactions],
    }
    data = serialize(cassette_dict, serializer)
    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise
