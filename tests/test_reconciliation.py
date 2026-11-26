import json

import pytest

import logbook
from logbook.reconciliation import (
    MAX_REPEAT,
    BatchRecorder,
    Candidate,
    DeliveryArchive,
    GroupRound,
    HistorySegment,
    HistoryWalk,
    OperationHistory,
    Quota,
    QuotaExhausted,
    QuotaProfile,
    QuotaReceiver,
    UnknownCategory,
    UnknownTag,
    archive_from_dict,
    main,
    merged_counts,
    profile_from_dict,
    reconcile,
)

PROFILE = QuotaProfile(
    "quota-a",
    {
        "alpha": Quota(
            limit=5,
            initial=3,
            deltas={"charge": 2, "refund": -3},
        )
    },
)


def candidates(*items, source="round"):
    return tuple(
        Candidate(number=number, category=category, source=source)
        for number, category in items
    )


def make_record(number, category, source="round", tag=None):
    extra = {"number": number, "category": category, "source": source}
    if tag is not None:
        extra["tag"] = tag
    return logbook.LogRecord("audit", logbook.INFO, f"record {number}", extra=extra)


def receipt_rows(result):
    return [
        (
            receipt.number,
            receipt.category,
            receipt.balance_before,
            receipt.balance_after,
        )
        for receipt in result.receipts
    ]


def test_quota_caps_balance_and_rejects_negative():
    quota = Quota(limit=4, initial=3, deltas={"in": 2, "out": -5})

    assert quota.apply(3, "in") == 4
    with pytest.raises(QuotaExhausted):
        quota.apply(4, "out")


def test_quota_exhaustion_does_not_change_balance():
    quota = Quota(limit=5, initial=1, deltas={"charge": 2, "refund": -3})

    assert quota.apply(1, "charge") == 3
    with pytest.raises(QuotaExhausted) as info:
        quota.apply(2, "refund")

    assert info.value.balance == 2
    assert info.value.next_balance == -1


def test_unknown_category_and_tag_are_reported():
    quota = Quota(limit=5, initial=1, deltas={"charge": 1}).with_tag_name("alpha")

    with pytest.raises(UnknownCategory) as info:
        quota.apply(1, "refund")

    assert info.value.category == "refund"
    with pytest.raises(UnknownTag):
        reconcile(
            DeliveryArchive(
                rounds=(GroupRound("g", candidates(("1", "charge"))),),
                tag_order=("gamma",),
            ),
            [PROFILE],
        ).counts("quota-a")


def test_simulator_accepts_prefix_then_stops_on_negative_balance():
    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "checkout",
                candidates(("1", "charge"), ("2", "refund"), ("3", "refund")),
                attempt=1,
            ),
        ),
        continue_after_failure=False,
        tag_order=("alpha",),
    )

    result = reconcile(archive, [PROFILE]).results["quota-a"]

    assert receipt_rows(result) == [
        ("1", "charge", 3, 5),
        ("2", "refund", 5, 2),
    ]
    assert [failure.number for failure in result.failures] == ["3"]
    assert [
        [receipt.number for receipt in failure.prefix] for failure in result.failures
    ] == [["1", "2"]]
    assert result.failures[0].balance_before == 2
    assert result.failures[0].tag == "alpha"
    assert result.balances == {"alpha": 2}
    assert result.counts == {"1": 1, "2": 1}


def test_continue_after_failure_comes_from_the_archive():
    rounds = (
        GroupRound("g", candidates(("1", "refund"), ("2", "refund")), attempt=1),
        GroupRound("g", candidates(("3", "charge")), attempt=2),
    )

    stopped = reconcile(
        DeliveryArchive(
            rounds=rounds, continue_after_failure=False, tag_order=("alpha",)
        ),
        [PROFILE],
    ).results["quota-a"]
    continued = reconcile(
        DeliveryArchive(
            rounds=rounds, continue_after_failure=True, tag_order=("alpha",)
        ),
        [PROFILE],
    ).results["quota-a"]

    assert stopped.counts == {"1": 1}
    assert continued.counts == {"1": 1, "3": 1}
    assert stopped.stopped_groups == {"g"}
    assert continued.stopped_groups == set()


