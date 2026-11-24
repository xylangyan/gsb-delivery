from loan_audit import build_report, domain_from_spec


def test_disjoint_whole_copies_do_not_become_continuous_span():
    report = build_report(
        {
            "window": ["1900-03-01", "1900-03-12"],
            "loans": [{"id": "L1", "item": "A"}],
            "receipts": [
                {
                    "id": "R1",
                    "copies": [
                        {
                            "id": "early",
                            "dates": {
                                "L1:checkout": "1900-03-03",
                                "L1:return": "1900-03-05",
                            },
                        },
                        {
                            "id": "late",
                            "dates": {
                                "L1:checkout": "1900-03-10",
                                "L1:return": "1900-03-12",
                            },
                        },
                    ],
                }
            ],
        }
    )

    by_range = {(row["date_start"], row["date_end"]): row for row in report["daily_runs"]}
    assert by_range[("1900-03-05", "1900-03-09")]["min_out"] == 0
    assert by_range[("1900-03-05", "1900-03-09")]["max_out"] == 0
    assert by_range[("1900-03-03", "1900-03-04")]["max_out"] == 1
    assert by_range[("1900-03-10", "1900-03-11")]["max_out"] == 1
    assert report["gaps"] == []


def test_edtf_one_of_set_is_not_envelope():
    domain = domain_from_spec("[1900-03-03,1900-03-10]")
    assert domain == domain_from_spec(["1900-03-03", "1900-03-10"])


def test_edtf_month_precision_covers_whole_calendar_month():
    domain = domain_from_spec("1900-03")
    assert len(domain) == 31
    assert domain[0] == domain_from_spec("1900-03-01")[0]
    assert domain[-1] == domain_from_spec("1900-03-31")[0]


def test_joint_receipt_preserves_exactly_one_xor():
    report = build_report(
        {
            "window": ["1900-03-06", "1900-03-06"],
            "loans": [
                {"id": "LA", "item": "A"},
                {"id": "LB", "item": "B"},
            ],
            "receipts": [
                {
                    "id": "RJ",
                    "copies": [
                        {
                            "id": "a-out",
                            "dates": {
                                "LA:checkout": "1900-03-03",
                                "LA:return": "1900-03-07",
                                "LB:checkout": "1900-03-07",
                                "LB:return": "1900-03-10",
                            },
                        },
                        {
                            "id": "b-out",
                            "dates": {
                                "LA:checkout": "1900-03-01",
                                "LA:return": "1900-03-06",
                                "LB:checkout": "1900-03-06",
                                "LB:return": "1900-03-10",
                            },
                        },
                    ],
                }
            ],
        }
    )

    row = report["daily_runs"][0]
    assert (row["min_out"], row["max_out"]) == (1, 1)
    min_day = [loan["out_on_witness_day"] for loan in row["min_witness"]["loans"]]
    max_day = [loan["out_on_witness_day"] for loan in row["max_witness"]["loans"]]
    assert sorted(min_day) == [False, True]
    assert sorted(max_day) == [False, True]
    assert row["min_witness"]["receipt_set"] == ["RJ"]


def test_same_item_is_counted_at_most_once_and_sequence_is_enforced():
    report = build_report(
        {
            "window": ["1900-03-08", "1900-03-08"],
            "loans": [
                {"id": "L1", "item": "A", "order": 1},
                {"id": "L2", "item": "A", "order": 2},
            ],
            "receipts": [
                {
                    "id": "R1",
                    "dates": {
                        "L1:checkout": ["1900-03-01", "1900-03-02", "1900-03-03"],
                        "L1:return": ["1900-03-04", "1900-03-05", "1900-03-06"],
                        "L2:checkout": ["1900-03-05", "1900-03-06", "1900-03-07"],
                        "L2:return": ["1900-03-09", "1900-03-10", "1900-03-11"],
                    },
                }
            ],
        }
    )
    assert report["daily_runs"][0]["max_out"] == 1
    assert report["gaps"] == []


def test_conflicting_endpoint_dates_name_both_receipts():
    report = build_report(
        {
            "window": ["1900-03-01", "1900-03-10"],
            "loans": [{"id": "L1", "item": "A"}],
            "receipts": [
                {
                    "id": "R1",
                    "dates": {
                        "L1:checkout": "1900-03-03",
                        "L1:return": "1900-03-05",
                    },
                },
                {
                    "id": "R2",
                    "dates": {
                        "L1:checkout": "1900-03-04",
                        "L1:return": "1900-03-08",
                    },
                },
            ],
        }
    )
    gap = report["gaps"][0]
    assert gap["type"] == "no_common_interpretation"
    assert set(gap["stuck_receipts"]) == {"R1", "R2"}
    assert gap["original_ids"] == ["A"]


def test_conflicting_batch_is_listed_but_independent_batch_reports():
    report = build_report(
        {
            "window": ["1900-03-03", "1900-03-03"],
            "loans": [
                {"id": "OK", "item": "GOOD"},
                {"id": "BAD", "item": "STUCK"},
            ],
            "receipts": [
                {
                    "id": "ROK",
                    "dates": {
                        "OK:checkout": "1900-03-01",
                        "OK:return": "1900-03-05",
                    },
                },
                {
                    "id": "RBAD1",
                    "dates": {
                        "BAD:checkout": "1900-03-03",
                        "BAD:return": "1900-03-05",
                    },
                },
                {
                    "id": "RBAD2",
                    "dates": {
                        "BAD:checkout": "1900-03-04",
                        "BAD:return": "1900-03-08",
                    },
                },
            ],
        }
    )

    assert report["daily_runs"][0]["min_out"] == 1
    assert report["daily_runs"][0]["max_out"] == 1
    assert report["reported_original_ids"] == ["GOOD"]
    assert report["blocked_original_ids"] == ["STUCK"]
    assert report["gaps"][0]["original_ids"] == ["STUCK"]


def test_long_fixed_loan_compresses_to_one_run():
    report = build_report(
        {
            "window": ["1900-01-01", "1900-12-31"],
            "loans": [{"id": "LONG", "item": "A"}],
            "receipts": [
                {
                    "id": "R",
                    "dates": {
                        "LONG:checkout": "1900-01-01",
                        "LONG:return": "1901-01-01",
                    },
                }
            ],
        }
    )
    assert len(report["daily_runs"]) == 1
    assert report["daily_runs"][0]["days"] == 365
    assert (report["daily_runs"][0]["min_out"], report["daily_runs"][0]["max_out"]) == (1, 1)
