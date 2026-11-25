# Rinda hand-off reconciliation

Replays a recorded reconciliation ledger and answers, for each disputed
receipt: **which original writes could have supplied it**, and whether the
same original write was handed over twice.

The replay follows this repository's `TupleSpace`. It does not rerun a live
TupleSpace, and it never consults `Time.now`: all ordering and lease
arithmetic uses the clock the log confirms.

## Usage

```sh
ruby bin/rinda-reconcile LEDGER.json
ruby bin/rinda-reconcile --summary LEDGER.json   # apply summary lines in order
```

Exit status is `0` when the ledger replays consistently, `2` when it cannot.

## What the replay models

- A `move` pushes to its port **before** deleting the entry and notifying
  `take`. If the push raises, the delete and notification are skipped and the
  entry stays in the bag, so a later take can hand the same entry over again.
  An errored `move` therefore neither proves nor disproves delivery; only a
  registration plus the bag state decides.
- A `write` whose lease is already exhausted at entry never enters the bag.
  A read waiter that is already blocked is still handed the value, so a read
  can observe a value no take could ever remove. Such an observation is a
  *ghost*: it is not evidence of a live take.
- `take`/`move` select the newest live match. Which of several live
  same-value entries a given take selected depends on the bag state at that
  instant, including entries earlier takes removed. The replay therefore does
  not prune an older write merely because a newer one exists; that judgement
  is made by the group analysis below.
- `read` either returns a present live match, or waits once. Two matching
  writes before the waiter re-takes the monitor leave `@found` on the last
  value; a wake-and-reacquire between the writes leaves it on the first.

## Ordering

An edge `a -> b` is forced only by:

- `before` in the ledger,
- confirmed timestamps (`a.finish <= b.start`) across endpoints,
- same-endpoint call order,
- a read that recorded which write it saw,
- a stated receipt `source`, or
- a summary assignment.

Calls inside one `windows` entry are known only to overlap and never get an
edge between them.

## Verdicts

For each group of receipts that share a value (or a registration label):

- **distinct**: the receipts are provably from different original writes.
- **duplicate**: one original write was handed over twice. Mechanically this
  needs the earlier hand-off to have left the entry in place (an errored
  `move`), or the two receipts to be one registration reported twice.
- **ambiguous**: both stories work. The tool prints two complete event
  orders, one per story; it never borrows a local verdict from the other.
- **conflict**: no story works.

Long logs are split into independent groups and analysed separately; only
events inside one waiting group are ever reordered.

## Ledger schema

```json
{
  "clock": "log-monotonic",
  "endpoints": ["src", "dst"],
  "calls": [
    {"id": "w1", "endpoint": "src", "method": "write", "tuple": ["job", 1],
     "lease": 600, "start": 10, "offset": 1, "recv": "SRC-1"},
    {"id": "m1", "endpoint": "dst", "method": "move", "template": ["job", 1],
     "port": "P1", "recv": "R1", "recv_value": ["job", 1],
     "start": 12, "offset": 2, "error": "reply lost"}
  ],
  "windows": [{"events": ["w1", "m1"]}],
  "before": [["w1", "m1"]],
  "read_sources": {"rd1": "w1"},
  "summary": [{"receipt": "R1", "source": "w1"}]
}
```

Call fields: `id`, `endpoint`, `method`, plus `tuple` for `write` and
`template` for the others. `recv` names the registration a write/take/move
produced; `recv_value` is the value the receiver got. `value` is what a
returning `read` observed. `lease` is seconds (or `true` for already
expired); `expires_at` is an absolute log-clock expiry. `error` marks a call
that cut short; `delivered: false` states a move's push did not reach the
port. `start`/`offset` are the confirmed call entry and duration.

`summary` is applied line by line; the first line that makes the ledger
unreplayable is reported with its conflicting evidence.
