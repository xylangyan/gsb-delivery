# pyuca: Python Unicode Collation Algorithm implementation

[![Build Status](http://img.shields.io/travis/jtauber/pyuca.svg)](https://travis-ci.org/jtauber/pyuca)
[![Coverage Status](http://img.shields.io/coveralls/jtauber/pyuca.svg)](https://coveralls.io/r/jtauber/pyuca?branch=master)
![MIT License](http://img.shields.io/badge/license-MIT-brightgreen.svg)

[![DOI](https://zenodo.org/badge/3769/jtauber/pyuca.svg)](https://zenodo.org/badge/latestdoi/3769/jtauber/pyuca)
[![JOSS](http://joss.theoj.org/papers/10.21105/joss.00021/status.svg)](http://joss.theoj.org/papers/10.21105/joss.00021)


This is a Python implementation of the
[Unicode Collation Algorithm (UCA)](http://unicode.org/reports/tr10/). It
passes 100% of the UCA conformance tests for Unicode 5.2.0 (Python 2.7),
Unicode 6.3.0 (Python 3.3+), Unicode 8.0.0 (Python 3.5+), Unicode 9.0.0
(Python 3.6+), and Unicode 10.0.0 (Python 3.7+) with a variable-weighting
setting of Non-ignorable.

## What do you use it for?

In short, sorting non-English strings properly.

The core of the algorithm involves multi-level comparison. For example,
``café`` comes before ``caff`` because at the primary level, the accent is
ignored and the first word is treated as if it were ``cafe``. The secondary
level (which considers accents) only applies then to words that are equivalent
at the primary level.

The Unicode Collation Algorithm and pyuca also support contraction and
expansion. **Contraction** is where multiple letters are treated as a single
unit. In Spanish, ``ch`` is treated as a letter coming between ``c`` and ``d``
so that, for example, words beginning ``ch`` should sort after all other words
beginnings with ``c``. **Expansion** is where a single letter is treated as
though it were multiple letters. In German, ``ä`` is sorted as if it were
``ae``, i.e. after ``ad`` but before ``af``.

## How to use it

Here is how to use the ``pyuca`` module.

    pip install pyuca

Usage example:

    from pyuca import Collator
    c = Collator()

    assert sorted(["cafe", "caff", "café"]) == ["cafe", "caff", "café"]
    assert sorted(["cafe", "caff", "café"], key=c.sort_key) == ["cafe", "café", "caff"]

``Collator`` can also take an optional filename for specifying a custom
collation element table.

You can also import collators for specific Unicode versions,
e.g. `from pyuca.collator import Collator_8_0_0`.
But just `from pyuca import Collator` will ensure that the collator version
matches the version of `unicodata` provided by the standard library for your
version of Python.

## Comparing two collation tables

`pyuca.audit` compares an archived goods-name catalog under an old and a new
allkeys table.  It answers the release questions directly:

- exact old and new rank spans for every accession or compressed range;
- how many original card pairs really changed order (weighted, so a repeated
  range is never expanded);
- which cards cross a page edge, including cards whose own key did not change
  but whose page did;
- the earliest UCA level (or accession tie-break) that decided any chosen
  pair, in both versions.

```python
from pyuca.audit import audit, format_report

report = audit(
    old="allkeys-old.txt",
    new="allkeys-new.txt",
    catalog={
        "records": [
            {"entry": 1, "name": "café"},
            {"entry_range": [10, 99], "name": "same archived card"},
        ],
        "pages": {"anchors": [10, 50]},
        "boundaries": [10],
    },
    mode="full",          # or "primary" for a first-level index sheet
    tie_break="entry",    # old behaviour: first accession number
)
print(format_report(report))
```

The catalog may store each card's `old_key` and `old_page`; a separate
`old_index` may hold the actual old keys and source accession ranges.  Names
can be assembled from reusable `fragments` before key generation; the
assembled text is what gets normalized and compared, never the individual
fragments.  `mode="primary"` uses only the primary level (suitable for a
primary index sheet), while `mode="full"` uses all four levels.

The command-line wrapper is installed as `pyuca-audit`:

```bash
pyuca-audit catalog.json --format json --pair 1 42
pyuca-audit --compare allkeys-old.txt:allkeys-new.txt
```

Large catalogs produce per-page details lazily by accessor; JSON output
keeps accession ranges compressed unless `--expand` is explicitly requested.
`--page-range N` returns just one page's old and new accession segments, and
the report object also exposes `rank_at(entry)` and `pair_detail(a, b)` for
single-card checks.

## How to cite it

Tauber, J. K. (2016). pyuca: a Python implementation of the Unicode Collation Algorithm. The Journal of Open Source Software. DOI: 10.21105/joss.00021

## License

Python code is made available under an MIT license (see `LICENSE`).
`allkeys.txt` is made available under the similar license defined in
`LICENSE-allkeys`.

## Contacting the Developer

If you have any problems, questions or suggestions, it's best to file an issue
on GitHub although you can also contact me at jtauber@jtauber.com.

For more of my work on linguistics and Ancient Greek, see
<http://jktauber.com/>.
