"""Build the synthetic redaction bundle used for self-testing.

Scenarios:

* ``s_merge``        - the prompt's A-X-B / C-Y-D case; sharing one empty row
  would synthesize an A->D path the checker never saw, so two rows are needed.
* ``s_merge_strict`` - same graph, but the license only allows one shared row;
  this sentence is infeasible and must be diagnosed.
* ``s_keep``         - the only witness of a yes-query runs through a hidden
  role; dropping those relations would erase a real path.
* ``s_tie``          - two single-row assignments are equally economical; one
  is delivered and the alternative is recorded with its original positions.
"""

from __future__ import annotations

import json
from pathlib import Path


def sentence(sent_id: str, rows: list[str]) -> str:
    return f"# sent_id = {sent_id}\n" + "\n".join(rows) + "\n\n"


MERGE_ROWS = [
    "1\tA\t_\t_\t_\t_\t0\troot\t0:root\t_",
    "2\tX\t_\t_\t_\t_\t1\tdep\t1:r1\t_",
    "3\tB\t_\t_\t_\t_\t2\tdep\t2:r2\t_",
    "4\tC\t_\t_\t_\t_\t0\troot\t0:root\t_",
    "5\tY\t_\t_\t_\t_\t4\tdep\t4:r3\t_",
    "6\tD\t_\t_\t_\t_\t5\tdep\t5:r4\t_",
]

MERGE_QUERIES = [
    {"name": "qAB", "source": "1", "target": "3", "labels": ["r1", "r2"]},
    {"name": "qCD", "source": "4", "target": "6", "labels": ["r3", "r4"]},
    {"name": "qAD", "source": "1", "target": "6", "labels": ["r1", "r4"]},
    {"name": "qCB", "source": "4", "target": "3", "labels": ["r3", "r2"]},
]

KEEP_ROWS = [
    "1\tP\t_\t_\t_\t_\t0\troot\t0:root\t_",
    "2\tQ\t_\t_\t_\t_\t3\tdep\t3:b\t_",
    "3\tH\t_\t_\t_\t_\t1\tdep\t1:a\t_",
]

TIE_ROWS = [
    "1\tM\t_\t_\t_\t_\t0\troot\t0:root\t_",
    "2\tN\t_\t_\t_\t_\t1\tdep\t1:u\t_",
    "3\tO\t_\t_\t_\t_\t2\tdep\t2:v\t_",
]


def main() -> None:
    out = Path(__file__).parent
    bundle = {
        "sentences": [
            {
                "sent_id": "s_merge",
                "conllu": sentence("s_merge", MERGE_ROWS),
                "id_map": {"1": "1", "3": "2", "4": "3", "6": "4"},
                "hidden_roles": ["2", "5"],
                "license": {"2.1": ["2", "5"], "4.1": ["2", "5"]},
                "placeholder_rows": {
                    "2.1": {"misc": {"OriginalId": ["X"]}},
                    "4.1": {"misc": {"OriginalId": ["Y"]}},
                },
                "basic_override": {
                    "3": {"head": "1", "deprel": "dep"},
                    "6": {"head": "3", "deprel": "dep"},
                },
                "queries": MERGE_QUERIES,
            },
            {
                "sent_id": "s_merge_strict",
                "conllu": sentence("s_merge_strict", MERGE_ROWS),
                "id_map": {"1": "1", "3": "2", "4": "3", "6": "4"},
                "hidden_roles": ["2", "5"],
                "license": {"2.1": ["2", "5"]},
                "basic_override": {
                    "3": {"head": "1", "deprel": "dep"},
                    "6": {"head": "3", "deprel": "dep"},
                },
                "queries": MERGE_QUERIES,
            },
            {
                "sent_id": "s_keep",
                "conllu": sentence("s_keep", KEEP_ROWS),
                "id_map": {"1": "1", "2": "2"},
                "hidden_roles": ["3"],
                "license": {"1.1": ["3"], "2.1": ["3"]},
                "basic_override": {"2": {"head": "1", "deprel": "dep"}},
                "queries": [
                    {"name": "qVia", "source": "1", "target": "2", "labels": ["a", "b"]},
                    {"name": "qDirect", "source": "1", "target": "2", "labels": ["b"]},
                ],
            },
            {
                "sent_id": "s_tie",
                "conllu": sentence("s_tie", TIE_ROWS),
                "id_map": {"1": "1", "3": "2"},
                "hidden_roles": ["2"],
                "license": {"1.1": ["2"], "2.1": ["2"]},
                "basic_override": {"3": {"head": "1", "deprel": "dep"}},
                "queries": [
                    {"name": "qTie", "source": "1", "target": "3", "labels": ["u", "v"]},
                    {"name": "qNo", "source": "3", "target": "1", "labels": ["v"]},
                ],
            },
        ]
    }
    (out / "bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", out / "bundle.json")


if __name__ == "__main__":
    main()
