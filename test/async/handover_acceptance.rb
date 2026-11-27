#!/usr/bin/env ruby
# frozen_string_literal: true

# Released under the MIT License.
# Copyright, 2026, by Samuel Williams.

# Acceptance harness for Async::Handover.
#
# Run directly (no sus needed in the offline environment):
#
#   ruby -Ilib test/async/handover_acceptance.rb
#
# It uses scripted gates (Thread::Queue mediated by the fibre scheduler) so
# the source position and the stub double can be paused at exact test events,
# without any real network. One physical slot is shared by the sealed old
# shift (pages A=7 and B=8) and the next shift (page C=9).

require "async"
require "async/handover"
require "async/semaphore"
require "async/barrier"

class ScriptedHarness
	def initialize
		@reached = Thread::Queue.new
		@gates = Hash.new {|hash, name| hash[name] = Thread::Queue.new}
	end

	# Called from the page fibre: announce the point, then park until the
	# driver releases this named gate.
	def pause(name)
		@reached.push(name)
		@gates[name].pop
	end

	# Driver side: block until the page fibre announces +name+.
	def wait_until(name)
		loop do
			event = @reached.pop
			return if event == name
		end
	end

	# Driver side: release a parked page fibre.
	def release(name)
		@gates[name].push(true)
	end
end

module Assertions
	def assert(condition, message)
		raise "ASSERT FAILED: #{message}" unless condition
	end

	def assert_equal(expected, actual, message)
		raise "ASSERT FAILED: #{message} (expected #{expected.inspect}, got #{actual.inspect})" unless expected == actual
	end
end

include Assertions

# Branch one: A only wrote a draft when the two cancels arrive.
def run_draft_branch
	Async do |task|
		harness = ScriptedHarness.new
		handover = Async::Handover.new([:source])
		old_shift = handover.current_shift
		events = []

		# A is page 7 of the old shift and holds the only physical slot.
		task_a = old_shift.process(7) do |page, page_task|
			# Provisional continuation number in a draft only:
			page.draft(continuations: [71])
			harness.pause(:a_drafted)
			page.commit(handover.stub) # never reached in this branch
		end

		# B is queued beyond the one-slot limit, still on the old shift.
		task_b = old_shift.process(8) do |page, _page_task|
			events << [:b_ran, page.number]
			page.commit(handover.stub)
		end

		# Next shift claims the same source before the old shift has drained.
		new_shift = handover.seal
		assert_equal(1, handover.current_shift.index, "seal rotates to the next shift")

		task_c = new_shift.process(9) do |page, _page_task|
			events << [:c_started, page.number]
			page.commit(handover.stub)
		end

		harness.wait_until(:a_drafted)

		# B withdraws while still queued; it never held the slot.
		withdrawn = handover.withdraw_waiter(old_shift, 8)
		assert(withdrawn, "queued claimant B can withdraw")
		task_b.wait

		slot = handover.slot(:source)
		assert(!slot.held_by?(handover.page(0, 8)), "withdrawn B never holds the slot")
		b_record = handover.stub.record_for(0, 8)
		assert_equal(:withdrawn, b_record.status, "B is signed as withdrawn")
		assert_equal([], b_record.continuations, "withdrawal accepts no continuations")

		# First cancel while still in the sealed section: only recorded.
		first = task_a.cancel
		assert_equal(false, first, "first cancel is deferred")
		assert(task_a.alive?, "A is still alive after the first cancel")
		assert(task_a.cancel_deferred?, "A has a pending deferred cancel")
		assert(slot.held_by?(handover.page(0, 7)), "slot stays with old page A after first cancel")

		# Second cancel keeps the original immediate-interrupt behaviour.
		task_a.cancel
		task_a.wait

		a_page = handover.page(0, 7)
		assert_equal(:withdrawn, a_page.outcome, "draft-only A is signed as withdrawn")

		a_record = handover.stub.record_for(0, 7)
		assert_equal(:withdrawn, a_record.status, "stub records page 7 as withdrawn")
		assert_equal([], a_record.continuations, "draft continuation 71 is not accepted")
		assert_equal([71], a_record.draft_continuations, "draft continuation number is kept for audit")

		# The provisional continuation never reserved the physical slot.
		assert(!slot.holders.any? {|holder| holder.number == 71}, "draft continuation does not occupy the slot")

		# C can only make its first legal start once A really let go.
		task_c.wait
		assert(events.include?([:c_started, 9]), "C starts after the old page releases the slot")
		c_index = events.index([:c_started, 9])
		assert(c_index && events[0...c_index].none? {|event| event.first == :c_started}, "C has exactly one legal start")

		new_shift.settle
		old_shift.settle
		assert(handover.drained?, "the single slot is free after the branch drains")

		puts "draft branch OK: #{handover.stub.accepted_order.map {|r| [r.claim, r.page, r.status]}.inspect}"
	end
