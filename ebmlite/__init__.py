"""
EBMLite: A lightweight EBML parsing library. It is designed to crawl through
EBML files quickly and efficiently, and that's about it.
"""

from .core import *
from .core import SCHEMA_PATH, SCHEMATA, __all__

name = "ebmlite"
__version__ = "3.4.1"

from .archive import *  # noqa: F401,F403
from .archive import __all__ as _archive_all
__all__ = list(__all__) + [n for n in _archive_all if n not in __all__]
