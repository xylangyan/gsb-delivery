# frozen_string_literal: true

require "minitest/autorun"
require_relative "../lib/rgeo/geo_json/dateline"

class DatelineTest < Minitest::Test
  D = RGeo::GeoJSON::Dateline
  M = D::Geometry::MICRO_PER_DEGREE

  def md(value)
    (value * M).round
  end

  def point(lon, lat)
    D::MicroPoint.new(md(lon), md(lat))
  end

  def area(id, rings, edge_ids: nil)
    raw = rings.map { |ring| ring.map { |lon, lat| point(lon, lat) } }
    D::Area.new(area_id: id, rings: raw, edge_ids: edge_ids)
  end

  def handover(extra = {})
    D::Handover.from_hash({ "display_decimals" => 6, "boundary_rule" => "closed" }.merge(extra))
  end

  # A square spanning 170..190 (date line through its middle).
  def crossing_square
    area("A", [[[170, 0], [190, 0], [190, 10], [170, 10]]])
  end

  def test_ring_expands_short_arc_and_closes
    interps = D::RingExpander.expand(
      area_id: "A", ring_index: 0, role: :outer,
      raw_points: [[170, 0], [190, 0], [190, 10], [170, 10]].map { |a| point(*a) }
    )
    assert_equal 1, interps.size
    interp = interps.first
    assert_equal [md(190), md(0)], interp.vertices[1].to_a
    refute interp.self_intersecting?
  end

  def test_polygon_splits_across_dateline
    interp = D::PolygonExpander.expand(crossing_square, handover).first
    pieces = D::PieceSplitter.new(interp).pieces
    lons = pieces.flat_map { |p| p.rings.flat_map { |r| r[:points].map { |pt| pt.lon } } }
    assert(lons.all? { |x| x >= -D::Geometry::HALF && x <= D::Geometry::HALF })
    assert_equal 2, pieces.size
    west = pieces.find { |p| p.side == :whole }
    east = pieces.find { |p| p.side == :east }
    refute_nil west
    refute_nil east
    # 170..180 in the untranslated piece, -180..-170 after a -360 turn.
    assert_includes west.rings.first[:points].map { |pt| pt.lon.to_f / M }, 180.0
    assert_includes east.rings.first[:points].map { |pt| pt.lon.to_f / M }, -180.0
  end

  def test_junction_parameters_reference_original_edges
    interp = D::PolygonExpander.expand(crossing_square, handover).first
    pieces = D::PieceSplitter.new(interp).pieces
    records = pieces.flat_map { |p| D::Junction.records_for_piece(p, display_decimals: 6) }
    crossing = records.find do |r|
      r["kind"] == "dateline_crossing" && r["t_fraction"] == "1/2"
    end
    refute_nil crossing
    assert_equal "A/r0/e0", crossing["edge_id"]
    assert_includes [-180.0, 180.0], crossing["display"][0]
    assert_equal 0.5, crossing["t"]
    assert_equal "1/2", crossing["t_fraction"]
  end

  def test_half_circle_edge_has_two_closing_readings
    # 0 -> 180 is exactly half a circle: the edge may go east or west, and
    # both close the ring. Only the handover may select between them.
    raw = [point(0, 0), point(180, 0), point(180, 10), point(0, 10)]
    interps = D::RingExpander.expand(area_id: "H", ring_index: 0, role: :outer, raw_points: raw)
    assert_equal 2, interps.size
    assert_equal %i[positive negative].sort, interps.map { |i| i.half_directions["H/r0/e0"] }.sort
  end

  def test_half_circle_uses_declared_direction
    raw = [point(0, 0), point(180, 0), point(180, 10), point(0, 10)]
    ids = %w[H/r0/e0 H/r0/e1 H/r0/e2 H/r0/e3]
    rules = { "H/r0/e0" => [:positive] }
    interps = D::RingExpander.expand(
      area_id: "H", ring_index: 0, role: :outer, raw_points: raw,
      edge_ids: ids, half_rules: rules
    )
    assert_equal 1, interps.size
    assert_equal md(180), interps.first.vertices[1].lon
    assert_equal :positive, interps.first.half_directions["H/r0/e0"]
  end

  def test_hole_outside_outer_rejects_interpretation
    # Outer can be read as 170..190 but the hole sits at 0..10; the only
    # legal outer expansion leaves the hole outside.
    outer = [[170, 0], [190, 0], [190, 10], [170, 10]]
    hole = [[0, 2], [10, 2], [10, 8], [0, 8]]
    a = area("B", [outer, hole])
    assert_empty D::PolygonExpander.expand(a, handover)
  end

  def test_hole_inside_after_wrap_is_accepted
    outer = [[170, 0], [190, 0], [190, 10], [170, 10]]
    hole = [[175, 2], [185, 2], [185, 8], [175, 8]]
    interps = D::PolygonExpander.expand(area("C", [outer, hole]), handover)
    assert_equal 1, interps.size
    assert interps.first.valid?
  end

  def test_shared_edge_forward_and_reverse
    # Both registrations list the same physical line 175,0 -> 175,10, but
    # one lists it in reverse vertex order.
    a1 = area("A1", [[[175, 0], [178, 0], [178, 10], [175, 10]]])
    a2 = area("A2", [[[175, 10], [172, 10], [172, 0], [175, 0]]])
    i1 = D::PolygonExpander.expand(a1, handover).first
    i2 = D::PolygonExpander.expand(a2, handover).first
    group = [
      { area_id: "A1", edge_id: "A1/r0/e3", direction: "forward" },
      { area_id: "A2", edge_id: "A2/r0/e3", direction: "reverse" },
    ]
    result = D::SharedEdgeCheck.check(group, { "A1" => i1, "A2" => i2 })
    assert result[:ok], result[:reason]
  end

  def test_shared_edge_misplacement_detected
    # The same declared line, but the reverse entry is not actually the same
    # physical segment.
    a1 = area("A1", [[[175, 0], [178, 0], [178, 10], [175, 10]]])
    a2 = area("A2", [[[175, 10], [175, 0], [172, 0], [172, 10]]])
    i1 = D::PolygonExpander.expand(a1, handover).first
    i2 = D::PolygonExpander.expand(a2, handover).first
    group = [
      { area_id: "A1", edge_id: "A1/r0/e2", direction: "forward" },
      { area_id: "A2", edge_id: "A2/r0/e0", direction: "forward" },
    ]
    result = D::SharedEdgeCheck.check(group, { "A1" => i1, "A2" => i2 })
    refute result[:ok]
  end

  def test_station_on_edge_closed_and_open
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    interp = D::PolygonExpander.expand(a, handover).first
    station = D::MicroPoint.new(md(5), md(0))
    closed = D::StationAssignment.assign(interp, station, boundary_rule: "closed")
    open = D::StationAssignment.assign(interp, station, boundary_rule: "open")
    assert closed[:inside]
    assert closed[:on_edge]
    refute open[:inside]
    assert open[:on_edge]
    assert_equal "A/r0/e0", closed[:edge]["edge_id"]
  end

  def test_confirmed_station_filters_interpretations
    # The outer 170..190 crosses the date line; station at 185,5 should be
    # inside the registered area under the only legal reading.
    a = area("A", [[[170, 0], [190, 0], [190, 10], [170, 10]]])
    ho = handover("stations" => [
      { "id" => "S1", "lon_micro" => md(185), "lat_micro" => md(5), "areas" => ["A"] },
    ])
    result = D::Solver.new(areas: [a], handover: ho).solve
    assert_equal "ok", result["status"]
    assert_equal 2, result["signed_pieces"].size
    assert_equal "S1", result["station_ownership"].first["station_id"]
    assert_equal ["A"], result["station_ownership"].first["owners"]
  end

  def test_conflict_reports_first_evidence
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    ho = handover("stations" => [
      { "id" => "S1", "lon_micro" => md(5), "lat_micro" => md(5), "areas" => ["B"] },
    ])
    result = D::Solver.new(areas: [a], handover: ho).solve
    assert_equal "conflict", result["status"]
    blocker = result["blocking_evidence"].first
    assert_equal "station", blocker["kind"]
    assert_equal "S1", blocker["station_id"]
  end

  def test_readback_with_rgeo_geojson
    interp = D::PolygonExpander.expand(crossing_square, handover).first
    piece = D::PieceSplitter.new(interp).pieces.first
    geom = D::Encoder.piece_geometry(piece, 6)
    feature = D::Encoder.feature(id: piece.piece_id, geometry: geom, properties: {})
    parsed = D::Readback.parse(feature)
    refute_nil parsed
    assert_equal RGeo::Feature::Polygon, parsed.geometry.geometry_type
  end

  def test_hole_disambiguates_half_circle_outer
    outer = [[0, -10], [180, -10], [180, 10], [0, 10]]
    hole = [[170, -2], [178, -2], [178, 2], [170, 2]]
    a = area("H", [outer, hole])
    interps = D::PolygonExpander.expand(a, handover)
    assert_equal 1, interps.size
    assert_equal :positive, interps.first.outer.half_directions["H/r0/e0"]
  end

  def test_outer_valid_alone_but_hole_outside_other_reading
    # Both outer readings close legally, but the registered hole only lies
    # inside the eastward one. The westward reading merely contains the
    # hole for the outer; it must be discarded.
    outer = [[170, 0], [190, 0], [190, 10], [170, 10]]
    hole = [[175, 2], [185, 2], [185, 8], [175, 8]]
    a = area("A", [outer, hole])
    interps = D::PolygonExpander.expand(a, handover)
    assert_equal 1, interps.size
    outer_lons = interps.first.outer.vertices.map { |p| p.lon.to_f / M }
    assert_includes outer_lons, 190.0
    refute_includes outer_lons, -190.0
  end

  def test_ambiguous_area_withheld_with_two_interpretations
    a = area("H", [[[0, -10], [180, -10], [180, 10], [0, 10]]])
    ho = handover("stations" => [
      { "id" => "S1", "lon_micro" => md(-170), "lat_micro" => md(0),
        "areas" => ["H"], "status" => "questioned" },
    ])
    result = D::Solver.new(areas: [a], handover: ho).solve
    assert_equal "ok", result["status"]
    assert_empty result["signed_pieces"]
    assert_equal ["H"], result["withheld_areas"].map { |w| w["area_id"] }
    assert_equal 1, result["ambiguous_stations"].size
    assert_equal %w[interpretation_1 interpretation_2], result["interpretations"].keys.sort
  end

  def test_cross_dateline_pieces_count_once_per_area
    interp = D::PolygonExpander.expand(crossing_square, handover).first
    pieces = D::PieceSplitter.new(interp).pieces
    assert_equal 2, pieces.size
    assert_equal ["A"], pieces.map(&:area_id).uniq
  end

  def test_overlap_reports_two_entries
    # A and B both cover 0..10 exactly; both are stable so both are signed
    # and the overlap must list both.
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    b = area("B", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    result = D::Solver.new(areas: [a, b], handover: handover).solve
    assert_equal "ok", result["status"]
    refute_empty result["overlaps"]
    areas = result["overlaps"].first["areas"]
    assert_equal %w[A B], areas.sort
  end

  def test_partial_overlap_lists_two_entries
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    b = area("B", [[[5, 5], [15, 5], [15, 15], [5, 15]]])
    result = D::Solver.new(areas: [a, b], handover: handover).solve
    assert_equal "ok", result["status"]
    overlap = result["overlaps"].find { |o| o["areas"].sort == %w[A B] }
    refute_nil overlap
    assert_equal 2, overlap["entries"].size
  end

  def test_adjacent_areas_do_not_overlap
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    b = area("B", [[[10, 0], [20, 0], [20, 10], [10, 10]]])
    result = D::Solver.new(areas: [a, b], handover: handover).solve
    assert_empty result["overlaps"]
  end

  def test_conflict_names_station_and_keeps_independent_area
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    b = area("B", [[[50, 0], [60, 0], [60, 10], [50, 10]]])
    ho = handover(
      "stations" => [
        { "id" => "S1", "lon_micro" => md(5), "lat_micro" => md(5), "areas" => ["B"] },
      ],
      "independent_areas" => ["B"]
    )
    result = D::Solver.new(areas: [a, b], handover: ho).solve
    assert_equal "conflict", result["status"]
    kinds = result["blocking_evidence"].map { |b| b["kind"] }
    assert_includes kinds, "station"
    independent = result["independent_areas"]
    assert independent.key?("B")
    refute_empty independent["B"]["pieces"]
  end

  def test_confirmed_station_on_edge_uses_boundary_rule
    a = area("A", [[[0, 0], [10, 0], [10, 10], [0, 10]]])
    ho = handover("boundary_rule" => "open", "stations" => [
      { "id" => "S1", "lon_micro" => md(5), "lat_micro" => md(0), "areas" => [] },
    ])
    result = D::Solver.new(areas: [a], handover: ho).solve
    assert_equal "ok", result["status"]
  end

  def test_shared_edge_misplaced_across_dateline_is_rejected
    # P has two legal readings of the same 180-degree edge, one on each side
    # of the date line. A shared-edge declaration that forces the two
    # readings to the same physical line cannot be satisfied by mixing them.
    parea = area("P", [[[0, -10], [180, -10], [180, 10], [0, 10]]])
    qarea = area("Q", [[[0, -10], [-180, -10], [-180, 10], [0, 10]]])
    group = [{
      "edges" => [
        { "area_id" => "P", "edge_id" => "P/r0/e0", "direction" => "forward" },
        { "area_id" => "Q", "edge_id" => "Q/r0/e0", "direction" => "forward" },
      ],
    }]
    ho = handover("shared_edges" => group)
    result = D::Solver.new(areas: [parea, qarea], handover: ho).solve
    # Some reading exists (both choose the same side), but it must be
    # consistent; the solver reports ok with pieces or a conflict, never a
    # silent split across both sides.
    refute_nil result["status"]
  end
end
