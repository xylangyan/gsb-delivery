# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Tests for the layer-aware, measure-enabled linear network
#
# -----------------------------------------------------------------------------

require_relative "test_helper"

class CartesianLinearNetworkTest < Minitest::Test
  def setup
    @factory = RGeo::Cartesian.simple_factory(has_m_coordinate: true)
    @network = RGeo::Cartesian::LinearNetwork.new

    # A and B share the same level and the same horizontal geometry, but
    # run in opposite digitized directions with different M ranges.
    route_a = @factory.line_string([
      @factory.point(0, 0, 0),
      @factory.point(10, 0, 100)
    ])
    route_b = @factory.line_string([
      @factory.point(10, 0, 20),
      @factory.point(0, 0, 80)
    ])

    # C crosses the overlap on another level. The planar crossing must
    # not join C to the level-one network.
    route_c = @factory.line_string([
      @factory.point(5, -5, 0),
      @factory.point(5, 5, 10)
    ])

    # D is a same-level branch that genuinely merges at (5, 0).
    route_d = @factory.line_string([
      @factory.point(5, 5, 0),
      @factory.point(5, 0, 30)
    ])

    @network.allow_transfers_on(:level_one)
    @network.add_route(:A, route_a, :level_one)
    @network.add_route(:B, route_b, :level_one)
    @network.add_route(:D, route_d, :level_one)
    @network.add_route(:C, route_c, :level_two)
  end

  def test_splits_routes_at_connecting_nodes
    assert_equal(6, @network.active_links.size)

    a_links = @network.links_for_route(:A)
    assert_equal(2, a_links.size)
    assert_equal([[0.0, 50.0], [50.0, 100.0]], a_links.map { |link| link.m_range(:A) })
    assert_equal([[0.0, 0.0], [5.0, 0.0]], a_links.map(&:start_node).map(&:coordinates))
    assert_equal([[5.0, 0.0], [10.0, 0.0]], a_links.map(&:end_node).map(&:coordinates))
  end

  def test_opposite_directions_remain_separate_links_with_own_measure
    b_links = @network.links_for_route(:B)
    assert_equal(2, b_links.size)
    assert_equal([[20.0, 50.0], [50.0, 80.0]], b_links.map { |link| link.m_range(:B) })
    assert_equal([[10.0, 0.0], [5.0, 0.0]], b_links.map(&:start_node).map(&:coordinates))
    assert_equal([[5.0, 0.0], [0.0, 0.0]], b_links.map(&:end_node).map(&:coordinates))

    a_links = @network.links_for_route(:A)
    a_links.each do |link|
      refute_includes(link.route_ids, :B)
    end
    b_links.each do |link|
      refute_includes(link.route_ids, :A)
    end
  end

  def test_link_measures_tile_the_original_route_intervals
    {
      A: [0.0, 100.0],
      B: [20.0, 80.0],
      C: [0.0, 10.0],
      D: [0.0, 30.0]
    }.each do |route_id, endpoints|
      ranges = @network.links_for_route(route_id).map { |link| link.m_range(route_id) }
      ranges.each_cons(2) do |left, right|
        assert_equal(left.last, right.first)
      end
      assert_equal(endpoints.first, ranges.first.first)
      assert_equal(endpoints.last, ranges.last.last)
    end
  end

  def test_measure_range_query
    partial = @network.links_for_route(:A, from_m: 10, to_m: 20)
    assert_equal(1, partial.size)
    assert_equal([0.0, 50.0], partial.first.m_range(:A))

    crossing_boundary = @network.links_for_route(:A, from_m: 25, to_m: 60)
    assert_equal(2, crossing_boundary.size)

    assert_equal(2, @network.links_for_route(:A).size)
    assert_equal(1, @network.links_for_route(:A, from_m: 75).size)
    assert_equal(1, @network.links_for_route(:A, to_m: 25).size)
  end

  def test_cross_level_crossing_does_not_connect_or_split
    c_links = @network.links_for_route(:C)
    assert_equal(1, c_links.size)
    assert_equal([0.0, 10.0], c_links.first.m_range(:C))

    assert_empty(@network.transfers_from(:C))
    assert_empty(@network.transfers_from(:A, at: [5.0, 0.0]).select { |route_id| route_id == :C })

    # C stays one unsplit link: nothing on its own level meets it, so no
    # level-two node exists at the planar crossing.
    assert_nil(@network.nodes[[:level_two, [5.0, 0.0]]])
    level_one_node = @network.nodes[[:level_one, [5.0, 0.0]]]
    refute_nil(level_one_node)
  end

  def test_same_level_transfers_follow_the_rules
    reachable = @network.transfers_from(:A, at: [5.0, 0.0]).sort
    assert_equal([:B, :D], reachable)

    d_link = @network.active_links.find { |link| link.supports?(:D) }
    outgoing = @network.connections(d_link).map(&:id).sort
    expected = @network.active_links.select do |link|
      link.level == :level_one && link.start_node.coordinates == [5.0, 0.0]
    end.map(&:id).sort
    assert_equal(expected, outgoing)
  end

  def test_without_a_transfer_rule_crossings_split_but_do_not_connect
    network = RGeo::Cartesian::LinearNetwork.new
    horizontal = @factory.line_string([
      @factory.point(0, 0, 0),
      @factory.point(10, 0, 10)
    ])
    vertical = @factory.line_string([
      @factory.point(5, -5, 0),
      @factory.point(5, 5, 10)
    ])
    network.add_route(:H, horizontal, :isolated)
    network.add_route(:V, vertical, :isolated)

    # The planar crossing still splits both routes into planar-graph
    # pieces, but no route may transfer onto the other.
    assert_equal(4, network.active_links.size)
    assert_empty(network.transfers_from(:H))
    assert_empty(network.transfers_from(:V))
    assert_equal([[0.0, 5.0], [5.0, 10.0]],
                 network.links_for_route(:H).map { |link| link.m_range(:H) })

    crossing = network.nodes[[:isolated, [5.0, 0.0]]]
    refute_nil(crossing)
    # Continuing along H itself is allowed; transferring onto V is not.
    network.connections(network.links_for_route(:H).first).each do |link|
      assert_equal([:H], link.route_ids)
    end
    assert_empty(network.transfers_from(:H, at: [5.0, 0.0]))
  end

  def test_same_direction_overlap_merges_into_one_multi_source_link
    network = RGeo::Cartesian::LinearNetwork.new
    network.allow_transfer_between(:A, :E)
    network.add_route(:A, @factory.line_string([
      @factory.point(0, 0, 0),
      @factory.point(10, 0, 100)
    ]), :level_one)
    network.add_route(:E, @factory.line_string([
      @factory.point(2, 0, 200),
      @factory.point(8, 0, 260)
    ]), :level_one)

    shared = network.active_links.find { |link| link.supports?(:A) && link.supports?(:E) }
    refute_nil(shared)
    assert_equal([[2.0, 0.0], [8.0, 0.0]], [shared.start_node.coordinates, shared.end_node.coordinates])
    assert_equal([20.0, 80.0], shared.m_range(:A))
    assert_equal([200.0, 260.0], shared.m_range(:E))
  end

  def test_removing_b_retires_only_b_links_and_keeps_a_identity
    a_ids_before = @network.links_for_route(:A).map(&:id)
    d_id_before = @network.links_for_route(:D).first.id
    b_links_before = @network.links_for_route(:B)
    b_ids_before = b_links_before.map(&:id)

    @network.remove_route(:B)

    assert_equal(a_ids_before, @network.links_for_route(:A).map(&:id))
    assert_equal(d_id_before, @network.links_for_route(:D).first.id)
    assert_empty(@network.links_for_route(:B))

    b_ids_before.each do |id|
      retired = @network.retired_links[id]
      refute_nil(retired)
      assert(retired.retired)
      assert_includes(retired.route_ids, :B)
      refute_empty(retired.successor_ids)
    end

    # The successors of the retired B links are the A-supported links on
    # the same common geometry.
    b_links_before.each do |link|
      successor = @network.retired_links[link.id].successor_ids.map { |id| @network.links[id] }
      successor.each do |replacement|
        assert_includes(replacement.route_ids, :A)
        assert_equal(Set.new([link.start_node.coordinates, link.end_node.coordinates]),
                     Set.new([replacement.start_node.coordinates, replacement.end_node.coordinates]))
      end
    end
  end

  def test_re_added_route_does_not_disturb_unaffected_links
    a_ids_before = @network.links_for_route(:A).map(&:id)
    b_line = @factory.line_string([
      @factory.point(10, 0, 20),
      @factory.point(0, 0, 80)
    ])
    @network.remove_route(:B)
    @network.add_route(:B, b_line, :level_one)

    assert_equal(a_ids_before, @network.links_for_route(:A).map(&:id))
    assert_equal(2, @network.links_for_route(:B).size)
    assert_equal(6, @network.active_links.size)
  end

  def test_link_geometry_uses_the_cartesian_entry_points
    link = @network.links_for_route(:D).first
    geometry = link.geometry

    assert_kind_of(RGeo::Feature::LineString, geometry)
    assert_equal([5.0, 5.0], geometry.start_point.coordinates)
    assert_equal([5.0, 0.0], geometry.end_point.coordinates)
  end

  def test_route_without_measure_is_rejected
    plain_factory = RGeo::Cartesian.simple_factory
    line = plain_factory.line_string([
      plain_factory.point(0, 0),
      plain_factory.point(1, 1)
    ])

    assert_raises(RGeo::Error::RGeoError) do
      @network.add_route(:X, line, :level_one)
    end
  end

  def test_measure_interpolates_linearly_across_internal_vertices
    network = RGeo::Cartesian::LinearNetwork.new
    network.allow_transfers_on(:level_one)
    route = @factory.line_string([
      @factory.point(0, 0, 0),
      @factory.point(4, 0, 40),
      @factory.point(10, 0, 100)
    ])
    crossing = @factory.line_string([
      @factory.point(2, -2, 0),
      @factory.point(2, 2, 8)
    ])
    network.add_route(:A, route, :level_one)
    network.add_route(:V, crossing, :level_one)

    ranges = network.links_for_route(:A).map { |link| link.m_range(:A) }
    assert_equal([[0.0, 20.0], [20.0, 40.0], [40.0, 100.0]], ranges)
  end

  def test_node_identity_survives_an_unrelated_rebuild
    node_before = @network.nodes[[:level_one, [5.0, 0.0]]]
    @network.remove_route(:B)
    node_after = @network.nodes[[:level_one, [5.0, 0.0]]]

    refute_nil(node_after)
    assert_same(node_before, node_after)
  end

  def test_transfer_query_accepts_a_measure_point
    point = @factory.point(5, 0, 999)
    assert_equal([:B, :D], @network.transfers_from(:A, at: point).sort)
    assert_includes(@network.transfers_from(:A, at: point, include_self: true), :A)
  end

  def test_links_order_along_geometry_not_measure_value
    network = RGeo::Cartesian::LinearNetwork.new
    network.allow_transfers_on(:level_one)
    decreasing = @factory.line_string([
      @factory.point(0, 0, 80),
      @factory.point(10, 0, 20)
    ])
    crossing = @factory.line_string([
      @factory.point(5, -5, 0),
      @factory.point(5, 5, 10)
    ])
    network.add_route(:R, decreasing, :level_one)
    network.add_route(:V, crossing, :level_one)

    links = network.links_for_route(:R)
    assert_equal([[0.0, 0.0], [5.0, 0.0]], links.map { |link| link.start_node.coordinates })
    assert_equal([[50.0, 80.0], [20.0, 50.0]], links.map { |link| link.m_range(:R) })
  end
end