end

# Branch two: the stub has durably recorded A but the fibre has not returned
# when the cancels arrive.
def run_committed_branch
	Async do |task|
		harness = ScriptedHarness.new
		handover = Async::Handover.new([:source])
		old_shift = handover.current_shift
		events = []

		task_a = old_shift.process(7) do |page, page_task|
			# A real registered continuation, then a commit whose fibre is
			# paused after the stub record is already durable:
			page.register_continuation(71)
			page.handoff(71)

			page.commit(handover.stub, continuations: [71]) do |_record|
				harness.pause(:a_committed)
			end

			events << [:a_returned, page.number]
		end

		task_b = old_shift.process(8) do |page, _page_task|
			events << [:b_ran, page.number]
			page.commit(handover.stub)
		end

		new_shift = handover.seal

		task_c = new_shift.process(9) do |page, _page_task|
			events << [:c_started, page.number]
			page.commit(handover.stub)
		end

		# An independent stocktake clerk for the old shift.
		stocktake_done = false
		old_shift.perform_stocktake(:audit) do
			# The stocktake clerk is a separate barrier from intake.
			stocktake_done = true
		end

		harness.wait_until(:a_committed)

		# B still withdraws cleanly from the queue in this branch too.
		assert(handover.withdraw_waiter(old_shift, 8), "B withdraws while queued")
		task_b.wait

		assert(!old_shift.complete?, "old shift clerks cannot sign off while A is still open")

		first = task_a.cancel
		assert_equal(false, first, "first cancel is deferred in the committed branch too")
		assert(task_a.alive?, "A stays alive after the first cancel")

		task_a.cancel
		task_a.wait

		a_page = handover.page(0, 7)
		a_record = handover.stub.record_for(0, 7)
		assert_equal(:signed, a_page.outcome, "A is signed per the completed stub")
		assert_equal(:signed, a_record.status, "stub page 7 stays signed")
		assert_equal([71], a_record.continuations, "accepted continuation 71 stays with the old shift")

		slot = handover.slot(:source)
		assert(!handover.drained?, "slot is still held by the accepted continuation")
		assert(!events.include?([:c_started, 9]), "C cannot start while the continuation chain holds the slot")

		# The continuation is warehoused only after its parent page has ended,
		# yet it keeps the slot and earns its own independent receipt:
		child = handover.continuation_page(a_page, 71)
		old_shift.warehouse(child) do |continuation, _continuation_task|
			continuation.commit(handover.stub)
			events << [:continuation_done, continuation.number]
		end
		old_shift.settle

		continuation_record = handover.stub.record_for(0, 71, 7)
		assert(continuation_record, "continuation has its own stub record")
		assert_equal(0, continuation_record.shift, "continuation receipt stays on the old shift")
		assert_equal(7, continuation_record.continuation_of, "receipt links back to parent page 7")
		assert(continuation_record.claim != a_record.claim, "continuation receipt claim is independent")

		# Actual acceptance order is verifiable through claim numbers.
		order = handover.stub.accepted_order.map {|record| [record.claim, record.page, record.status]}
		assert_equal(a_record.claim, order.first.first, "page 7 is the first accepted record")
		assert(order.any? {|claim, page, _| claim == continuation_record.claim && page == 71}, "continuation appears in acceptance order")

		task_c.wait
		c_index = events.index([:c_started, 9])
		continuation_index = events.index([:continuation_done, 71])
		assert(continuation_index < c_index, "C starts only after the continuation releases the slot")

		assert(stocktake_done, "the independent stocktake clerk ran")
		new_shift.settle
		assert(handover.drained?, "slot drains only after the full old page chain ends")

		puts "committed branch OK: #{order.inspect}"
	end
