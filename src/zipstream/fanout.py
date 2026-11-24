#!/usr/bin/env python

"""Fan out a single read-once zip source to multiple independent subscribers.

The source members are iterated exactly once. Each subscriber receives its own
valid zip stream (its own local headers, central directory and end-of-central
directory record) even if it only asked for a subset of the members. Completed
entry bytes are retained in a bounded staging area so a slow subscriber can
catch up, and evicted once every remaining subscriber is past them. A
subscriber that falls outside of what staging can still hold is failed
explicitly instead of being allowed to stall the faster ones.
"""

import struct
import threading

from zipstream.ng import ZipStream, ZipStreamInfo, _sanitize_arcname


__all__ = [
    "FanoutError",
    "SubscriberClosed",
    "StagingWindowExpired",
    "SourceIncomplete",
    "ZipFanout",
    "FanoutSubscriber",
]


class FanoutError(Exception):
    """Base class for errors raised by the fanout machinery."""


class SubscriberClosed(FanoutError):
    """Raised when a cancelled/closed subscriber is iterated."""


class StagingWindowExpired(FanoutError):
    """Raised when a subscriber needs bytes that have left the staging window.

    `window_end_offset` is the source-relative offset of the first byte that
    could no longer be retained for this subscriber - everything before it is
    all the subscriber could have resumed from.
    """

    def __init__(self, message, *, window_end_offset=None):
        super().__init__(message)
        self.window_end_offset = window_end_offset


class SourceIncomplete(FanoutError):
    """Raised when a complete-package subscriber loses members to a source failure."""


class _Chunk:
    """A retained piece of an entry's on-the-wire data."""

    __slots__ = ("frame", "data", "offset", "end", "next")

    def __init__(self, frame, data, offset):
        self.frame = frame
        self.data = data
        self.offset = offset
        self.end = offset + len(data)
        self.next = None


class _RetainedEntry:
    """A generated entry. Its chunk list may shrink as bytes get reclaimed."""

    __slots__ = (
        "arcname", "zinfo", "source_index", "start_offset", "size",
        "head", "tail", "retained_bytes", "reclaim_start", "retain",
    )

    def __init__(self, arcname, source_index, start_offset, retain):
        self.arcname = arcname
        self.zinfo = None
        self.source_index = source_index
        self.start_offset = start_offset
        self.size = 0
        self.head = None
        self.tail = None
        self.retained_bytes = 0
        # Source-relative offset up to which bytes have been reclaimed.
        self.reclaim_start = start_offset
        # When False no active subscriber wants this entry - its bytes flow
        # through the source once and are discarded instead of staged.
        self.retain = retain

    def append(self, frame, data):
        chunk = _Chunk(frame, data, self.start_offset + self.size)
        self.size += len(data)
        self.retained_bytes += len(data)
        if self.head is None:
            self.head = chunk
        else:
            self.tail.next = chunk
        self.tail = chunk


def _gen_footer(zinfos, pos, comment):
    """Yield central directory + EOCD bytes for a standalone subscriber zip.

    Reuses the tested `ZipStream` footer machinery with the subscriber's own
    ZipStreamInfo records (whose `header_offset` values are subscriber-local)
    and its own current byte position.
    """
    stream = ZipStream.__new__(ZipStream)
    stream._comment = comment
    stream._filelist = zinfos
    stream._pos = pos
    stream._final = False
    stream._gen_lock = threading.Lock()
    yield from stream._gen_archive_footer()


