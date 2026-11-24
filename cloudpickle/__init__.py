from . import cloudpickle
from .cloudpickle import *  # noqa
from .review_session import (
    ArchiveConflict,
    ArchiveRef,
    ArchiveSession,
    HandoffManifest,
    IsolatedUnpickler,
    OrderResult,
    Review,
    ReviewSession,
    loads as isolated_loads,
    open_review,
)

# Preserve plain cloudpickle.loads behavior outside a ReviewSession context,
# while allowing loads() inside `with session:` to join that session.
loads = isolated_loads
ReviewContext = ReviewSession
ArchiveSession = ReviewSession
ReviewBook = ReviewSession
ArchiveConflictError = ArchiveConflict

__doc__ = cloudpickle.__doc__

__version__ = "3.2.0.dev0"

__all__ = [  # noqa
    "__version__",
    "Pickler",
    "CloudPickler",
    "dumps",
    "loads",
    "dump",
    "load",
    "register_pickle_by_value",
    "unregister_pickle_by_value",
    "ReviewSession",
    "Review",
    "ArchiveSession",
    "ArchiveConflict",
    "ArchiveRef",
    "HandoffManifest",
    "IsolatedUnpickler",
    "OrderResult",
    "open_review",
]