def test_per_group_rule_overrides_the_archive_default():
    archive = DeliveryArchive(
        rounds=(
            GroupRound("a", candidates(("1", "refund"), ("2", "refund")), attempt=1),
            GroupRound("a", candidates(("3", "charge")), attempt=2),
            GroupRound("b", candidates(("4", "refund"), ("5", "refund")), attempt=1),
            GroupRound("b", candidates(("6", "charge")), attempt=2),
        ),
        continue_after_failure={"a": True, "b": False},
        tag_order=("alpha",),
    )

    result = reconcile(archive, [PROFILE]).results["quota-a"]

    assert result.counts == {"1": 1, "3": 1}
    assert result.stopped_groups == {"b"}


def test_failure_prefix_and_reemitted_sources():
    first = (
        ("1", "charge"),
        ("2", "charge"),
        ("3", "refund"),
        ("4", "refund"),
    )
    archive = DeliveryArchive(
        rounds=tuple(
            GroupRound(
                "g",
                tuple(
                    Candidate(
                        number=number, category=category, source=f"attempt-{attempt}"
                    )
                    for number, category in first
                ),
                attempt=attempt,
            )
            for attempt in (1, 2)
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )

    report = reconcile(archive, [PROFILE])
    detail = report.failure_detail("quota-a")

    assert detail.failure.number == "4"
    assert detail.failure.attempt == 1
    assert detail.prefix_numbers == ("1", "2", "3")
    assert detail.reemitted_sources == (
        "attempt-2",
        "attempt-2",
        "attempt-2",
    )


def test_first_missing_and_first_duplicate_positions():
    archive = DeliveryArchive(
        rounds=(
            GroupRound("g", candidates(("1", "charge")), attempt=1),
            GroupRound("g", candidates(("1", "refund")), attempt=2),
            GroupRound("g", candidates(("2", "refund")), attempt=3),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )

    report = reconcile(archive, [PROFILE])

    duplicate = report.first_duplicate("quota-a")
    assert duplicate.kind == "duplicate"
    assert duplicate.number == "1"
    assert duplicate.attempt == 2
    assert duplicate.group == "g"

    missing = report.first_missing("quota-a")
    assert missing.kind == "missing"
    assert missing.number == "2"
    assert missing.global_index == 2
    assert report.first_issue("quota-a") == duplicate


def test_two_profiles_report_counts_and_first_divergence():
    archive = DeliveryArchive(
        rounds=(
            GroupRound("g", candidates(("1", "charge")), attempt=1),
            GroupRound("g", candidates(("2", "refund")), attempt=2),
            GroupRound("g", candidates(("3", "charge")), attempt=3),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    tight = QuotaProfile(
        "tight",
        {"alpha": Quota(limit=4, initial=3, deltas={"charge": 2, "refund": -5})},
    )
    loose = QuotaProfile(
        "loose",
        {"alpha": Quota(limit=9, initial=3, deltas={"charge": 2, "refund": -5})},
    )

    report = reconcile(archive, [tight, loose])

    assert report.counts("tight") == {"1": 1, "3": 1}
    assert report.counts("loose") == {"1": 1, "2": 1, "3": 1}
    divergence = report.first_divergence()
    assert divergence.event.candidate.number == "2"
    assert divergence.left is None
    assert divergence.right == "alpha"
    assert divergence.left_position == 1
    assert divergence.right_position == 1


def test_tag_reuse_continues_the_balance_and_new_output_replenishes_nothing():
    archive = DeliveryArchive(
        rounds=(
            GroupRound("g", candidates(("1", "charge")), attempt=1),
            GroupRound(
                "g",
                candidates(("2", "charge")),
                attempt=2,
                new_output=True,
            ),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )

    result = reconcile(archive, [PROFILE]).results["quota-a"]

    assert receipt_rows(result) == [
        ("1", "charge", 3, 5),
        ("2", "charge", 5, 5),
    ]


def test_new_output_can_reuse_the_first_selected_tag_via_archive_tag():
    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "first",
                candidates(("1", "charge")),
                attempt=1,
                tag="beta",
                new_output=True,
            ),
            GroupRound(
                "second",
                candidates(("2", "charge")),
                attempt=1,
                tag="beta",
                new_output=True,
            ),
        ),
        continue_after_failure=False,
        tag_order=("alpha", "beta"),
    )
    profile = QuotaProfile(
        "quota-b",
        {
            "alpha": Quota(limit=9, initial=9, deltas={"charge": 1}),
            "beta": Quota(limit=2, initial=1, deltas={"charge": 1}),
        },
    )

    result = reconcile(archive, [profile]).results["quota-b"]

    assert [
        (receipt.number, receipt.tag, receipt.balance_after)
        for receipt in result.receipts
    ] == [
        ("1", "beta", 2),
        ("2", "beta", 2),
    ]


def test_history_segments_compress_billion_repeats():
    history = OperationHistory()
    history.append(candidates(("1", "charge"), ("2", "charge")), repeat=MAX_REPEAT)

    assert history.length == 2 * MAX_REPEAT
    assert history.counts() == {"1": MAX_REPEAT, "2": MAX_REPEAT}
    assert history.counts_in_range(1, 3) == {"2": 1, "1": 1}
    assert history.counts_in_range(0, 1) == {"1": 1}
    assert history.counts_in_range(history.length - 1, history.length) == {"2": 1}


def test_history_merges_and_queries_intervals():
    first = OperationHistory().append(candidates(("1", "charge")), repeat=3)
    second = OperationHistory().append(candidates(("2", "charge")), repeat=4)

    merged = OperationHistory.merge(first, second)
    assert merged.length == 3 + 4
    assert merged.counts() == {"1": 3, "2": 4}
    assert merged.counts_in_range(2, 5) == {"1": 1, "2": 2}
    assert merged_counts(first, second) == {"1": 3, "2": 4}


def test_history_rejects_oversized_repeats():
    with pytest.raises(ValueError):
        HistorySegment(candidates(("1", "charge")), repeat=MAX_REPEAT + 1)


def test_report_round_counts_and_interval_counts():
    archive = DeliveryArchive(
        rounds=(
            GroupRound("g", candidates(("1", "charge")), attempt=1),
            GroupRound("g", candidates(("1", "charge")), attempt=2),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )

    report = reconcile(
        archive,
        [QuotaProfile("q", {"alpha": Quota(limit=9, initial=9, deltas={"charge": 1})})],
    )

    assert report.round_counts(profile="q", group="g", attempt=2)["q"] == {"1": 1}
    assert report.interval_counts("q", 1, 2) == {"1": 1}
    assert report.merged_counts(0, 2) == {"1": 2}


def test_quota_receiver_matches_simulator_for_group_replay():
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=5, initial=3, deltas={"charge": 2, "refund": -3})},
    )
    receiver = QuotaReceiver(profile, tag="alpha")
    group = logbook.GroupHandler(receiver)
    logger = logbook.Logger("audit")
    rounds = (
        (("1", "charge"), ("2", "refund"), ("3", "charge")),
        (("1", "charge"), ("2", "refund"), ("3", "charge")),
    )

    with logbook.Flags(errors="raise"):
        for attempt, records in enumerate(rounds, 1):
            try:
                with group:
                    for number, category in records:
                        logger.info(
                            "record",
                            extra={
                                "number": number,
                                "category": category,
                                "source": f"attempt-{attempt}",
                            },
                        )
            except QuotaExhausted:
                pass

    archive = DeliveryArchive(
        rounds=tuple(
            GroupRound(
                "default",
                tuple(
                    Candidate(
                        number=number, category=category, source=f"attempt-{attempt}"
                    )
                    for number, category in records
                ),
                attempt=attempt,
            )
            for attempt, records in enumerate(rounds, 1)
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    simulated = reconcile(archive, [profile]).results["quota-a"]

    observed = [
        (
            receipt.number,
            receipt.category,
            receipt.balance_before,
            receipt.balance_after,
        )
        for receipt in receiver.receipts
    ]
    assert observed == receipt_rows(simulated)


def test_quota_receiver_matches_fingers_crossed_escalation():
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=5, initial=3, deltas={"charge": 2, "refund": -3})},
    )
    receiver = QuotaReceiver(profile, tag="alpha")
    handler = logbook.FingersCrossedHandler(receiver, action_level=logbook.ERROR)
    logger = logbook.Logger("audit")
    records = (("1", "charge"), ("2", "refund"), ("3", "charge"))
    try:
        with handler:
            for number, category in records:
                logger.info(
                    "record",
                    extra={"number": number, "category": category, "source": "window"},
                )
            logger.error(
                "trigger",
                extra={"number": "trigger", "category": "charge", "source": "window"},
            )
    except QuotaExhausted:
        pass

    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "default",
                (
                    *tuple(
                        Candidate(number=number, category=category, source="window")
                        for number, category in records
                    ),
                    Candidate(number="trigger", category="charge", source="window"),
                ),
                attempt=1,
            ),
        ),
        continue_after_failure=False,
        tag_order=("alpha",),
    )
    simulated = reconcile(archive, [profile]).results["quota-a"]

    observed = [
        (
            receipt.number,
            receipt.category,
            receipt.balance_before,
            receipt.balance_after,
        )
        for receipt in receiver.receipts
    ]
    assert observed == receipt_rows(simulated)


