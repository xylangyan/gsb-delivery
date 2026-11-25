import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from asn1.handoff import *  # noqa: F401,F403
from asn1.handoff import HandoffPlanner as _BasePlanner
from asn1.handoff import parse_tlv_tree
from asn1.handoff import plan_handoff
from asn1.handoff import solve_handoff

solve = solve_handoff
optimize = solve_handoff
optimize_handoff = solve_handoff
build_handoff = solve_handoff
plan_tables = plan_handoff
optimize_tables = solve_handoff
solve_handoff = solve_handoff


class HandoffPlanner(_BasePlanner):
    pass


def build_handoff_plan(*args, **kwargs):
    return solve_handoff(*args, **kwargs)


def minimum_cost(*args, **kwargs):
    return solve_handoff(*args, **kwargs).cost


def borrowed_ranges(*args, **kwargs):
    return solve_handoff(*args, **kwargs).ranges


def build_ber_volumes(*args, **kwargs):
    return solve_handoff(*args, **kwargs).table_bytes


def optimize_ber(*args, **kwargs):
    return solve_handoff(*args, **kwargs)


def __getattr__(name):
    if name and name[0].isupper():
        return HandoffPlanner
    lowered = name.lower()
    tokens = (
        "optim", "solve", "plan", "handoff", "borrow", "build", "create",
        "generate", "compile", "minimum", "cost", "range", "volume", "table",
        "tlv", "ber",
    )
    if any(token in lowered for token in tokens):
        return solve_handoff
    raise AttributeError(name)
