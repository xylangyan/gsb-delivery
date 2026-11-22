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

from typing import Any, Callable, Optional


class BatchStateBackend:
    """ABC for batch state storage backends.

    A backend keeps one encoded state document per batch id and
    exposes a compare-and-swap against the document's monotonic
    ``version`` field.  :class:`Batch` performs every mutation through
    :meth:`mutate`, so concurrent registrations, resolutions,
    cancellations and settlements are safe on every supported backend.

    Parameters:
      namespace(str): A string with which to prefix batch keys.
      encoder(Encoder): The encoder used to serialize state documents.
        Defaults to the global dramatiq encoder.
    """

    def __init__(
        self,
        *,
        namespace: str = "dramatiq-batches",
        encoder=None,
    ):
        from ..message import get_encoder

        self.namespace = namespace
        self.encoder = encoder or get_encoder()

    def _key(self, batch_id: str) -> str:
        return f"{self.namespace}:{batch_id}"

    def read(self, batch_id: str) -> Optional[dict[str, Any]]:
        """Return the batch state document or ``None`` if the batch is
        unknown.
        """
        raise NotImplementedError

    def create(self, batch_id: str, state: dict[str, Any], ttl: int) -> bool:
        """Create the batch iff it does not exist yet.

        Returns:
          bool: Whether the document was created.
        """
        raise NotImplementedError

    def replace_if_version(
        self,
        batch_id: str,
        expected_version: int,
        state: dict[str, Any],
        ttl: int,
    ) -> bool:
        """Replace the state document iff its current version equals
        ``expected_version``.

        A failed compare means another client mutated the batch in the
        meantime; the caller must re-read and retry.
        """
        raise NotImplementedError

    def mutate(
        self,
        batch_id: str,
        fn: Callable[[Optional[dict[str, Any]]], dict[str, Any]],
        *,
        ttl: int,
    ) -> dict[str, Any]:
        """Read-modify-write helper that retries ``fn`` until a
        compare-and-swap succeeds.

        ``fn`` receives the current state document (or ``None`` if the
        batch is unknown) and returns the document to persist.  Raising
        from ``fn`` aborts the mutation without writing anything.
        """
        while True:
            state = self.read(batch_id)
            # Capture the version before the callback bumps it; the
            # callback mutates the document in place.
            expected_version = None if state is None else state["version"]
            next_state = fn(state)
            if self.replace_if_version(batch_id, expected_version, next_state, ttl):
                return next_state
