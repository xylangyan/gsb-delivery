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

from ..errors import DramatiqError


class BatchError(DramatiqError):
    """Base class for batch errors."""


class BatchNotFound(BatchError):
    """Raised when referencing a batch that was never opened."""


class BatchAlreadyExists(BatchError):
    """Raised when opening a batch id that is already in use."""


class BatchSettled(BatchError):
    """Raised when mutating a batch that has already been settled."""


class BatchVersionConflict(BatchError):
    """Raised when a command references a batch version that does not
    match the batch's current version.  Such a stale command must be
    re-evaluated against the latest version instead of being applied.
    """


class BatchOpen(BatchError):
    """Raised when settling a batch that still has unresolved
    obligations.
    """


class ObligationNotFound(BatchError):
    """Raised when resolving or cancelling an obligation key that the
    batch never accepted.
    """


class ObligationClosed(BatchError):
    """Raised when an obligation that already has explicit completion,
    cancellation or termination evidence is driven into another
    terminal state.
    """


class DuplicateObligation(BatchError):
    """Raised when an obligation key is reused for a different site.
    A lost registration acknowledgement must never turn into a second
    obligation.
    """