end

# Observer semantics: re-opening only replays the observer's own last signed
# position, and one observer's failed read cannot make another miss its page.
def run_observer_check
	Async do
		handover = Async::Handover.new([:source])
		old_shift = handover.current_shift

		# Both observers register before the page signs, so each gets its own message.
		gate_x = handover.gate(:source, as: :reader_x)
		gate_y = handover.gate(:source, as: :reader_y)

		old_shift.process(7) do |page, _task|
			page.commit(handover.stub)
		end
		old_shift.settle

		assert_equal(7, handover.observe(:source, as: :reader_x), "observer X reads the signed position")
		assert_equal(7, handover.observe(:source, as: :reader_y), "observer Y reads its own signed position")

		# Re-opening without a new signal replays each observer's own last position.
		assert_equal(7, handover.observe(:source, as: :reader_x), "X re-opening replays its own last position")
		assert_equal(7, handover.observe(:source, as: :reader_y), "Y re-opening replays its own last position")

		# Y then suffers a failed read; the failure is delivered to Y alone.
		gate_y.fail!(RuntimeError.new("transient read failure"))
		failed = false
		begin
			handover.observe(:source, as: :reader_y)
		rescue RuntimeError
			failed = true
		end
		assert(failed, "observer Y receives its own failure")

		# X never misses its position because of Y's failure.
		assert_equal(7, handover.observe(:source, as: :reader_x), "X still gets its last position after Y's failure")
		assert_equal(7, gate_x.last_position, "X retained its last signed position")
		# Y's own last signed position also survives the failed read.
		assert_equal(7, gate_y.last_position, "Y kept its last signed position despite the failure")
		assert_equal(7, handover.observe(:source, as: :reader_y), "Y re-opening after failure replays its last position")

		puts "observer check OK"
	end
end

# Primitives that do not use the handover entry keep their old behaviour.
def run_primitive_regression
	Async do
		semaphore = Async::Semaphore.new(1)
		order = []
		barrier = Async::Barrier.new

		2.times do |index|
			semaphore.async(parent: barrier) do
				order << [:enter, index]
				sleep(0.001)
				order << [:leave, index]
			end
		end
		barrier.wait

		assert_equal([[:enter, 0], [:leave, 0], [:enter, 1], [:leave, 1]], order, "Semaphore still serialises")
		assert(semaphore.empty?, "Semaphore releases normally")
		puts "primitive regression OK"
	end
end

run_draft_branch
run_committed_branch
run_observer_check
run_primitive_regression

# The two recipients (intake and stocktake clerks) each have their own
# barrier, so the old shift can only be fully signed off when both are idle.
def run_clerk_signoff_check
	Async do
		harness = ScriptedHarness.new
		handover = Async::Handover.new([:source])
		old_shift = handover.current_shift

		# Each shift gets independent clerks:
		new_shift = handover.seal
		assert(!old_shift.intake.equal?(new_shift.intake), "shifts have distinct intake clerks")
		assert(!old_shift.stocktake.equal?(new_shift.stocktake), "shifts have distinct stocktake clerks")

		old_shift.process(7) do |page, _task|
			harness.pause(:page_open)
			page.commit(handover.stub)
		end

		old_shift.perform_stocktake(:audit) do
			harness.pause(:stocktake_open)
		end

		sleep 0.01
		assert(old_shift.intake_busy?, "intake clerk is working before sign-off")
		assert(old_shift.stocktake_busy?, "stocktake clerk is working before sign-off")
		assert(!old_shift.complete?, "old shift cannot sign off while intake and stocktake are open")

		# Intake finishes first, but the stocktake clerk is still open.
		harness.release(:page_open)
		sleep 0.01
		assert(!old_shift.intake_busy?, "intake clerk is idle after the page is signed")
		assert(old_shift.stocktake_busy?, "stocktake clerk is still working")
		assert(!old_shift.complete?, "old shift is not fully signed off with the stocktake still open")

		harness.release(:stocktake_open)
		old_shift.wait
		assert(!old_shift.intake_busy? && !old_shift.stocktake_busy?, "both recipients are idle")
		assert(old_shift.complete?, "old shift signs off only once both recipients are idle")
		assert(handover.drained?, "slot is free after sign-off")

		puts "clerk sign-off check OK"
	end
