"""Runnable acceptance check for the isolated cloudpickle review sessions.

The real handoff archives are supplied by the review process.  When run
directly this file creates equivalent trusted archives in an isolated
namespace and verifies every behavior described in the task.
"""

from __future__ import annotations

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


def build_archives():
    namespace = {"__name__": "handoff_pricing"}
    exec(compile(ARCHIVE_SOURCE, "<handoff-pricing>", "exec"), namespace)
    row_class = namespace["Row"]
    set_threshold = namespace["set_threshold"]

    archives = {
        "rule-original": cloudpickle.dumps(set_threshold),
        "row-original": cloudpickle.dumps(row_class),
        "existing-original": cloudpickle.dumps(row_class(18)),
        "supplement-22": cloudpickle.dumps(row_class(22)),
        "order-normal": cloudpickle.dumps(row_class(30)),
    }

    original_review = row_class.review
    exec(compile(BAD_REVIEW_SOURCE, "<bad-review>", "exec"), namespace)
    archives["order-bad"] = cloudpickle.dumps(row_class(24))

    row_class.review = original_review
    archives["order-good"] = cloudpickle.dumps(row_class(24))
    return archives


def main() -> int:
    archives = build_archives()

    review_a = ReviewSession(threshold=20)
    rule_a = review_a.load_rule(archives["rule-original"], "rule-original")
    row_a = review_a.load_row(archives["row-original"], "row-original")
    existing_a = review_a.load_sample(
        archives["existing-original"], "existing-original", "existing"
    )

    review_b = ReviewSession(threshold=20)
    rule_b = review_b.load_rule(archives["rule-original"], "rule-original")
    row_b = review_b.load_row(archives["row-original"], "row-original")
    review_b.load_sample(archives["existing-original"], "existing-original", "existing")

    rule_a(25)
    supplement_b = review_b.merge_sample(archives["supplement-22"], "supplement-22")
    supplement_a = review_a.merge_sample(archives["supplement-22"], "supplement-22")

    assert row_a is not row_b
    assert type(existing_a) is row_a
    assert type(supplement_a) is row_a
    assert type(supplement_b) is row_b

    result_a = supplement_a.review()
    result_b = supplement_b.review()
    assert result_a[0] is supplement_a and result_a[1:] == (25, False)
    assert result_b[0] is supplement_b and result_b[1:] == (20, True)
    print("A supplement:", result_a)
    print("B supplement:", result_b)

    rule_b(30)
    assert supplement_b.review()[1:] == (30, False)
    assert supplement_a.review()[1:] == (25, False)
    print("After B=30:", supplement_b.review(), "A remains:", supplement_a.review())

    bad_order = [
        ("order-normal", archives["order-normal"]),
        ("order-bad", archives["order-bad"]),
    ]
    try:
        review_a.merge_order(bad_order, order_id="order-42")
    except ArchiveConflict as conflict:
        print("Conflict:", conflict)
    else:
        raise AssertionError("bad order was accepted")

    assert review_a.samples_for_order("order-42") == []
    assert existing_a.review()[1:] == (25, False)
    assert supplement_a.review()[1:] == (25, False)

    corrected_order = [
        ("order-normal", archives["order-normal"]),
        ("order-good", archives["order-good"]),
    ]
    order = review_a.merge_order(corrected_order, order_id="order-42")
    assert review_a.Row is row_a
    assert all(type(sample) is row_a for sample in order.samples)
    assert review_a.threshold == 25
    print("Corrected order:", [sample.review() for sample in order.samples])
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
