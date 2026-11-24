require 'concurrent/agent'
require 'concurrent/executor/single_thread_executor'
require 'concurrent/executor/immediate_executor'
require 'concurrent/executor/fixed_thread_pool'
require 'concurrent/atomic/count_down_latch'
require 'concurrent/atomic/atomic_reference'
require 'concurrent/atomic/atomic_fixnum'

module Concurrent

  RSpec.describe Agent, 'tracked receipts' do

    let(:immediate) { Concurrent::ImmediateExecutor.new }

    def wait_until(timeout = 5)
      deadline = Concurrent.monotonic_time + timeout
      Thread.pass until yield || Concurrent.monotonic_time > deadline
      yield
    end

    context 'a single tracked dispatch' do

      it 'returns a receipt that completes with the validated root value' do
        agent   = Agent.new(0)
        receipt = agent.send_via_tracked(immediate) { |value| value + 1 }

        expect(receipt).to be_a Agent::Receipt
        expect(receipt.wait(1)).to be true
        expect(receipt).to be_complete
        expect(receipt).to be_accepted
        expect(receipt.root).to be_completed
        expect(receipt.root.value).to eq 1
        expect(receipt.root.agent).to eq agent
        expect(receipt.items.size).to eq 1
        expect(receipt.outstanding).to be_empty
      end

      it 'reports every tracked variant with a receipt' do
        fast = Agent.new(0)
        io   = Agent.new(0)
        via  = Agent.new(0)

        expect(fast.send_tracked { |v| v + 1 }.wait(1)).to be true
        expect(io.send_off_tracked { |v| v + 1 }.wait(1)).to be true
        expect(via.send_via_tracked(immediate) { |v| v + 1 }.wait(1)).to be true
      end

      it 'returns nil (not a receipt) when the agent is failed' do
        agent = Agent.new(0, error_mode: :fail)
        agent.send_via(immediate) { raise StandardError }
        expect(agent.send_tracked { |v| v + 1 }).to be nil
      end

      it 'raises from the bang variant when the agent is failed' do
        agent = Agent.new(0, error_mode: :fail)
        agent.send_via(immediate) { raise StandardError }
        expect { agent.send_tracked! { |v| v + 1 } }.to raise_error(Agent::Error)
      end
    end

    context 'nested follow-on work' do

      it 'attributes the whole chain (A, then B, then back to A) to one receipt' do
        agent_a = Agent.new(0)
        agent_b = Agent.new(100)
        executor = SingleThreadExecutor.new

        receipt = agent_a.send_via_tracked(executor, agent_a, agent_b) do |value, a, b|
          b.send_via_tracked(executor, a) do |b_value, back|
            back.send_via_tracked(executor) { |a_value| a_value + 7 }
            b_value + 1
          end
          value + 1
        end

        expect(receipt.wait(5)).to be true
        expect(receipt).to be_accepted
        expect(receipt.items.map(&:agent)).to eq [agent_a, agent_b, agent_a]
        expect(receipt.items).to all(be_completed)
        expect(agent_a.value).to eq 8
        expect(agent_b.value).to eq 101
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end

      it 'lists a normally completed C branch' do
        agent_a = Agent.new(0)
        agent_c = Agent.new(0)
        executor = SingleThreadExecutor.new

        receipt = agent_a.send_via_tracked(executor, agent_c) do |value, c|
          c.send_via_tracked(executor) { |c_value| c_value + 100 }
          value + 1
        end

        expect(receipt.wait(5)).to be true
        c_item = receipt.items.find { |item| item.agent == agent_c }
        expect(c_item).to be_completed
        expect(c_item.value).to eq 100
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end

      it 'keeps a second dispatch from the same thread on its own receipt' do
        agent = Agent.new(0)
        executor = SingleThreadExecutor.new

        first  = agent.send_via_tracked(executor) { |v| v + 1 }
        second = agent.send_via_tracked(executor) { |v| v + 10 }

        expect(first).to_not equal second
        expect(first.wait(2) && second.wait(2)).to be true
        expect(first.items.size).to eq 1
        expect(second.items.size).to eq 1
        expect(first.root.value).to eq 1
        expect(second.root.value).to eq 11
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end

      it 'keeps work interleaved from another thread on its own receipt' do
        agent = Agent.new(0)
        executor = SingleThreadExecutor.new
        hold = CountDownLatch.new
        other_receipt = nil

        thread = in_thread do
          other_receipt = agent.send_via_tracked(executor) { hold.wait; 99 }
        end
        wait_until { other_receipt }
        mine = agent.send_via_tracked(executor) { |v| v + 5 }
        hold.count_down
        thread.join

        expect(mine).to_not equal other_receipt
        expect(mine.wait(2) && other_receipt.wait(2)).to be true
        expect(mine.items.size).to eq 1
        expect(other_receipt.items.size).to eq 1
        expect(other_receipt.root.value).to eq 99
        expect(mine.root.value).to eq 104
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end

      it 'runs but does not report an untracked nested send' do
        agent_a = Agent.new(0)
        agent_b = Agent.new(100)

        receipt = agent_a.send_via_tracked(immediate, agent_b) do |value, b|
          b.send_via(immediate) { |b_value| b_value + 1 }
          value + 1
        end

        expect(receipt.wait(1)).to be true
        expect(agent_b.value).to eq 101
        expect(receipt.items.map(&:agent)).to eq [agent_a]
      end
    end

    context 'validation and failure gating' do

      it 'never lets a nested action touch its agent when the enclosing action fails validation' do
        agent_a = Agent.new(0, validator: ->(value) { value != 42 })
        agent_b = Agent.new(0)
        touched = AtomicReference.new(false)

        receipt = agent_a.send_via_tracked(immediate, agent_b) do |_value, b|
          b.send_via_tracked(immediate) do |b_value|
            touched.value = true
            b_value + 1
          end
          42
        end

        expect(receipt.wait(1)).to be true
        expect(receipt).to_not be_accepted
        expect(receipt.root).to be_rejected
        expect(receipt.root.error).to be_a Agent::ValidationError
        nested = receipt.items - [receipt.root]
        expect(nested.size).to eq 1
        expect(nested).to all(be_cancelled)
        expect(nested.first.agent).to eq agent_b
        expect(touched.value).to be false
        expect(agent_b.value).to eq 0
        expect(agent_a.value).to eq 0
      end

      it 'cancels nested work and stores the error when the enclosing action raises' do
        agent_a = Agent.new(0, error_mode: :continue)
        agent_b = Agent.new(0)
        touched = AtomicReference.new(false)
        boom = StandardError.new('boom')

        receipt = agent_a.send_via_tracked(immediate, agent_b) do |_value, b|
          b.send_via_tracked(immediate) do |b_value|
            touched.value = true
            b_value + 1
          end
          raise boom
        end

        expect(receipt.wait(1)).to be true
        expect(receipt.root).to be_rejected
        expect(receipt.root.error).to eq boom
        nested = receipt.items - [receipt.root]
        expect(nested).to all(be_cancelled)
        expect(touched.value).to be false
        expect(agent_b.value).to eq 0
        expect(agent_a).to_not be_failed
      end

      it 'cancels the whole transitive subtree when a middle action is rejected' do
        agent_a = Agent.new(0)
        agent_b = Agent.new(0, validator: ->(value) { value != 5 })
        agent_c = Agent.new(0)
        executor = SingleThreadExecutor.new
        touched_c = AtomicReference.new(false)

        receipt = agent_a.send_via_tracked(executor, agent_b, agent_c) do |_v, b, c|
          b.send_via_tracked(executor, c) do |_bv, cc|
            cc.send_via_tracked(executor) { |cv| touched_c.value = true; cv + 1 }
            5
          end
          1
        end

        expect(receipt.wait(2)).to be true
        expect(receipt.root).to be_completed
        expect(receipt.items.find { |i| i.agent == agent_b }).to be_rejected
        c_item = receipt.items.find { |i| i.agent == agent_c }
        expect(c_item).to be_cancelled
        expect(touched_c.value).to be false
        expect(receipt).to_not be_accepted
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end
    end

    context 'a failed agent holding queued work' do

      let(:agent_a) { Agent.new(0) }
      let(:agent_b) { Agent.new(0, error_mode: :fail) }
      let(:executor_a) { SingleThreadExecutor.new }
      let(:executor_b) { SingleThreadExecutor.new }

      after do
        executor_a.kill
        executor_b.kill
        executor_a.wait_for_termination(5)
        executor_b.wait_for_termination(5)
      end

      def enqueue_held_receipt
        block = CountDownLatch.new
        agent_b.send_via(executor_b) { block.wait; raise StandardError, 'fail' }
        receipt = agent_a.send_via_tracked(executor_a, agent_b) do |value, b|
          b.send_via_tracked(executor_b) { |b_value| b_value + 1 }
          value + 1
        end
        wait_until { receipt.root }
        block.count_down
        wait_until { agent_b.failed? }
        [block, receipt]
      end

      it 'keeps the held job on a normal restart and the receipt eventually completes' do
        _block, receipt = enqueue_held_receipt

        expect(receipt.wait(0.1)).to be false
        expect(receipt).to_not be_complete

        agent_b.restart(50, clear_actions: false)
        expect(receipt.wait(5)).to be true
        expect(receipt).to be_accepted
        expect(agent_b.value).to eq 51
      end

      it 'reports the discarded job as cancelled, naming B, on a clearing restart' do
        _block, receipt = enqueue_held_receipt

        agent_b.restart(7, clear_actions: true)
        expect(receipt.wait(5)).to be true
        expect(receipt).to_not be_accepted
        expect(receipt.root).to be_completed
        cancelled = receipt.items.select(&:cancelled?)
        expect(cancelled.size).to eq 1
        expect(cancelled.first.agent).to eq agent_b
        expect(receipt.outstanding.map(&:agent)).to eq [agent_b]
        expect(agent_b.value).to eq 7
      end
    end

    context 'waiting on a receipt' do

      it 'times out and can be re-queried for the same receipt' do
        agent = Agent.new(0)
        executor = SingleThreadExecutor.new
        hold = CountDownLatch.new

        receipt = agent.send_via_tracked(executor) { hold.wait; 1 }
        expect(receipt.wait(0.05)).to be false
        expect(receipt).to_not be_complete
        expect(receipt.items.any?(&:pending?)).to be true

        hold.count_down
        expect(receipt.wait(5)).to be true
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end

      it 'does not block the agent from processing other work while a receipt is open' do
        agent = Agent.new(0)
        executor = SingleThreadExecutor.new
        hold = CountDownLatch.new

        pending_receipt = agent.send_via_tracked(executor) { hold.wait; 1 }
        unrelated = agent.send_via(executor) { |v| v + 5 }

        hold.count_down
        expect(pending_receipt.wait(5)).to be true
        expect(agent.await_for(5)).to be true
        expect(unrelated).to be true
        expect(agent.value).to eq 6
      ensure
        executor.kill
        executor.wait_for_termination(5)
      end
    end

    context 'isolation under concurrency' do

      it 'keeps every receipt separate across many threads and two agents' do
        agent_a = Agent.new(0)
        agent_b = Agent.new(0)
        pool = FixedThreadPool.new(8)
        count = 40
        barrier = CountDownLatch.new(count)

        receipts = count.times.map do
          in_thread do
            barrier.count_down
            barrier.wait
            agent_a.send_via_tracked(pool, agent_b) do |value, b|
              b.send_via_tracked(pool) { |b_value| b_value + 1 }
              value + 1
            end
          end
        end.map(&:value)

        expect(receipts.uniq.size).to eq count
        expect(receipts.all? { |r| r.wait(10) }).to be true
        expect(receipts).to all(be_accepted)
        expect(receipts.all? { |r| r.items.map(&:agent) == [agent_a, agent_b] }).to be true
        expect(receipts.sum { |r| r.items.size }).to eq 2 * count
        expect(agent_a.value).to eq count
        expect(agent_b.value).to eq count
      ensure
        pool.kill
        pool.wait_for_termination(5)
      end

      it 'only accepts follow-on work whose enclosing action passed validation' do
        agent_a = Agent.new(0, validator: ->(value) { value.even? }, error_mode: :continue)
        agent_b = Agent.new(0)
        pool = FixedThreadPool.new(8)
        count = 30
        barrier = CountDownLatch.new(count)
        b_hits = AtomicFixnum.new(0)

        receipts = count.times.map do |i|
          in_thread do
            barrier.count_down
            barrier.wait
            receipt = agent_a.send_via_tracked(pool, agent_b, i) do |_value, b, idx|
              b.send_via_tracked(pool) { |b_value| b_hits.increment; b_value + 1 }
              idx
            end
            [i, receipt]
          end
        end.map(&:value)

        receipts.each { |_, receipt| expect(receipt.wait(10)).to be true }
        accepted = receipts.select { |i, _| i.even? }
        rejected = receipts.select { |i, _| i.odd? }

        expect(accepted.map(&:last)).to all(be_accepted)
        expect(rejected.map(&:last).map(&:root)).to all(be_rejected)
        expect(rejected.all? { |_, r| (r.items - [r.root]).all?(&:cancelled?) }).to be true
        expect(b_hits.value).to eq accepted.size
        expect(agent_b.value).to eq accepted.size
      ensure
        pool.kill
        pool.wait_for_termination(5)
      end
    end

    context 'ordinary sends remain unchanged' do

      it 'still returns a boolean and supports await' do
        agent = Agent.new(0)
        expect(agent.send { |v| v + 1 }).to be true
        expect(agent.await_for(1)).to be true
        expect(agent.value).to eq 1
      end
    end

    context 'independent batches' do

      it 'lets a later intake settle first while an earlier receipt is still open' do
        agent_a = Agent.new(0)
        agent_b = Agent.new(0, error_mode: :fail)
        executor_a = SingleThreadExecutor.new
        executor_b = SingleThreadExecutor.new
        block = CountDownLatch.new

        agent_b.send_via(executor_b) { block.wait; raise StandardError, 'fail' }
        earlier = agent_a.send_via_tracked(executor_a, agent_b) do |value, b|
          b.send_via_tracked(executor_b) { |b_value| b_value + 1 }
          value + 1
        end
        wait_until { earlier.root }

        later = agent_a.send_via_tracked(executor_a) { |value| value + 100 }
        expect(later.wait(5)).to be true
        expect(later).to be_accepted
        expect(later.root.value).to eq 101

        expect(earlier).to_not be_complete
        expect(earlier.items.size).to eq 2

        block.count_down
        wait_until { agent_b.failed? }
        agent_b.restart(0, clear_actions: true)
        expect(earlier.wait(5)).to be true
        expect(earlier).to_not be_accepted
      ensure
        executor_a.kill
        executor_b.kill
        executor_a.wait_for_termination(5)
        executor_b.wait_for_termination(5)
      end
    end
  end
end
