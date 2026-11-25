from solution import *  # noqa: F401,F403
import solution as _solution


def __getattr__(name):
    return getattr(_solution, name)
