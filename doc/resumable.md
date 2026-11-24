# Resumable jobs

`Kiba::ResumableRunner` is an opt-in runner for jobs which must survive a stop
and continue later without re-delivering work already acknowledged by their
destinations. Jobs which do not opt in keep using `Kiba::StreamingRunner`
unchanged.

```ruby
store = Kiba::Checkpoint::FileStore.new("tmp/nightly.checkpoint")

Kiba.run(job, checkpoint: store)
# later, possibly the next day:
Kiba.run(job, checkpoint: store)
```

A checkpoint can also be declared in the job, in which case `Kiba.run(job)`
selects the resumable runner:

```ruby
Kiba.parse do
  extend Kiba::DSLExtensions::Config
  config :kiba, resumable: true, checkpoint: "tmp/nightly.checkpoint", job_id: "nightly"

  source MyPagedSource, ...
  transform MyBufferingTransform
  destination MyLedger, ...
end
```

`Kiba::ResumableRunner.run(job, checkpoint: store)` and
`Kiba::ResumableRunner.status(job, checkpoint: store)` are the explicit forms.
Passing a checkpoint to `Kiba.run` is enough to select the resumable runner.

## Ordering

The resumable runner preserves the order of `Kiba::StreamingRunner`:

- each row from a source goes through all transforms in order;
- a transform may `yield` several rows and then return one more, all of which
  reach the destinations in that order;
- when sources are exhausted, transforms are closed from the first layer to
  the last, and rows emitted by a `close` continue through the remaining
  transforms.

Rows emitted by an upstream `close` and rows emitted independently by a
downstream `close` are both delivered, even when their values are identical.

## Checkpoint stores

`Kiba::Checkpoint::Store.coerce` accepts:

| Value | Store |
| --- | --- |
| `nil` | in-memory (progress is lost with the process) |
| `Hash` | `HashStore`, updated in place |
| `String` | `FileStore`, written atomically with fsync + rename |
| object responding to `load` / `save` | used as-is |
| object responding to `read` / `write` | wrapped in `IOStore` |

## Component protocol

Every part is optional; components which do not implement it keep working, but
only the parts which do can provide the corresponding guarantee.

### Sources

A resumable source implements `read_page(cursor)` and returns
`Kiba::Checkpoint::Page.new(rows, next_cursor)` (a `[rows, next_cursor]` array
or a hash with `:rows` / `:next_cursor` also works). `nil` as the next cursor
marks the end of the source. Cursors are opaque and only need to be reopenable
for the page being read; once a page is fully acknowledged the checkpoint
advances past it and it is never read again.

### Transforms

A resumable transform implements `snapshot` and `restore(state)`
(`save_state`/`load_state` and `dump_state`/`restore_state` are accepted too).
The snapshot must be Marshal-able and must be enough to reproduce the same
output sequence from the same input. Kiba snapshots all transforms before
starting a page or a close layer and restores them before replaying it.

### Destinations

A destination may implement `write(row, write_id:)` and `landed?(write_id)`.
Both are optional, `id:` and `write_identifier:` are accepted for the keyword.
Destinations which can look up a write id should accept a retry of the same id
with the same content. Kiba probes `landed?` before every write, so a write
which actually landed but was not yet confirmed is not written twice.

## When progress is committed

Progress is committed after every acknowledged destination write:

1. the output and its write id are saved before the first write;
2. each destination acknowledgement is saved as soon as it returns;
3. once all destinations are acknowledged the output is marked done and the
   next output starts.

A stop between step 2 calls leaves the acknowledged destinations done and the
remaining ones pending. On restart Kiba replays the same segment, skips all
done outputs, probes the pending output's write id and writes only what is
still missing.

Close layers are committed one layer at a time, and the destination `close`
calls are recorded individually.

## Status

```ruby
state = Kiba::ResumableRunner.status(job, checkpoint: store)
state.phase                     # :new, :running or :completed
state.completed?
state.segment_kind              # :page or :close
state.segment_id                # e.g. "s0:p2" or "close0"
state.pending?                  # is one output partially acknowledged?
state.pending_layer             # zero-based producing transform layer
state.pending_output_index      # zero-based output index in the segment
state.pending_write_id
state.pending_destinations      # destination indexes not yet confirmed
state.pending_details           # all of the above in one hash
state.source_index
state.source_cursor(index)      # cursor of the page currently in progress
```

The state object is read-only; it never resumes or modifies the job.
