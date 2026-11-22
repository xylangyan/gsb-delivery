.. _advanced_usage:

**************
Advanced usage
**************

Extract selected members
========================

:meth:`~py7zr.SevenZipFile.extract` takes a collection of member names as
``targets``. Combine it with :meth:`~py7zr.SevenZipFile.getnames` to select
members by pattern:

.. code-block:: python

    import re
    from py7zr import SevenZipFile

    filter_pattern = re.compile(r'scripts.*')
    with SevenZipFile('archive.7z', 'r') as archive:
        allfiles = archive.getnames()
        targets = [f for f in allfiles if filter_pattern.match(f)]
        archive.extract(path='out', targets=targets)

With ``recursive=True``, a target also selects every member whose name starts
with it, so naming a directory extracts its whole subtree:

.. code-block:: python

    with SevenZipFile('archive.7z', 'r') as archive:
        archive.extract(path='out', targets=['scripts'], recursive=True)

An archive can be extracted only once per open. To extract again from the same
:class:`~py7zr.SevenZipFile` object, call :meth:`~py7zr.SevenZipFile.reset`
first, or reopen the archive.


.. _stream-7z-content:

Stream 7z content
=================

:meth:`~py7zr.SevenZipFile.extract` and :meth:`~py7zr.SevenZipFile.extractall`
accept a ``factory`` argument: an object implementing the
:class:`py7zr.WriterFactory` interface. When it is given, py7zr does not write
files to disk. For each regular file or symbolic link in the archive it calls
the factory's ``create`` method with the output file name, and writes the
member's data to the returned object. Directories and special files are skipped.

The object returned by ``create`` must implement the :class:`py7zr.Py7zIO`
interface, which is similar to Python's standard :class:`typing.BinaryIO`
with an additional ``size`` method:

``write(data)``
    Called repeatedly, on the fly, with successive chunks of the member's data
    while it is decompressed. Append each chunk; py7zr never holds the whole
    archive in memory, so this is where data can be streamed elsewhere.

``read``, ``seek``, ``flush``, ``size``
    Standard stream operations; ``size`` returns the number of bytes written.

``close()``
    Called once when the member is complete. It is optional to override; use it
    to release per-member resources or to finalize an upload before py7zr
    moves on to the next member.

.. note::

   py7zr may extract members in worker threads, so your classes should be
   thread-safe.


Example: keep data in memory
----------------------------

This factory keeps at most ``limit`` bytes of each member in memory.

.. code-block:: python

    from typing import Optional, Union

    from py7zr import Py7zIO, SevenZipFile, WriterFactory


    class MyIO(Py7zIO):
        def __init__(self, limit: int):
            self._limit = limit
            self._buffer = bytearray()

        def write(self, s: Union[bytes, bytearray]) -> int:
            """Keep only the first ``limit`` bytes."""
            room = self._limit - len(self._buffer)
            if room > 0:
                self._buffer += s[:room]
            return len(s)

        def read(self, size: Optional[int] = None) -> bytes:
            if size is None:
                return bytes(self._buffer)
            return bytes(self._buffer[:size])

        def seek(self, offset: int, whence: int = 0) -> int:
            return 0

        def flush(self) -> None:
            pass

        def size(self) -> int:
            return len(self._buffer)


    class MyFactory(WriterFactory):
        def __init__(self, limit: int):
            self.limit = limit
            self.products = {}

        def create(self, filename: str) -> Py7zIO:
            product = MyIO(self.limit)
            self.products[filename] = product
            return product


    factory = MyFactory(10)
    with SevenZipFile('archive.7z', 'r') as archive:
        archive.extractall(factory=factory)
    for filename, fileobj in factory.products.items():
        print(f'{filename}: {fileobj.read()}...')


Example: extract into network storage
-------------------------------------

Here is pseudo code that streams contents to cloud storage.
To keep the example simple, authentication, bucket operations and
mandatory headers are omitted.