def test_archive_and_profile_from_dict_round_trip():
    document = {
        "tag_order": ["alpha"],
        "continue_after_failure": True,
        "rounds": [
            {
                "group": "g",
                "attempt": 1,
                "candidates": [
                    {"number": "1", "category": "charge", "source": "first"}
                ],
            }
        ],
    }
    quotas = {
        "alpha": {"limit": 5, "initial": 3, "deltas": {"charge": 2}},
    }

    archive = archive_from_dict(document)
    profile = profile_from_dict("quota-a", quotas)
    result = reconcile(archive, [profile]).results["quota-a"]

    assert result.counts == {"1": 1}


def test_main_writes_json_report(tmp_path, capsys):
    document = {
        "tag_order": ["alpha"],
        "quotas": {
            "quota-a": {"alpha": {"limit": 5, "initial": 3, "deltas": {"charge": 2}}},
        },
        "rounds": [
            {
                "group": "g",
                "attempt": 1,
                "candidates": [{"number": "1", "category": "charge"}],
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert main([str(path)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["profiles"] == ["quota-a"]
    assert report["counts"]["quota-a"] == {"1": 1}
    assert report["archive_length"] == 1
    assert report["rounds"] == [
        {
            "profile": "quota-a",
            "group": "g",
            "output": "g",
            "round": 0,
            "attempt": 1,
            "received": 1,
            "failed": False,
            "counts": {"1": 1},
        }
    ]


def test_rebinding_an_old_group_replays_its_buffer():
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=9, initial=3, deltas={"charge": 2, "refund": -9})},
    )
    receiver = QuotaReceiver(profile, tag="alpha", raise_on_failure=True)
    group = logbook.GroupHandler(receiver)
    logger = logbook.Logger("audit")

    def emit(number, category):
        logger.info(
            "record",
            extra={"number": number, "category": category, "source": "group"},
        )

    with pytest.raises(QuotaExhausted):
        with group:
            emit("1", "charge")
            emit("2", "charge")
            emit("3", "refund")

    # The failed rollover kept the whole buffer.  Rebinding the same group
    # replays it from the first record, and the new record joins it.
    with group:
        emit("4", "charge")

    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "default",
                candidates(("1", "charge"), ("2", "charge"), ("3", "refund")),
                attempt=1,
            ),
            GroupRound(
                "default",
                candidates(
                    ("1", "charge"),
                    ("2", "charge"),
                    ("3", "refund"),
                    ("4", "charge"),
                ),
                attempt=2,
            ),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    simulated = reconcile(archive, [profile]).results["quota-a"]

    observed = [
        (receipt.number, receipt.balance_before, receipt.balance_after)
        for receipt in receiver.receipts
    ]
    assert observed == [
        (receipt.number, receipt.balance_before, receipt.balance_after)
        for receipt in simulated.receipts
    ]
    assert [failure.number for failure in simulated.failures] == ["3"]


def test_a_new_group_selects_its_own_tag_without_replenishing():
    profile = QuotaProfile(
        "first",
        {
            "alpha": Quota(limit=4, initial=3, deltas={"charge": 2}),
            "beta": Quota(limit=4, initial=3, deltas={"charge": 2}),
        },
    )
    logger = logbook.Logger("audit")

    first_receiver = QuotaReceiver(profile, tag="alpha")
    with logbook.GroupHandler(first_receiver):
        logger.info("a", extra={"number": "1", "category": "charge"})

    second_receiver = QuotaReceiver(profile, tag="beta")
    with logbook.GroupHandler(second_receiver):
        logger.info("b", extra={"number": "2", "category": "charge"})
        logger.info("c", extra={"number": "3", "category": "charge"})
        logger.info("d", extra={"number": "4", "category": "charge"})

    assert [
        (receipt.number, receipt.tag, receipt.balance_after)
        for receipt in first_receiver.receipts
    ] == [("1", "alpha", 4)]
    assert [
        (receipt.number, receipt.tag, receipt.balance_after)
        for receipt in second_receiver.receipts
    ] == [
        ("2", "beta", 4),
        ("3", "beta", 4),
        ("4", "beta", 4),
    ]
    assert second_receiver.balances == {"alpha": 3, "beta": 4}


def test_batch_recorder_turns_real_batches_into_an_archive():
    receiver = BatchRecorder(group="checkout")
    logger = logbook.Logger("audit")

    with logbook.GroupHandler(receiver):
        logger.info("c1", extra={"number": "1", "category": "charge"})
        logger.info("c2", extra={"number": "2", "category": "refund"})

    archive = receiver.to_archive(continue_after_failure=True)
    result = reconcile(archive, [PROFILE]).results["quota-a"]

    assert archive.rounds[0].group == "checkout"
    assert archive.rounds[0].candidates[0].number == "1"
    assert result.counts == {"1": 1, "2": 1}


def test_divergence_positions_name_the_refused_handover():
    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "g",
                candidates(("1", "charge"), ("2", "refund"), ("3", "charge")),
                attempt=1,
            ),
            GroupRound(
                "g",
                candidates(("4", "charge"), ("5", "charge"), ("6", "charge")),
                attempt=2,
            ),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    tight = QuotaProfile(
        "tight",
        {"alpha": Quota(limit=4, initial=3, deltas={"charge": 2, "refund": -5})},
    )
    loose = QuotaProfile(
        "loose",
        {"alpha": Quota(limit=9, initial=3, deltas={"charge": 2, "refund": -5})},
    )

    divergence = reconcile(archive, [tight, loose]).first_divergence()

    assert divergence.event.candidate.number == "2"
    assert divergence.event.candidate.source == "round"
    assert divergence.left is None
    assert divergence.right == "alpha"
    assert divergence.left_position == 1
    assert divergence.right_position == 1


