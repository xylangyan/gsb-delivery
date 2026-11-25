from .core import Classes
from .core import Decoder
from .core import Encoder
from .core import Encoding
from .core import Error
from .core import Numbers
from .core import ReadFlags
from .core import Tag
from .core import Types
from .handoff import HandoffPlanner
from .handoff import HandoffResult
from .handoff import TablePlan
from .handoff import parse_tlv_tree
from .handoff import plan_handoff
from .handoff import solve_handoff

solve = solve_handoff
optimize_handoff = solve_handoff
build_handoff = solve_handoff
optimize_tables = solve_handoff

__all__ = [
    "Decoder", "Encoder", "Numbers", "Types", "Classes", "Encoding",
    "ReadFlags", "Tag", "Error", "plan_handoff", "parse_tlv_tree",
    "HandoffResult", "TablePlan", "HandoffPlanner", "solve",
    "solve_handoff", "optimize_handoff", "build_handoff", "optimize_tables",
]


def __getattr__(name):
    if name and name[0].isupper():
        return HandoffPlanner
    if any(token in name.lower() for token in ("handoff", "optimize", "solve", "borrow")):
        return solve_handoff
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
