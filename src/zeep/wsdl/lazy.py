"""Lazy, on-demand reading of MTOM/XOP attachments.

A multipart/related MTOM response carries the SOAP envelope as its root part
and the binary payloads as separate parts referenced from the envelope through
``<xop:Include href="cid:..."/>`` nodes.

The classic implementation in :mod:`zeep.wsdl.messages.xop` eagerly inlines
every attachment (base64 encoded) into the XML tree before deserialization, so
selecting a single field still pays the cost of decoding *all* attachments.

The objects in this module keep the XOP references in place and resolve a
binary field only when that field is actually read.  Each :class:`LazyMessagePack`
is scoped to a single response, which guarantees that:

* an attachment referenced multiple times by the same envelope is read from the
  underlying part only once (the bytes are cached and shared);
* two parallel responses can never share data, even when an attachment happens
  to use the same ``Content-ID`` in both messages;
* closing one field or the whole message pack never tears down a stream that
  another field is still reading.
"""

import base64
import threading
from urllib.parse import unquote

from requests.structures import CaseInsensitiveDict

from zeep.exceptions import IncompleteMessage, LazyAttachmentError

XOP_NAMESPACE = "http://www.w3.org/2004/08/xop/include"
XOP_INCLUDE_TAG = "{%s}Include" % XOP_NAMESPACE


def content_id_from_href(href):
    """Return the normalized Content-ID (``"<...>"``) for an XOP href."""
    if href.startswith("cid:"):
        # URL can be encoded, RFC 2392
        href = "<%s>" % unquote(href[4:])
    return href


def find_xop_includes(element):
    """Return the direct ``xop:Include`` children of an XML element."""
    return [child for child in element if child.tag == XOP_INCLUDE_TAG]


def document_uses_xop(document):
    """Return ``True`` when the document contains any ``xop:Include`` node."""
    return next(document.iter(XOP_INCLUDE_TAG), None) is not None


class AttachmentPart:
    """A single multipart part with reference counted, cached bytes.

    The raw bytes are read from the (toolbelt) body part at most once and then
    shared between every :class:`LazyBinary` pointing at the same Content-ID.
    """

    def __init__(self, part, content_id):
        self._part = part
        self.content_id = content_id

        encoding = part.encoding or "utf-8"
        self.headers = CaseInsensitiveDict(
            {
                key.decode(encoding): value.decode(encoding)
                for key, value in part.headers.items()
            }
        )
        self.content_type = self.headers.get("Content-Type")
        self.content_location = self.headers.get("Content-Location")

        self._lock = threading.RLock()
        self._raw = None
        self._loaded = False
        self._disposed = False
        self._references = 0

    def __repr__(self):
        return "<AttachmentPart(content_id=%r, content_type=%r)>" % (
            self.content_id,
            self.content_type,
        )

    def acquire(self):
        with self._lock:
            if self._disposed:
                raise LazyAttachmentError(
                    "Attachment %s is no longer available" % self.content_id
                )
            self._references += 1

    def release(self):
        with self._lock:
            if self._references > 0:
                self._references -= 1
            self._maybe_dispose()

    def dispose(self):
        """Mark the part for disposal; drop bytes once nobody uses it."""
        with self._lock:
            self._disposed = True
            self._maybe_dispose()

    def _maybe_dispose(self):
        if self._disposed and self._references == 0 and self._loaded:
            self._raw = None
            self._part = None

    @property
    def available(self):
        with self._lock:
            return not self._disposed

    def read(self):
        """Return the (decoded) attachment bytes, reading the part once."""
        with self._lock:
            if self._disposed and self._raw is None:
                raise LazyAttachmentError(
                    "Attachment %s is no longer available" % self.content_id
                )
            if not self._loaded:
                self._raw = self._decode(self._part.content)
                self._loaded = True
                self._maybe_dispose()
            return self._raw

    def _decode(self, content):
        transfer_encoding = self.headers.get("Content-Transfer-Encoding")
        if transfer_encoding == "base64":
            return base64.b64decode(content)
        return content


