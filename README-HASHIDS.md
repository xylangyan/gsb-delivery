# Short-code counter rules

`hashids_counter.py` builds the minimum longest-prefix routing table for two
Hashids books, using each book's native `encode`/`decode` as the only source
of truth.

## Run

```bash
.venv/bin/python hashids_counter.py --demo
.venv/bin/python hashids_counter.py examples/hashids-book-pair.json --json
.venv/bin/python hashids_counter.py manifest.json --audit-limit 5000
```

`--demo` replays the pair stated in the task:

```text
old    salt '',        minimum 4, default alphabet
other  salt 'local-b', minimum 4, alphabet without 'f' and '5'

old.encode(151)    == 'epQ6'
other.encode(55263) == 'epQ6'
```

`--audit-limit N` is a small-ledger cross-check only: it walks `0..N` in each
book and compares the counter with native encode/decode.  Routing itself never
scans numeric ranges or short codes.

## Manifest

The handoff manifest carries both books and the reviewed material:

- `books[].alphabet`, `salt`, `min_length`: the book's real parameters.
  `drop` may remove characters instead of spelling out the alphabet.
- `books[].fields`: number of original fields (1–3).
- `books[].ranges`: loaded per-field closed intervals (`min`/`max` arrays).
- `books[].orders`: registered originals; duplicates count once.
- `books[].reviewed_ranges`: the reviewed per-field closed intervals.
- `reviewed_codes`: reviewed short codes whose two readings matter.
- `allowed_prefixes` / `max_prefix`: the separate prefix and length limits, if
  the counter only accepts a listed subset.
- `reviewed_scan_limit`: optional budget for expanding reviewed numeric boxes.
  A box larger than the budget stays symbolic; set it large only when you
  want the tool to enumerate that box.  Default `1000000`.

## Rules

- A book may read a code only when `decode(code)` returns an order and
  `encode(*order)` reprints the exact code.  Separators, guards and the
  position alphabets are never re-derived; they come from the real engine.
- A collision is a code that both books read, to different orders, and both
  decoded orders are real in their own book (registered or inside the loaded
  range).
- If one side of a collision is reviewed, the rule table must send the code to
  that book.  If both sides are reviewed, no prefix can separate them; the
  report gives both printable codes, both original orders and the blocking
  prefix instead of inventing a rule.
- Only true two-book collisions need rules.  A code with one real reading is
  delivered to that book without a rule, even if the other book can decode the
  characters to an unregistered number.
- Rules use the longest matching prefix.  Without a rule the counter consults
  the old book.
- The search is a trie dynamic program over reviewed collision codes.  It
  minimizes the number of rules exactly, then picks the ASCII-smallest prefix,
  then the ASCII-smallest book number.
- If the allowed-prefix list stops before a pair separates, the solver returns
  infeasible and names the finest blocking prefix rather than guessing.

## Worked result

With both sides reviewed, `epQ6` is one short code with two real originals:

```text
old:   epQ6 -> (151,)
other: epQ6 -> (55263,)
```

Every prefix rule that selects one book also selects the code itself, so no
longest-prefix table can keep both reviewed originals distinguishable.  The
tool reports that pair as indistinguishable and does not emit a rule.

When the old side is not reviewed and only `55263` is, the same code is
routable; the minimum table is one entry, `e -> other`.

## Tests

```bash
.venv/bin/python -m pytest -q -o addopts='' test/test_hashids_counter.py
```
