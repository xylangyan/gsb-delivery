# Receipt analysis

`Fluent::Logger::ReceiptAnalysis` answers a 签收单 (signed-off receipt) question:
*given a recorded run of the native `Fluent::Logger::FluentLogger`, which integer
`buffer_limit` values make the sender behave exactly as the receipt requires?*

It replays the sender deterministically, counts what a receiver actually read,
accounts overflow-callback bytes separately, and merges the passing limits into
contiguous integer intervals.

## What the sender does (and the model reproduces)

The simulator mirrors `FluentLogger#write`, `#send_data` and `#close`:

- A `post` appends the packed `[tag, time, record]` frame to `pending`, then
  sends the *entire* pending buffer.
- Reconnect suppression is only consulted while `pending.bytesize <= limit`
  (`FluentLogger#write`). The suppress window is
  `0.5 * 1.5**(retries-1)`, capped at 60s, over a 13-entry error history.
- On a short or failed write the pending buffer is kept intact. The next send
  connects again and writes the *whole* pending, so a receiver may see a message
  twice (once per connection) — every full receipt counts.
- `pending.bytesize > limit` after a failed write hands the whole pending to
  the overflow callback and clears it.
- `close` sends pending without the limit check and hands it to the overflow
  callback on failure.
- The receiver counts a message only when it has parsed the full frame on that
  same connection. Partial frames never count.

A limit passes only if every checkpointed certificate item holds. Because
behaviour changes only at the comparisons above, certification probes a small
number of regions rather than every integer in the range or replaying the long
archive once per limit.

## Archive format

A JSON archive (`Archive.from_h` / `ReceiptAnalysis.load(path)`) has:

| key | meaning |
| --- | --- |
| `tag` | tag prefix applied to every frame |
| `identifier_width` | fixed byte width for generated original numbers (default 12) |
| `messages` | map `number => record` (packed as `[tag, time, record]`); missing numbers get a bootstrap record `{"id": number}` |
| `operations` | ordered list of operations (below) |
| `fixture` | local rule table deciding connection and write outcomes |
| `checkpoints` | named boundaries at which the receipt is read |
| `certificate` | per-checkpoint requirements: count bounds, callback cap, pending cap |

An operation is one of:

```json
{"kind": "post",    "number": "A", "time": 0}
{"kind": "close",   "time": 5}
{"kind": "connect", "connection": 1, "time": 0}
{"kind": "write",   "time": 0, "outcome": "ok"}
{"kind": "disconnect","time": 0}
```

`post` and `close` are the API calls the sender makes; `connect` / `write` /
`disconnect` describe the raw network calls when the archive records them
directly.

### Long archives

A long archive is a short run of a few dozen operations repeated with a fixed
second stride and fresh, fixed-width original numbers. Put a `_repeat` marker in
`operations` where the repeated block belongs:

```json
{
  "operations": [
    {"kind": "post", "number": "000001", "time": 0},
    {"_repeat": {
      "count": 100000000,
      "stride": 1,
      "start_time": 1,
      "first_number": 2,
      "template": [
        {"kind": "post", "time": 0},
        {"kind": "post", "time": 1},
        {"kind": "close", "time": 2}
      ]
    }},
    {"kind": "close", "time": 200000000}
  ]
}
```

`_repeat` expansion is lazy (`RepeatExpander`, an `Enumerator`), so hundreds of
millions of operations are never materialised. Each template `post` without a
`number` takes the next generated number; a template entry may set an explicit
`number` or `"reuse": "<slot>"` to re-send one already used in that iteration.

### Fixture rules

A fixture branches on the documented variables: operation `position` (1-based),
`time` (second-resolution wall clock), the current `pending_size`, and whether a
class of original numbers has crossed a complete-receipt threshold
(`at_least` / `below`). Rules are tried in order; first match wins.

```json
{
  "connect": [
    {"when": {"connection": [1]}, "then": "fail"},
    {"then": "complete"}
  ],
  "write": [
    {"when": {"at_least": [1, ["A"]]}, "then": "complete"},
    {"then": {"outcome": "partial", "bytes": "pending-1"}}
  ]
}
```

Write actions: `"complete"`, `"fail"`, `{"outcome": "partial", "bytes": N}`.
`bytes` is an integer or a relative expression `"pending"` / `"pending-N"`
(the whole pending length minus N). Programmatic fixtures use `Fixture::Callable`
with lambdas receiving a `Snapshot`.

## Checkpoints and certificate

A checkpoint latches on the first operation boundary that matches it: by
`index` (1-based operation position), by `number` (the post of that original),
by `kind`, or by `time`.

```json
{
  "checkpoints": [{"name": "after_c", "number": "C"}],
  "certificate": [
    {
      "checkpoint": "after_c",
      "numbers": {"A": {"min": 1}, "C": {"min": 1}, "B": {"max": 1}},
      "callback_cap": 224,
      "pending_cap": 0
    }
  ]
}
```

## Library use

```ruby
require 'fluent/logger/receipt_analysis'

analysis = Fluent::Logger::ReceiptAnalysis.analyze(archive_hash) # or .load(path)

analysis.run(limit)                 # => RunResult for one limit
analysis.violations(limit)          # => { checkpoint => [violations] }
analysis.passes?(limit)             # => true/false
analysis.certify_intervals(range)   # => Certification of merged intervals
analysis.certify(limits)            # evaluate an explicit shortlist
```

`RunResult` exposes `complete_count(number)`, `occurrences(number)` (connection
and submitting operation per full receipt), `callback_bytes`,
`callback_occurrences(number)`, `pending_occurrences(number)`, and
`checkpoint_state(name)`.

## Command line

```
receipt-analysis certify ARCHIVE --range MIN..MAX [--pilot L,...] [--threshold C,...]
receipt-analysis run     ARCHIVE --limit L [--format json|text]
receipt-analysis query   ARCHIVE --limit L --checkpoint NAME [--number ID]
```

`certify` prints the merged passing intervals. `query` reports, for one limit /
checkpoint / original number, how many times it was received, from which
connections and submissions, which bytes reached the overflow callback, and
whether it is still pending.
