from .event import Event
from .state import HistoryState
from .state import HistoryType
from .state import State
from .statemachine import StateChart
from .statemachine import StateMachine
from .statemachine import TModel

__author__ = """Fernando Macedo"""
__email__ = "fgmacedo@gmail.com"
__version__ = "3.2.2"

__all__ = [
    "StateChart",
    "StateMachine",
    "DurableStateChart",
    "State",
    "HistoryState",
    "HistoryType",
    "Event",
    "TModel",
]


def __getattr__(name: str):
    # ``durable`` imports ``StateChart`` from this package's sibling module, so importing it
    # eagerly here would close a cycle.  Resolve it on first use instead.
    if name == "DurableStateChart":
        from .durable import DurableStateChart

        return DurableStateChart
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
