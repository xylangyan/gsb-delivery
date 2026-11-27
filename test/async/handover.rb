# frozen_string_literal: true

# Released under the MIT License.
# Copyright, 2026, by Samuel Williams.

require "async/handover"
require "async/semaphore"
require "async/barrier"
require "sus/fixtures/async"

describe Async::Handover do
	include Sus::Fixtures::Async::ReactorContext

	let(:handover) {subject.new([:source])}
	let(:old_shift) {handover.current_shift}
	let(:new_shift) {handover.seal}

	with "#seal" do
		it "rotates shifts while keeping the same slot list" do
			expect(handover.current_shift.index).to be == 0
			next_shift = handover.seal
			expect(next_shift.index).to be == 1
			expect(handover.current_shift).to be_equal(next_shift)
			expect(handover.slots.size).to be == 1
		end

		it "gives each shift independent intake and stocktake clerks" do
			next_shift = handover.seal
			expect(old_shift.intake).not.to be_equal(next_shift.intake)
			expect(old_shift.stocktake).not.to be_equal(next_shift.stocktake)
		end
	end

	with "a sealed old shift" do
		it "keeps a queued old page on the old shift" do
			gate = Thread::Queue.new

			holder = old_shift.process(7) do |page, task|
				gate.pop
				page.commit(handover.stub)
			end

			queued = old_shift.process(8) do |page, task|
				page.commit(handover.stub)
			end

			handover.seal

			expect(handover.waiting.size).to be == 1
			gate.push(true)
			holder.wait
			queued.wait

			record = handover.stub.record_for(0, 8)
			expect(record.shift).to be == 0
		end

		it "registers new pages on the next shift" do
			next_shift = handover.seal
			task = next_shift.process(9) do |page|
				page.commit(handover.stub)
			end
			task.wait

			expect(handover.stub.record_for(1, 9).shift).to be == 1
		end
	end

	with "cancellation" do
		it "defers the first cancel and interrupts on the second" do
			gate = Thread::Queue.new

			task = old_shift.process(7) do |page, page_task|
				page.draft(continuations: [71])
				gate.pop
				page.commit(handover.stub)
			end

			sleep(0.001)
			expect(task.cancel).to be == false
			expect(task).to be(:alive?)
			expect(handover.slot(:source)).to be(:held_by?, handover.page(0, 7))

			task.cancel
			task.wait

			page = handover.page(0, 7)
			expect(page.outcome).to be == :withdrawn
			record = handover.stub.record_for(0, 7)
			expect(record.status).to be == :withdrawn
			expect(record.continuations).to be == []
			expect(record.draft_continuations).to be == [71]
		end

		it "signs a committed page per the stub even when cancelled before returning" do
			reached = Thread::Queue.new
			resume = Thread::Queue.new

			task = old_shift.process(7) do |page, page_task|
				page.register_continuation(71)
				page.handoff(71)
				page.commit(handover.stub, continuations: [71]) do
					reached.push(true)
					resume.pop
				end
			end

			reached.pop
			expect(task.cancel).to be == false
			task.cancel
			task.wait

			record = handover.stub.record_for(0, 7)
			expect(record.status).to be == :signed
			expect(record.continuations).to be == [71]
			expect(handover.page(0, 7).outcome).to be == :signed
			expect(handover).not.to be(:drained?)
		end
	end

	with "continuations" do
		it "keeps an accepted continuation on the old shift with its own receipt" do
			child_page = nil

			parent = old_shift.process(7) do |page|
				page.register_continuation(71)
				page.handoff(71)
				page.commit(handover.stub, continuations: [71])
				child_page = handover.continuation_page(page, 71)
			end
			parent.wait

			old_shift.warehouse(child_page) do |continuation|
				continuation.commit(handover.stub)
			end
			old_shift.settle

			parent_record = handover.stub.record_for(0, 7)
			continuation_record = handover.stub.record_for(0, 71, 7)
			expect(continuation_record).not.to be_nil
			expect(continuation_record.claim).not.to be == parent_record.claim
			expect(continuation_record.continuation_of).to be == 7
		end

		it "does not let a draft continuation reserve the slot" do
			task = old_shift.process(7) do |page|
				page.draft(continuations: [71])
				raise Async::Cancel.new("test")
			end

			expect{task.wait}.to raise_exception(Async::Cancel)
			expect(handover.slot(:source).holders).to be(:empty?)
		end
	end

	with "#withdraw_waiter" do
		it "withdraws a queued claimant without holding the slot" do
			gate = Thread::Queue.new

			old_shift.process(7) {|page| gate.pop; page.commit(handover.stub)}
			queued = old_shift.process(8) {|page| page.commit(handover.stub)}

			sleep(0.001)
			expect(handover.withdraw_waiter(old_shift, 8)).to be == true
			queued.wait

			expect(handover.stub.record_for(0, 8).status).to be == :withdrawn
			expect(handover.slot(:source).held_by?(handover.page(0, 8))).to be == false

			gate.push(true)
			old_shift.settle
			expect(handover).to be(:drained?)
		end
	end

	with "observers" do
		it "replays the observer's own last position and isolates failures" do
			gate_x = handover.gate(:source, as: :x)
			gate_y = handover.gate(:source, as: :y)

			old_shift.process(7) {|page| page.commit(handover.stub)}
			old_shift.settle

			expect(handover.observe(:source, as: :x)).to be == 7
			expect(handover.observe(:source, as: :x)).to be == 7
			expect(handover.observe(:source, as: :y)).to be == 7

			gate_y.fail!(RuntimeError.new("boom"))
			expect{handover.observe(:source, as: :y)}.to raise_exception(RuntimeError)
			expect(handover.observe(:source, as: :x)).to be == 7
			expect(gate_x.last_position).to be == 7
		end
	end

	with "unrelated primitives" do
		it "keeps Semaphore and Barrier behaviour unchanged" do
			semaphore = Async::Semaphore.new(1)
			barrier = Async::Barrier.new
			order = []

			2.times do |index|
				semaphore.async(parent: barrier) do
					order << [:enter, index]
					sleep(0.001)
					order << [:leave, index]
				end
			end
			barrier.wait

			expect(order).to be == [[:enter, 0], [:leave, 0], [:enter, 1], [:leave, 1]]
			expect(semaphore).to be(:empty?)
		end
	end
end