class FanoutSubscriber:
    """An independent zip view over a shared `.ZipFanout` source.

    Iterating one yields the bytes of a complete, standalone zip file containing
    only the members this subscriber asked for. Each subscriber builds its own
    local headers and central directory with its own offsets, so cancelling one
    subscriber never changes the offsets already published to any other.

    A subscriber is intended to be iterated by one thread at a time.
    """

    def __init__(self, fanout, name, wanted, complete):
        self._fanout = fanout
        self.name = name
        self._wanted = wanted  # set of names still expected, or None for complete
        self._complete = complete

        self._closed = False
        self._exc = None
        self._failed = False
        self._finished = False

        # Entries queued for delivery (_RetainedEntry objects, oldest first).
        self._queue = []
        # Entry currently being delivered and our position inside it.
        self._current = None
        self._current_node = None
        self._entry_bytes_sent = 0

        # Subscriber-local byte position in its own output zip.
        self._pos = 0
        # ZipStreamInfo records already delivered (for the central directory).
        self._delivered = []
        # Wanted names that never made it out of the source.
        self.missing = set()
        self._footer_node = None

    def __iter__(self):
        return self

    def __next__(self):
        fanout = self._fanout
        with fanout._cond:
            while True:
                if self._exc is not None:
                    exc = self._exc
                    self._exc = None
                    raise exc
                if self._closed:
                    raise SubscriberClosed(
                        "Subscriber {!r} has been closed".format(self.name)
                    )

                chunk = self._next_chunk_locked()
                if chunk is not None:
                    return chunk

                if self._finished:
                    raise StopIteration
                fanout._cond.wait()

    def _next_chunk_locked(self):
        # Called with the fanout condition held.
        if self._footer_node is not None:
            try:
                data = next(self._footer_node)
            except StopIteration:
                self._footer_node = None
                self._finished = True
                self._fanout._cond.notify_all()
                raise StopIteration
            self._pos += len(data)
            return data

        if self._current_node is not None:
            retained = self._current
            # Staging may have been reclaimed past where this reader is. The
            # held chunk references keep the bytes alive, but the contract is
            # to report the end of the resumable window instead of sneaking
            # the reclaimed bytes through.
            position = retained.start_offset + self._entry_bytes_sent
            if position < retained.reclaim_start:
                self._fail(StagingWindowExpired(
                    "Subscriber {!r} needed bytes of {!r} starting at offset "
                    "{} but staging was reclaimed up to {}; its resumable "
                    "window has ended".format(
                        self.name, retained.arcname, position,
                        retained.reclaim_start
                    ),
                    window_end_offset=retained.reclaim_start,
                ))
                return None

            node = self._current_node
            data = node.data
            self._current_node = node.next

            if node.frame == "header":
                # Replace the source header with one carrying this subscriber's
                # own local offset. Sizes/CRC still come from the descriptor.
                data = self._rebuild_header(retained, data)

            self._pos += len(data)
            self._entry_bytes_sent += len(data)

            if self._current_node is None:
                self._finish_entry_locked(retained)
            return data

        if self._queue:
            retained = self._queue.pop(0)
            if retained.head is None or retained.head.frame != "header":
                # Staging was reclaimed past the start of this entry while it
                # sat queued - the resumable window has ended for this reader.
                self._fail(StagingWindowExpired(
                    "Subscriber {!r} missed the start of member {!r}; its "
                    "resumable window ended at source offset {}".format(
                        self.name, retained.arcname, retained.reclaim_start
                    ),
                    window_end_offset=retained.reclaim_start,
                ))
                return None
            self._current = retained
            self._current_node = retained.head
            self._entry_bytes_sent = 0
            return self._next_chunk_locked()

        return None

    def _rebuild_header(self, retained, source_header):
        src = retained.zinfo
        zinfo = ZipStreamInfo(retained.arcname, date_time=src.date_time)
        zinfo.CRC = src.CRC
        zinfo.compress_size = src.compress_size
        zinfo.file_size = src.file_size
        zinfo.compress_type = src.compress_type
        zinfo.extract_version = src.extract_version
        zinfo.create_version = src.create_version
        zinfo.create_system = src.create_system
        zinfo.external_attr = src.external_attr
        zinfo.internal_attr = src.internal_attr
        zinfo.flag_bits = src.flag_bits
        zinfo.extra = src.extra
        zinfo.comment = src.comment
        if hasattr(src, "_compresslevel"):
            zinfo._compresslevel = src._compresslevel
        zinfo.header_offset = self._pos

        # The source used zip64 exactly when its header size fields are the
        # 0xFFFFFFFF markers. Rebuilding with the same flag keeps the header
        # (and thus every retained offset that follows) byte-identical in size.
        zip64 = struct.unpack_from("<I", source_header, 18)[0] == 0xFFFFFFFF
        header = zinfo.FileHeader(zip64=zip64)
        if len(header) != len(source_header):  # pragma: no cover
            # Same library, same fields - the sizes must match. Keep the stream
            # structurally valid even if that assumption ever breaks.
            return source_header
        return header

    def _finish_entry_locked(self, retained):
        self._current = None
        self._current_node = None
        zinfo = self._local_zinfo(retained)
        self._delivered.append(zinfo)
        self._fanout._on_subscriber_progress()

        if self._complete:
            if (
                not self._queue
                and self._fanout._source_done
                and self._fanout._source_exc is None
            ):
                self._begin_footer_locked()
            return
        self._wanted.discard(retained.arcname)
        if not self._wanted and not self._queue:
            # Everything asked for has been delivered - this zip can finalize
            # even if the source still has more members to produce.
            self._begin_footer_locked()

    def _local_zinfo(self, retained):
        src = retained.zinfo
        zinfo = ZipStreamInfo(retained.arcname, date_time=src.date_time)
        zinfo.CRC = src.CRC
        zinfo.compress_size = src.compress_size
        zinfo.file_size = src.file_size
        zinfo.compress_type = src.compress_type
        zinfo.extract_version = src.extract_version
        zinfo.create_version = src.create_version
        zinfo.create_system = src.create_system
        zinfo.external_attr = src.external_attr
        zinfo.internal_attr = src.internal_attr
        zinfo.flag_bits = src.flag_bits
        zinfo.extra = src.extra
        zinfo.comment = src.comment
        if hasattr(src, "_compresslevel"):
            zinfo._compresslevel = src._compresslevel
        zinfo.header_offset = self._pos - retained.size
        return zinfo

    def _begin_footer_locked(self):
        if self._footer_node is not None or self._finished:
            return
        self._footer_node = iter(_gen_footer(self._delivered, self._pos,
                                             self._fanout._comment))

    def _unfulfilled_locked(self):
        """Wanted names with no entry delivered, queued, or in progress."""
        if self._wanted is None:
            return set()
        accounted = {z.filename for z in self._delivered}
        accounted.update(r.arcname for r in self._queue)
        if self._current is not None:
            accounted.add(self._current.arcname)
        return self._wanted - accounted

    def _gate_offset(self, source_pos):
        """Source-relative offset of the next byte this subscriber needs.

        Bytes at or below the returned offset are no longer needed by this
        subscriber and may be reclaimed.
        """
        if self._closed or self._failed or self._finished:
            return None
        if self._current is not None:
            return self._current.start_offset + self._entry_bytes_sent
        if self._queue:
            return self._queue[0].start_offset
        # Waiting for members the source has not produced yet - nothing already
        # produced is held back by this subscriber.
        return source_pos

    def _fail(self, exc):
        # Called with the fanout condition held.
        if self._closed or self._failed or self._finished:
            return
        self._exc = exc
        self._failed = True
        self._queue.clear()
        self._current = None
        self._current_node = None

    def close(self):
        """Cancel this subscriber.

        A closed subscriber stops holding staging space and can no longer be
        iterated. Closing one subscriber never affects any other and never
        re-opens the shared source iterator.
        """
        self._fanout._close_subscriber(self)

    @property
    def closed(self):
        with self._fanout._cond:
            return self._closed

    @property
    def complete(self):
        """True if this subscriber was promised every member of the source."""
        return self._complete

    def is_finished(self):
        """True once the full zip (including its central directory) was delivered."""
        with self._fanout._cond:
            return self._finished


