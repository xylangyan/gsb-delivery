# -*- coding: utf-8 -*-
from .archive import *  # noqa: F401,F403
from .archive import query_archive

query = query_archive

__all__ = ["query_archive", "query"]
