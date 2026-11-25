"""Shared-choice receipt reconciliation example.

Run with: uv run python examples/reconciliation.py
"""

from pyDatalog.reconcile import reconcile_receipts


payload = {
    "facts": [
        {"receipt": "signed-Q-1", "department": "quality", "total": 1},
        {"original_voucher": "V-17"},
    ],
    "base_totals": {"issue": 3},
    "transfers": [
        ("P-100", "X-9", "transfer-edge-1"),
        ("P-100", "Y-9", "transfer-edge-2"),
        ("X-9", "ACT-77", "transfer-edge-3"),
        ("Y-9", "ACT-77", "transfer-edge-4"),
    ],
    "withheld": ["HELD-1"],
    "choices": [
        {
            "id": "voucher-gap-1",
            "copies": {
                "approved-copy-A": [
                    {"department": "issue", "part": "P-100", "amount": 3, "source": "issue-line-1"},
                    {"department": "quality", "part": "P-100", "amount": 1, "source": "quality-line-1"},
                ],
                "approved-copy-B": [
                    {"department": "issue", "part": "P-100", "amount": 7, "source": "issue-line-1"},
                    {"department": "quality", "part": "P-100", "amount": 1, "source": "quality-line-1"},
                    {"department": "quality", "part": "HELD-1", "amount": 9, "source": "hold-line", "withheld": True},
                ],
            },
        }
    ],
    "signed_totals": [
        {"department": "quality", "total": 1, "receipt": "signed-Q-1"}
    ],
    "department": "issue",
    "interval": [6, 10],
}


if __name__ == "__main__":
    result = reconcile_receipts(payload)
    print("distinct totals:", result.possible_totals)
    print("minimum reachable:", result.minimum_reachable)
    print("minimum unreachable:", result.minimum_unreachable)
    print("witness:", result.witness)
    print("parts:", [line.part for line in result.included_lines])
    print("release paths:", [line.release_paths for line in result.included_lines])

    assert result.possible_totals == (6, 10)
    assert result.minimum_reachable == 6
    assert result.minimum_unreachable == 7
    assert result.witness == {"voucher-gap-1": "approved-copy-A"}