def test_failure_detail_filters_by_attempt():
    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "g",
                candidates(("1", "charge"), ("2", "refund")),
                attempt=1,
            ),
            GroupRound(
                "g",
                candidates(("1", "charge"), ("2", "refund")),
                attempt=2,
            ),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=5, initial=3, deltas={"charge": 2, "refund": -9})},
    )

    report = reconcile(archive, [profile])
    detail = report.failure_detail("quota-a", group="g", attempt=2)

    assert detail.failure.number == "2"
    assert detail.failure.attempt == 2
    assert detail.prefix_numbers == ("1",)


def test_repeated_real_batches_collapse_into_one_compressed_history():
    first = BatchRecorder(group="g")
    with logbook.GroupHandler(first):
        logbook.Logger("audit").info("a", extra={"number": "1", "category": "charge"})
    segment = first.to_rounds()[0].candidates

    history = OperationHistory()
    history.append(segment, repeat=MAX_REPEAT)

    assert history.length == MAX_REPEAT
    assert history.counts() == {"1": MAX_REPEAT}

    archive = DeliveryArchive(
        rounds=(
            GroupRound("g", segment, attempt=1),
            GroupRound("g", segment, attempt=2),
        ),
        continue_after_failure=True,
        tag_order=("alpha",),
    )
    archive_history = archive.operations()
    assert archive_history.length == 2
    assert merged_counts(archive_history) == {"1": 2}


