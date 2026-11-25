# Row files: forward-only reads, correction orders and bounded windows

`rows.rowfile` deals with a source that can be read **forward exactly once** (for
example, a supplier table delivered as a stream that must not be reopened). The
source is ingested incrementally into a shared buffer and two kinds of readers
share the rows already read, so neither ever rewinds or re-reads the source:

- **old readers** iterate the original rows forward and may stop early;
- **new readers** request a small window of the *corrected* document and need
  the actual source of each position.

The scheduler only knows an inclusive `(lower, upper)` bound on the final total
row count; that bound can range from tens of millions to hundreds of millions.

## Reading a source once

```python
import rows.rowfile

rowfile = rows.rowfile.RowFile(source, total_bounds=(20_000_000, 100_000_000))

# Old reader: forward streaming, may stop early.
first_rows = []
for row in rowfile.old_view():
    first_rows.append(row)
    if len(first_rows) == 100:
        break

# The source is not reopened: later reads reuse the buffer and continue forward.
```

`rowfile.total_length` stays `None` until the source is exhausted (an old
reader that stops early never forces a full read); after `finish()` it is exact.

## Correction orders

An **order** is a batch of Python slice edits applied to a plain list, in order.
Slices may count from the end (negative indices), use negative steps and change
the middle length (`step == 1` replacements):

```python
order = (
    rows.rowfile.Order()
    .delete(-1, None, -2)          # del container[-1::-2]
    .assign(-3, -1, ["fix"])       # container[-3:-1] = ["fix"]
)
rowfile.apply(order)
```

An order is **atomic**: it only takes effect when the whole sequence succeeds
as a plain list. If a single extended slice assignment has the wrong number of
items (or a step is zero), the entire order is void and the original rows are
untouched. `apply()` returns `APPLIED`, `VOID` (it is void at every length in
the interval) or `UNDECIDED` (valid for some lengths, void for others; resolved
by `finish()`).

## Bounded windows and provenance

```python
report = rowfile.window(0, 10, read=True)
for entry in report:
    print(entry.position, entry.outcome)
```

Each position's outcome is one of:

- `Origin(index)` - the original row `index` now occupies that position;
- `Inserted(value, token)` - a literal supplied by an assignment (the token keeps
  equal values from different edits distinct);
- `MissingBody(index)` - the origin is recognised but its body has not been read
  yet (`read=True` fills forward only as far as needed);
- `OutOfRange(position)` - the corrected document is shorter than the position;
- `Pending(position, alternatives)` - the bound leaves the position ambiguous.
  `alternatives` holds up to two witness total lengths and the different results
  they produce, so the position can be verified by hand. Two equal values that
  belong to different original rows are never merged.

`rowfile.safe_prefix_stop()` returns the largest prefix of final positions that
no correction can reach. A window inside that prefix is delivered without
waiting for the tail: its rows are the same whether the order succeeds or is
void. A correction that reaches towards the beginning (for example
`del container[-1::-2]`, which touches every other row down to index 1) leaves a
very small safe prefix; a bounded tail edit (for example
`del container[-4::-2]`) leaves almost the whole document safe.

The total bound is never probed length by length and the common-multiple period
of slice steps is never laid out across the whole interval: only the interval
ends and one period at each end are examined.

## Finishing and replay verification

```python
rowfile.finish()                  # read to the end; exact length known
ok, details = rowfile.verify()    # replay every order on a plain list
```

`verify()` runs every order on a literal plain list built from the same original
input and checks each successful position's traced origin (or the complete
identity of a void order) against the replay, so every outcome can be
cross-checked.
