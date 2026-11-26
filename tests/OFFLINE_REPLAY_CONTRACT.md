# Offline replay contract tests

This directory contains a repeatable, network-free contract checker for two
catalog collection procedures that are expected to share one offline replay
fixture.

## Run

From the workspace root:

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_offline_replay_contract.py
```

The `-o addopts=''` form avoids the repository's coverage/xdist defaults when
a virtual environment only has the runtime test dependencies. In a fully
provisioned development environment, `pytest tests/test_offline_replay_contract.py`
also works.

Generate a JSON decision report for a supplier configuration:

```bash
.venv/bin/python tests/offline_replay_contract.py \
  tests/fixtures/offline_replay_supplier.json \
  --write-report tests/fixtures/offline_replay_report.json
```

Exit status is `0` for `same`, `1` for `divergent`, and `2` when a large
configuration needs an unsupported symbolic `update_query` proof shape and the
bounded scan is therefore explicitly `inconclusive` rather than falsely
reported as `same`.

## Configuration

`tests/fixtures/offline_replay_supplier.json` is the recorded z24 fixture.

- `input.references`: ordered `r` candidates. Registration order is authoritative.
- `input.k` and `input.v`: ASCII `alphabet` and inclusive length range.
- `input.v.binding.kind`:
  - `independent`
  - `equal_to_k`
  - `append_literal_to_k` with `suffix`
- `procedures`: exactly two procedures.
- `procedures[*].encoded`: value passed to `URL(..., encoded=...)` for both
  the catalog URL and the relative reference.
- `procedures[*].method`: `extend_query` or `update_query`.
- `procedures[*].pairs`: ordered `[key, value]` templates. A template is either
  `{"literal": "x"}` or `{"var": "k"}` / `{"var": "v"}`.

The renderer performs exactly the required pipeline:

1. parse the fixed catalog URL;
2. parse `r` with the same `encoded` value and call `URL.join`;
3. apply the one ordered query modification;
4. pass the inner `raw_path_qs` as the single `target` query value;
5. compare the outer replay URL's `raw_path_qs`.

## Ordering and reports

Inputs are visited in `r` registration order, then `k`, then `v`. String
domains are ordered by length first and ASCII lexicographic order within a
length.

A divergent report contains:

- complete `r`, `k`, and `v`;
- both outer `raw_path_qs` values;
- both full outer URLs for convenience;
- stage targets for `catalog`, `join`, `query_modification`, and `replay`;
- the first stage at which the two procedures differ;
- an exact mixed-radix `order_index`.

Every reported counterexample is built by the in-repository `yarl` and can be
replayed with `replay_counterexample`.

## 512-input bound

`HARD_MAX_CONCRETE_CASES` is 512. If a configuration has at most 512 permitted
inputs, every input is generated lazily, rendered with `yarl`, and compared.

Larger domains are not brute-forced. The checker jumps directly to the earliest
structurally relevant key collision or expression witness, verifies that
concrete input with `yarl`, and reports it. Configurations that are identical
by construction, or extend/update identical pairs over a canonical query with
no permitted key collision, receive an all-input `same` proof with zero or a
small number of concrete checks. A generated-but-discarded case still advances
`ConcreteBudget.generated`; no decision can exceed 512 concrete permitted
inputs.

The small `ab` domain test independently loops through every input with `yarl`
and checks the checker's first divergence. The actual z24 catalog also has a
78-case direct slice covering every one-character `k` across all three `r`
values.
