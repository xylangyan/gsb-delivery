# Cabinet TZInfo packaging factory

Builds the small, independently-openable `TZInfo::Data` packages that go on the
collection cabinets, plus the change list (换包清单) and the verification
evidence. Everything is driven from one work-order file so the tooling stays
generic.

## Run

```sh
# gem environment used by the task
export GEM_HOME=$PWD/.gems/ruby/4.0.0 GEM_PATH=$PWD/.gems/ruby/4.0.0 RUBYLIB=$PWD/lib
RUBY=$(mise which ruby)   # or the 4.0.7 ruby in .gsb-runtime.json

$RUBY factory/build.rb        # build every cabinet x edition package + byte bill
$RUBY factory/run_audit.rb    # build, isolated audit, feasibility, shipping manifest
```

Outputs land in `factory/build/` (git-ignored):

- `build/<cabinet>/<edition>/` — one standalone package root (`lib/tzinfo/...`)
  with a `manifest.json`.
- `build/baseline/<edition>/` — the unclipped full edition generated from the
  short rule source; cabinet packages are cross-checked against it.
- `build/SHIPPING_MANIFEST.json` / `.md` — the consolidated change list.

## Work-order mapping

| Work-order concept | Where it lives |
|---|---|
| Two complete editions of source data | `rules_a_old.tzdata`, `rules_b_new.tzdata` (short tzdata-style files) |
| Reusable, approved rewrite forms | `generator.rb` emits full / windowed data zones and linked zones |
| Cabinets, queried zones, countries, local windows, samples, budget | `catalog.rb` `CABINETS` |
| Candidate codes, measured byte fee, edition | `catalog.rb` `CANDIDATES` |
| Old name now points at a new name; old entry still in recovery | Edition **A** keeps `Asia/Calcutta`/`Asia/Dacca` as separate data zones; edition **B** makes them links. Aliases are never collapsed into the canonical entry. |
| Shared compressed modules | `catalog.rb` `SHARED_BLOCKS`; still materialised on disk under the original `tzinfo/data/definitions/...` entry point |
| Cheapest affordable combination under a cap | `packer.rb` picks the cheapest feasible candidate per zone/edition and enforces each budget and `GLOBAL_BUDGET` |
| Minimum budget / what blocks a cut | `feasibility.rb` |

## Correctness rules enforced by `auditor.rb`

Each package is opened in a **separate Ruby process** whose load path contains
only that package's `lib` (plus the tzinfo gem), proving standalone opening.

1. Standalone open via `require 'tzinfo/data'`, both indexes load.
2. Timezone index is canonically sorted and matches the edition's own
   data/link classification; shared identifiers keep the full-edition relative
   order.
3. Links resolve to a shipped target (no dangling modules); old name stays a
   data zone in A and becomes a link in B — never renamed in place.
4. Country index keeps the full-edition visible order of the shipped
   candidates and drops (never dangles) the unshipped entries.
5. Samples with no embedded UTC offset recompute to the reference instants:
   the spring skipped hour returns **zero** readings; the fall repeated hour
   returns **two** original UTC instants.
6. Fold-in probes just outside both window edges still resolve, and every
   reference transition inside the coverage window is retained (whole-window
   coverage, no per-second sampling).
7. Every clipped reading equals the unclipped full baseline from the same
   source, and old (A) and new (B) packages return identical UTC instants for
   the same reading, so a booked sample can be re-verified after the swap and
   again after switching back.

If a target budget is below the minimum feasible cost, `feasibility.rb` reports
the shortfall and names the blocking queried zone, link target, country index,
or overlap/gap hour rather than silently dropping a required part.
