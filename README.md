# tldextract [![PyPI version](https://badge.fury.io/py/tldextract.svg)](https://badge.fury.io/py/tldextract) [![Build Status](https://github.com/john-kurkowski/tldextract/actions/workflows/ci.yml/badge.svg)](https://github.com/john-kurkowski/tldextract/actions/workflows/ci.yml)

`tldextract` accurately separates a URL's subdomain, domain, and public suffix,
using [the Public Suffix List (PSL)](https://publicsuffix.org).

**Why?** Naive URL parsing like splitting on dots fails for domains like
`forums.bbc.co.uk` (gives "co" instead of "bbc"). `tldextract` handles the edge
cases, so you don't have to.

## Quick Start

```python
>>> import tldextract

>>> tldextract.extract('http://forums.news.cnn.com/')
ExtractResult(subdomain='forums.news', domain='cnn', suffix='com', is_private=False)

>>> tldextract.extract('http://forums.bbc.co.uk/')
ExtractResult(subdomain='forums', domain='bbc', suffix='co.uk', is_private=False)

>>> # Access the parts you need
>>> ext = tldextract.extract('http://forums.bbc.co.uk')
>>> ext.domain
'bbc'
>>> ext.top_domain_under_public_suffix
'bbc.co.uk'
>>> ext.fqdn
'forums.bbc.co.uk'
```

## Install

```zsh
pip install tldextract
```

## How-to Guides

### How to disable HTTP suffix list fetching for production

```python
no_fetch_extract = tldextract.TLDExtract(suffix_list_urls=())
no_fetch_extract("http://www.google.com")
```

Or set the default for new extractors with an empty environment variable. Set
it before importing `tldextract` to affect the module-level `extract` function:

```zsh
export TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS=""
```

With fetching disabled, `tldextract` uses its bundled snapshot if no matching
cache entry exists. See [Caching behavior](#caching-behavior) for details.

### How to set a custom cache location

Via environment variable:

```zsh
export TLDEXTRACT_CACHE="/path/to/cache"
```

Or in code:

```python
custom_cache_extract = tldextract.TLDExtract(cache_dir="/path/to/cache/")
```

### How to update TLD definitions

Command line:

```zsh
tldextract --update
```

Or delete the cache folder:

```zsh
rm -rf $HOME/.cache/python-tldextract
```

### How to treat private domains as suffixes

```python
extract = tldextract.TLDExtract(include_psl_private_domains=True)
extract("waiterrant.blogspot.com")
# ExtractResult(subdomain='', domain='waiterrant', suffix='blogspot.com', is_private=True)
```

### How to use a custom suffix list

#### Local suffix list

```python
extract = tldextract.TLDExtract(
    suffix_list_urls=["file:///path/to/your/list.dat"],
    cache_dir="/path/to/cache/",
    fallback_to_snapshot=False,
)
```

#### Remote suffix list

```python
extract = tldextract.TLDExtract(
    suffix_list_urls=["https://myserver.com/suffix-list.dat"]
)
```

#### Set the default suffix list sources

For either local or remote lists, set the default through a newline-delimited
environment variable:

```zsh
export TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS="https://myserver.com/suffix-list.dat"
```

New `TLDExtract()` instances read this value when constructed. An explicit
`suffix_list_urls` argument takes precedence. Set the environment variable
before importing `tldextract` to affect the module-level `extract` function.

An existing local path in `TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS` is converted to
a `file://` URL, like the `--suffix_list_url` command line option.

### How to set the suffix list fetch timeout

Set a default timeout in seconds before starting Python:

```zsh
export TLDEXTRACT_DEFAULT_FETCH_TIMEOUT="1.2"
```

Or set it for one extractor in code:

```python
extract = tldextract.TLDExtract(cache_fetch_timeout=1.2)
```

A single value applies separately to the connection and response-read phases of
each remote Public Suffix List request.

### How to add extra suffixes

```python
extract = tldextract.TLDExtract(extra_suffixes=["foo", "bar.baz"])
```

### How to validate URLs before extraction

```python
from urllib.parse import urlsplit

extract = tldextract.TLDExtract()
split_url = urlsplit("https://example.com/path")
result = extract.extract_urllib(split_url)
```

## Command Line

```zsh
$ tldextract http://forums.bbc.co.uk
forums bbc co.uk

$ tldextract --update  # Update cached suffix list
$ tldextract --help    # See all options
```

## Understanding Domain Parsing

### Public Suffix List

`tldextract` uses the [Public Suffix List](https://publicsuffix.org), a
community-maintained list of domain suffixes. The PSL contains both:

- **Public suffixes**: Where anyone can register a domain (`.com`, `.co.uk`,
  `.org.kg`)
- **Private suffixes**: Operated by companies for customer subdomains
  (`blogspot.com`, `github.io`)

Web browsers use this same list for security decisions like cookie scoping.

### Suffix vs. TLD

While `.com` is a top-level domain (TLD), many suffixes like `.co.uk` are
technically second-level. The PSL uses "public suffix" to cover both.

### Default behavior with private domains

By default, `tldextract` treats private suffixes as regular domains:

```python
>>> tldextract.extract('waiterrant.blogspot.com')
ExtractResult(subdomain='waiterrant', domain='blogspot', suffix='com', is_private=False)
```

To treat them as suffixes instead, see
[How to treat private domains as suffixes](#how-to-treat-private-domains-as-suffixes).

### Default behavior with unlisted suffixes

Unlike the [formal PSL algorithm](https://github.com/publicsuffix/list/wiki/Format#algorithm),
`tldextract` does not apply the implicit `*` rule when no suffix matches. For an
unlisted final label, `suffix` remains empty so callers can distinguish configured
public suffixes from unknown, invalid, or internal hostnames.

### Hostname representation

`tldextract` identifies public suffix boundaries but does not validate or
canonicalize hostnames. Returned strings can retain the input's casing and its
Unicode or ASCII-compatible encoding (ACE, commonly called Punycode).
Equivalent IDNA hostnames can therefore produce textually different values even
when `tldextract` identifies the same suffix boundary.

Before using `suffix`, `fqdn`, or joined-domain properties in equality checks,
allowlists, blocklists, or other security decisions, validate and normalize
both hostnames to the same IDNA representation. IDNA defines label equivalence
in terms of A-labels. See
[RFC 5890, section 2.3.2.4](https://www.rfc-editor.org/rfc/rfc5890.html#section-2.3.2.4).

### Caching behavior

By default, `tldextract` fetches the latest Public Suffix List on first use and
caches it indefinitely in `$HOME/.cache/python-tldextract`.

The cache key includes the configured URLs. An empty URL list does not reuse a
cache entry fetched with the standard URLs; if no entry exists for the empty
URL list, `tldextract` uses its bundled snapshot.

### URL validation

`tldextract` accepts any string and is very lenient. It prioritizes ease of use
over strict validation, extracting domains from any string, even partial URLs or
non-URLs.

## Migrating from `registered_domain`

The `registered_domain` property is deprecated since 5.3.0 and will be removed
in the next major version:

```text
DeprecationWarning: The 'registered_domain' property is deprecated and will be
removed in the next major version. Use 'top_domain_under_public_suffix'
instead, which has the same behavior but a more accurate name.
```

### Same configuration, same values

`top_domain_under_public_suffix` returns exactly what `registered_domain`
returned under the same suffix list and `include_psl_private_domains` setting,
so migrating is a pure rename as long as the configuration is unchanged. Teams
that keep their historical per-tenant grouping should swap the property name,
keep the same suffix list snapshot and `include_psl_private_domains` flag, and
continue validating the same field they already use. If you instead need a
grouping that does not vary with the private-domains flag, consider
`top_domain_under_registry_suffix`.

### When the historical configuration was not recorded

Some old reports stored only the grouped values, not which suffix-list
snapshot or `include_psl_private_domains` flag produced them. The candidate
configurations are then the historical suffix lists crossed with the flag, and
the only way to confirm a report's organization grouping is to re-check
original URLs from the log archive — often at a per-lookup cost.

Two rules keep the cost down:

- **Group candidates into equivalence classes first.** Candidates whose
  sources differ but which produce identical groupings on every report row are
  a single historical explanation. Never spend a lookup separating candidates
  inside a class.
- **A match on an ordinary public domain is not evidence.** Every candidate
  returns `bbc.co.uk` for `forums.bbc.co.uk`, because no historical list or
  flag setting changes that result. Such matches say nothing about which
  configuration was used; only rows on private suffixes, or on suffixes that
  moved between lists, discriminate.

### Runnable reconciliation example

The following script runs offline against two fixed local suffix lists and
reproduces the three situations you will meet: one lookup that cannot
distinguish the candidates, a second lookup that determines the organization
grouping, and a report that no further lookup can decide. It also prints a
provably minimal evidence plan.

```python
"""Reconcile an old report's `registered_domain` grouping with candidate configs.

Scenario: a daily report was grouped by the deprecated `registered_domain`
property, but nobody recorded which suffix-list snapshot or
`include_psl_private_domains` flag produced it. The archive can still return a
few original URLs, each lookup billed separately. This script finds the
cheapest set of URL lookups that proves which grouping the report used.

Runs offline against two fixed local suffix lists. Requires only `tldextract`.
"""

import itertools
import tempfile
from pathlib import Path

import tldextract

# Two historical suffix lists. They differ only in whether "github.io" is a
# private suffix. Everything below uses the migration-target property
# `top_domain_under_public_suffix`, which returns exactly what the deprecated
# `registered_domain` returned under the same configuration.
LIST_2023 = """\
// Snapshot 2023: github.io not yet listed
com
co.uk
io
// ===BEGIN PRIVATE DOMAINS===
blogspot.com
"""

LIST_2024 = """\
// Snapshot 2024: github.io added as a private suffix
com
co.uk
io
// ===BEGIN PRIVATE DOMAINS===
blogspot.com
github.io
"""


def make_extractor(list_path, include_private):
    return tldextract.TLDExtract(
        cache_dir=None,  # no cache writes; the lists above are fixed
        suffix_list_urls=[Path(list_path).as_uri()],
        fallback_to_snapshot=False,  # never silently use another list
        include_psl_private_domains=include_private,
    )


def grouping(extractor, urls):
    """The report's organization grouping: one grouped value per report URL."""
    return tuple(extractor(url).top_domain_under_public_suffix for url in urls)


def consistent_classes(classes, evidence):
    """Classes whose grouping reproduces every logged value in `evidence`."""
    return {
        key: names
        for key, names in classes.items()
        if all(key[url_index[url]] == value for url, value in evidence.items())
    }


def show(label, classes):
    print(label)
    for key, names in classes.items():
        print(f"    {', '.join(names)}")
        print(f"        grouping: {key}")


with tempfile.TemporaryDirectory() as tmp:
    list_2023 = Path(tmp, "list_2023.dat")
    list_2023.write_text(LIST_2023, encoding="utf-8")
    list_2024 = Path(tmp, "list_2024.dat")
    list_2024.write_text(LIST_2024, encoding="utf-8")

    candidates = {
        "2023 list, private=False": make_extractor(list_2023, False),
        "2023 list, private=True": make_extractor(list_2023, True),
        "2024 list, private=False": make_extractor(list_2024, False),
        "2024 list, private=True": make_extractor(list_2024, True),
    }

    # The daily report: original URL -> grouped value the report stored.
    report = {
        "http://forums.bbc.co.uk/": "bbc.co.uk",
        "http://waiterrant.blogspot.com/": "blogspot.com",
        "http://alice.github.io/": "github.io",
    }
    urls = list(report)
    url_index = {url: i for i, url in enumerate(urls)}

    # Merge candidates that produce identical groupings on every report row.
    # They are one historical explanation; never spend a lookup separating
    # candidates inside a class, even if their suffix-list sources differ.
    classes = {}
    for name, extractor in candidates.items():
        classes.setdefault(grouping(extractor, urls), []).append(name)

    show("Equivalence classes over the report rows:", classes)

    # --- Situation 1: one lookup is not enough -------------------------------
    evidence = {"http://alice.github.io/": report["http://alice.github.io/"]}
    remaining = consistent_classes(classes, evidence)
    show(
        "\nSituation 1: after re-checking only alice.github.io, "
        f"{len(remaining)} explanations still fit:",
        remaining,
    )

    # --- Situation 2: two lookups determine the grouping ---------------------
    evidence["http://waiterrant.blogspot.com/"] = report[
        "http://waiterrant.blogspot.com/"
    ]
    remaining = consistent_classes(classes, evidence)
    show(
        "\nSituation 2: after also re-checking waiterrant.blogspot.com, "
        f"{len(remaining)} explanation fits:",
        remaining,
    )

    # --- Proof that 2 lookups is the minimum ---------------------------------
    # A lookup set distinguishes all classes iff it gives every class a unique
    # signature of grouped values. Enumerate subsets, smallest first.
    print("\nMinimal evidence plan:")
    for size in range(len(urls) + 1):
        for subset in itertools.combinations(urls, size):
            signatures = {
                tuple(key[url_index[url]] for url in subset) for key in classes
            }
            if len(signatures) == len(classes):
                print(f"    sufficient ({size} lookups): {list(subset)}")
                for url in urls:
                    values = {key[url_index[url]] for key in classes}
                    if url not in subset and len(values) == 1:
                        print(
                            f"    {url} is in no minimal plan: every candidate "
                            "returns the same value here, so a match proves "
                            "nothing"
                        )
                break
        else:
            continue
        break

    # --- Situation 3: undecidable, no matter what else you check -------------
    # A different daily report. Only two original URLs are still retrievable;
    # its "github.io" aggregate row has no retrievable URL behind it.
    report2_retrievable = {
        "http://forums.bbc.co.uk/": "bbc.co.uk",
        "http://waiterrant.blogspot.com/": "waiterrant.blogspot.com",
    }
    remaining = consistent_classes(classes, report2_retrievable)
    show(
        f"\nSituation 3: all retrievable evidence checked, "
        f"{len(remaining)} explanations still fit:",
        remaining,
    )
    separable = [
        url
        for url in report2_retrievable
        if len({key[url_index[url]] for key in remaining}) > 1
    ]
    print(f"    further lookups that could separate them: {separable}")
    print(
        "    Display loss: under '2023 list, private=True' the report's "
        '"github.io"\n    row folds every *.github.io site into one '
        "organization; under\n    '2024 list, private=True' each site would "
        "have been its own row, so the\n    stored row can only be the bare "
        "domain. Both explanations reproduce the\n    stored report exactly, "
        "so the row's organization attribution cannot be\n    proven and must "
        "be flagged as unverifiable in the migrated report."
    )
```

Output:

```text
Equivalence classes over the report rows:
    2023 list, private=False, 2024 list, private=False
        grouping: ('bbc.co.uk', 'blogspot.com', 'github.io')
    2023 list, private=True
        grouping: ('bbc.co.uk', 'waiterrant.blogspot.com', 'github.io')
    2024 list, private=True
        grouping: ('bbc.co.uk', 'waiterrant.blogspot.com', 'alice.github.io')

Situation 1: after re-checking only alice.github.io, 2 explanations still fit:
    2023 list, private=False, 2024 list, private=False
        grouping: ('bbc.co.uk', 'blogspot.com', 'github.io')
    2023 list, private=True
        grouping: ('bbc.co.uk', 'waiterrant.blogspot.com', 'github.io')

Situation 2: after also re-checking waiterrant.blogspot.com, 1 explanation fits:
    2023 list, private=False, 2024 list, private=False
        grouping: ('bbc.co.uk', 'blogspot.com', 'github.io')

Minimal evidence plan:
    sufficient (2 lookups): ['http://waiterrant.blogspot.com/', 'http://alice.github.io/']
    http://forums.bbc.co.uk/ is in no minimal plan: every candidate returns the same value here, so a match proves nothing

Situation 3: all retrievable evidence checked, 2 explanations still fit:
    2023 list, private=True
        grouping: ('bbc.co.uk', 'waiterrant.blogspot.com', 'github.io')
    2024 list, private=True
        grouping: ('bbc.co.uk', 'waiterrant.blogspot.com', 'alice.github.io')
    further lookups that could separate them: []
    Display loss: under '2023 list, private=True' the report's "github.io"
    row folds every *.github.io site into one organization; under
    '2024 list, private=True' each site would have been its own row, so the
    stored row can only be the bare domain. Both explanations reproduce the
    stored report exactly, so the row's organization attribution cannot be
    proven and must be flagged as unverifiable in the migrated report.
```

Note the first equivalence class: the 2023 and 2024 lists with
`include_psl_private_domains=False` come from different sources but produce
identical groupings on every row of this report, so the plan spends no lookup
separating them.

### Why the evidence plan is provably minimal

A set of URL lookups distinguishes all candidate explanations if and only if
it assigns each equivalence class a unique signature of grouped values. The
script enumerates every subset of retrievable URLs, smallest first, so the
first sufficient subset found is minimal by construction. In the example, each
single URL is agreed upon by at least two classes — `forums.bbc.co.uk` by all
of them, `waiterrant.blogspot.com` by the two `private=True` classes, and
`alice.github.io` by the two classes that fold `github.io` — so no one-lookup
plan can work, and the two-lookup plan
`{waiterrant.blogspot.com, alice.github.io}` is the cheapest that
distinguishes every explanation that would change the target report.

### Undecidable reports and display loss

Situation 3 shows the failure mode: two different historical explanations —
`2023 list, private=True` and `2024 list, private=True` — both reproduce every
retrievable logged value, yet imply different organization groupings for the
report's `github.io` row. No remaining lookup can separate them, because they
differ only on `github.io` URLs and none are retrievable. The display loss is
concrete: under the first explanation the `github.io` row folds every
`*.github.io` site into one organization, while under the second each site
would have been its own row and the stored row can only be the bare domain.
Both reproduce the stored report exactly, so the migrated report must flag the
row's organization attribution as unverifiable rather than pick one
explanation silently.

### Migrating reports that only kept aggregates

When no original URLs survive at all, whether a report can migrate depends on
which suffixes its rows touch:

- **Rows on ordinary public suffixes keep the old grouping for free.** A row
  like `bbc.co.uk` or `cnn.com` is produced identically by every candidate
  list and flag setting, so the rename to `top_domain_under_public_suffix` is
  value-preserving with no log lookups. The same applies to any report whose
  evidence plan above confirmed a single explanation.
- **Rows on private or relocated suffixes cannot prove a new grouping.** A
  stored aggregate like `blogspot.com` or `github.io` hides which hostnames
  were folded into it. Without a retrievable URL you cannot restate the row
  under a new configuration — keep the old grouping and mark it as produced by
  an unverified configuration.
- **Do not treat matching values on ordinary public domains as evidence of a
  matching historical configuration.** Agreement on `bbc.co.uk` is guaranteed
  across all candidates and says nothing about the rows that actually
  discriminate.

## FAQ

### Can you add/remove suffix \_\_\_\_?

`tldextract` doesn't maintain the suffix list. Submit changes to
[the Public Suffix List](https://publicsuffix.org/submit/).

Meanwhile, [add extra suffixes](#how-to-add-extra-suffixes) with the
`extra_suffixes` parameter, or fork the PSL and
[use a custom suffix list](#how-to-use-a-custom-suffix-list) with the
`suffix_list_urls` parameter.

### My suffix is in the PSL but not extracted correctly

Check if it's in the "PRIVATE" section. See
[How to treat private domains as suffixes](#how-to-treat-private-domains-as-suffixes).

### Why does it parse invalid URLs?

See [URL validation](#url-validation) and
[How to validate URLs before extraction](#how-to-validate-urls-before-extraction).

## Contribute

### Setting up

1. [Install uv](https://docs.astral.sh/uv/getting-started/installation/).
2. `git clone` this repository.
3. Change into the new directory.
4. `uv sync`

### Running tests

```zsh
uv run pytest          # Fast local suite
uv run tox -e py310    # Supported-version suite
uv run tox --parallel  # Full matrix
uv run ruff format .   # Format code
```

## History

This package started from a
[StackOverflow answer](http://stackoverflow.com/questions/569137/how-to-get-domain-name-from-url/569219#569219)
about regex-based domain extraction. The regex approach fails for many domains,
so this library switched to the Public Suffix List for accuracy.
