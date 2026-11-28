# csv-diff

[![PyPI](https://img.shields.io/pypi/v/csv-diff.svg)](https://pypi.org/project/csv-diff/)
[![Changelog](https://img.shields.io/github/v/release/simonw/csv-diff?include_prereleases&label=changelog)](https://github.com/simonw/csv-diff/releases)
[![Tests](https://github.com/simonw/csv-diff/workflows/Test/badge.svg)](https://github.com/simonw/csv-diff/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/csv-diff/blob/main/LICENSE)

Tool for viewing the difference between two CSV, TSV or JSON files. See [Generating a commit log for San Francisco’s official list of trees](https://simonwillison.net/2019/Mar/13/tree-history/) (and the [sf-tree-history repo commit log](https://github.com/simonw/sf-tree-history/commits)) for background information on this project.

## Installation

    pip install csv-diff

## Usage

Consider two CSV files:

`one.csv`

    id,name,age
    1,Cleo,4
    2,Pancakes,2

`two.csv`

    id,name,age
    1,Cleo,5
    3,Bailey,1

`csv-diff` can show a human-readable summary of differences between the files:

    $ csv-diff one.csv two.csv --key=id
    1 row changed, 1 row added, 1 row removed

    1 row changed

      Row 1
        age: "4" => "5"

    1 row added

      id: 3
      name: Bailey
      age: 1

    1 row removed

      id: 2
      name: Pancakes
      age: 2

The `--key=id` option means that the `id` column should be treated as the unique key, to identify which records have changed.

The tool will automatically detect if your files are comma- or tab-separated. You can over-ride this automatic detection and force the tool to use a specific format using `--format=tsv` or `--format=csv`.

You can also feed it JSON files, provided they are a JSON array of objects where each object has the same keys. Use `--format=json` if your input files are JSON.

Use `--show-unchanged` to include full details of the unchanged values for rows with at least one change in the diff output:

    % csv-diff one.csv two.csv --key=id --show-unchanged
    1 row changed

      id: 1
        age: "4" => "5"

        Unchanged:
          name: "Cleo"

### JSON output

You can use the `--json` option to get a machine-readable difference:

    $ csv-diff one.csv two.csv --key=id --json
    {
        "added": [
            {
                "id": "3",
                "name": "Bailey",
                "age": "1"
            }
        ],
        "removed": [
            {
                "id": "2",
                "name": "Pancakes",
                "age": "2"
            }
        ],
        "changed": [
            {
                "key": "1",
                "changes": {
                    "age": [
                        "4",
                        "5"
                    ]
                }
            }
        ],
        "columns_added": [],
        "columns_removed": []
    }

### Adding templated extras

You can specify additional keys to be displayed in the human-readable format using the `--extra` option:

    --extra name "Python format string with {id} for variables"

For example, to output a link to `https://news.ycombinator.com/latest?id={id}` for each item with an ID, you could use this:

```bash
csv-diff one.csv two.csv --key=id \
  --extra latest "https://news.ycombinator.com/latest?id={id}"
```
These extras display something like this:
```
1 row changed

  id: 41459472
    points: "24" => "25"
    numComments: "5" => "6"
  extras:
    latest: https://news.ycombinator.com/latest?id=41459472
```

## Reconciling stocktakes with split and merged packages

Some inventories break one package into several parts, move those parts between
locations, and later merge them back into a new package.  A plain row diff
cannot tell whether a new `5` came from the right original pack, whether a
transfer was double-posted by two teams, or whether an intermediate step sent a
location negative.

Use the reconciliation mode when the occurrence archive contains `split`,
`merge`, `transfer`, `issue`, `receive` or `adjust` records:

```bash
csv-diff previous.csv current.csv \
  --occurrences occurrences.csv \
  --reconcile
```

The command prints a repair note with:

- a deterministic replay from the previous official stocktake to the current one;
- a team split (default `A,B`) so two independent teams can take one segment each;
- pause/resume checkpoints (see `--segment-limit` and `--checkpoint`);
- acceptance lists covering every `issue` and `merge` record in the archive.

Add `--json` for the machine-readable plan.  Resume a segment with:

```bash
csv-diff previous.csv current.csv --occurrences occurrences.csv --reconcile \
  --segment-limit A=2 --segment-limit B=2 --json \
  --execute-team A --checkpoint 1 --checkpoint-offset 0
```

`--checkpoint-offset` resumes partway through a segment after a pause; the
executed checkpoint reports the next segment and offset to re-read.

### Input files

Stocktake files need a package id and a quantity; location is strongly
recommended.  Common English and Chinese headings are accepted:

| meaning | example headings |
| --- | --- |
| package | `package`, `package_id`, `包号`, `实物号` |
| location | `location`, `warehouse`, `库位`, `仓库`, `仓` |
| quantity | `qty`, `quantity`, `available`, `数量`, `可用数量` |
| original pack | `origins`, `source`, `原包`, `来源包` |
| physical tag | `tags`, `tag_ids`, `标签` |

Occurrence columns include `occurrence_id` (`发生号`), `type` (`类型`), `inputs`
/ `outputs` (or `from_package` / `to_package`), `from_location` /
`to_location`, `teams` (`班组`), and optional `variants` (`批准对照`).  Inputs
and outputs may be JSON lists or compact strings such as `A-3:3|B0:2`.

### Placement rules

An optional rules JSON file can constrain the replay:

```json
{
  "transfers": [["A仓", "B仓"], ["B仓", "A仓"]],
  "location_limits": {"A仓": 20, "B仓": 15}
}
```

```bash
csv-diff previous.csv current.csv --occurrences occurrences.csv \
  --reconcile --rules rules.json
```

The planner enforces:

- a physical tag (or package, when tags are absent) may be transferred only once;
- no package or location may go negative at any intermediate step;
- final quantities must match the current stocktake and lineage must name the
  right original packages, not merely add up to the same total;
- duplicate pending transfer messages seen by both teams are applied once, while
  both teams' signed occurrence ids remain on the acceptance lists;
- the same logical posting under two team-signed ids is collapsed only when the
  postings are identical, the teams are disjoint, and the event is pending or a
  transfer; independently signed repeats are otherwise kept;
- if the same occurrence id has divergent postings, a transfer pair is illegal,
  or no common replay order exists, the plan stops with `manual_review` and
  returns the shared credentials (with their teams) to the supervisor.

### Acceptance lists

`--json` exposes `acceptance.journal`, `acceptance.checklists` (one per team),
and `acceptance.origin_coverage`.  Each checklist covers every issue and merge
record in the archive, marking records owned by the other team as `other_team`.
`acceptance.complete` is true only when both lists match the archive and every
required original pack is covered.

### When a plan is impossible

`status: manual_review` carries one of these reasons:

| reason | meaning |
| --- | --- |
| `shared_credential_conflict` | same occurrence id, different postings |
| `illegal_or_cyclic_archive` | illegal transfer pair or dependency cycle |
| `unexplained_stock` | positive current stock with no prior or archive source |
| `invalid_target` | negative quantity on the current stocktake |
| `no_common_replay` | archive cannot explain the difference; see `conflicts` |

### Worked example

`examples/` contains a minimal A→B split/transfer/merge case:

```bash
csv-diff examples/previous.csv examples/current.csv \
  --occurrences examples/reconcile_split_merge.csv \
  --reconcile --segment-limit A=1 --segment-limit B=1
```

### Plain tables

Files without split/merge lineage need no extra options; the original row-diff
behaviour is unchanged.

## As a Python library

You can also import the Python library into your own code like so:

    from csv_diff import load_csv, compare
    diff = compare(
        load_csv(open("one.csv"), key="id"),
        load_csv(open("two.csv"), key="id")
    )

`diff` will now contain the same data structure as the output in the `--json` example above.

If the columns in the CSV have changed, those added or removed columns will be ignored when calculating changes made to specific rows.

For split/merge reconciliation:

```python
from csv_diff.reconcile import plan_reconciliation, execute_segment, render_text

plan = plan_reconciliation(
    "previous.csv", "current.csv", "occurrences.csv",
    segment_limits={"A": 2, "B": 2},
    rules={"transfers": [["A仓", "B仓"]]},
)
print(render_text(plan))
result = execute_segment(plan, team="A", from_segment=1)
```

`plan_reconciliation` accepts paths, file objects, or row lists.  The returned
dict includes `status`, `actions`, `segments`, `execution`, `acceptance`, and
`conflicts`.

## As a Docker container

### Build the image

    $ docker build -t csvdiff .

### Run the container

    $ docker run --rm -v $(pwd):/files csvdiff

Suppose current directory contains two csv files : one.csv two.csv

    $ docker run --rm -v $(pwd):/files csvdiff one.csv two.csv
    
## Alternatives

- [csvdiff](https://github.com/aswinkarthik/csvdiff) is a "fast diff tool for comparing CSV files" - you may get better results from this than from `csv-diff` against larger files.
