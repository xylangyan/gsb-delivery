# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Extract time windows from rosbag2 recordings.

The extraction keeps message payloads untouched and rewrites the type
definitions where same-named types differ between source recordings. Each
source connection is resolved against its own definition, so identical payload
bytes deserialize with the definition that belonged to the connection that
produced them.

"""

from .definitions import (
    DefinitionError,
    DefinitionRegistry,
    MissingDefinitionError,
    parse_definition,
    resolve_connection,
    resolve_from_candidates,
)
from .extractor import ExtractError, ExtractResult, Origin, extract

__all__ = [
    'DefinitionError',
    'DefinitionRegistry',
    'ExtractError',
    'ExtractResult',
    'MissingDefinitionError',
    'Origin',
    'extract',
    'parse_definition',
    'resolve_connection',
    'resolve_from_candidates',
]
