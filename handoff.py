from solution import *  # noqa: F401,F403
import solution as _solution
from solution import HandoffPlanner  # noqa: F401
from solution import plan_handoff  # noqa: F401
from solution import solve_handoff  # noqa: F401


def __getattr__(name):
    return getattr(_solution, name)
