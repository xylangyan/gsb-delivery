"""Randomised differential tests against a naive material model."""

from decimal import Decimal

import pytest

from gcodeparser.reconcile import Handover, MaterialLedger, PrintProgram, Repeat, Snippet


class Naive:
    """Deliberately simple command-at-a-time model for cross-checking."""

    def __init__(self, tool, mode, spools):
        self.tool = tool
        self.mode = mode
        self.spools = dict(spools)
        self.e = {}
        self.debt = {spool: [] for spool in spools.values()}
        self.new = []
        self.payments = []

    def apply(self, command, params):
        if command == ("M", 82):
            self.mode = "absolute"
        elif command == ("M", 83):
            self.mode = "relative"
        elif command == ("G", 92):
            if "E" in params:
                self.e[self.tool] = Decimal(str(params["E"]))
        elif command[0] == "T":
            self.tool = command[1]
        elif command[0] == "G" and command[1] in (0, 1, 2, 3) and "E" in params:
            value = Decimal(str(params["E"]))
            if self.mode == "absolute":
                previous = self.e.get(self.tool, Decimal(0))
                delta = value - previous
                self.e[self.tool] = value
            else:
                delta = value
                self.e[self.tool] = self.e.get(self.tool, Decimal(0)) + value
            spool = self.spools[self.tool]
            if delta < 0:
                self.debt.setdefault(spool, []).append(-delta)
            elif delta > 0:
                remaining = delta
                queue = self.debt.setdefault(spool, [])
                while remaining > 0 and queue:
                    pay = min(remaining, queue[0])
                    queue[0] -= pay
                    remaining -= pay
                    self.payments.append(pay)
                    if queue[0] == 0:
                        queue.pop(0)
                if remaining > 0:
                    self.new.append((spool, remaining))

    def handover(self, tool, spool):
        self.spools[tool] = spool
        self.debt.setdefault(spool, [])

    def usage(self):
        result = {}
        for spool, amount in self.new:
            result[spool] = result.get(spool, Decimal(0)) + amount
        return result


def naive_run(program, tool, mode, spools):
    naive = Naive(tool, mode, spools)
    naive.debt = {}
    ordinal = 0
    for kind, payload in program.iter_actions():
        if kind == "handover":
            naive.handover(payload.tool, payload.spool)
            continue
        node, _, _ = payload
        naive.apply(node.line.command, node.line.params)
        ordinal += 1
    return naive, ordinal


def make_program(seed: int) -> PrintProgram:
    import random

    rng = random.Random(seed)
    snippets = {}
    snippets["leaf"] = Snippet(
        [
            "M83",
            f"G1 E0.{rng.randint(1, 9)}",
            f"G1 E-0.{rng.randint(1, 4)}",
            f"G1 E0.{rng.randint(1, 9)}",
        ],
        name="leaf",
    )
    snippets["mid"] = Snippet(
        [
            Repeat("leaf", rng.randint(0, 3)),
            f"G1 E0.{rng.randint(1, 5)}",
            Repeat("leaf", rng.randint(0, 2)),
        ],
        name="mid",
    )
    snippets["top"] = Snippet(
        [
            "M83",
            Repeat("mid", rng.randint(0, 4)),
            Handover(0, "B"),
            Repeat("leaf", rng.randint(0, 3)),
        ],
        name="top",
    )
    return PrintProgram(Repeat("top", rng.randint(1, 3)), snippets=snippets)


@pytest.mark.parametrize("seed", range(20))
def test_random_programs_match_naive(seed):
    program = make_program(seed)
    spools = {0: "A", 1: "A"}
    naive, ordinal = naive_run(program, 0, "relative", spools)
    led = MaterialLedger(program, initial_spools=spools, mode="relative")
    assert led.command_count == ordinal
    assert led.usage() == naive.usage()

    # Every single-command window must also agree with command-by-command replay.
    for position in range(led.command_count):
        window = led.usage(position, position)
        # Rebuild a fresh ledger and replay only to this point to isolate the command.
        prefix = MaterialLedger(program, initial_spools=spools, mode="relative")
        before = prefix.usage(0, position - 1) if position else {}
        through = prefix.usage(0, position)
        delta = {
            spool: through.get(spool, Decimal(0)) - before.get(spool, Decimal(0))
            for spool in set(through) | set(before)
        }
        delta = {spool: amount for spool, amount in delta.items() if amount != 0}
        assert window == delta, (seed, position)


@pytest.mark.parametrize("seed", range(10))
def test_random_windows_match_prefix_differences(seed):
    program = make_program(seed)
    led = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")
    total = led.command_count
    import random

    rng = random.Random(seed + 1000)
    for _ in range(15):
        start = rng.randrange(total)
        end = rng.randrange(start, total)
        window = led.usage(start, end)
        through_end = led.usage(0, end)
        before = led.usage(0, start - 1) if start else {}
        expected = {
            spool: through_end.get(spool, Decimal(0)) - before.get(spool, Decimal(0))
            for spool in set(through_end) | set(before)
        }
        expected = {spool: amount for spool, amount in expected.items() if amount != 0}
        assert window == expected
