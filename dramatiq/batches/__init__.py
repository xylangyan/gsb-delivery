# This file is a part of Dramatiq.
#
# Copyright (C) 2017,2018 CLEARTYPE SRL <bogdan@cleartype.io>
#
# Dramatiq is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Dramatiq is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import warnings

from .backend import BatchStateBackend
from .backends import StubBatchStateBackend
from .batch import (
    CANCELLED,
    COMPLETED,
    DEFAULT_BATCH_TTL,
    PENDING,
    TERMINATED,
    TERMINAL_STATES,
    Batch,
)
from .errors import (
    BatchAlreadyExists,
    BatchError,
    BatchNotFound,
    BatchOpen,
    BatchSettled,
    BatchVersionConflict,
    DuplicateObligation,
    ObligationClosed,
    ObligationNotFound,
)

__all__ = [
    "Batch",
    "BatchStateBackend",
    "StubBatchStateBackend",
    "RedisBatchStateBackend",
    "DEFAULT_BATCH_TTL",
    "PENDING",
    "COMPLETED",
    "CANCELLED",
    "TERMINATED",
    "TERMINAL_STATES",
    "BatchError",
    "BatchNotFound",
    "BatchAlreadyExists",
    "BatchSettled",
    "BatchVersionConflict",
    "BatchOpen",
    "ObligationNotFound",
    "ObligationClosed",
    "DuplicateObligation",
]


def __getattr__(name):
    if name == "RedisBatchStateBackend":
        try:
            from .backends.redis import RedisBatchStateBackend

            return RedisBatchStateBackend
        except ModuleNotFoundError as e:
            message = (
                "RedisBatchStateBackend is not available. "
                "Run `pip install dramatiq[redis]` to add support for that backend."
            )
            warnings.warn(message, category=ImportWarning, stacklevel=2)
            raise AttributeError(message) from e

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
