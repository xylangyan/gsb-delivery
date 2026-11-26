(durable)=
# Durable execution

A `StateChart` normally lives in memory. Its configuration, its two event queues and the
work its invocations started are gone when the process exits. `DurableStateChart` records
a **checkpoint** at every safe step boundary, so a new instance can resume at the position
the old one actually reached instead of starting over at the initial state.

Use it when a run spans more than one process — a queued job that may be retried, a
worker that can be restarted — and the side effects it performs must not be repeated.

```{seealso}
For a persisted *configuration* alone, a model that stores the state value is enough
(see {ref}`domain models <models>`). `DurableStateChart` adds the queues, the invocation
generations and the business-write receipts on top of that.
```

## What a checkpoint holds

| Part | Why it is needed |
|---|---|
| **Configuration** | Resume in the states the run was in, not the initial state. |
| **History values** | A `HistoryState` re-entered after the restart restores what it recorded. |
| **Internal queue** | `error.execution`, `done.state.<id>` and raised events that the exit interrupted. |
| **External queue** | Triggers queued earlier that the run had not claimed yet, in their order. |
| **Invocation generations** | Distinguish a live round of a reused invoke id from a retired one. |
| **Effect receipts** | Business writes already accepted by their stable id. |
| **Running flag** | A run that already reached a final state stays completed after the restart. |

A checkpoint is written after each microstep and after each taken trigger — the points
where the machine is between two atomic steps. An actual exit therefore loses at most the
step that was running, never a completed one.

## Checkpoint stores

`CheckpointStore` is the seam to wherever a checkpoint lives. Two implementations ship:

- `MemoryCheckpointStore` — keyed by run id, for a restart simulated in one process (tests,
  notebooks).
- `PickleFileStore(directory)` — one atomically-replaced file per run, for an exit that
  really happens.

Pass one with `checkpoint_store=`, and name the run with `run_id=`. Every machine of the
same run shares the store, the `Journal` of receipts, and the run id.

## Effects with a stable id

A business write gets a stable id. `accept_effect(key, ticket=...)` accepts it **once**.
The first call records the receipt and the ticket; a repeated call does not write again —
it returns the original receipt and ticket. That is what lets an `after` callback that
runs on both the success and the failure path leave the same closing slip on a retry.

```py
>>> from statemachine.durable import DurableStateChart, Journal, MemoryCheckpointStore
>>> from statemachine import State

>>> class Dispatch(DurableStateChart):
...     pending = State(initial=True)
...     sent = State(final=True)
...     dispatch = pending.to(sent, on="write_sample", after="close_slip")
...
...     def __init__(self, warehouse, **kwargs):
...         self.warehouse = warehouse
...         self.slips = []
...         super().__init__(**kwargs)
...
...     def write_sample(self, key="sample:S-77", ticket="T-77"):
...         created, received = self.warehouse.receive(key, ticket)
...         self.receipt = self.accept_effect(key, ticket=received)
...
...     def close_slip(self, key="slip:S-77", ticket="END-S77"):
...         self.slips.append(self.accept_effect(key, ticket=ticket).ticket)

>>> class Warehouse:
...     def __init__(self):
...         self.received = {}
...     def receive(self, key, ticket):
...         if key in self.received:
...             return False, self.received[key]
...         self.received[key] = ticket
...         return True, ticket

>>> warehouse = Warehouse()
>>> journal = Journal()
>>> store = MemoryCheckpointStore()
>>> sm = Dispatch(warehouse, checkpoint_store=store, journal=journal, run_id="run-1")
>>> sm.send("dispatch")
>>> warehouse.received
{'sample:S-77': 'T-77'}
>>> sm.slips
['END-S77']

```

`receive_effect()` is the same call, also reporting whether this invocation created the
receipt (`(receipt, True)`) or found it already landed (`(receipt, False)`). A receipt
records the generation and the state that produced it, so a write can be traced back to
its call.

## Restoring and resuming

Constructing an instance of a run that has a checkpoint **restores** it: the configuration,
the history, the generations, the receipts and the queues are in place, and the initial
entry is skipped, so entry side effects do not run a second time. `is_restored` tells the
two cases apart.

`resume()` then continues the run. It settles the pending **internal** events — including
the `error.execution` an interrupted action raised — before any queued **external** trigger
is claimed. This follows the original run-to-completion order: a review queued before a
failure cannot advance the run to its next round while the failure, and the writes that
landed before it, are still being handled. The external triggers left stay queued, in their
order, for the next `send()`.

```{seealso}
`settle_internal()` exposes the same internal-only step without the requirement of a
restore, for a caller that wants to drive the loop a macrostep at a time.
```

## Generations: a reused invoke id

A state that is exited and entered again starts a new round of its invocation. When the
invoke id is declared, the id is reused, but the round is not: each activation gets the
next **generation**.

A `done.invoke.<id>` completion carries the generation of the round that produced it.

- A completion from a **retired** round is dropped, so it cannot pass the new round.
- A completion from the **live** round is taken normally.
- `send_to_child(invoke_id, event)` routes to the round that is live now, never to a
  retired one.

The generation survives a checkpoint: a handler cannot be resumed across a process exit,
but its id and its generation can, so a completion delivered after the restart is still
attributed to the round that asked for it. `live_rounds()` reports the generation of each
id that is live now.

```{note}
A declared invoke id names its own completion event. The `done_invoke_<state>` naming
convention matches the `done.invoke.<state>` family, so pair it with
`Event(..., id="done.invoke.<declared-id>")` when you declare an id.
```

## Halt at a step boundary

`halt()` asks the engine to stop at the next step boundary with both queues intact, and
records the checkpoint there. It is the deliberate counterpart of an exit: the microstep
that was running has finished, and the triggers it queued are still pending.

## Guarantees

- **A plain `StateChart` is unchanged.** The engine's step-boundary hook is a no-op on any
  instance that does not opt in; no checkpoint is stored and no generation is assigned to an
  event sent by user code.
- **Restoring does not re-run the initial entry.** The configuration is written directly.
- **Internal events precede external ones after a resume.** The failure a restart
  interrupted is handled before an earlier-queued external trigger can advance the run.
- **An effect lands once.** The stable id, not the callback's retry, decides.

```{warning}
A checkpoint records the configuration, the queues, the generations and the receipts — not
the Python state of the machine or its model. Recreate the model (or reload it from your
own storage) before constructing the resumed instance, and let its `state` field start
empty so the checkpoint's configuration is what the run carries.
```
