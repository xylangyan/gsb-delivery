# -*- coding: utf-8 -*-
from .archive import *  # noqa: F401,F403
from .archive import query_archive, query_segment_archive

query = query_segment_archive

__all__ = ["query_archive", "query_segment_archive", "query"]
