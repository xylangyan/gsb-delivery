Extract time windows
====================

The :py:mod:`rosbags.extract` package writes a time window from one or more
rosbag2 recordings into a new rosbag2 directory. The result reopens with the
existing readers, and message payloads are copied byte for byte.

Conflicting type definitions
----------------------------

Connections are resolved against their own message definition. If two
connections use the same type name but different definitions, the definitions
are written as separate variants and each connection points to the variant it
was recorded with. Connections from one recording that used the same definition
keep sharing one variant. Nested definitions that are identical in both
recordings are written once.

Variants keep the original type name for the first definition and receive a
suffix (or the package given via ``--scope``) for later ones. The original
name for every renamed variant is returned in ``result.renames``, and the
source recording and original type of every written connection are stored in
the destination metadata under ``custom_data['rosbags.extract.origins']``.

.. code-block:: python

   from pathlib import Path

   from rosbags.extract import extract

   result = extract(
       [Path('bag_a'), Path('bag_b')],
       Path('window'),
       start=1_700_000_000_000_000_000,
       stop=1_700_000_060_000_000_000,
       topics=['/status'],
   )
   print(result.renamed)

Incomplete definitions
----------------------

A connection whose embedded definition does not declare every referenced type
is reported in ``result.missing`` and omitted, while the remaining connections
are written. Passing ``fallback`` (a typestore, definition text, or mapping of
definitions; a sequence of these is tried in order) only restores a connection
when the fallback's digest matches the digest stored with that connection; a
same-named definition from another connection is never substituted. Use
``include_missing=False`` to fail instead of writing a partial result.

Command line
------------

.. code-block:: console

   $ rosbags-extract --src bag_a bag_b --dst window \
       --start 1700000000000000000 --stop 1700000060000000000

   # Supply a definition for a connection whose declaration is incomplete.
   $ rosbags-extract --src bag_a bag_b --dst window \
       --fallback-definition my_msgs/msg/State.msg
