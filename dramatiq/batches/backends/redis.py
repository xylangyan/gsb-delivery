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

import redis

from ..backend import BatchStateBackend


class RedisBatchStateBackend(BatchStateBackend):
    """A Redis-backed batch state backend.

    Optimistic concurrency is implemented with WATCH/MULTI/EXEC, the
    same primitive the rate limiter backends rely on.  Every write
    refreshes the key's TTL.

    Parameters:
      client(Redis): An optional client.  If this is passed,
        then all other parameters are ignored.
      url(str): An optional connection URL.  If both a URL and
        connection parameters are provided, the URL is used.
      **parameters: Connection parameters are passed directly
        to :class:`redis.Redis`.
    """

    def __init__(self, *, client=None, url=None, **kwargs):
        super().__init__(**kwargs)
        if url is not None:
            kwargs["connection_pool"] = redis.ConnectionPool.from_url(url)
        self.client = client or redis.Redis(**kwargs)

    def read(self, batch_id):
        data = self.client.get(self._key(batch_id))
        if data is None:
            return None
        return self.encoder.decode(data)

    def create(self, batch_id, state, ttl):
        return bool(self.client.set(self._key(batch_id), self.encoder.encode(state), px=ttl, nx=True))

    def replace_if_version(self, batch_id, expected_version, state, ttl):
        key = self._key(batch_id)
        payload = self.encoder.encode(state)
        with self.client.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    current = pipe.get(key)
                    if current is None or self.encoder.decode(current)["version"] != expected_version:
                        pipe.unwatch()
                        return False

                    pipe.multi()
                    pipe.set(key, payload, px=ttl)
                    pipe.execute()
                    return True
                except redis.WatchError:
                    continue
