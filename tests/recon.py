from unittest.mock import Mock

import pytest

from invoke import Collection, Executor, Task
from invoke.recon import (
    BindingRule,
    Entry,
    GuaranteeViolation,
    JointExecutor,
    OutcomeRule,
    SettledRule,
    SlotRule,
    TimeSlot,
    WorkOrder,
)

pytestmark = pytest.mark.usefixtures("integration")


class _EastWestWorkOrder:
    """
    The canonical east/west reconciliation order.

    Entries A, D (east) and B, C, F (west) share one fetch task and the
    same parameters. The east collection picks ledger E, the west
    collection picks ledger W; both books are version V7. Slots at nine,
    ten and eleven, with the west nine-o'clock original withdrawn before
    ten and D's signature switching C onto the east book.
    """

    def setup_method(self):
        self.fetch_body = Mock()
        self.pre_body = Mock()
        self.post_body = Mock()
        self.pre = Task(self.pre_body, name="prefetch")
        self.post = Task(self.post_body, name="postfetch")
        self.fetch = Task(
            self.fetch_body, name="fetch", pre=[self.pre], post=[self.post]
        )
        self.collection = Collection()
        self.collection.add_task(self.fetch)
        self.collection.add_task(self.pre)
        self.collection.add_task(self.post)
        self.order = WorkOrder("east-west")
        self.east = self.order.register_ledger("east", "E", "V7")
        self.west = self.order.register_ledger("west", "W", "V7")
        assert self.east != self.west
        self._build_entries()
        self._build_slots()
        self.order.add_switch(
            "D", "signed", "C", self.east, collection="east"
        )
        self.order.withdraw_before("ten", self.west)

    def _build_entries(self):
        self.A = Entry(
            "A",
            collection="east",
            task=self.fetch,
            outcomes=("signed",),
        )
        self.B = Entry(
            "B",
            collection="west",
            task=self.fetch,
            outcomes=("signed",),
        )
        self.C = Entry(
            "C",
            collection="west",
            task=self.fetch,
            outcomes=("signed",),
            rules=(
                SettledRule("D"),
                BindingRule("C", self.east),
            ),
        )
        self.D = Entry(
            "D",
            collection="east",
            task=self.fetch,
            outcomes=("signed", "rejected"),
            rules=(SlotRule("ten", ledger=self.east),),
        )
        self.F = Entry(
            "F",
            collection="west",
            task=self.fetch,
            outcomes=("signed",),
            rules=(SlotRule("eleven", ledger=self.west),),
        )
        for entry, ledger in (
            (self.A, self.east),
            (self.B, self.west),
            (self.C, self.west),
            (self.D, self.east),
            (self.F, self.west),
        ):
            self.order.add_entry(entry, ledger)

    def _build_slots(self):
        nine = TimeSlot(
            "nine",
            {self.east: [self.A], self.west: [self.B, self.C]},
            max_originals=2,
        )
        ten = TimeSlot(
            "ten",
            {self.east: [self.D, self.C], self.west: [self.D, self.C]},
            max_originals=1,
        )
        eleven = TimeSlot(
            "eleven",
            {self.west: [self.C, self.F]},
            max_originals=1,
        )
        for slot in (nine, ten, eleven):
            self.order.add_slot(slot)

    def execute(self, results):
        self.fetch_body.side_effect = results
        executor = Executor(collection=self.collection)
        joint = JointExecutor(
            order=self.order,
            collection=self.collection,
            executor=executor,
            outcome_of=lambda value: value,
        )
        return joint, joint.run()