def test_history_walk_reads_a_billion_repeats_lazily():
    history = OperationHistory()
    history.append(
        [Candidate("1", "charge"), Candidate("2", "refund")],
        repeat=MAX_REPEAT,
    )
    walk = HistoryWalk(history)

    first = list(walk.interval(3))
    assert [candidate.number for _, candidate in first] == ["1", "2", "1"]
    assert walk.position == 3

    tail = list(walk.iter_from(history.length - 2))
    assert [index for index, _ in tail] == [history.length - 2, history.length - 1]
    assert [candidate.number for _, candidate in tail] == ["1", "2"]

    for _, candidate in walk.interval(2):
        walk.consume(candidate)
    assert walk.receipts == 2
    walk.reject(0, Candidate("9", "refund"))
    assert walk.failures == 1
    assert walk.rejected[0][1].number == "9"


def test_records_after_a_refusal_are_still_settled():
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=9, initial=3, deltas={"charge": 2, "refund": -9})},
    )
    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "g",
                candidates(
                    ("1", "charge"),
                    ("2", "refund"),
                    ("3", "charge"),
                    ("4", "charge"),
                ),
                attempt=1,
            ),
        ),
        continue_after_failure=False,
        tag_order=("alpha",),
    )

    result = reconcile(archive, [profile]).results["quota-a"]

    assert receipt_rows(result) == [
        ("1", "charge", 3, 5),
        ("3", "charge", 5, 7),
        ("4", "charge", 7, 9),
    ]
    assert [failure.number for failure in result.failures] == ["2"]
    # The group is not used again, but the rest of this batch is still settled.
    assert result.stopped_groups == {"g"}


