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

import threading
from typing import Any

from ..backend import BatchStateBackend


class StubBatchStateBackend(BatchStateBackend):
    """An in-memory batch state backend.  For use in unit tests."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mutex = threading.RLock()
        self.db: dict[str, dict[str, Any]] = {}

    def read(self, batch_id):
        with self.mutex:
            document = self.db.get(self._key(batch_id))
            if document is None:
                return None
            return self.encoder.decode(self.encoder.encode(document))

    def create(self, batch_id, state, ttl):
        with self.mutex:
            key = self._key(batch_id)
            if key in self.db:
                return False
            self.db[key] = self.encoder.decode(self.encoder.encode(state))
            return True

    def replace_if_version(self, batch_id, expected_version, state, ttl):
        with self.mutex:
            key = self._key(batch_id)
            current = self.db.get(key)
            if current is None or current["version"] != expected_version:
                return False
            self.db[key] = self.encoder.decode(self.encoder.encode(state))
            return True
