# frozen_string_literal: true

require 'test/unit'
require 'ox'

class CancellationSchedulerTest < Test::Unit::TestCase
  XML = <<~XML.freeze
    <root>
      <group>
        <bin id="A"><item/></bin>
        <bin id="B"><item/></bin>
      </group>
      <group>
        <bin id="A"><item/></bin>
        <bin id="B"><item/></bin>
      </group>
    </root>
  XML

  REGISTRATIONS = {
    'LA' => 'group[0]/bin[@id=A]/item[0]',
    'LB' => 'group[0]/bin[@id=B]/item[0]',
    'RA' => 'group[-1]/bin[@id=A]/item[0]',
    'RB' => 'group[-1]/bin[@id=B]/item[0]'
  }.freeze

  TICKETS = [
    {
      id: 'A', station: 'A', bin: 'A', mode: :object,
      receiver: 'group[-1]/bin[@id=A]',
      object_path: 'group[-1]/bin[@id=A]/item[0]', approved: ['RA']
    },
    {
      id: 'B', station: 'B', bin: 'B', mode: :object,
      receiver: 'group[-1]/bin[@id=B]',
      object_path: 'group[-1]/bin[@id=B]/item[0]', approved: ['RB']
    }
  ].freeze

  GROUP = {
    id: 'G', mode: :path, path: 'group[0]', approved: %w[LA LB]
  }.freeze

  def test_minimal_mirror_positions
    result = Ox::CancellationScheduler.analyze(
      XML,
      registrations: REGISTRATIONS,
      tickets: TICKETS,
      group_ticket: GROUP
    )

    assert_equal [0, 2, 2], result.counts
    assert_nil result.positions[0].sequence

    [1, 2].each do |position|
      replay = result.replay(position)
      assert replay[:ok]
      assert_equal TICKETS.size + 1, replay[:steps].size
      assert replay[:steps].all?(&:ok)
    end
  end

  def test_caller_tree_is_not_mutated
    tree = Ox.parse(XML)
    before = Ox.dump(tree)

    result = Ox::CancellationScheduler.analyze(
      tree,
      registrations: REGISTRATIONS,
      tickets: TICKETS,
      group_ticket: GROUP
    )
    result.replay(1)

    assert_equal before, Ox.dump(tree)
  end
end