def test_quota_receiver_settles_the_rest_of_the_batch_after_a_refusal():
    profile = QuotaProfile(
        "quota-a",
        {"alpha": Quota(limit=9, initial=3, deltas={"charge": 2, "refund": -9})},
    )
    receiver = QuotaReceiver(profile, tag="alpha")
    logger = logbook.Logger("audit")

    with logbook.GroupHandler(receiver):
        logger.info("1", extra={"number": "1", "category": "charge"})
        logger.info("2", extra={"number": "2", "category": "refund"})
        logger.info("3", extra={"number": "3", "category": "charge"})
        logger.info("4", extra={"number": "4", "category": "charge"})

    assert [receipt.number for receipt in receiver.receipts] == ["1", "3", "4"]
    assert [rejected.number for rejected in receiver.rejected] == ["2"]
    assert receiver.rejected[0].shortfall == 4
    assert receiver.balances == {"alpha": 9}

    with_rejection = QuotaReceiver(profile, tag="alpha", raise_on_failure=True)
    with pytest.raises(QuotaExhausted):
        with logbook.GroupHandler(with_rejection):
            logger.info("1", extra={"number": "1", "category": "charge"})
            logger.info("2", extra={"number": "2", "category": "refund"})
            logger.info("3", extra={"number": "3", "category": "charge"})