class planning:
    "up-front guarantee and minimax planning"

    def setup_method(self):
        self.scenario = _EastWestWorkOrder()
        self.scenario.setup_method()

    def guarantee_holds_and_worst_case_is_minimal(self):
        # Construction performs the full branch walk without touching the
        # real executor: no fetch may run during planning.
        joint = JointExecutor(
            order=self.scenario.order,
            collection=self.scenario.collection,
            executor=Executor(collection=self.scenario.collection),
            outcome_of=lambda value: value,
        )
        assert self.scenario.fetch_body.call_count == 0
        # Nine needs two originals (E for A, W for B); ten needs a fresh E
        # original for D; eleven needs a fresh W original for F. No
        # arrangement can do better than four real fetches in total.
        assert joint.worst_case_fetches == 4

    def impossible_order_reports_the_blocking_rule(self):
        order = WorkOrder("doomed")
        book = order.register_ledger("c", "BK", "V1")
        gate = Entry(
            "gate",
            collection="c",
            task=self.scenario.fetch,
            outcomes=("signed",),
            required=False,
        )
        stuck = Entry(
            "stuck",
            collection="c",
            task=self.scenario.fetch,
            rules=(OutcomeRule("gate", "signed"),),
        )
        order.add_entry(gate, book)
        order.add_entry(stuck, book)
        # 'gate' is never allowed on any roster.
        order.add_slot(
            TimeSlot("only", {book: [stuck]}, max_originals=1)
        )
        with pytest.raises(GuaranteeViolation) as info:
            JointExecutor(
                order=order,
                collection=self.scenario.collection,
                executor=Executor(collection=self.scenario.collection),
            )
        assert info.value.entry.name == "stuck"
        assert "gate=signed" in info.value.rule.describe()


class signed_branch:
    "D signs at ten, C switches to the east book"

    def setup_method(self):
        self.scenario = _EastWestWorkOrder()
        self.scenario.setup_method()
        self.joint, self.trail = self.scenario.execute(
            ["signed", "signed", "signed", "signed"]
        )

    def nine_o_clock_rosters_are_fixed_and_scoped(self):
        east_nine, west_nine = self.trail.originals["ORIG-0001"], \
            self.trail.originals["ORIG-0002"]
        assert [entry.name for entry in east_nine.roster] == ["A"]
        assert [entry.name for entry in west_nine.roster] == ["B", "C"]
        # A's east original grants nothing to D or C.
        assert self.scenario.D not in east_nine.roster
        assert self.scenario.C not in east_nine.roster
        with pytest.raises(AttributeError):
            east_nine.roster.append(self.scenario.D)

    def west_nine_original_is_withdrawn_but_B_keeps_provenance(self):
        west_nine = self.trail.originals["ORIG-0002"]
        assert west_nine.withdrawn is True
        provenance = self.trail.provenance("B")
        assert provenance.original_serial == west_nine.serial
        assert provenance.status == "signed"
        assert provenance.ledger == "W"

    def ten_o_clock_east_original_lists_D_and_C(self):
        ten = self.trail.originals["ORIG-0003"]
        assert ten.ledger == self.scenario.east
        assert [entry.name for entry in ten.roster] == ["D", "C"]

    def D_and_C_share_the_ten_o_clock_fetch_and_book(self):
        d_record = self.trail.provenance("D")
        c_record = self.trail.provenance("C")
        assert d_record.outcome == "signed"
        assert d_record.claimed_slot == "ten"
        assert c_record.original_serial == d_record.original_serial
        assert c_record.claimed_slot == "ten"
        assert c_record.ledger == "E"
        assert c_record.collection == "east"
        assert c_record.fetch_seq == d_record.fetch_seq == 3

    def eleven_o_clock_west_original_goes_to_F_alone(self):
        eleven = self.trail.originals["ORIG-0004"]
        assert eleven.ledger == self.scenario.west
        assert [entry.name for entry in eleven.roster] == ["F"]
        f_record = self.trail.provenance("F")
        assert f_record.original_serial == eleven.serial
        assert f_record.fetch_seq == 4

    def only_two_fetches_happen_after_ten(self):
        seqs = {
            record.fetch_seq
            for record in self.trail.records
            if record.status == "signed" and record.claimed_slot == "ten"
        } | {
            record.fetch_seq
            for record in self.trail.records
            if record.status == "signed" and record.claimed_slot == "eleven"
        }
        assert seqs == {3, 4}
        assert self.scenario.fetch_body.call_count == 4
        # Pre/post tasks run once per real fetch, via the ordinary
        # Executor, so the existing task chain is preserved.
        assert self.scenario.pre_body.call_count == 4
        assert self.scenario.post_body.call_count == 4

    def every_signed_record_carries_book_claim_and_serial_audit(self):
        for name in ("A", "B", "C", "D", "F"):
            record = self.trail.provenance(name)
            assert record.ledger_version == "V7"
            assert record.original_serial is not None
            assert record.claimed_slot is not None
            assert record.status == "signed"


