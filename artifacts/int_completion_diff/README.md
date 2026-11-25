# Fixed-page diff for IntCompletionDAWG

The book is split into 512-byte pages.  A store needs only the pages in
`changed_pages`; every other byte stays as it was, and `total_size` is locked.

Files:

- `patch.json` — diff for `dev_data/small/int_completion_dawg.dawg`.
  Original book `bar=5, foo=1, foobar=3`; target book
  `bar=7, baz=9, foo=2, foobar=3, food=4`.
- `completion.patch.json` — diff for `dev_data/small/completion.dawg`.
  Original book `bar=0, f=0, foo=0, foobar=0`; target book
  `bar=7, baz=9, foo=2, foobar=0, food=4`.  `f` is deleted while
  the longer `foo`/`foobar` keys remain.
- `*.patched.dawg` — the fully patched books, for verification only.

Each page entry carries:

- `offset` and `length` in the original file,
- `sha256_original` / `crc32_original` — byte check of the page to be replaced,
- `sha256_patched` / `crc32_patched` — byte check of the replacement page,
- `original_base64` / `patch_base64` — literal page bytes.

Apply with `scripts/apply_page_diff.py SOURCE MANIFEST OUTPUT`.  The script
checks the original page hashes before writing anything.

## Why two pages

For the 1544-byte book:

- unit slot 0 (the root) is on page 0,
- guide slot 0 (the root's child list) is on page 2,
- page 2 also holds unit slot 255; page 3 holds only guide slots 252..255.

A non-empty book must change both the root unit and the root guide entry, so
two pages is the minimum.  Both diffs reach that minimum.  Page 1 and page 3
are byte-identical to the original.

## Layout constraints

- A node at slot `i` with base `B` reaches label `L` at `i = B ^ L`; the unit
  stored there must have label `L`.
- `B = 0` aliases the root; `B = 1` aliases every unused unit, which is why
  the builder rejects those bases.
- An internal terminal (a key that is also a prefix, such as `foo`) must store
  its value at the node's base slot.
- `scripts/diagnose_slots.py` prints the failing pages and these jump
  constraints when the provided free slots cannot hold a requested book.
