# Copyright (c) 2023, Aalborg University (pygrametl@cs.aau.dk)
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.

# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import multiprocessing
import unittest

import pygrametl
from pygrametl.parallel import FutureResult
from pygrametl.tables import DecoupledDimension, DecoupledFactTable


class RecordingDimension(object):
    """A tiny dimension that records the keys it hands out."""

    def __init__(self, prefix, events):
        self.prefix = prefix
        self.events = events

    def ensure(self, row, namemapping={}):
        name = row[namemapping.get("name") or "name"]
        self.events.put(("dimension", name))
        return "%s-%s" % (self.prefix, name)

    def endload(self):
        pass


class RecordingFactTable(object):
    """A fact table that records the rows it is given."""

    def __init__(self, name, events):
        self.name = name
        self.events = events

    def insert(self, row, namemapping={}):
        self.events.put((self.name, _actualvalues(row)))

    def endload(self):
        pass


def _actualvalues(value):
    if isinstance(value, FutureResult):
        return ("unresolved", value.creator, value.id)
    if isinstance(value, dict):
        return {k: _actualvalues(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_actualvalues(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_actualvalues(v) for v in value)
    return value


class DecoupledTest(unittest.TestCase):
    def setUp(self):
        self.events = multiprocessing.Queue()

    def tearDown(self):
        for table in list(pygrametl._alltables):
            if isinstance(table, (DecoupledDimension, DecoupledFactTable)):
                table.shutdowndecoupled()

    def take(self):
        return self.events.get(True, 10)

    def assertempty(self):
        self.assertTrue(self.events.empty())

    def test_future_is_shared_between_caller_consumer_and_nested_details(self):
        dimension = DecoupledDimension(
            RecordingDimension("dim", self.events), batchsize=100
        )
        fact = DecoupledFactTable(
            RecordingFactTable("fact", self.events),
            consumes=[dimension],
            batchsize=100,
        )
        other = DecoupledFactTable(
            RecordingFactTable("other", self.events),
            consumes=[dimension],
            batchsize=100,
        )

        key = dimension.ensure({"name": "Product"})
        dimension.ensure({"name": "Not yet needed"})

        factrow = {"productid": key, "details": [{"productid": key}]}
        pendingfact = fact.insert(factrow)
        pendingother = other.insert({"productid": key, "note": "unrelated"})

        # The caller can get the value, and so can both consumers.
        self.assertEqual(key.get(), "dim-Product")
        pendingfact.get()
        pendingother.get()

        events = [self.take() for _ in range(3)]
        self.assertEqual(
            sorted(events),
            sorted(
                [
                    ("dimension", "Product"),
                    (
                        "fact",
                        {
                            "productid": "dim-Product",
                            "details": [{"productid": "dim-Product"}],
                        },
                    ),
                    ("other", {"productid": "dim-Product", "note": "unrelated"}),
                ]
            ),
        )
        # The unrelated dimension work was left for later.
        self.assertempty()

        dimension.endload()
        self.assertEqual(self.take(), ("dimension", "Not yet needed"))
        fact.endload()
        other.endload()

    def test_same_id_from_different_dimensions_is_kept_apart(self):
        firstdimension = DecoupledDimension(
            RecordingDimension("first", self.events), batchsize=100
        )
        seconddimension = DecoupledDimension(
            RecordingDimension("second", self.events), batchsize=100
        )
        fact = DecoupledFactTable(
            RecordingFactTable("fact", self.events),
            consumes=[firstdimension, seconddimension],
            batchsize=100,
        )

        # Both dimensions hand out their first key, i.e., both have id 0.
        firstkey = firstdimension.ensure({"name": "Same"})
        secondkey = seconddimension.ensure({"name": "Same"})
        self.assertEqual(firstkey.id, 0)
        self.assertEqual(secondkey.id, 0)
        self.assertNotEqual(firstkey.creator, secondkey.creator)

        pendingfact = fact.insert({"firstid": firstkey, "secondid": secondkey})
        pendingfact.get()

        self.assertEqual(self.events.get(True, 10), ("dimension", "Same"))
        self.assertEqual(self.events.get(True, 10), ("dimension", "Same"))
        self.assertEqual(
            self.events.get(True, 10),
            ("fact", {"firstid": "first-Same", "secondid": "second-Same"}),
        )

        firstdimension.endload()
        seconddimension.endload()
        fact.endload()

    def test_missing_configured_position_falls_back_to_recursive_lookup(self):
        dimension = DecoupledDimension(
            RecordingDimension("dim", self.events), batchsize=100
        )
        fact = DecoupledFactTable(
            RecordingFactTable("fact", self.events),
            consumes=[dimension],
            attstoconsume=["missing"],
            batchsize=100,
        )

        key = dimension.ensure({"name": "Product"})
        pendingfact = fact.insert({"productid": key, "detail": {"productid": key}})
        pendingfact.get()

        self.assertEqual(self.events.get(True, 10), ("dimension", "Product"))
        self.assertEqual(
            self.events.get(True, 10),
            (
                "fact",
                {"productid": "dim-Product", "detail": {"productid": "dim-Product"}},
            ),
        )

        dimension.endload()
        fact.endload()

    def test_blocked_writes_are_not_advanced(self):
        dimension = DecoupledDimension(
            RecordingDimension("dim", self.events), batchsize=100
        )
        fact = DecoupledFactTable(
            RecordingFactTable("fact", self.events), consumes=[dimension], batchsize=100
        )

        key = dimension.ensure({"name": "Approved"})
        dimension.ensure({"name": "Pending"})
        approved = fact.insert({"productid": key})
        fact.insert({"productid": "blocked"})
        fact.insert({"productid": "also blocked"})

        approved.get()
        self.assertEqual(self.events.get(True, 10), ("dimension", "Approved"))
        self.assertEqual(
            self.events.get(True, 10), ("fact", {"productid": "dim-Approved"})
        )
        self.assertempty()

        dimension.endload()
        fact.endload()

    def test_getresult_only_sends_the_prefix_needed(self):
        dimension = DecoupledDimension(
            RecordingDimension("dim", self.events), batchsize=100
        )

        first = dimension.ensure({"name": "First"})
        second = dimension.ensure({"name": "Second"})

        self.assertEqual(dimension.getresult(second), "dim-Second")
        self.assertEqual(
            [self.take(), self.take()],
            [("dimension", "First"), ("dimension", "Second")],
        )

        # No new work is needed for an already computed result.
        self.assertEqual(first.result, "dim-First")
        self.assertempty()

        dimension.endload()

    def test_short_queue_still_allows_the_needed_future(self):
        dimension = DecoupledDimension(
            RecordingDimension("dim", self.events), batchsize=10, queuesize=1
        )
        fact = DecoupledFactTable(
            RecordingFactTable("fact", self.events),
            consumes=[dimension],
            batchsize=10,
            queuesize=1,
        )
        other = DecoupledFactTable(
            RecordingFactTable("other", self.events),
            consumes=[dimension],
            batchsize=10,
        )

        key = dimension.ensure({"name": "Required"})
        for name in ["Other1", "Other2", "Other3"]:
            dimension.ensure({"name": name})

        self.assertEqual(key.get(), "dim-Required")
        pendingfact = fact.insert({"productid": key})
        pendingother = other.insert({"productid": key})
        pendingfact.get()
        pendingother.get()

        self.assertEqual(
            [self.take() for _ in range(3)],
            [
                ("dimension", "Required"),
                ("fact", {"productid": "dim-Required"}),
                ("other", {"productid": "dim-Required"}),
            ],
        )
        self.assertempty()

        dimension.endload()
        self.assertEqual(
            [self.take() for _ in range(3)],
            [("dimension", "Other1"), ("dimension", "Other2"), ("dimension", "Other3")],
        )
        fact.endload()
        other.endload()


if __name__ == "__main__":
    unittest.main()
