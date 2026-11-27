A two-shift handover primitive, which lets two shifts rotate through the same list of physical source positions while each shift keeps its own intake and stocktake clerks. It is built from the same fibre scheduler primitives as {Semaphore} and {Barrier} and does not change their behaviour.

A position can only be held by one page chain at a time. Sealing a shift keeps every page already registered on the old shift, even pages still queued outside a {Semaphore}; registrations made afterwards belong to the next shift. An old page may register continuation pages that keep the same physical position on the old shift after the parent page lets it go.

Page work runs under {Task#defer_cancel}: the first cancel is only recorded and the position stays with the old page, while a second cancel interrupts immediately. A page recorded in the local stub is always signed exactly as the stub says; a page that only wrote a draft is signed as withdrawn and its provisional continuation numbers never reserve a position.

## Example

~~~ ruby
require 'async'
require 'async/handover'

Sync do
	# One physical source position shared by both shifts:
	handover = Async::Handover.new([:source])
	old_shift = handover.current_shift
	
	# Page 7 holds the position and may reserve a continuation:
	old_shift.process(7) do |page|
		page.register_continuation(71)
		page.handoff(71)
		page.commit(handover.stub, continuations: [71])
	end
	
	# Seal the old shift; new registrations now go to the next shift, but page 7
	# (and its accepted continuation) keep the position until they release it:
	new_shift = handover.seal
	
	new_shift.process(9) do |page|
		# This only starts once the old page chain really lets the position go.
		page.commit(handover.stub)
	end
	
	# A continuation warehoused after its parent ended still gets its own receipt:
	continuation = handover.continuation_page(handover.page(0, 7), 71)
	old_shift.warehouse(continuation) do |page|
		page.commit(handover.stub)
	end
	
	handover.shift(0).wait
	handover.shift(1).wait
	
	# Every signed page is verifiable by its claim number:
	handover.stub.accepted_order.each do |record|
		puts "claim #{record.claim}: page #{record.page} (#{record.status})"
	end
end
~~~

## Cancellation

The first cancel raised while a page is in its sealed section is deferred by {Task#defer_cancel}, so the position remains with the old page until it formally exits or hands the position to a continuation. A second cancel keeps the original immediate-interrupt behaviour. If the stub has already recorded the page, the page is signed per the stub and its accepted continuations stay with the old shift; if only a draft was written, the page is signed as withdrawn and no provisional continuation holds the position.

## Observers

Each observer identity gets an independent gate for a position. Re-opening an observation without a new signal replays the last position that observer itself signed, and a failed read is delivered to that observer's own gate without affecting any other observer.