class ZipFanout:
    """Split one read-once zip source into many independent zip streams.

    Members of the source `.ZipStream` are generated exactly once, in order. A
    subscriber created with ``complete=True`` is promised every member of the
    source; other subscribers only receive the member names they asked for, but
    still get a standalone zip with a correct central directory of its own.

    `staging_limit` is the fixed byte budget retained across all entries so
    slow subscribers can catch up. When the budget is full the pump evicts
    bytes every remaining subscriber is past; if the slowest subscriber blocks
    that eviction it is failed with `.StagingWindowExpired` so the others can
    keep moving instead of stalling behind it.
    """

    def __init__(self, source, *, staging_limit=1024 * 1024, comment=b""):
        if not isinstance(source, ZipStream):
            raise TypeError("source must be a ZipStream")
        if staging_limit < 0:
            raise ValueError("staging_limit must not be negative")

        self._source = source
        self._staging_limit = staging_limit
        self._comment = self._normalize_comment(comment)

        self._cond = threading.Condition()
        self._subscribers = []
        self._entries = []
        self._retained_total = 0

        # Source-relative byte position and entry index of the pump.
        self._source_pos = 0
        self._source_index = 0
        self._source_exc = None
        self._source_done = False
        self._pump_thread = None

    @staticmethod
    def _normalize_comment(comment):
        if comment is None:
            return b""
        if isinstance(comment, str):
            return comment.encode("utf-8")
        if isinstance(comment, bytearray):
            return bytes(comment)
        if not isinstance(comment, bytes):
            raise TypeError("comment must be bytes or str")
        return comment

    @property
    def comment(self):
        return self._comment

    @property
    def source_error(self):
        """The exception that stopped the source, or None if it is intact."""
        with self._cond:
            return self._source_exc

    def subscribe(self, name=None, *, members=None, complete=False):
        """Add a subscriber and start delivering entries to it.

        `members`: member names this subscriber wants. Names the source has not
        produced yet arrive when they do; names already passed are served from
        staging if still retained, otherwise iteration raises
        `.StagingWindowExpired`.

        `complete`: when True the subscriber must receive every source member.
        If the source fails early, iteration raises `.SourceIncomplete` rather
        than delivering a zip that silently misses files.
        """
        if not complete:
            if members is None:
                raise ValueError(
                    "Either specify members or subscribe with complete=True"
                )
            wanted = set()
            for member in members:
                member = _sanitize_arcname(member)
                if not member:
                    raise ValueError("A valid member name is required")
                wanted.add(member)
            if not wanted:
                raise ValueError("At least one member must be requested")
        else:
            if members is not None:
                raise ValueError("A complete subscriber cannot filter members")
            wanted = None

        with self._cond:
            sub = FanoutSubscriber(self, name, wanted, complete)

            for retained in self._entries:
                takes = complete or retained.arcname in sub._wanted
                if not takes:
                    continue
                if retained.head is None or retained.head.frame != "header":
                    # The entry (or just its start) has already been reclaimed -
                    # the subscriber cannot rebuild a valid local header for it.
                    sub._fail(StagingWindowExpired(
                        "Member {!r} is already outside the staging window "
                        "for subscriber {!r}".format(retained.arcname, name),
                        window_end_offset=retained.reclaim_start
                        if retained.head is None else retained.head.offset,
                    ))
                    break
                self._offer_locked(sub, retained)

            if self._source_done and self._source_exc is not None and (
                complete or self._source_index == 0
            ):
                sub._fail(SourceIncomplete(
                    "Source failed before complete subscriber {!r} could be "
                    "served: {}".format(name, self._source_exc)
                ))
            elif self._source_done and not sub._failed:
                # No more members can ever arrive. Selective subscribers record
                # anything unfulfilled as missing; anyone with an empty queue
                # can finalize immediately.
                if not complete:
                    unfulfilled = sub._unfulfilled_locked()
                    sub.missing.update(unfulfilled)
                    sub._wanted.difference_update(unfulfilled)
                if not sub._queue:
                    if (
                        self._source_exc is not None
                        and not sub._delivered
                    ):
                        sub._fail(SourceIncomplete(
                            "Source failed before any requested member could "
                            "be completely delivered to {!r}: {}".format(
                                name, self._source_exc
                            )
                        ))
                    else:
                        sub._begin_footer_locked()

            self._subscribers.append(sub)
            self._cond.notify_all()
        return sub

    def run(self):
        """Generate the whole source on the calling thread."""
        with self._cond:
            if self._pump_thread is not None:
                raise RuntimeError("ZipFanout is already pumping in a thread")
        self._pump()

    def run_threaded(self):
        """Start pumping the source on a background daemon thread."""
        with self._cond:
            if self._pump_thread is not None:
                return
            thread = threading.Thread(
                target=self._pump, name="ZipFanout-pump", daemon=True
            )
            self._pump_thread = thread
        thread.start()

    def _pump(self):
        try:
            self._pump_entries()
            exc = None
        except BaseException as source_exc:  # propagate to promised subscribers
            exc = source_exc

        with self._cond:
            self._source_exc = exc
            self._source_done = True
            if exc is not None and self._source_index == 0:
                # No entry ever completed - there is nothing valid to hand out,
                # so no subscriber may succeed (no silent empty "preview").
                for sub in self._subscribers:
                    sub._fail(SourceIncomplete(
                        "Source failed before producing a complete entry; no "
                        "zip can be delivered to {!r}: {}".format(sub.name, exc)
                    ))
            elif exc is not None:
                for sub in self._subscribers:
                    if sub.complete and not sub._closed:
                        sub._fail(SourceIncomplete(
                            "Source failed after {} complete entries; the "
                            "complete package promised to {!r} cannot be "
                            "delivered: {}".format(
                                self._source_index, sub.name, exc
                            )
                        ))
            for sub in self._subscribers:
                if sub._closed or sub._failed:
                    continue
                if not sub.complete:
                    # Anything still wanted with no entry in flight never made
                    # it out of the source.
                    unfulfilled = sub._unfulfilled_locked()
                    sub.missing.update(unfulfilled)
                    sub._wanted.difference_update(unfulfilled)
                    if sub._current is None and not sub._queue:
                        if exc is not None and not sub._delivered:
                            # The source failed before any requested member
                            # could be completely delivered - an empty zip
                            # would be an invalid "preview" of a failed source.
                            sub._fail(SourceIncomplete(
                                "Source failed before any requested member "
                                "could be completely delivered to {!r}: "
                                "{}".format(sub.name, exc)
                            ))
                        else:
                            sub._begin_footer_locked()
                elif exc is None and sub._current is None and not sub._queue:
                    sub._begin_footer_locked()
            self._cond.notify_all()

    def _pump_entries(self):
        while True:
            retained = None
            saw_frame = False

            for frame, arcname, payload in self._source.entry_frames():
                saw_frame = True
                if frame == "end":
                    with self._cond:
                        retained.zinfo = payload
                        if retained.retain:
                            self._publish_locked(retained)
                        self._source_index += 1
                        self._cond.notify_all()
                    continue

                with self._cond:
                    if retained is None:
                        retained = self._start_entry_locked(arcname)
                    self._source_pos += len(payload)
                    if retained.retain:
                        self._make_room_locked(len(payload))
                        retained.append(frame, payload)
                        self._retained_total += len(payload)
                    self._cond.notify_all()

            if not saw_frame:
                return

    def _start_entry_locked(self, arcname):
        wants = any(
            not sub._closed
            and not sub._failed
            and (sub.complete or arcname in sub._wanted)
            for sub in self._subscribers
        )
        retained = _RetainedEntry(
            arcname, self._source_index, self._source_pos, wants
        )
        self._entries.append(retained)
        return retained

    def _make_room_locked(self, incoming):
        """Enforce the staging budget, evicting and failing stragglers."""
        while self._retained_total + incoming > self._staging_limit:
            if self._reclaim_locked():
                continue

            active = [s for s in self._subscribers
                      if not s._closed and not s._failed and not s._finished]
            if not active:
                # Nobody left to protect - retain what we need and let the
                # entries be reclaimed as soon as a new subscriber moves.
                return

            # The slowest subscriber holds the oldest bytes. Fail it explicitly
            # so it cannot stall the rest of the fanout.
            gates = [(s._gate_offset(self._source_pos), s) for s in active]
            gates.sort(key=lambda item: item[0])
            gate, victim = gates[0]
            victim._fail(StagingWindowExpired(
                "Subscriber {!r} fell outside the staging window; its "
                "resumable window ended at source offset {}".format(
                    victim.name, gate
                ),
                window_end_offset=gate,
            ))
            self._cond.notify_all()

    def _publish_locked(self, retained):
        for sub in self._subscribers:
            if sub._closed or sub._failed:
                continue
            if sub.complete or retained.arcname in sub._wanted:
                self._offer_locked(sub, retained)

    def _offer_locked(self, sub, retained):
        sub._queue.append(retained)
        self._cond.notify_all()

    def _on_subscriber_progress(self):
        # Called with the condition held whenever a subscriber advances.
        self._reclaim_locked()
        self._cond.notify_all()

    def _reclaim_locked(self):
        """Drop retained bytes no active subscriber can need anymore."""
        active = [s for s in self._subscribers
                  if not s._closed and not s._failed and not s._finished]
        if active:
            gate = min(s._gate_offset(self._source_pos) for s in active)
        else:
            gate = self._source_pos

        freed = False
        for retained in self._entries:
            if retained.zinfo is None:
                # The pump is still writing this entry and nobody has been
                # offered it yet - its bytes cannot be reclaimed.
                continue
            while retained.head is not None and retained.head.end <= gate:
                chunk = retained.head
                retained.head = chunk.next
                retained.retained_bytes -= len(chunk.data)
                self._retained_total -= len(chunk.data)
                retained.reclaim_start = chunk.end
                freed = True
            if retained.head is None:
                retained.tail = None
        return freed

    def _close_subscriber(self, sub):
        with self._cond:
            if sub._closed:
                return
            sub._closed = True
            sub._queue.clear()
            sub._current = None
            sub._current_node = None
            self._reclaim_locked()
            self._cond.notify_all()

    def wait_until_idle(self, timeout=None):
        """Wait for source exhaustion and every subscriber to finish or fail."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._source_done and all(
                    s._closed or s._finished or s._failed
                    for s in self._subscribers
                ),
                timeout=timeout,
            )
