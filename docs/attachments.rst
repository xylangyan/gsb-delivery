============================
SOAP Attachments (multipart)
============================

If the server responds with a Content-type: multipart, a MessagePack object
will be returned. It contains a root object and some attachments.

Example based on https://www.w3.org/TR/SOAP-attachments

.. code-block:: python

    from zeep import Client

    client = Client('http://www.risky-stuff.com/claim.svc?wsdl')

    pack = client.service.GetClaimDetails('061400a')

    ClaimDetails = pack.root
    SignedFormTiffImage = pack.attachments[0].content
    CrashPhotoJpeg = pack.attachments[1].content

    # Or lookup by content_id
    pack.get_by_content_id('<claim061400a.tiff@claiming-it.com>').content


Lazy (on-demand) MTOM/XOP attachments
=====================================

By default every ``xop:Include`` attachment is decoded and inlined into the
response eagerly, even if you only need a single field.  Enabling
``mtom_lazy`` keeps the references in place and resolves a binary field only
when that field is actually read::

    from zeep import Client
    from zeep.settings import Settings

    client = Client(
        'http://www.risky-stuff.com/claim.svc?wsdl',
        settings=Settings(mtom_lazy=True),
    )

    pack = client.service.GetClaimDetails('061400a')

The returned :class:`zeep.wsdl.lazy.LazyMessagePack` is scoped to that single
response and exposes:

* ``pack.root``: the deserialized SOAP body. Plain fields (strings, numbers,
  ...) are available immediately; each ``base64Binary`` field is a
  :class:`zeep.wsdl.lazy.LazyBinary` instead of ``bytes``.
* ``LazyBinary.read(size=-1)`` / ``LazyBinary.iter_content(chunk_size)`` to
  consume the bytes incrementally and ``bytes(binary)`` to materialize them.
* ``LazyBinary.close()`` (or use it as a context manager) to release a field.
* ``pack.close()`` to cancel the whole response; fields still open keep their
  shared bytes until they are closed themselves.
* ``LazyBinary.part`` / ``pack.attachment_for(content_id)`` to verify a
  completed field against its original part.

Guarantees:

* An attachment referenced several times by the same envelope is read from the
  underlying part only once; the bytes are cached and shared between fields,
  while every field keeps its own read cursor.
* Two parallel responses never share data, even when an attachment uses the
  same ``Content-ID`` in both.
* A missing attachment raises
  :class:`zeep.exceptions.IncompleteMessage` when the field is read; a present
  but genuinely empty attachment still yields real empty ``b""`` bytes.
* Two parts with the same ``Content-ID`` but different content raise
  :class:`zeep.exceptions.LazyAttachmentError` while the response is parsed;
  byte-identical duplicates are accepted.