.. code-block:: python

    from typing import Optional, Union

    import requests

    import py7zr


    class StreamIO(py7zr.Py7zIO):
        """Example network storage writer."""

        def __init__(self, fname: str):
            self.fname = fname
            self.length = 0

        def write(self, data: Union[bytes, bytearray]) -> int:
            # py7zr calls write multiple times, so append the data.
            self.length += len(data)
            requests.put("https://your.custom.network.storage.example.com/append/to/file/command/", data=data)
            return len(data)

        def read(self, size: Optional[int] = None) -> bytes:
            return b''

        def seek(self, offset: int, whence: int = 0) -> int:
            return offset

        def flush(self) -> None:
            pass

        def size(self) -> int:
            return self.length

        def close(self) -> None:
            # The member is complete; commit the upload here.
            pass


    class StreamIOFactory(py7zr.WriterFactory):
        """Factory class returning StreamIO objects."""

        def __init__(self):
            self.products = {}

        def create(self, filename: str) -> py7zr.Py7zIO:
            product = StreamIO(filename)
            self.products[filename] = product
            return product


    def extract_stream():
        factory = StreamIOFactory()
        with py7zr.SevenZipFile("target.7z") as archive:
            archive.extractall(factory=factory)


.. _plan-solid-delivery:

Plan delivery from a solid archive
==================================

A solid 7z archive keeps several members in one block (folder). The decoder
cannot be paused and must start again at the block's first packed byte, so
delivering members in an arbitrary receiver order may require decoding the same
block more than once. :meth:`py7zr.SevenZipFile.extraction_plan` builds a
minimum-cost plan from the block and substream boundaries stored in the archive
header, the receiver order, a per-block open cost, and a staging quota.

.. code-block:: python

    from py7zr import SevenZipFile, VerificationCache

    with SevenZipFile('solid.7z') as archive:
        plan = archive.extraction_plan(
            ['b', 'd', 'a', 'c'],       # receiver order, repeats allowed
            staging_quota=2,            # bytes retained between deliveries
            open_costs={0: 5, 1: 3},    # cost of decoding each block
        )
        print(plan.describe())
        execution = plan.execute(archive, block_cache=VerificationCache())

``staging_quota`` counts bytes kept for later deliveries. The member being
handed over does not count; a copy retained after the delivery does. A
repeated request is delivered again and may reuse a staged copy.

Planning is a shortest-path search over the staged copies and the open decoder.
For member ``B`` it reports the block opens, the staging high-water mark of
every step, and the resulting plan cost:

* with a quota of 1 byte and two 1-byte members per block, ``b, d, a, c``
  needs three block opens;
* with a quota of 2 bytes, one open per block is enough;
* if the receiver asks for ``a, b`` from the same block in order, one open
  serves both and no staging is needed.

Header evidence
---------------

``plan.proof`` records the boundaries the plan relies on. ``boundaries`` is a
``(block, offset, size)`` tuple for every substream and ``block_spans`` is a
``(block, pack_offset, pack_size)`` tuple for every block, both taken from the
folder list, PackInfo and the substream size vector. Execution re-derives the
same boundaries and raises :class:`py7zr.SubstreamBoundaryError` if the decoded
block does not split at them.

Verification states
-------------------

When a block stores only a whole-block checksum, execution may hand over the
current member's bytes as soon as its range is complete. The delivery is marked
``provisional`` and becomes ``verified`` only when the whole-block checksum
passes; a mismatch marks the affected deliveries ``failed``. Pass an
``on_status`` callback to observe each transition:

.. code-block:: python

    def report(kind, name):
        print(kind, name)

    execution = plan.execute(archive, on_status=report, chunk_size=4096)

Member checksums are applied immediately and independently, so one bad member
does not hide the verified state of its neighbours.

If a block's header span points past the readable bytes, the plan lists it in
``unverifiable`` and execution marks every delivery from it ``unverifiable``
instead of presenting guessed bytes as verified. Verified whole blocks may be
reused from :class:`py7zr.VerificationCache` for the same archive version; the
version key is a fingerprint of the header fields that determine block content.
