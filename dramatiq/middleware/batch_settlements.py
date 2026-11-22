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

from typing import TYPE_CHECKING, Any

from ..batches import DEFAULT_BATCH_TTL, Batch
from ..batches.backend import BatchStateBackend
from .middleware import Middleware

if TYPE_CHECKING:
    from ..message import Message

#: Message option carrying the id of the batch a message belongs to.
BATCH_ID_OPTION = "batch_id"

#: Message option carrying the obligation key a message fulfills.
BATCH_OBLIGATION_OPTION = "batch_obligation_key"


def stamp_message(message: "Message", batch_id: str, obligation_key: str) -> "Message":
    """Stamp a message with the batch and obligation it fulfills.

    A stamped message completes its obligation when it finishes
    successfully under the :class:`BatchSettlements` middleware.  A
    skipped or failed message never completes the obligation.

    Parameters:
      message(Message): The message to stamp.
      batch_id(str): The id of the owning batch.
      obligation_key(str): The key of the obligation being fulfilled.
    """
    return message.copy(
        options={
            BATCH_ID_OPTION: batch_id,
            BATCH_OBLIGATION_OPTION: obligation_key,
        }
    )


class BatchSettlements(Middleware):
    """Optional middleware that drives :class:`Batch` settlement from
    message processing.

    Messages stamped with :func:`stamp_message` report back to their
    batch when they finish successfully.  An actor may return a dict
    with any of these keys:

    - ``range``: the page range the obligation completed with.
    - ``evidence``: arbitrary completion evidence.
    - ``discovered``: a list of ``{"key", "site", "message", "detail"}``
      dicts for branches the page discovered.  Each branch is accepted
      into the batch atomically at submission time and, when it carries
      a ``message``, that message is enqueued already stamped for the
      new obligation.  Branches stay invisible to settlement until they
      are accepted here.

    A successful result for an obligation that was cancelled or
    terminated is registered as late material and never reopens the
    obligation.  Once every accepted obligation has completion,
    cancellation or termination evidence, the batch settles exactly
    once and its settlement callback messages are enqueued.  Regular
    |GroupCallbacks| are unaffected and continue to work alongside this
    middleware.

    Parameters:
      backend(BatchStateBackend): The backend storing batch state.
      ttl(int): The TTL applied to batch writes, in milliseconds.
    """

    def __init__(self, backend: BatchStateBackend, *, ttl: int = DEFAULT_BATCH_TTL) -> None:
        self.backend = backend
        self.ttl = ttl

    def get_batch(self, batch_id: str) -> Batch:
        """Return the :class:`Batch` handle for ``batch_id``."""
        return Batch(batch_id, backend=self.backend, ttl=self.ttl)

    def after_process_message(self, broker, message, *, result=None, exception=None):
        if exception is not None:
            return

        batch_id = message.options.get(BATCH_ID_OPTION)
        if not batch_id:
            return

        obligation_key = message.options.get(BATCH_OBLIGATION_OPTION) or message.message_id
        batch = self.get_batch(batch_id)
        payload = result if isinstance(result, dict) else {}
        page_range = payload.get("range")
        evidence = payload.get("evidence")
        discovered = payload.get("discovered")

        if discovered is not None:
            new_obligations = batch.submit_page(
                obligation_key,
                range=page_range,
                evidence=evidence,
                discovered=discovered,
            )
        else:
            batch.complete_obligation(obligation_key, range=page_range, evidence=evidence)
            new_obligations = []

        for obligation in new_obligations:
            new_message = obligation.get("message")
            if new_message is not None:
                from ..message import Message

                broker.enqueue(stamp_message(Message(**new_message), batch_id, obligation["key"]))

        settlement = batch.try_settle()
        if settlement is not None:
            self._dispatch_settlement_callbacks(broker, batch, settlement)

    def _dispatch_settlement_callbacks(self, broker, batch: Batch, settlement: dict[str, Any]) -> None:
        from ..message import Message

        for callback in batch.state["settlement_callbacks"]:
            broker.enqueue(Message(**callback))
