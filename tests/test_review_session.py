import pytest

import cloudpickle
from cloudpickle.review_session import ArchiveConflict, ReviewSession

ARCHIVE_SOURCE = """
threshold = 20

def set_threshold(value):
    global threshold
    threshold = value

class Row:
    def __init__(self, unit_price):
        self.unit_price = unit_price
        self.review = self.review

    def review(self):
        return self, threshold, self.unit_price >= threshold
"""

BAD_REVIEW_SOURCE = """
def tampered_review(self):
    return self, threshold, "tampered"

Row.review = tampered_review
"""


@pytest.fixture
def archives():
    tracker_module = cloudpickle.cloudpickle
    saved_by_class = dict(tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS)
    saved_by_id = dict(tracker_module._DYNAMIC_CLASS_TRACKER_BY_ID)
    tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS.clear()
    tracker_module._DYNAMIC_CLASS_TRACKER_BY_ID.clear()

    namespace = {"__name__": "handoff_pricing"}
    exec(compile(ARCHIVE_SOURCE, "<handoff-pricing>", "exec"), namespace)
    Row = namespace["Row"]
    set_threshold = namespace["set_threshold"]

    payloads = {
        "rule": cloudpickle.dumps(set_threshold),
        "row": cloudpickle.dumps(Row),
        "existing": cloudpickle.dumps(Row(18)),
        "supplement": cloudpickle.dumps(Row(22)),
        "normal-order": cloudpickle.dumps(Row(30)),
    }

    original_review = Row.review
    exec(compile(BAD_REVIEW_SOURCE, "<bad-review>", "exec"), namespace)
    payloads["bad"] = cloudpickle.dumps(Row(24))
    Row.review = original_review
    payloads["good"] = cloudpickle.dumps(Row(24))
    payloads["tracker_id"] = tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS.get(Row)

    tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS.clear()
    tracker_module._DYNAMIC_CLASS_TRACKER_BY_ID.clear()
    yield payloads

    tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS.clear()
    tracker_module._DYNAMIC_CLASS_TRACKER_BY_ID.update(saved_by_id)
    tracker_module._DYNAMIC_CLASS_TRACKER_BY_CLASS.update(saved_by_class)


def test_two_reviews_are_isolated_and_merge_same_row(archives):
    review_a = ReviewSession(threshold=20)
    rule_a = review_a.load_rule(archives["rule"], "rule-original")
    row_a = review_a.load_row(archives["row"], "row-original")
    existing_a = review_a.load_sample(
        archives["existing"], "existing-original", "existing"
    )

    review_b = ReviewSession(threshold=20)
    rule_b = review_b.load_rule(archives["rule"], "rule-original")
    row_b = review_b.load_row(archives["row"], "row-original")
    existing_b = review_b.load_sample(
        archives["existing"], "existing-original", "existing"
    )

    rule_a(25)

    # Merge B first to prove that process-wide load ordering does not leak.
    supplement_b = review_b.merge_sample(archives["supplement"], "supplement-22")
    supplement_a = review_a.merge_sample(archives["supplement"], "supplement-22")

    assert row_a is not row_b
    assert type(existing_a) is row_a
    assert type(supplement_a) is row_a
    assert type(existing_b) is row_b
    assert type(supplement_b) is row_b

    assert supplement_a.__dict__["review"].__self__ is supplement_a
    assert supplement_b.__dict__["review"].__self__ is supplement_b
    assert supplement_a.__dict__["review"].__func__ is row_a.review
    assert supplement_b.__dict__["review"].__func__ is row_b.review

    result_a = supplement_a.review()
    detail_result_a = review_a.rerun_detail(
        {"review": True, "sample": supplement_a}
    )
    result_b = supplement_b.review()
    assert detail_result_a[0] is supplement_a
    assert result_a[0] is supplement_a
    assert result_a[1:] == (25, False)
    assert result_b[0] is supplement_b
    assert result_b[1:] == (20, True)

    rule_b(30)
    result_b = supplement_b.review()
    assert result_b[1:] == (30, False)
    assert supplement_a.review()[1:] == (25, False)

    ordinary = cloudpickle.loads(archives["supplement"])
    ordinary_result = ordinary.review()
    assert ordinary_result[0] is ordinary
    assert ordinary_result[1:] == (20, True)

    assert archives["tracker_id"] not in (
        cloudpickle.cloudpickle._DYNAMIC_CLASS_TRACKER_BY_ID
    ) or cloudpickle.cloudpickle._DYNAMIC_CLASS_TRACKER_BY_ID[
        archives["tracker_id"]
    ] is not row_a


def test_conflicting_order_is_atomic_and_can_be_resubmitted(archives):
    review_a = ReviewSession(threshold=20)
    rule_a = review_a.load_rule(archives["rule"], "rule-original")
    row_a = review_a.load_row(archives["row"], "row-original")
    existing_a = review_a.load_sample(
        archives["existing"], "existing-original", "existing"
    )
    rule_a(25)
    supplement_a = review_a.merge_sample(archives["supplement"], "supplement-22")

    review_b = ReviewSession(threshold=20)
    review_b.load_rule(archives["rule"], "rule-original")
    row_b = review_b.load_row(archives["row"], "row-original")
    supplement_b = review_b.merge_sample(archives["supplement"], "supplement-22")

    order_archives = [
        ("order-normal", archives["normal-order"]),
        ("order-bad", archives["bad"]),
    ]
    with pytest.raises(ArchiveConflict) as exc_info:
        review_a.merge_order(order_archives, order_id="order-42")

    conflict = exc_info.value
    assert "row-original" in str(conflict)
    assert "order-bad" in str(conflict)
    assert "review" in conflict.differences
    assert conflict.original_archive_id == "row-original"
    assert conflict.incoming_archive_id == "order-bad"

    assert review_a.samples_for_order("order-42") == []
    assert "order-normal" not in review_a.samples
    assert "order-bad" not in review_a.samples
    assert "order-42" not in review_a.orders
    assert "order-42" in review_a.rejected_orders

    assert type(existing_a) is row_a
    assert existing_a.review()[1:] == (25, False)
    assert supplement_a.review()[1:] == (25, False)
    assert review_a.threshold == 25

    assert type(supplement_b) is row_b
    assert supplement_b.review()[1:] == (20, True)

    ordinary_bad = cloudpickle.loads(archives["bad"])
    assert ordinary_bad.review()[2] == "tampered"

    corrected_archives = [
        ("order-normal", archives["normal-order"]),
        ("order-good", archives["good"]),
    ]
    order = review_a.merge_order(corrected_archives, order_id="order-42")

    assert review_a.Row is row_a
    assert all(type(sample) is row_a for sample in order.samples)
    assert review_a.samples_for_order("order-42") == order.samples
    assert existing_a.review()[1:] == (25, False)
    assert review_a.samples["order-normal"].review()[1:] == (25, True)
    assert review_a.samples["order-good"].review()[1:] == (25, False)
    assert "order-42" not in review_a.rejected_orders
