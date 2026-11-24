require 'concurrent/edge/channel'

RSpec.describe 'Concurrent' do
  describe 'Promises::Channel', edge: true do
    specify "#capacity" do
      channel = Concurrent::Promises::Channel.new 2
      expect(channel.capacity).to be 2
    end

    specify "#to_s" do
      channel = Concurrent::Promises::Channel.new
      expect(channel.to_s).to match(/Channel.*unlimited/)
      channel = Concurrent::Promises::Channel.new 2
      expect(channel.to_s).to match(/Channel.*0.*2/)
      channel.push :value
      expect(channel.to_s).to match(/Channel.*1.*2/)
    end

    specify "#(try_)push(_op)" do
      channel = Concurrent::Promises::Channel.new 1

      expect(channel.size).to eq 0
      expect(channel.try_push(:v1)).to be_truthy
      expect(channel.size).to eq 1
      expect(channel.try_push(:v2)).to be_falsey
      expect(channel.size).to eq 1

      channel = Concurrent::Promises::Channel.new 1
      expect(channel.push(:v1)).to eq channel
      expect(channel.size).to eq 1
      thread = in_thread { channel.push :v2 }
      is_sleeping thread
      expect(channel.size).to eq 1
      channel.pop
      expect(channel.size).to eq 1
      expect(thread.value).to eq channel
      channel.pop
      expect(channel.size).to eq 0

      channel = Concurrent::Promises::Channel.new 1
      expect(channel.push(:v1)).to eq channel
      expect(channel.size).to eq 1
      thread = in_thread { channel.push :v2, 0.01 }
      is_sleeping thread
      expect(channel.size).to eq 1
      expect(thread.value).to eq false
      channel.pop
      expect(channel.size).to eq 0
      expect(channel.push(:v3, 0)).to eq true
      expect(channel.size).to eq 1
      thread = in_thread { channel.push :v2, 1 }
      is_sleeping thread
      channel.pop
      expect(channel.size).to eq 1
      expect(thread.value).to eq true

      channel = Concurrent::Promises::Channel.new 1
      expect(channel.push_op(:v1).value!).to eq channel
      expect(channel.size).to eq 1
      push_op = channel.push_op :v2
      expect(channel.size).to eq 1
      expect(push_op.pending?).to be_truthy
      channel.pop
      expect(channel.size).to eq 1
      expect(push_op.value!).to eq channel
      channel.pop
      expect(channel.size).to eq 0
    end

    specify "#(try_)pop(_op)" do
      channel = Concurrent::Promises::Channel.new 1
      channel.push :v1

      expect(channel.size).to eq 1
      expect(channel.try_pop).to eq :v1
      expect(channel.size).to eq 0
      expect(channel.try_pop).to eq nil
      expect(channel.size).to eq 0

      channel = Concurrent::Promises::Channel.new 1
      channel.push :v1
      expect(channel.pop).to eq :v1
      expect(channel.size).to eq 0
      thread = in_thread { channel.pop }
      is_sleeping thread
      expect(channel.size).to eq 0
      channel.push :v2
      expect(thread.value).to eq :v2
      expect(channel.size).to eq 0

      channel = Concurrent::Promises::Channel.new 1
      channel.push :v1
      expect(channel.pop).to eq :v1
      expect(channel.size).to eq 0
      thread = in_thread { channel.pop 0.01 }
      is_sleeping thread
      expect(channel.size).to eq 0
      expect(thread.value).to eq nil
      channel.push :v2
      expect(channel.size).to eq 1
      expect(channel.pop).to eq :v2
      expect(channel.size).to eq 0
      thread = in_thread { channel.pop 1 }
      is_sleeping thread
      channel.push :v3
      expect(channel.size).to eq 0
      expect(thread.value).to eq :v3
      channel.push :v4
      expect(channel.pop(0)).to eq :v4

      channel = Concurrent::Promises::Channel.new 1
      channel.push :v1
      expect(channel.pop_op.value!).to eq :v1
      expect(channel.size).to eq 0
      pop_op = channel.pop_op
      expect(channel.size).to eq 0
      expect(pop_op.pending?).to be_truthy
      channel.push :v2
      expect(channel.size).to eq 0
      expect(pop_op.value!).to eq :v2
    end

    specify "#(try_)pop(_op)_matching" do
      channel = Concurrent::Promises::Channel.new 2
      channel.push 'junk'
      channel.push :v1

      expect(channel.size).to eq 2
      expect(channel.try_pop_matching(Symbol)).to eq :v1
      expect(channel.size).to eq 1
      expect(channel.try_pop_matching(Symbol)).to eq nil
      expect(channel.size).to eq 1

      channel = Concurrent::Promises::Channel.new 2
      channel.push 'junk'
      channel.push :v1
      expect(channel.pop_matching(Symbol)).to eq :v1
      expect(channel.size).to eq 1
      thread = in_thread { channel.pop_matching(Symbol) }
      is_sleeping thread
      expect(channel.size).to eq 1
      channel.push 'junk'
      channel.pop
      channel.push :v2
      expect(thread.value).to eq :v2
      expect(channel.size).to eq 1

      channel = Concurrent::Promises::Channel.new 2
      channel.push 'junk'
      channel.push :v1
      expect(channel.pop_matching(Symbol)).to eq :v1
      expect(channel.size).to eq 1
      thread = in_thread { channel.pop_matching(Symbol, 0.01) }
      is_sleeping thread
      expect(channel.size).to eq 1
      expect(thread.value).to eq nil
      channel.push :v2
      expect(channel.size).to eq 2
      expect(channel.pop_matching(Symbol)).to eq :v2
      expect(channel.size).to eq 1
      thread = in_thread { channel.pop_matching(Symbol,1) }
      is_sleeping thread
      channel.push :v3
      expect(channel.size).to eq 1
      expect(thread.value).to eq :v3
      channel.push :v4
      expect(channel.pop_matching(Symbol,0)).to eq :v4

      channel = Concurrent::Promises::Channel.new 2
      channel.push 'junk'
      channel.push :v1
      expect(channel.pop_op_matching(Symbol).value!).to eq :v1
      expect(channel.size).to eq 1
      pop_op = channel.pop_op_matching(Symbol)
      expect(channel.size).to eq 1
      expect(pop_op.pending?).to be_truthy
      channel.push :v2
      expect(channel.size).to eq 1
      expect(pop_op.value!).to eq :v2
    end

    specify "#(try_)select(_op)" do
      channel1 = Concurrent::Promises::Channel.new 1
      channel2 = Concurrent::Promises::Channel.new 1

      expect(channel1.try_select(channel2)).to eq nil
      expect(Concurrent::Promises::Channel.try_select([channel1, channel2])).to eq nil
      channel1.push :v1
      expect(channel1.try_select(channel2)).to eq [channel1, :v1]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0

      channel1 = Concurrent::Promises::Channel.new 1
      channel2 = Concurrent::Promises::Channel.new 1
      channel1.push :v1
      expect(Concurrent::Promises::Channel.select([channel1, channel2])).to eq [channel1, :v1]
      channel1.push :v1
      expect(channel1.select(channel2)).to eq [channel1, :v1]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      thread = in_thread { channel1.select(channel2) }
      is_sleeping thread
      expect(channel1.size).to eq 0
      channel2.push :v2
      expect(thread.value).to eq [channel2, :v2]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0

      channel1 = Concurrent::Promises::Channel.new 1
      channel2 = Concurrent::Promises::Channel.new 1
      channel1.push :v1
      expect(channel1.select(channel2)).to eq [channel1, :v1]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      thread = in_thread { channel1.select(channel2, 0.01) }
      is_sleeping thread
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      expect(thread.value).to eq nil
      channel2.push :v2
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 1
      expect(channel2.select(channel1)).to eq [channel2, :v2]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0

      channel1 = Concurrent::Promises::Channel.new 1
      channel2 = Concurrent::Promises::Channel.new 1
      channel1.push :v1
      expect(channel1.select_op(channel2).value!).to eq [channel1, :v1]
      channel1.push :v1
      expect(Concurrent::Promises::Channel.select_op([channel1, channel2]).value!).to eq [channel1, :v1]
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      select_op = channel2.select_op(channel1)
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      expect(select_op.pending?).to be_truthy
      channel2.push :v2
      expect(channel1.size).to eq 0
      expect(channel2.size).to eq 0
      expect(select_op.value!).to eq [channel2, :v2]
    end

    def push_first(push_type, pop_type)
      channel = Concurrent::Promises::Channel.new 0
      message = Object.new

      case push_type
      when :push
        thread = in_thread { channel.push message }
        is_sleeping thread
      when :push_op
        push = channel.push_op message
        expect(push.pending?).to eq true
      else
        raise
      end

      expect(channel.size).to eq 0

      case pop_type
      when :try_pop
        expect(channel.try_pop).to eq message
      when :pop
        expect(channel.pop).to eq message
      when :pop_op
        expect(channel.pop_op.value!).to eq message
      else
        raise
      end

      expect(channel.size).to eq 0

      case push_type
      when :push
        expect(thread.value).to eq channel
      when :push_op
        expect(push.value!).to eq channel
      else
        raise
      end
    end

    def pop_first(pop_type, push_type)
      channel = Concurrent::Promises::Channel.new 0
      message = Object.new

      case pop_type
      when :pop
        thread = in_thread { channel.pop }
        is_sleeping thread
      when :pop_op
        pop = channel.pop_op
        expect(pop.pending?).to eq true
      else
        raise
      end

      expect(channel.size).to eq 0

      case push_type
      when :try_push
        expect(channel.try_push message).to eq true
      when :push
        expect(channel.push(message)).to eq channel
      when :push_op
        expect(channel.push_op(message).value!).to eq channel
      else
        raise
      end

      expect(channel.size).to eq 0

      case pop_type
      when :pop
        expect(thread.value).to eq message
      when :pop_op
        expect(pop.value!).to eq message
      else
        raise
      end
    end


    specify 'exchanging' do
      push_first :push, :try_pop
      push_first :push, :pop
      push_first :push, :pop_op
      push_first :push_op, :try_pop
      push_first :push_op, :pop
      push_first :push_op, :pop_op

      pop_first :pop, :try_push
      pop_first :pop, :push
      pop_first :pop, :push_op
      pop_first :pop_op, :try_push
      pop_first :pop_op, :push
      pop_first :pop_op, :push_op

      ch1       = Concurrent::Promises::Channel.new 0
      ch2       = Concurrent::Promises::Channel.new 0
      selection = ch1.select_op(ch2)
      expect(ch2.try_push(:v3)).to be_truthy
      expect(selection.value!).to eq [ch2, :v3]
    end

    specify 'integration' do
      ch1 = Concurrent::Promises::Channel.new
      ch2 = Concurrent::Promises::Channel.new
      ch3 = Concurrent::Promises::Channel.new

      add = -> *_ do
        (ch1.pop_op & ch2.pop_op).then do |a, b|
          if a == :done && b == :done
            :done
          else
            # do not add again until push is done
            ch3.push_op(a + b).then(&add)
          end
        end
      end

      ch1.push_op 1
      ch2.push_op 2
      ch1.push_op 'a'
      ch2.push_op 'b'
      ch1.push_op nil
      ch2.push_op true

      result = Concurrent::Promises.future(&add).run.result
      expect(result[0..1]).to eq [false, nil]
      expect(result[2]).to be_a_kind_of(NoMethodError)
      expect(ch3.pop_op.value!).to eq 3
      expect(ch3.pop_op.value!).to eq 'ab'

      ch1.push_op 1
      ch2.push_op 2
      ch1.push_op 'a'
      ch2.push_op 'b'
      ch1.push_op :done
      ch2.push_op :done

      expect(Concurrent::Promises.future(&add).run.result).to eq [true, :done, nil]
      expect(ch3.pop_op.value!).to eq 3
      expect(ch3.pop_op.value!).to eq 'ab'
    end

    describe '#pop_pair' do
      Slip = Struct.new(:batch, :round, :serial)

      def slip(batch, round, serial)
        Slip.new(batch, round, serial)
      end

      specify 'takes one matching message from each channel' do
        originals = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        original  = slip(:a, 1, 0)
        stamp     = slip(:a, 1, 0)
        originals.push original
        stamps.push stamp

        expect(originals.pop_pair(stamps, 0.5)).to eq [[originals, original], [stamps, stamp]]
        expect(originals.size).to eq 0
        expect(stamps.size).to eq 0
      end

      specify 'waits for the second message' do
        originals = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        original  = slip(:a, 1, 0)
        stamp     = slip(:a, 1, 0)
        originals.push original

        thread = in_thread { originals.pop_pair stamps, 1 }
        is_sleeping thread
        expect(originals.size).to eq 1

        stamps.push stamp
        expect(thread.value).to eq [[originals, original], [stamps, stamp]]
        expect(originals.size).to eq 0
        expect(stamps.size).to eq 0
      end

      specify 'does not pair different batches or review rounds' do
        originals = Concurrent::Promises::Channel.new 2
        stamps    = Concurrent::Promises::Channel.new 2
        originals.push slip(:a, 1, 0)
        originals.push slip(:a, 2, 1)
        stamps.push slip(:b, 1, 2)
        stamps.push slip(:a, 2, 3)

        expect(originals.pop_pair(stamps, 0.1)).to eq [[originals, slip(:a, 2, 1)], [stamps, slip(:a, 2, 3)]]
        expect(originals.size).to eq 1
        expect(stamps.size).to eq 1
      end

      specify 'accepts an explicit pairing key' do
        originals = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        originals.push slip(:a, 1, 0)
        stamps.push slip(:a, 1, 1)

        expect(originals.pop_pair(stamps, :batch, 0.5)).
            to eq [[originals, slip(:a, 1, 0)], [stamps, slip(:a, 1, 1)]]
      end

      specify 'leaves pending pushes and buffered messages if no pair can be taken' do
        originals = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        original  = slip(:a, 1, 0)
        originals.push original
        pending = originals.push_op slip(:b, 1, 1)
        expect(pending.pending?).to be_truthy

        expect(originals.try_pop_pair(stamps, 0.1)).to be_nil
        expect(pending.pending?).to be_truthy
        expect(originals.size).to eq 1
        expect(stamps.size).to eq 0

        stamps.push slip(:b, 1, 2)
        expect(originals.pop_pair(stamps, 0.5)).
            to eq [[originals, slip(:b, 1, 1)], [stamps, slip(:b, 1, 2)]]
        expect(pending).to be_fulfilled
        expect(originals.size).to eq 1
        expect(originals.pop).to eq original
      end

      specify 'a timed out claim does not take messages which arrive later' do
        originals = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        original  = slip(:a, 1, 0)
        stamp     = slip(:a, 1, 0)
        originals.push original

        expect(originals.pop_pair(stamps, 0.01)).to be_nil
        stamps.push stamp
        sleep 0.02

        expect(originals.size).to eq 1
        expect(stamps.size).to eq 1
        expect(originals.pop_pair(stamps, 0.5)).to eq [[originals, original], [stamps, stamp]]
      end

      specify 'one occurrence is claimed only once even with overlapping claims' do
        original1 = Concurrent::Promises::Channel.new 1
        original2 = Concurrent::Promises::Channel.new 1
        stamps    = Concurrent::Promises::Channel.new 1
        original1.push slip(:a, 1, 0)
        original2.push slip(:a, 1, 0)

        claim1 = original1.pop_pair_op stamps
        claim2 = original2.pop_pair_op stamps
        sleep 0.01
        stamps.push slip(:a, 1, 0)
        sleep 0.05

        results = [claim1, claim2].map { |claim| claim.fulfilled? ? claim.value : nil }.compact
        expect(results.size).to eq 1
        expect(stamps.size).to eq 0

        losing_channel = results.first[0][0].equal?(original1) ? original2 : original1
        expect(losing_channel.size).to eq 1
      end

      specify 'keeps the order of ordinary pops and repeated content' do
        originals = Concurrent::Promises::Channel.new 2
        stamps    = Concurrent::Promises::Channel.new 2
        first     = slip(:a, 1, 0)
        second    = slip(:a, 1, 1)
        originals.push first
        originals.push second
        stamps.push slip(:a, 1, 0)
        stamps.push slip(:a, 1, 1)

        expect(originals.pop).to eq first
        expect(originals.pop_pair(stamps, 0.5)).
            to eq [[originals, second], [stamps, slip(:a, 1, 0)]]
      end
    end
  end
end
