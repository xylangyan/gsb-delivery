from pyDatalog.reconcile import ChoiceSet, Reconciler, TransferIndex, reconcile_receipts


def line(department, part, value, source, **kwargs):
    return {
        "department": department,
        "part": part,
        "amount": value,
        "source": source,
        **kwargs,
    }


def test_discrete_choice_totals_do_not_fill_interval():
    reconciler = Reconciler(
        base_totals={"missing": 3},
        choices=[
            ChoiceSet(
                "only",
                {
                    "six": [line("missing", "P", 3, "line-1")],
                    "ten": [line("missing", "P", 7, "line-1")],
                },
            )
        ],
        transfers=[("P", "ACT")],
    )

    result = reconciler.reconcile("missing", (6, 10))

    assert result.possible_totals == (6, 10)
    assert result.count == 2
    assert result.minimum_reachable == 6
    assert result.minimum_unreachable == 7
    assert result.witness == {"only": "six"}
    assert result.witness_total == 6
    assert [item.part for item in result.included_lines] == ["ACT"]


def test_signed_total_selects_one_shared_choice():
    reconciler = Reconciler(
        base_totals={"missing": 3, "signed": 0},
        choices=[
            ChoiceSet(
                "place",
                {
                    "a": [
                        line("missing", "P", 3, "m"),
                        line("signed", "P", 1, "s"),
                    ],
                    "b": [
                        line("missing", "P", 7, "m"),
                        line("signed", "P", 5, "s"),
                    ],
                },
            )
        ],
        signed_totals=[{"department": "signed", "total": 1}],
        transfers=[("P", "ACT")],
    )

    result = reconciler.reconcile("missing", (0, 20))

    assert result.possible_totals == (6,)
    assert result.witness == {"place": "a"}


def test_equal_totals_from_two_assignments_count_once():
    reconciler = Reconciler(
        choices=[
            ChoiceSet(
                "c1",
                {
                    "a": [line("missing", "P", 1, "s1")],
                    "b": [line("missing", "P", 2, "s1")],
                },
            ),
            ChoiceSet(
                "c2",
                {
                    "a": [line("missing", "Q", 2, "s2")],
                    "b": [line("missing", "Q", 1, "s2")],
                },
            ),
        ]
    )

    result = reconciler.reconcile("missing", (0, 10))

    assert 3 in result.possible_totals
    assert result.possible_totals == (2, 3, 4)
    assert result.count == 3


def test_diamond_transfer_derives_one_release_fact_with_provenance():
    index = TransferIndex(
        transfers=[
            ("P", "X", "e1"),
            ("P", "Y", "e2"),
            ("X", "A", "e3"),
            ("Y", "A", "e4"),
        ]
    )

    assert index.actual_parts("P") == frozenset({"A"})
    releases = index.releases("P")
    assert {actual for actual, _ in releases} == {"A"}
    assert index.release_cache["P"] == frozenset({"A"})
    provenance = index.provenance("P", "A")
    assert {"e1", "e2", "e3", "e4"}.issubset(set(provenance))


def test_withheld_actual_part_is_excluded_after_merge():
    reconciler = Reconciler(
        choices=[
            ChoiceSet(
                "c",
                {
                    "a": [line("missing", "P", 4, "s")],
                    "b": [line("missing", "P", 4, "s"), line("missing", "H", 6, "h", withheld=True)],
                },
            )
        ],
        transfers=[("P", "ACT"), ("H", "HELD")],
        withheld=["HELD"],
    )

    result = reconciler.reconcile("missing", (0, 20))

    assert result.possible_totals == (4,)
    assert all(item.part != "HELD" for item in result.included_lines)


def test_conflict_returns_signed_receipt_and_original_facts():
    reconciler = Reconciler(
        facts=[{"voucher": "original-1"}],
        choices=[
            ChoiceSet(
                "c",
                {
                    "a": [line("signed", "P", 1, "s")],
                    "b": [line("signed", "P", 2, "s")],
                },
            )
        ],
        signed_totals=[
            {"department": "signed", "total": 9, "receipt": "signed-receipt-9"}
        ],
    )

    result = reconciler.reconcile("missing", (0, 5))

    assert result.count == 0
    assert result.minimum_reachable is None
    assert result.minimum_unreachable == 0
    assert result.conflicts
    assert result.explanation["facts"] == [{"voucher": "original-1"}]
    assert "signed-receipt-9" in str(result.conflicts)


def test_functional_json_ready_entry():
    result = reconcile_receipts(
        {
            "choices": [
                {
                    "id": "c",
                    "copies": {
                        "a": [{"department": "m", "part": "P", "amount": 2, "source": "s"}],
                        "b": [{"department": "m", "part": "P", "amount": 5, "source": "s"}],
                    },
                }
            ],
            "department": "m",
            "interval": [2, 5],
        },
        as_dict=True,
    )

    assert result["count"] == 2
    assert result["minimum_reachable"] == 2
    assert result["minimum_unreachable"] == 3
    assert result["selection"] == {"c": "a"}
