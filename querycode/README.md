# querycode

`querycode` turns templated, Unicode origin names into short ASCII query codes
for two legacy procurement sheets.  It is built directly on this repository's
Unidecode tables and is intended for review work: it reports which codes both
sheets can form, which of those come from *different* origin names, one legal
derivation per side, and whether a code is absent rather than a same-name
duplicate.

## Model

A JSON spec has four parts.

`fragments` maps fragment ids to text.  Ids are independent of the sheets and
are what witnesses report, so the pieces used to spell a name can always be
traced back.

`templates` maps template ids to finite, acyclic expressions:

| operator | meaning |
| --- | --- |
| `{"lit": "abc"}` | short literal string |
| `{"frag": "F01"}` | fragment text |
| `{"ref": "T2"}` | another template (must stay acyclic) |
| `{"seq": [e1, e2, ...]}` | ordered concatenation |
| `{"alt": [e1, e2, ...]}` | one of the alternatives |
| `{"rep": {"expr": e, "min": n, "max": m}}` | between `n` and `m` copies, with a finite `max` |

`sources` maps a sheet id to its template ids and transliteration policy.  A
sheet may list several templates: this is how one sheet's two legal routes are
represented.  A sheet with an independent number is just another entry.  Every
sheet also declares `missing`:

- `ignore` – drop characters with no table entry;
- `replace` – substitute `replace_str` (default `?`);
- `strict` – reject the name at the first character with no table entry.

`queries` is a list of `{"source": id, "prefix": "ascii", "limit": n}`.  The
prefix is an ASCII byte string, so paging follows real byte order.

`examples/querycode_demo.json` exercises every case.

## Transliteration rules

Transliteration goes through `unidecode._get_repl_str`, exactly as the library
does.  Three table outcomes are distinguished:

- a string of one or more ASCII characters – emitted as is;
- `''` – explicitly empty, and stays empty under every policy, including
  `replace` and `strict`;
- `None` – absent from the tables, and follows the sheet's `missing` policy.

Because matching works on the expanded ASCII stream, a character that expands
to two characters on one side can align with two neighbouring characters that
expand to one character each on the other side.  This is why the automaton is
built over characters, not origin-name positions.

`transliterate(name, missing, replace_str)` exposes the same rules.  `strict`
raises `StrictError` with `index` pointing at the first rejected character in
the *origin* name.

## Counting and paging

Templates are compiled to a finite acyclic NFA and determinised lazily.  The
intersection of two sheets is their product DFA.  Counting is dynamic
programming over that DAG, so the totals are exact without ever materialising
all origin names or all codes.  Only the requested page is reconstructed.

A code is counted once no matter how many template routes, fragment choices or
repeat counts spell it.

## Classification

For a shared code, each sheet is asked for up to two distinct origin names:

- both sides only produce the same single name → `same-name-repeat`;
- any pair of distinct names → `different-name`;
- only one side can spell it → `absent-a` / `absent-b`.

Each returned witness carries the origin name, template id, expression path and
the fragment ids actually used.

## Usage

```console
$ python -m querycode compare examples/querycode_demo.json \
      --left OLD_A --right OLD_B --limit 5
$ python -m querycode compare examples/querycode_demo.json \
      --left OLD_A --right OLD_B --prefix f --distinct
$ python -m querycode code examples/querycode_demo.json \
      --left OLD_A --right OLD_B --code fis
$ python -m querycode report examples/querycode_demo.json --right OLD_B
$ python -m querycode report examples/querycode_demo.json --right OLD_B --json
$ python -m querycode check examples/querycode_demo.json \
      --left OLD_A --right OLD_B
```

`check` re-transliterates every witness the search can produce with the
library, and, when two sheets are given, compares the optimised intersection
against brute-force enumeration.  Anything too large for the reference path is
reported as such rather than silently approximated.

`report` prints both the shared-code total and the subset whose two sides spell
it with different names; `--all` pages every shared code, including same-name
repeats, while `--json` emits the same structure for downstream review.

## Limits

The product DFA is worst-case exponential in the number of templates, as with
any intersection of regular languages.  In practice the codes are short and the
template graph is small.  Counting itself never expands names, but requesting
every page entry (`limit=None`) does reconstruct each shared code, so use
`limit` for large sheets.