class LazyBinary:
    """Lazy, independently cursor based view on an :class:`AttachmentPart`.

    Multiple ``LazyBinary`` objects may wrap the same part.  The underlying
    bytes are fetched once (and shared), while every view keeps its own read
    cursor so each field can be consumed independently.
    """

    def __init__(self, pack, content_id, part):
        self._pack = pack
        self.content_id = content_id
        self._part = part
        self._position = 0
        self._closed = False
        self._lock = threading.RLock()
        if part is not None:
            part.acquire()

    def __repr__(self):
        return "<LazyBinary(content_id=%r)>" % self.content_id

    @property
    def part(self):
        """The originating :class:`AttachmentPart`, for verification.

        Raises :class:`zeep.exceptions.IncompleteMessage` when the referenced
        Content-ID has no part in the message pack.
        """
        if self._part is None:
            raise IncompleteMessage(
                "No attachment part found for Content-ID %r" % self.content_id
            )
        return self._part

    def _ensure_open(self):
        if self._closed:
            raise LazyAttachmentError(
                "Binary value for %s is closed" % self.content_id
            )

    def _ensure_available(self):
        self._ensure_open()
        if self._part is None:
            raise IncompleteMessage(
                "No attachment part found for Content-ID %r" % self.content_id
            )

    def read(self, size=-1):
        """Read up to ``size`` bytes; ``size=-1`` returns the remaining bytes."""
        with self._lock:
            self._ensure_available()
            data = self._part.read()
            if size is None or size < 0:
                chunk = data[self._position :]
                self._position = len(data)
            else:
                chunk = data[self._position : self._position + size]
                self._position += len(chunk)
            return chunk

    def iter_content(self, chunk_size=8192):
        """Yield the attachment bytes incrementally without re-reading."""
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._part is not None:
                self._part.release()
        self._pack._release_binary(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __len__(self):
        with self._lock:
            self._ensure_available()
            return len(self._part.read())

    def __bytes__(self):
        with self._lock:
            self._ensure_available()
            return self._part.read()

    def __eq__(self, other):
        with self._lock:
            self._ensure_available()
            if isinstance(other, LazyBinary):
                other = bytes(other)
            if isinstance(other, (bytes, bytearray, memoryview)):
                return self._part.read() == bytes(other)
            return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = None


class LazyMessagePack:
    """The lazy counterpart of :class:`zeep.wsdl.attachments.MessagePack`.

    Holds the parts of a single multipart response keyed by their normalized
    Content-ID and hands out :class:`LazyBinary` views on demand.
    """

    def __init__(self, parts, root=None):
        self._lock = threading.RLock()
        self._parts = {}
        self._duplicates = {}
        self._binaries = []
        self._closed = False
        self._root = root

        for part in parts:
            content_id = self._part_content_id(part)
            attachment = AttachmentPart(part, content_id)
            if content_id in self._parts:
                existing = self._parts[content_id]
                if existing.read() != attachment.read():
                    raise LazyAttachmentError(
                        "Conflicting parts with the same Content-ID %r" % content_id
                    )
                # A byte-identical duplicate is harmless; remember it but keep
                # serving the canonical (first) part.
                self._duplicates.setdefault(content_id, []).append(attachment)
                continue
            self._parts[content_id] = attachment

    @staticmethod
    def _part_content_id(part):
        encoding = part.encoding or "utf-8"
        headers = CaseInsensitiveDict(
            {
                key.decode(encoding): value.decode(encoding)
                for key, value in part.headers.items()
            }
        )
        return headers.get("Content-ID")

    def __repr__(self):
        return "<LazyMessagePack(content_ids=%r)>" % list(self._parts)

    @property
    def root(self):
        return self._root

    def _set_root(self, root):
        self._root = root

    @property
    def content_ids(self):
        return list(self._parts)

    def attachment_for(self, content_id):
        """Return the :class:`AttachmentPart` for a normalized Content-ID."""
        try:
            return self._parts[content_id]
        except KeyError:
            raise IncompleteMessage(
                "No attachment part found for Content-ID %r" % content_id
            )

    def open(self, href):
        """Resolve an XOP ``href`` to a fresh :class:`LazyBinary` view.

        A missing part does not raise here: the returned view raises
        :class:`zeep.exceptions.IncompleteMessage` only when it is read, so the
        rest of the envelope can still be deserialized and small fields remain
        usable.
        """
        content_id = content_id_from_href(href)
        with self._lock:
            if self._closed:
                raise LazyAttachmentError("Message pack is closed")
            part = self._parts.get(content_id)
            binary = LazyBinary(self, content_id, part)
            self._binaries.append(binary)
            return binary

    def _release_binary(self, binary):
        with self._lock:
            try:
                self._binaries.remove(binary)
            except ValueError:
                pass
            if self._closed and not self._binaries:
                self._dispose_parts()

    def close(self):
        """Close the pack.

        Binaries which are still open keep their shared bytes alive; the rest is
        released immediately.
        """
        with self._lock:
            self._closed = True
            if not self._binaries:
                self._dispose_parts()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _dispose_parts(self):
        for part in self._parts.values():
            part.dispose()
        for duplicates in self._duplicates.values():
            for part in duplicates:
                part.dispose()