end

# General sanity check with more than one physical position: distinct pages
# run in parallel but a single position is never double-booked.
def run_multi_slot_check
	Async do
		handover = Async::Handover.new([:s1, :s2])
		shift = handover.current_shift
		active = Mutex.new
		current = 0
		maximum = 0
		occupied = Hash.new(0)

		4.times do |number|
			shift.process(number) do |page, _task|
				active.synchronize do
					current += 1
					maximum = [maximum, current].max
					occupied[page.slot.key] += 1
				end
				sleep(0.005)
				page.commit(handover.stub)
				active.synchronize {current -= 1}
			end
		end

		shift.wait
		assert_equal(2, maximum, "two slots allow two concurrent pages")
		assert_equal([2, 2], [:s1, :s2].map {|key| occupied[key]}, "each position is used in turn, never twice at once")
		assert(handover.drained?, "both positions drain")
		puts "multi-slot check OK"
	end
end

# Fill every position, then free them in close succession while more pages
# stay queued. The pump must not lose a wake-up, otherwise the run hangs.
def run_wakeup_pump_check
	Async do
		handover = Async::Handover.new([:s1, :s2, :s3])
		shift = handover.current_shift
		gates = Array.new(3) {Thread::Queue.new}
		completed = []

		# The first three pages occupy all positions and block on scripted gates.
		3.times do |number|
			shift.process(number) do |page|
				gates[number].pop
				page.commit(handover.stub)
				completed << number
			end
		end

		sleep 0.01

		# Five more queue behind them.
		5.times do |offset|
			number = offset + 3
			shift.process(number) do |page|
				page.commit(handover.stub)
				completed << number
			end
		end

		sleep 0.01
		assert_equal(5, handover.waiting.size, "five claimants are queued")

		# Release the three holders nearly together; the pump chains through the queue.
		gates.each {|gate| gate.push(true)}

		shift.settle
		assert_equal((0...8).to_a, completed.sort, "every queued page eventually acquires a position")
		assert(handover.drained?, "all positions drain without a lost wake-up")
		puts "wake-up pump check OK"
	end
end

# A continuation handed off early keeps the slot only if the parent actually
# accepts it at commit time; an unaccepted one gives the position back.
def run_rejected_continuation_check
	Async do
		handover = Async::Handover.new([:source])
		shift = handover.current_shift

		shift.process(7) do |page|
			page.register_continuation(71)
			page.register_continuation(72)
			page.handoff(71)
			page.handoff(72)

			# Only 71 is actually accepted; 72 must release the slot.
			page.commit(handover.stub, continuations: [71])
		end
		shift.settle

		record = handover.stub.record_for(0, 7)
		assert_equal([71], record.continuations, "only continuation 71 is accepted")

		child_71 = handover.continuation_page(handover.page(0, 7), 71)
		child_72 = handover.continuation_page(handover.page(0, 7), 72)
		assert_equal(:reserved, child_71.outcome, "accepted continuation stays reserved")
		assert_equal(:rejected, child_72.outcome, "unaccepted continuation is rejected")
		assert(!handover.slot(:source).held_by?(child_72), "rejected continuation no longer holds the slot")
		assert(handover.slot(:source).held_by?(child_71), "accepted continuation still holds the slot")

		# Once the accepted continuation is warehoused and signed, the slot frees.
		shift.warehouse(child_71) {|continuation| continuation.commit(handover.stub)}
		shift.settle
		assert(handover.drained?, "the position drains after the accepted continuation ends")
		puts "rejected continuation check OK"
	end
end

run_clerk_signoff_check
run_multi_slot_check
run_wakeup_pump_check
run_rejected_continuation_check

puts "all acceptance checks passed"
