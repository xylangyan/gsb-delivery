"""Migration planning for MessagePack extension-code handovers.

Given a handover sheet (移交单) describing every writer's old/final
extension tables, borrowable temporary codes, record templates and
every reader's permitted handler set, this package computes an
executable switch sequence to the final tables — fewest borrowed
codes, then fewest switches — and proves every intermediate
in-flight mix safe at every reader without enumerating the
(potentially huge) field domains.
"""

from .spec import Handover, SpecError, load
from .checks import Collision, Problem, find_key_collision, validate_inflight
from .planner import PlanResult, Step, plan

__all__ = [
    "Handover", "SpecError", "load",
    "Collision", "Problem", "find_key_collision", "validate_inflight",
    "PlanResult", "Step", "plan",
]
