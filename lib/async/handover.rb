# frozen_string_literal: true

# Released under the MIT License.
# Copyright, 2026, by Samuel Williams.

require_relative "list"
require_relative "barrier"
require_relative "task"

module Async
	# A two-shift handover primitive built from the same fibre scheduler
	# primitives as {Semaphore} and {Barrier}, without changing either of them.
	#
	# A physical source position ("slot") can only be held by one page chain at
	# a time. A shift owns the pages registered while it is current. Sealing a
	# shift does not disturb pages already registered, even ones still queued
	# outside of a {Semaphore}; registrations made afterwards belong to the
	# next shift. An old page may register continuation pages that keep the
	# same physical slot on the old shift after the parent page lets it go.
	#
	# Page work runs under {Task#defer_cancel}: the first cancel received while
	# the page is still in its sealed section is only recorded and the slot
	# stays with the old page; a second cancel interrupts immediately. If the
	# local stub has already recorded the page, the page is signed exactly as
	# the stub says and accepted continuations stay with the old shift. A page
	# that only wrote a draft is signed as withdrawn, and provisional draft
	# continuation numbers never reserve the slot.
	class Handover
		# Raised when a queued claimant withdraws before it acquires a slot.
		class Withdrawn < StandardError
		end

		# A single physical source position shared by both shifts.
		class Slot
			# @parameter key [Object] The identity of the physical position.
			def initialize(key)
				@key = key
				@holders = []
			end

			# @attribute [Object] The identity of the physical position.
			attr :key

			# @attribute [Array(Page)] Pages currently holding this position.
			attr :holders

			# @returns [Boolean] Whether no page currently holds the position.
			def empty?
				@holders.empty?
			end

			# @returns [Boolean] Whether the given page currently holds the position.
			def held_by?(page)
				@holders.include?(page)
			end

			# Attach a page. Independent claimants never overlap: a second
			# page may only attach when the slot is free, or when it is a
			# continuation of a page already holding the position.
			def attach(page)
				unless empty? or held_by?(page) or page.continuation?
					raise "Slot #{@key.inspect} is already held by another page!"
				end

				@holders << page unless held_by?(page)
			end

			def detach(page)
				@holders.delete(page)
			end

			def to_s
				"#<#{self.class} key=#{@key.inspect} holders=#{@holders.size}>"
			end

			alias inspect to_s
		end

		# A FIFO queue node, mirroring the private FibreNode in {Semaphore}.
		# Resuming is scheduler mediated, so it works while the fibre is
		# blocked inside the reactor.
		class Waiter < List::Node
			def initialize(fiber, shift, number)
				@fiber = fiber
				@shift = shift
				@number = number
				@withdrawn = false
			end

			attr :shift
			attr :number
			attr_accessor :slot

			def withdrawn?
				@withdrawn
			end

			def matches?(shift, number)
				@shift.equal?(shift) and @number == number
			end

			def resume
				Fiber.scheduler.resume(@fiber) if @fiber.alive?
			end

			# Remove a still-queued claimant; it wakes with {Withdrawn}.
			def withdraw
				@withdrawn = true
				Fiber.scheduler.resume(@fiber) if @fiber.alive?
			end
		end

		# Per-observer, re-openable observation point for a slot. Each
		# observer only ever receives positions it itself signed for; a
		# failed read is delivered to that observer's own inbox and never
		# touches peers.
		class ObserverGate
			Failure = Struct.new(:error)

			def initialize
				@inbox = Thread::Queue.new
				@last_position = nil
			end

			# @returns [Object, Nil] The last position this observer signed.
			attr :last_position

			# Wait for the next signed position. Re-opening without a new
			# signal replays the last position instead of blocking, so an
			# observer can never miss its own last sign-off.
			def wait
				message =
					if @inbox.empty?
						return @last_position unless @last_position.nil?

						@inbox.pop
					else
						@inbox.pop
					end

				if message.is_a?(Failure)
					raise message.error
				end

				@last_position = message
				return message
			end

			def signal(position)
				@inbox.push(position)
			end

			# Fail only fibres waiting on this gate. Other observers have
			# their own gates and keep their last position.
			def fail!(error)
				@inbox.push(Failure.new(error))
			end
		end

		# The local stub ("存根"). Every signed page is recorded under a
		# unique claim number and can be verified by that number afterwards.
		# A commit hook may suspend the calling fibre after the record has
		# been written but before the call returns, modelling a stub that has
		# accepted the page while the old fibre has not resumed yet.
		class Stub
			Record = Struct.new(
				:claim, :page, :shift, :continuations, :draft_continuations,
				:status, :continuation_of, keyword_init: true
			)

			def initialize
				@records = {}
				@by_page = {}
				@next_claim = 1
				@mutex = Thread::Mutex.new
			end

			# @attribute [Hash(Integer, Record)] All records keyed by claim number.
			attr :records

			# Look a record up by its claim number ("领取号").
			def [](claim)
				@records[claim]
			end

			# Look a record up by page key.
			def page(key)
				@by_page[key]
			end

			# @returns [Record, Nil] The record for a shift/page pair.
			def record_for(shift_index, number, continuation_of = nil)
				@records.each_value do |record|
					if record.shift == shift_index and
						record.page == number and
						record.continuation_of == continuation_of
						return record
					end
				end

				nil
			end

			# @returns [Array(Record)] Records in the order they were accepted.
			def accepted_order
				@records.values.sort_by(&:claim)
			end

			# Record a signed page.
			#
			# @parameter page [Page] The page being warehoused.
			# @parameter continuations [Array(Integer)] Continuation numbers actually accepted.
			# @parameter after_commit [Proc, Nil] Hook invoked after the record is written and any slot bookkeeping has run; it may suspend the calling fibre.
			# @yields [Record] Optional block invoked immediately after the record is written, before {#after_commit}, so slot bookkeeping cannot be skipped by a suspended fibre.
			# @returns [Record] The stub record, whose claim number is the receipt.
			def submit(page, continuations, after_commit: nil, &committed)
				record = @mutex.synchronize do
					claim = @next_claim
					@next_claim += 1

					entry = Record.new(
						claim: claim,
						page: page.number,
						shift: page.shift_index,
						continuations: continuations.dup,
						draft_continuations: page.draft_continuations.dup,
						status: :signed,
						continuation_of: page.parent&.number
					)

					@records[claim] = entry
					@by_page[page.key] = entry
					entry
				end

				# The record is durable first; slot bookkeeping runs while the
				# fibre is still guaranteed to be running:
				committed&.call(record)

				# The stub has accepted the page; the fibre may now be paused
				# before this method returns:
				after_commit&.call(record)

				return record
			end

			# Record a page that was withdrawn before it was committed. Draft
			# continuation numbers are kept for audit but never accepted and
			# never reserve a slot.
			def withdraw(page, draft_continuations = [])
				@mutex.synchronize do
					claim = @next_claim
					@next_claim += 1

					record = Record.new(
						claim: claim,
						page: page.number,
						shift: page.shift_index,
						continuations: [],
						draft_continuations: draft_continuations.dup,
						status: :withdrawn,
						continuation_of: page.parent&.number
					)

					@records[claim] = record
					@by_page[page.key] = record

					return record
				end
			end
		end

		# A page ("页") working through a slot.
		class Page
			def initialize(handover, number, shift, slot = nil, parent: nil)
				@handover = handover
				@number = number
				@shift = shift
				@slot = slot
				@parent = parent
				@key = [shift.index, number, parent&.number]
				@registered_continuations = []
				@draft_continuations = []
				@accepted_continuations = nil
				@outcome = :queued
				@task = nil
			end

			# @attribute [Integer] Page number within the shift.
			attr :number

			# @attribute [Shift] Shift this page belongs to.
			attr :shift

			# @attribute [Slot, Nil] Physical position held by this page.
			attr :slot

			# @attribute [Page, Nil] Parent page for a continuation page.
			attr :parent

			# @attribute [Task, Nil] The task processing this page.
			attr_accessor :task

			# @attribute [Array(Integer)] Continuation numbers registered so far.
			attr :registered_continuations

			# @attribute [Array(Integer)] Continuation numbers actually accepted.
			attr_accessor :accepted_continuations

			# @attribute [Array(Integer)] Provisional continuation numbers from a draft.
			attr :draft_continuations

			# @attribute [Symbol] :queued, :running, :signed, :withdrawn, :reserved or :rejected.
			attr_accessor :outcome

			# A stable identity even if page numbers repeat across shifts.
			attr :key

			# @returns [Boolean] Whether this page continues another page.
			def continuation?
				!@parent.nil?
			end

			# @returns [Integer] Index of the owning shift.
			def shift_index
				@shift.index
			end

			# @returns [Boolean] Whether a stub record has been written.
			def committed?
				!@accepted_continuations.nil?
			end

			def attach_slot(slot)
				@slot = slot
				slot.attach(self)
				@outcome = :running
			end

			# Write a draft. Draft continuation numbers are provisional: they
			# are not registered against the slot and never reserve it.
			def draft(continuations: [])
				raise "Page #{@number} has already been committed!" if committed?

				@draft_continuations = continuations.dup
			end

			# Register a continuation page against the same physical slot.
			# The continuation belongs to this page's shift. Registration
			# alone does not hold the slot; it is reserved only when handed
			# off or accepted at commit time, so a mere draft cannot reserve
			# anything.
			#
			# @returns [Page] The pre-registered continuation page.
			def register_continuation(number)
				@handover.register_continuation(self, number)
			end

			# Hand the slot to a registered continuation before this page has
			# finished its own paperwork. The continuation co-holds the slot,
			# so releasing this page later leaves the position with the old
			# shift rather than freeing it for the next shift.
			#
			# @returns [Page] The continuation page now holding the slot.
			def handoff(number)
				@handover.handoff_continuation(self, number)
			end

			# Commit the page to the local stub.
			#
			# @parameter stub [Stub] The stub to write to.
			# @parameter continuations [Array(Integer)] Subset of registered continuations actually accepted.
			# @parameter after_commit [Proc, Nil] Hook allowed to suspend after the record and slot bookkeeping are durable.
			# @yields [Record] Alternative way to pass the +after_commit+ hook.
			# @returns [Stub::Record] The stub record, whose claim number is the receipt.
			def commit(stub, continuations: @draft_continuations, after_commit: nil, &block)
				after_commit ||= block

				unknown = continuations - @registered_continuations
				unless unknown.empty?
					raise ArgumentError, "Unknown continuation(s) for page #{@number}: #{unknown.inspect}"
				end

				record = stub.submit(self, continuations, after_commit: after_commit) do |stub_record|
					@accepted_continuations = stub_record.continuations.dup
					@outcome = :signed

					# A continuation that was handed off earlier is only kept if
					# it is actually accepted now; anything else gives the slot
					# back so the position can drain:
					@registered_continuations.each do |number|
						unless continuations.include?(number)
							@handover.reject_continuation(self, number)
						end
					end

					# Accepted continuations hold the slot and keep their own
					# independent receipt even if warehoused later. This runs
					# before any suspending hook, so a cancel arriving while the
					# old fibre is paused cannot undo the reservation:
					continuations.each do |number|
						child = @handover.continuation_page(self, number)
						@slot.attach(child) unless @slot.held_by?(child)
						child.outcome = :reserved
					end
				end

				return record
			end

			def to_s
				"#<#{self.class} shift=#{@shift.index} page=#{@number} slot=#{@slot&.key.inspect}#{continuation? ? " of #{@parent.number}" : ""}>"
			end

			alias inspect to_s
		end

		# One shift. Two shifts rotate through the same slot list, but each
		# shift gets its own intake clerk ("入库员") and stocktake clerk
		# ("盘点员").
		class Shift
			def initialize(handover, index)
				@handover = handover
				@index = index
				@intake = Barrier.new
				@stocktake = Barrier.new
				@intake_active = 0
				@stocktake_active = 0
				@sealed = false
			end

			# @attribute [Integer] Zero-based shift index.
			attr :index

			# @attribute [Barrier] Tasks run by the intake clerk.
			attr :intake

			# @attribute [Barrier] Tasks run by the stocktake clerk.
			attr :stocktake

			# @returns [Boolean] Whether this shift has been sealed.
			def sealed?
				@sealed
			end

			def seal!
				@sealed = true
			end

			# @returns [Boolean] Whether the intake clerk still has running tasks.
			def intake_busy?
				@intake_active > 0
			end

			# @returns [Boolean] Whether the stocktake clerk still has running tasks.
			def stocktake_busy?
				@stocktake_active > 0
			end

			# Acquire a physical slot and process a page under
			# {Task#defer_cancel}. The block receives the {Page}; the first
			# cancel inside the block is deferred, the second interrupts
			# immediately. A claimant that withdraws while queued finishes as
			# a withdrawal without ever holding a slot.
			#
			# @parameter continuation_of [Integer, Nil] When given, the page continues an old page and inherits its slot.
			# @returns [Task] The task processing the page.
			def process(number, continuation_of: nil, parent: Task.current, &block)
				@intake.async(parent: parent) do |task|
					@intake_active += 1

					task.defer_cancel do
						# A withdrawn claimant still gets an orderly ensure so
						# its withdrawal is written to the stub:
						page = @handover.register(self, number, continuation_of)
						page.task = task

						begin
							yield(page, task) unless page.outcome == :withdrawn
						rescue Cancel
							@handover.finalize_cancel(page)
							raise
						ensure
							@handover.finish(page)
						end
					end
				ensure
					@intake_active -= 1
				end
			end

			# Run stocktake work under the independent stocktake clerk.
			#
			# @yields [Task] The clerk task running the block.
			# @returns [Task]
			def perform_stocktake(number, parent: Task.current, &block)
				@stocktake.async(parent: parent) do |task|
					@stocktake_active += 1
					block.call(task)
				ensure
					@stocktake_active -= 1
				end
			end

			# Warehouse a continuation page that was registered earlier,
			# possibly after its parent page has already ended. It keeps the
			# slot hold it inherited and writes its own stub receipt.
			#
			# @returns [Task]
			def warehouse(page, parent: Task.current, &block)
				@intake.async(parent: parent) do |task|
					@intake_active += 1
					page.task = task
					page.outcome = :running

					task.defer_cancel do
						begin
							yield(page, task)
						rescue Cancel
							@handover.finalize_cancel(page)
							raise
						ensure
							@handover.finish(page)
						end
					end
				ensure
					@intake_active -= 1
				end
			end

			# Wait until both clerks have signed off every task of this shift.
			def wait
				@intake.wait
				@stocktake.wait
			end

			# Wait until both clerks are idle, swallowing the cancels that a
			# deliberately cancelled page leaves behind in its barrier.
			#
			# @returns [void]
			def settle
				until !intake_busy? and !stocktake_busy?
					begin
						@intake.wait
					rescue Cancel
					end

					begin
						@stocktake.wait
					rescue Cancel
					end
				end
			end

			# @returns [Boolean] Whether both clerks have signed off this shift.
			def complete?
				!intake_busy? and !stocktake_busy?
			end
		end

		# Build a handover over a shared list of physical source positions.
		#
		# @parameter slots [Array(Object)] Identities of the physical positions.
		# @parameter parent [Task | Nil] Parent for clerk tasks.
		def initialize(slots = [:default], parent: nil)
			@slots = slots.map {|key| Slot.new(key)}
			@slot_by_key = @slots.each_with_object({}) {|slot, table| table[slot.key] = slot}

			# One FIFO queue shared by every physical position and both shifts.
			# Only the head may take a free slot, which preserves strict order
			# even when several positions exist.
			@waiting = List.new

			@shifts = [Shift.new(self, 0), Shift.new(self, 1)]
			@current_shift = @shifts.first

			@stub = Stub.new

			@pages = {}
			@continuations = Hash.new {|hash, key| hash[key] = {}}

			@observers = Hash.new {|hash, key| hash[key] = {}}
		end

		# @attribute [Array(Slot)] The shared source positions.
		attr :slots

		# @attribute [List] The shared FIFO queue of waiting claimants.
		attr :waiting

		# @attribute [Stub] The local stub shared by both shifts.
		attr :stub

		# @attribute [Shift] The shift accepting new registrations.
		attr :current_shift

		# @returns [Shift] The shift at the given index.
		def shift(index)
			@shifts.fetch(index)
		end

		# @returns [Slot] The slot for a physical position identity.
		def slot(key)
			@slot_by_key.fetch(key)
		end

		# @returns [Page, Nil] The top-level page registered for a shift/number pair.
		def page(shift_index, number)
			@pages[[shift_index, number, nil]]
		end

		# Seal the current shift and rotate to the next one. Pages already
		# registered stay with the old shift; new registrations go to the next.
		#
		# @returns [Shift] The new current shift.
		def seal
			old = @current_shift
			old.seal!
			@current_shift = @shifts.find {|shift| shift != old}

			return @current_shift
		end

		# Register and acquire a slot for a page. The acquisition happens on
		# the calling (clerk) fibre and blocks in FIFO order; a claimant tagged
		# with the old shift keeps its place across a seal.
		def register(shift, number, continuation_of = nil)
			if continuation_of
				parent_page = page(shift.index, continuation_of) or
					raise ArgumentError, "Unknown parent page #{continuation_of.inspect} in shift #{shift.index}"

				# Registration alone never reserves the slot; the continuation
				# holds it only via Page#handoff or acceptance at commit time.
				return register_continuation(parent_page, number)
			end

			page = Page.new(self, number, shift)
			@pages[page.key] = page

			begin
				slot = acquire_slot(page)
			rescue Withdrawn
				# The claimant was pulled out of the queue before it ever held
				# the slot; return it so the clerk runs its orderly finish.
				page.outcome = :withdrawn
				return page
			end

			# The slot is attached inside +acquire_slot+ before any other
			# claimant can be woken, so it can never be double-booked.
			return page
		end

		# Pre-register a continuation of an existing page. The continuation
		# belongs to the same shift as its parent and shares the physical slot
		# identity, but only holds it once handed off or accepted.
		def register_continuation(parent_page, number)
			table = @continuations[parent_page.key]
			raise ArgumentError, "Continuation #{number} is already registered!" if table.key?(number)

			child = Page.new(self, number, parent_page.shift, parent_page.slot, parent: parent_page)
			table[number] = child
			parent_page.registered_continuations << number
			@pages[child.key] = child

			return child
		end

		# @returns [Page, Nil] The registered continuation page.
		def continuation_page(parent_page, number)
			@continuations[parent_page.key][number]
		end

		# Attach a registered continuation to the slot before the parent exits.
		def handoff_continuation(parent_page, number)
			child = continuation_page(parent_page, number) or
				raise ArgumentError, "Continuation #{number} was never registered!"

			parent_page.slot.attach(child)
			child.outcome = :reserved

			return child
		end

		# Release a continuation that was handed off but is not actually
		# accepted when the parent commits. The parent still holds the slot at
		# this point, so no waiting claimant is woken prematurely.
		def reject_continuation(parent_page, number)
			child = continuation_page(parent_page, number)
			return if child.nil?

			if parent_page.slot.held_by?(child)
				parent_page.slot.detach(child)
			end

			child.outcome = :rejected
		end

		# Withdraw a claimant that is still queued. It never holds a slot and
		# therefore cannot block the next shift.
		#
		# @returns [Boolean] Whether a queued claimant was found and withdrawn.
		def withdraw_waiter(shift, number)
			@waiting.each do |node|
				if node.is_a?(Waiter) and node.matches?(shift, number)
					@waiting.remove?(node)
					node.withdraw
					return true
				end
			end

			false
		end

		# Observe a slot. Each observer identity gets its own gate; re-opening
		# the observation replays the last position that observer itself
		# signed, and a failed read only raises into that observer.
		def observe(slot_key, as:)
			gate(slot_key, as: as).wait
		end

		# @returns [ObserverGate] The gate for a given slot/observer pair.
		def gate(slot_key, as:)
			slot = slot(slot_key)
			@observers[slot.key][as] ||= ObserverGate.new
		end

		# Signal a signed position to every observer of a slot. A failure can
		# afterwards be injected into one gate without affecting the others.
		def signal_position(slot, position)
			@observers[slot.key].each_value do |observer_gate|
				observer_gate.signal(position)
			end
		end

		# @returns [Boolean] Whether every slot is free of page holds.
		def drained?
			@slots.all?(&:empty?)
		end

		# Invoked by a page task when a Cancel has interrupted it.
		def finalize_cancel(page)
			return if page.nil?

			record = @stub.page(page.key)

			if record&.status == :signed
				# The stub is authoritative: the page is signed exactly as
				# recorded and accepted continuations stay with the old shift.
				page.accepted_continuations = record.continuations.dup
				page.outcome = :signed
			else
				# Only a draft (or nothing) was committed. Any continuation that
				# was handed off prematurely is released now, because it was
				# never actually accepted; the slot then drains through ensure.
				page.registered_continuations.each do |number|
					reject_continuation(page, number)
				end

				page.accepted_continuations = nil
				page.outcome = :withdrawn
			end
		end

		# Invoked by every page task in its ensure clause.
		def finish(page)
			return if page.nil?

			record = @stub.page(page.key)

			if page.outcome == :queued or page.outcome == :running
				if record&.status == :signed
					page.outcome = :signed
				else
					page.outcome = :withdrawn
					@stub.withdraw(page, page.draft_continuations) unless record
				end
			elsif page.outcome == :withdrawn and record.nil?
				@stub.withdraw(page, page.draft_continuations)
			end

			release_page(page)
		end

		protected

		# FIFO acquisition across every physical position and both shifts.
		# Only the queue head may claim a free position, so a position is
		# never lent to two pages at once and strict ordering is preserved.
		# The node stays in the list while blocked, exactly like {Semaphore};
		# it is removed by the claimant's own ensure clause.
		def acquire_slot(page)
			waiter = Waiter.new(Fiber.current, page.shift, page.number)
			@waiting.append(waiter)

			begin
				loop do
					raise Withdrawn, "Claimant withdrew before acquiring a slot!" if waiter.withdrawn?

					if @waiting.first.equal?(waiter)
						slot = @slots.find(&:empty?)
						if slot
							# Attach before leaving the loop, so the position is
							# marked held before any queued claimant is resumed.
							page.attach_slot(slot)
							waiter.slot = slot
							break
						end
					end

					Fiber.scheduler.transfer
				end
			ensure
				@waiting.remove?(waiter)

				# Another position may already be free; pass the wake-up down
				# the queue so a freed position is never left unused while a
				# claimant sleeps.
				pump_waiting
			end

			return waiter.slot
		end

		def release_page(page)
			return if page.slot.nil?
			return unless page.slot.held_by?(page)

			page.slot.detach(page)
			signal_position(page.slot, page.number)

			# Only wake a claimant when a position is genuinely free. A
			# continuation still holding the slot keeps the next shift waiting.
			pump_waiting if page.slot.empty?
		end

		# Resume the queue head when at least one position is free. The
		# resumed claimant re-checks its place and either acquires or sleeps
		# again, and chains the wake-up via {#acquire_slot}'s ensure clause.
		def pump_waiting
			node = @waiting.first
			return unless node.is_a?(Waiter)
			return unless @slots.any?(&:empty?)

			node.resume
		end
	end
end
