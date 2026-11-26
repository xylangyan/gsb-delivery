# Handover Audit

Reconstructs which sealed view version and which material row every published
line was actually read from, when two receipts share one prepared
`SQLite3::Statement` and parts of the shift log only record the action
combinations that were allowed.

The audit replays the handover log against a **real in-memory SQLite database**
through the installed `sqlite3` gem, so the gem's own behavior supplies the
tricky semantics instead of a hand-written approximation:

- a schema change (drop/recreate a view) transparently reprepares an active
  statement and restarts it at the first row of the new view;
- a hash cursor keeps the column names cached when its statement first ran, so
  it can hand over new-view rows under old column names;
- every result set spawned by one statement shares that statement, so they
  consume one interlocking sequence of rows, and closing one closes all of
  them;
- bound parameters stay bound until rebound or `clear_bindings!`, so an
  `execute` that omits a parameter reuses the previous value.

## Why a replay is necessary

Receiving observations do not identify a source by themselves. A line can show
identical values and an identical column name under two view versions, and the
time a row was fetched cannot reveal which view version was live at that
moment. The only sound approach is to enumerate every handover allowed by the
log's uncertain cells, replay each one, keep those that reproduce every
receiving observation, and read the provenance (statement, view version, view
row, material id) off the surviving replays.

## Running

```sh
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby tasks/handover_audit/bin/handover-audit CASE.yaml
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby tasks/handover_audit/bin/handover-audit CASE.yaml --json
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby tasks/handover_audit/bin/handover-audit CASE.yaml --replay D1 3
```

`examples/` contains a worked cross-cursor case, an ambiguous case, and a case
whose book cannot be matched. Tests run without extra gems:

```sh
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby tasks/handover_audit/test/handover_audit_test.rb
```

## Case document

### `material`

The archived raw rows. Long tables stay as one continuous table; they are
loaded once and never rebuilt per handover combination.

```yaml
material:
  table: items            # table name created in the audit database
  id_col: id              # column carrying the original material number
  view_name: v            # name swap_view installs into when not explicit
  rows:
    - { id: 1, grp: A, v: a-one }
```

### `views`

The sealed view versions in version order. A value is either a bare `SELECT`
body or a full `CREATE VIEW ... AS SELECT ...` statement; selection, ordering
and any template expansion are taken verbatim and only executed locally.

```yaml
views:
  - version: v1
    sql: "SELECT id AS code, v AS label FROM items WHERE grp = 'A' ORDER BY id"
```

### `statements`

One prepared statement per shared-cursor group.

```yaml
statements:
  - id: S1
    sql: "SELECT * FROM v"
    # view_name: v          # defaults to material.view_name
    # initial_view: v1      # defaults to the first sealed view
```

### `deliveries` (approval slips)

Each slip states the view version the receiver expected, the output-column to
raw-field mapping, and how publish line numbers map to original material ids.
The id output column is inferred from the column that maps to `material.id_col`,
or set explicitly with `id_column`.

`row_map` supports:

- `sequence`: material id = `start + (line - 1) * step` (fixed step);
- `segment`: explicit `[publish_first, publish_last, material_first]` ranges;
- `fixed`: every line maps to one material id.

```yaml
deliveries:
  - id: D1
    statement: S1
    expect_view: v1
    columns: { code: id, label: v }
    row_map: { method: sequence, start: 1, step: 1 }
```

### `groups` and `handovers`

A handover entry is one shift cell. A plain action is the single permitted
choice; a `group` (or inline `any`) turns the cell uncertain. Referencing the
same group from several cells shares one choice, matching a missing cell that
is reused in more than one place.

```yaml
groups:
  maybe_swap:
    - { op: pass }                       # do nothing at this cell
    - { op: swap_view, view: v2 }
handovers:
  - { id: h0, actions: [{ op: prepare, statement: S1 }] }
  - { id: h1, actions: [{ op: execute, statement: S1, handle: H1, mode: hash }] }
  - { id: h2, actions: [{ op: columns, handle: H1 }] }   # cache column names
  - { id: h3, actions: [{ group: maybe_swap }] }
```

Actions: `prepare`, `execute` (`mode: hash|array`, optional `vars`), `step`,
`columns`, `close`, `swap_view`, `bind`, `reset`, `clear_bindings`, `pass`.
Handles bind a cursor to the delivery that owns them.

### `ledger` (receiving book)

Spot checks name a column and the value observed; plain row events only
certify that a continuous line was written, with `end` or `error` terminals.

```yaml
ledger:
  - delivery: D1
    handle: H1
    events:
      - { type: sample, column: code, value: 3 }
      - { type: row, line: 1 }
      - { type: end }            # or { type: error, message: "closed statement" }
```

## Verdict

For every delivery the lines are compressed into three bands:

- **proven-wrong-source** — every compatible handover reads the line from the
  wrong view version or wrong material id relative to the approval slip;
- **proven-correct** — every compatible handover reads the expected view and
  material id;
- **either-source-possible** — some compatible handovers are correct and others
  are not. `--replay` lists each possibility with the handover that witnesses
  it.

If no handover reproduces the book, the earliest receiving observation that
every candidate fails is reported instead, located by receipt and event index.

## Cost

All combinations replay on one database inside savepoints: view swaps roll back
after each replay, so the material table and the sealed views are built a single
time. The number of combinations is the product of the uncertain cells' option
counts and is capped (`Solver::MAX_COMBINATIONS`) rather than expanded into
rebuilt full tables.