class rejected_branch:
    "D refuses at ten, C stays on the west book and joins F at eleven"

    def setup_method(self):
        self.scenario = _EastWestWorkOrder()
        self.scenario.setup_method()
        self.joint, self.trail = self.scenario.execute(
            ["signed", "signed", "rejected", "signed"]
        )

    def refusal_is_recorded_on_D(self):
        d_record = self.trail.provenance("D")
        assert d_record.status == "signed"
        assert d_record.outcome == "rejected"
        assert d_record.ledger == "E"
        assert d_record.claimed_slot == "ten"

    def C_cannot_claim_the_east_original_that_listed_it(self):
        ten = self.trail.originals["ORIG-0003"]
        assert self.scenario.C in ten.roster
        ten_claims = [
            record
            for record in self.trail.for_original(ten.serial)
            if record.entry == "C" and record.status == "signed"
        ]
        assert ten_claims == []
        c_record = self.trail.provenance("C")
        assert c_record.original_serial == "ORIG-0004"
        assert c_record.ledger == "W"
        assert c_record.collection == "west"
        assert c_record.claimed_slot == "eleven"

    def C_and_F_share_the_eleven_o_clock_west_original(self):
        eleven = self.trail.originals["ORIG-0004"]
        assert [entry.name for entry in eleven.roster] == ["C", "F"]
        assert self.trail.provenance("C").fetch_seq == \
            self.trail.provenance("F").fetch_seq

    def nine_o_clock_signatures_remain_untouched(self):
        assert self.trail.provenance("A").original_serial == "ORIG-0001"
        b_record = self.trail.provenance("B")
        assert b_record.original_serial == "ORIG-0002"
        assert self.trail.original("ORIG-0002").withdrawn is True

    def still_only_two_fetches_after_ten(self):
        assert self.scenario.fetch_body.call_count == 4
        assert self.trail.provenance("D").fetch_seq == 3
        assert self.trail.provenance("F").fetch_seq == 4


class collection_semantics:
    "implicit vs explicit collection selection and ledger registration"

    def same_name_and_version_can_be_two_distinct_books(self):
        order = WorkOrder("books")
        first = order.register_ledger("c1", "X", "V9")
        second = order.register_ledger("c2", "X", "V9")
        assert first != second
        # Re-registering the same collection/name/version returns the book.
        again = order.register_ledger("c1", "X", "V9")
        assert again == first

    def different_collections_may_share_one_book(self):
        order = WorkOrder("shared")
        book = order.register_ledger("c1", "S", "V1")
        other = order.register_ledger("c2", "S", "V1", shared=book)
        assert other == book

    def explicit_collection_call_always_uses_named_collection(self):
        body = Mock(return_value="signed")
        fetch = Task(body, name="fetch")
        coll = Collection()
        coll.add_task(fetch)
        order = WorkOrder("explicit")
        book = order.register_ledger("implicit-coll", "L", "V1")
        entry = Entry(
            "E",
            collection="implicit-coll",
            task=fetch,
            explicit_collection="locked-coll",
        )
        order.add_entry(entry, book)
        order.add_slot(TimeSlot("s1", {book: [entry]}))
        joint = JointExecutor(
            order=order,
            collection=coll,
            executor=Executor(collection=coll),
            outcome_of=lambda value: value,
        )
        trail = joint.run()
        record = trail.provenance("E")
        assert record.collection == "locked-coll"
        assert record.explicit_collection is True

    def implicit_call_follows_collection_switch(self):
        scenario = _EastWestWorkOrder()
        scenario.setup_method()
        _joint, trail = scenario.execute(
            ["signed", "signed", "signed", "signed"]
        )
        c_record = trail.provenance("C")
        # C started in the west collection but the D=signed switch moved
        # its effective configuration to the east collection.
        assert c_record.collection == "east"
        assert c_record.explicit_collection is False
