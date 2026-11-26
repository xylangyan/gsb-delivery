# frozen_string_literal: true

require 'receipt_analysis_helper'

describe Fluent::Logger::ReceiptAnalysis do

  def message(id, pad)
    Fluent::Logger::ReceiptAnalysis::Message.new(
      number: id, tag: 'tag', record: { 'id' => id, 'p' => 'x' * pad }
    )
  end

  def drive(limit, operations, fixture)
    simulator = Fluent::Logger::ReceiptAnalysis::Simulator.new(limit: limit, fixture: fixture, identifier_width: 12)
    operations.each_with_index { |op, index| simulator.execute(op, index + 1) }
    simulator
  end

  # Pick a pad value that yields the same packed length for A/B/C.
  let(:pad) { 100 }
  let(:a) { message('A', pad) }
  let(:b) { message('B', pad) }
  let(:c) { message('C', pad) }
  let(:size) { a.bytesize }

  let(:prose_fixture) do
    Fluent::Logger::ReceiptAnalysis::Fixture::Callable.new(
      connect_proc: ->(snap) { snap.connection != 1 },
      write_proc: lambda do |snap, bytes|
        if snap.class_complete_count(['A']) >= 1
          :complete
        else
          { outcome: :partial, bytes: [2 * size - 1, bytes.bytesize].min }
        end
      end
    )
  end

  let(:operations) do
    [
      Fluent::Logger::ReceiptAnalysis::Operation.connect(0, 1),
      Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'A', message: a),
      Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'B', message: b),
      Fluent::Logger::ReceiptAnalysis::Operation.post(1, 'C', message: c)
    ]
  end

  describe 'the signed-off short example' do
    it 'connects early at limit S..2S-1 and receives B before failing' do
      simulator = drive(size, operations, prose_fixture)
      expect(simulator.complete_count('A')).to eq(1)
      expect(simulator.complete_count('B')).to eq(0)
      expect(simulator.complete_count('C')).to eq(1)
      expect(simulator.callback_bytes).to eq(2 * size)
    end

    it 'overflows the whole of A and B at limit 2S' do
      simulator = drive(2 * size, operations, prose_fixture)
      expect(simulator.complete_count('A')).to eq(1)
      expect(simulator.complete_count('B')).to eq(0)
      expect(simulator.complete_count('C')).to eq(0)
      expect(simulator.callback_bytes).to eq(3 * size)
    end

    it 'leaves everything pending at limit 3S' do
      simulator = drive(3 * size, operations, prose_fixture)
      expect(simulator.complete_count('A')).to eq(1)
      expect(simulator.complete_count('B')).to eq(0)
      expect(simulator.complete_count('C')).to eq(0)
      expect(simulator.pending_items.map(&:number)).to eq(%w[A B C])
      expect(simulator.callback_bytes).to eq(0)
    end

    it 'certs exactly the contiguous interval that passes every checkpoint' do
      archive = Fluent::Logger::ReceiptAnalysis::Archive.new(
        tag: 'tag', identifier_width: 12, messages: {}, operations: operations,
        fixture: prose_fixture,
        checkpoints: [Fluent::Logger::ReceiptAnalysis::Checkpoint.new(name: 'after_c', number: 'C')],
        requirements: [
          Fluent::Logger::ReceiptAnalysis::Requirement
            .new(checkpoint: Fluent::Logger::ReceiptAnalysis::Checkpoint.new(name: 'after_c', number: 'C'))
            .require_number('A', min: 1).require_number('C', min: 1)
            .require_number('B', max: 1).callback_cap(2 * size)
        ]
      )
      range = (size - 2)..(3 * size)
      analysis = Fluent::Logger::ReceiptAnalysis::Analysis.new(archive)
      # Brute-force ground truth over the same range.
      passing = []
      range.each do |limit|
        simulator = drive(limit, operations, prose_fixture)
        passing << limit if simulator.complete_count('A') >= 1 &&
                             simulator.complete_count('C') >= 1 &&
                             simulator.complete_count('B') <= 1 &&
                             simulator.callback_bytes <= 2 * size
      end
      expected = passing.sort.slice_when { |x, y| y != x + 1 }.map { |g| [g.first, g.last] }
      certification = analysis.certify_intervals(range)
      expect(certification.to_a.map { |interval| [interval.begin, interval.end] }).to eq(expected)
      expect(passing.first).to eq(size - 2)
    end
  end

  describe Fluent::Logger::ReceiptAnalysis::Simulator do    it 'counts a re-sent original once per full receipt on each connection' do
      fixture = Fluent::Logger::ReceiptAnalysis::Fixture::Callable.new(write_proc: ->(_snap, _bytes) { { outcome: :partial, bytes: 0 } })
      ops = [
        Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'A', message: a),
        Fluent::Logger::ReceiptAnalysis::Operation.post(1, 'A', message: a)
      ]
      simulator = drive(1, ops, fixture)
      # Each write fails immediately, so nothing is received.
      expect(simulator.complete_count('A')).to eq(0)
    end

    it 'does not count a partial message as received' do
      fixture = Fluent::Logger::ReceiptAnalysis::Fixture::Callable.new(write_proc: ->(_snap, _bytes) { { outcome: :partial, bytes: 100 } })
      ops = [Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'A', message: a)]
      simulator = drive(1_000, ops, fixture)
      expect(simulator.complete_count('A')).to eq(0)
    end

    it 'separately accounts bytes handed to the overflow callback' do
      fixture = Fluent::Logger::ReceiptAnalysis::Fixture::Callable.new(write_proc: ->(_snap, _bytes) { { outcome: :epipe, bytes: 0 } })
      ops = [Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'A', message: a), Fluent::Logger::ReceiptAnalysis::Operation.post(0, 'B', message: b)]
      simulator = drive(size, ops, fixture)
      expect(simulator.callback_bytes).to eq(2 * size)
      expect(simulator.pending_items).to be_empty
      expect(simulator.complete_count('A')).to eq(0)
    end
  end

  describe 'repeat archives' do
    it 'expands a repeat marker in place with a fixed stride and fresh numbers' do
      hash = {
        'tag' => 'tag',
        'identifier_width' => 6,
        'operations' => [
          { 'kind' => 'post', 'number' => '000001', 'time' => 0 },
          {
            '_repeat' => {
              'count' => 3,
              'stride' => 1,
              'start_time' => 1,
              'first_number' => 2,
              'template' => [
                { 'kind' => 'post', 'time' => 0 },
                { 'kind' => 'post', 'time' => 1 },
                { 'kind' => 'close', 'time' => 2 }
              ]
            }
          },
          { 'kind' => 'close', 'time' => 99 }
        ]
      }
      stream = Fluent::Logger::ReceiptAnalysis::Archive.from_h(hash).operation_stream.to_a
      expect(stream.size).to eq(1 + 3 * 3 + 1)
      posts = stream.select(&:post?)
      expect(posts.map(&:number)).to eq(%w[000001 000002 000003 000004 000005 000006 000007])
      expect(posts.map(&:time)).to eq([0, 0, 1, 1, 2, 2, 3])
      expect(stream.last.kind).to eq(:close)
    end
  end
end
