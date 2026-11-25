# Replaying a cache audit locally

`bin/geocoder-replay` runs recorded Geocoder material entirely in memory. It
never contacts a geocoding service: Nominatim responses come from the scenario
file, and the cache is a local hash behind the library's own
`Geocoder::CacheStore::Generic` adapter.

The point is to separate two questions that a downstream report conflates:

1. **Which raw body produced these coordinates?**
2. **Which fetch last set the `cache_hit` marker on this result?**

`Geocoder::Lookup::Base#search` stamps every result with the lookup's current
`@cache_hit` at wrap time, not at fetch time. If another slot on the same
process fetches between two wraps, the later result can carry different
coordinates from the earlier one but the marker of the later fetch. That is why
the same query can produce one candidate marked as a hit and another marked as
a miss.

## Run it

```sh
ruby bin/geocoder-replay path/to/scenario.json
ruby bin/geocoder-replay path/to/scenario.json --format json
ruby bin/geocoder-replay path/to/scenario.json --order 's1:1,s1:2,s2:1,s2:2,s2:3,s1:3'
ruby bin/geocoder-replay path/to/scenario.json --reproduce
```

Exit status is `0` when the declared order replays, `1` when it does not, `2`
for input errors, and `3` for an unexpected replay violation. `--reproduce`
prints the smallest recorded prefix needed to reach the focused candidate.

## Input

A scenario is JSON or YAML with `bodies`, `processes`, and either `slots`
(pre-grouped operations) or `events` (a flat ordered log). See
`test/fixtures/replay/three_slots.json` and
`test/fixtures/replay/three_slots_events.json` for equivalent examples.

### Flat event log

When the audit material is one ordered list, use `events`. Each event names a
`slot` and the `process` it belongs to; `seq` supplies the global order when
every event has one, otherwise array order is used.

```json
{
  "bodies": { "old-two": "...", "unparseable": "..." },
  "processes": [ { "id": "p1", "cache": { "...": "old-two" } } ],
  "events": [
    { "seq": 1, "slot": "s1", "process": "p1", "type": "query",
      "query": "Main Street", "cache_hit": true },
    { "seq": 2, "slot": "s1", "process": "p1", "type": "result",
      "candidate": 0, "cache_hit": true, "coordinates": [10.0, 20.0] },
    { "seq": 3, "slot": "s2", "process": "p1", "type": "response",
      "query": "Main Street", "body": "unparseable" }
  ]
}
```

Accepted `type` values: `query`/`search`/`fetch`, `response`/`receive`/
`arrival`, `result`/`wrap`, `cache_delete`/`delete`/`expire`, `cache_write`/
`write`, and `note`. Field aliases: `body_id` for `body`, `coords` for
`coordinates`, `track` for `slot`, `worker`/`pid` for `process`, and `cache`
for `initial_cache`.

```json
{
  "bodies": {
    "old-two":     { "code": 200, "body": "[{...}, {...}]" },
    "unparseable": { "code": 200, "body": "<html>not JSON</html>" }
  },
  "processes": [
    {
      "id": "p1",
      "prefix": "geocoder:",
      "config": { "host": "nominatim.local", "language": "en" },
      "initial_cache": {
        "https://nominatim.local/search?...": "old-two"
      }
    }
  ],
  "slots": [
    { "id": "s1", "process": "p1", "operations": [...] }
  ],
  "order": ["s1:1", "s1:2", "s2:1"]
}
```

- `bodies` are raw HTTP bodies. `code` defaults to 200. A body that is not JSON
  is still cached when its status is 2xx/3xx, exactly as Geocoder does; it just
  yields no candidates.
- `initial_cache` keys are physical cache keys. Include the prefix when the
  original key had it. Values may be a body id or an inline
  `{ "body": "..." }`.
- Slots on the same process share one `Lookup` and one cache. Slots on different
  processes are independent unless `groups` puts them together.
- Operation indexes are 1-based in `order` and in reports: `"s1:2"` is the
  second operation of slot `s1`.

### Operations

| `op` | Fields | Meaning |
| --- | --- | --- |
| `search` | `query`, `options`, `body`?, `cache_hit`?, `results`? | Run Geocoder's real `fetch_raw_data`. `body` forces one response for this call (for reproducing a lost arrival); otherwise the next queued `respond` for the query is used. |
| `respond` | `query`, `body` | A response has arrived but has not been consumed yet. It stays queued until a miss takes it. |
| `fetch` | same as `search`, plus `id` | Alias for `search`, with a stable fetch id for later `result` references. |
| `result` | `candidate`, `body`?, `cache_hit`?, `coordinates`?, `address`?, `fetch`? | Wrap one candidate. Without `body`, the slot's preceding fetch is used. `coordinates`/`address` are assertions checked against the real `Nominatim` result object. |
| `cache_delete` | `key` or `query` | Call `Cache#expire` with the given key. The key is literal: pass the original cache key, including the prefix. |
| `cache_write` | `key` or `query`, `body` | Write a raw body into the cache. |
| `note` | `message` | Annotation only. |

Assertions are deliberate. `cache_hit`, `coordinates`, and `address` let the
scenario state what the downstream report saw; a mismatch becomes the earliest
unexplained step rather than a silent success.

## What the report answers

- **Declared order** — replays the `order` as given and shows each fetch, wrap,
  cache read/write/delete, and marker. If no `order` is present, the tool
  synthesizes the first consistent interleaving and replays it.
- **Consistent orders** — enumerates interleavings that replay,
  bounded by `--max-orders` and `--max-nodes`. If none exists, it reports the
  deepest explained prefix and the first step no branch could execute.
- **Provenance** — for each wrapped candidate, the body that supplied it, its
  coordinates, and the marker it inherited.
- **Focused candidate** — lists bodies that could supply the same candidate at
  the same index. `attribution: unique` means no other available body explains
  it; otherwise the alternatives are named.

## Independent batches

When the log contains many batches that cannot affect each other, put each
independent set of processes in its own `groups` entry:

```json
"groups": [
  { "id": "batch-a", "processes": ["p1"] },
  { "id": "batch-b", "processes": ["p2"] }
]
```

Each group is solved separately and the global count is the product of the
per-group counts; the tool never enumerates a global interleaving. Processes in
the same group share a cache namespace and are solved together. Without
`groups`, every process is its own group.

## Groups vs. flat logs

With `groups`, processes listed together share one cache. Their initial caches
are merged (conflicting values for the same key are an input error) and their
slots are replayed against one store and one `@cache_hit` marker. Processes in
different groups are truly independent and are solved separately; the global
combination count is the product of the per-group counts.

## Key semantics

Cache keys are always computed by the current library:
`base_query_url(query) + hash_to_query(cache_key_params(query))`, with API keys
and tokens omitted and parameters sorted. Deletions are literal, because
`Geocoder::Cache#expire` passes the caller's key straight to the store adapter.
A delete that omits the prefix therefore removes nothing; the trace shows
`removed` or `was absent`.

Some notes on the replay model:

- A 200 response with an unparseable body is still written to cache. A later
  `cache_hit: true` fetch can return that body and still yield no addresses.
- Result markers are read from the shared lookup at wrap time. Wrapping is
  modelled one candidate at a time so a fetch in another slot can fall between
  two wrappers, which is the case that produces mixed markers.
- A response recorded for a different query is never substituted; doing so is a
  hard violation.

## Tests

```sh
ruby -Ilib -Itest test/unit/replay_test.rb
```

Fixtures cover the mixed-marker three-slot case, an unsatisfiable report, and
coordinate attribution ambiguity.
