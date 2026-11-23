# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Tests for byte-preserving WKB exchange planning
#
# -----------------------------------------------------------------------------

require_relative "../test_helper"

class WKBExchangeTest < Minitest::Test # :nodoc:
  # Builds raw WKB fragments so tests can express byte-order, SRID, and
  # dimension combinations that a single RGeo factory cannot hold at once.
  module Builder
    module_function

    def header(type, dimensions: :xy, srid: nil, dialect: :ewkb, little: true)
      has_z = dimensions == :xyz || dimensions == :xyzm
      has_m = dimensions == :xym || dimensions == :xyzm
      code = type
      case dialect
      when :ewkb
        code |= 0x80000000 if has_z
        code |= 0x40000000 if has_m
        code |= 0x20000000 if srid
      when :wkb12
        code += 1000 if has_z
        code += 2000 if has_m
      end
      out = +""
      out << [little ? 1 : 0].pack("C")
      out << [code].pack(little ? "V" : "N")
      out << [srid].pack(little ? "V" : "N") if dialect == :ewkb && srid
      out
    end

    def point(coords, dimensions: :xy, srid: nil, dialect: :ewkb, little: true)
      header(1, dimensions: dimensions, srid: srid, dialect: dialect, little: little) +
        coords.pack(little ? "E*" : "G*")
    end

    def line_string(points, dimensions: :xy, srid: nil, dialect: :ewkb, little: true)
      header(2, dimensions: dimensions, srid: srid, dialect: dialect, little: little) +
        [points.size].pack(little ? "V" : "N") +
        points.flatten.pack(little ? "E*" : "G*")
    end

    def polygon(rings, dimensions: :xy, srid: nil, dialect: :ewkb, little: true)
      out = header(3, dimensions: dimensions, srid: srid, dialect: dialect, little: little)
      out << [rings.size].pack(little ? "V" : "N")
      rings.each do |ring|
        out << [ring.size].pack(little ? "V" : "N")
        out << ring.flatten.pack(little ? "E*" : "G*")
      end
      out
    end

    def collection(children, dimensions: :xy, srid: nil, dialect: :ewkb, little: true)
      header(7, dimensions: dimensions, srid: srid, dialect: dialect, little: little) +
        [children.size].pack(little ? "V" : "N") +
        children.join
    end
  end

  def archive(bytes, srid: nil)
    RGeo::WKRep::WKBExchange::Archive.parse(bytes, srid: srid)
  end

  def reader(name, dialect:, dimensions:, srid: nil, srid_prelude: false, child_srid: false)
    RGeo::WKRep::WKBExchange::Reader.new(
      name: name,
      dialect: dialect,
      dimensions: dimensions,
      srid: srid,
      srid_prelude: srid_prelude,
      child_srid: child_srid
    )
  end

  def plan(archive, manifest, readers = nil, cost_model: nil, **reader_options)
    readers ||= reader_options
    cost_model ||= RGeo::WKRep::WKBExchange::CostModel.new
    RGeo::WKRep::WKBExchange::Planner.new(
      archive: archive,
      manifest: manifest,
      readers: readers,
      cost_model: cost_model
    ).plan
  end

  def test_archive_records_nested_paths_and_inherited_srid
    child = Builder.point([1, 2], dialect: :wkb12)
    inner = Builder.collection([child], dialect: :wkb12)
    outer = Builder.point([3, 4], dialect: :wkb12)
    root = Builder.collection([inner, outer], srid: 4326, dialect: :ewkb)

    archive = archive(root)
    assert_equal 4, archive.nodes.size
    assert_equal ["/", "/0", "/0/0", "/1"], archive.nodes.map(&:path_string)
    assert_equal [[0, 0], [1]], archive.atoms.map(&:path)
    assert_equal [4326, 4326], archive.atoms.map(&:effective_srid)
    assert_equal [false, false], archive.atoms.map(&:explicit_srid?)
    assert_equal [true, true], archive.atoms.map { |node| node.effective_srid == 4326 }

    inner_node = archive.node_at([0])
    assert_equal :geometry_collection, inner_node.geometry_type
    assert_operator inner_node.offset, :>, archive.root.offset
    assert_operator inner_node.header_end, :>=, inner_node.offset + 5
    assert_equal inner_node.offset + 5, inner_node.header_end
  end

  def test_archive_keeps_explicit_child_srid_distinct_from_outer
    child = Builder.point([1, 2], srid: 3857)
    root = Builder.collection([child], srid: 4326)
    archive = archive(root)

    node = archive.atoms.first
    assert_equal 4326, archive.root.effective_srid
    assert_equal 3857, node.explicit_srid
    assert_equal 3857, node.effective_srid
  end

  def test_packet_preserves_atoms_and_collection_order
    bytes = Builder.collection(
      [
        Builder.point([1, 2], dialect: :wkb12),
        Builder.point([3, 4], dialect: :wkb12)
      ],
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :legacy)
    manifest.route("/1", to: :legacy)

    result = plan(archive, manifest,
                  legacy: reader(:legacy, dialect: :wkb12, dimensions: [:xy]))

    assert result.feasible?
    assert_equal 1, result.num_packets
    packet = result.packets.first
    assert_equal [[0], [1]], packet.paths
    assert_equal [true, true], packet.entries.map(&:preserved?)
    assert_equal 0, result.cost
    assert result.verify!
  end

  def test_packet_root_has_fixed_dialect_and_dimensions
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 3], dimensions: :xyz, dialect: :wkb12),
        Builder.point([4, 5, 6], dimensions: :xym, dialect: :wkb12)
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :old)
    manifest.route("/1", to: :old)
    manifest.permit("/1", reader: :old, dimensions: :xyz, axes: { z: :m })

    result = plan(archive, manifest,
                  old: reader(:old, dialect: :wkb12, dimensions: [:xyz], srid: 0),
                  cost_model: RGeo::WKRep::WKBExchange::CostModel.new(rewrite_cost: 1))

    assert result.feasible?
    assert_equal 1, result.num_packets
    packet = result.packets.first
    assert_equal :wkb12, packet.dialect
    assert_equal :xyz, packet.dimensions
    assert_equal [:xyz, :xyz], packet.entries.map(&:output_dimensions)
    assert result.verify!
  end

  def test_mixed_byte_order_is_preserved_when_legal
    little = Builder.point([1, 2], dialect: :wkb12, little: true)
    big = Builder.point([3, 4], dialect: :wkb12, little: false)
    bytes = Builder.collection([little, big], dialect: :wkb12, little: true)
    archive = archive(bytes)

    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :r)
    manifest.route("/1", to: :r)

    result = plan(archive, manifest, r: reader(:r, dialect: :wkb12, dimensions: [:xy]))
    assert result.feasible?
    packet = result.packets.first
    assert_equal [true, true], packet.entries.map(&:preserved?)
    assert_equal little, packet.entries[0].bytes
    assert_equal big, packet.entries[1].bytes
    assert result.verify!
  end

  def test_distinct_explicit_srids_stay_on_children_for_ewkb
    bytes = Builder.collection(
      [
        Builder.point([1, 2], srid: 4326),
        Builder.point([3, 4], srid: 3857)
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :modern)
    manifest.route("/1", to: :modern)

    result = plan(archive, manifest,
                  modern: reader(:modern, dialect: :ewkb, dimensions: [:xy],
                                 srid: 4326, srid_prelude: :ewkb,
                                 child_srid: true))

    assert result.feasible?
    assert_equal 1, result.num_packets
    packet = result.packets.first
    assert_equal [4326, 3857], packet.entries.map(&:expected_srid)
    assert_equal [true, true], packet.entries.map(&:preserved?)
    assert result.verify!
  end

  def test_ewkb_packet_can_carry_outer_srid_and_child_overrides
    bytes = Builder.collection(
      [
        Builder.point([1, 2]),
        Builder.point([3, 4], srid: 3857)
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :modern)
    manifest.route("/1", to: :modern)

    result = plan(archive, manifest,
                  modern: reader(:modern, dialect: :ewkb, dimensions: [:xy],
                                 srid: nil, srid_prelude: :ewkb,
                                 child_srid: true))
    assert result.feasible?
    packet = result.packets.first
    assert_equal [4326, 3857], packet.entries.map(&:expected_srid)
    assert result.verify!
  end

  def test_distinct_explicit_srids_split_for_standard_reader
    bytes = Builder.collection(
      [
        Builder.point([1, 2], srid: 4326),
        Builder.point([3, 4], srid: 3857)
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :standard)
    manifest.route("/1", to: :standard)

    result = plan(archive, manifest,
                  standard: reader(:standard, dialect: :wkb12, dimensions: [:xy],
                                   srid_prelude: :uint32_le))

    assert result.feasible?
    assert_equal 2, result.num_packets
    assert_equal [4326, 3857], result.packets.map(&:srid)
    assert_equal [:uint32_le, :uint32_le], result.packets.map(&:prelude_format)
    assert result.verify!
  end

  def test_inherited_outer_srid_can_be_stated_once_in_packet_prelude
    bytes = Builder.collection([Builder.point([1, 2])], srid: 4326)
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :standard)

    result = plan(archive, manifest,
                  standard: reader(:standard, dialect: :wkb12, dimensions: [:xy],
                                   srid_prelude: :uint32_le))

    assert result.feasible?
    packet = result.packets.first
    assert_equal 4326, packet.srid
    assert_equal :uint32_le, packet.prelude_format
    assert packet.entries.first.preserved?
    assert result.verify!
  end

  def test_local_srid_is_reported_instead_of_outer_srid
    bytes = Builder.collection([Builder.point([1, 2], srid: 3857)], srid: 4326)
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :standard)

    result = plan(archive, manifest,
                  standard: reader(:standard, dialect: :wkb12, dimensions: [:xy],
                                   srid: 4326))

    refute result.feasible?
    failure = result.infeasibilities.first
    assert_equal "/0", failure.path
    assert_equal "standard", failure.reader
    assert_equal "SRID", failure.missing
    assert_includes failure.message, "3857"
    assert_raises(RGeo::WKRep::WKBExchange::UnsupportedMember) { result.verify! }
  end

  def test_rewrite_is_chosen_when_splitting_is_expensive
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 9], dimensions: :xyz, dialect: :wkb12),
        Builder.point([3, 4, 7], dimensions: :xym, dialect: :wkb12)
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :old)
    manifest.route("/1", to: :old)
    manifest.permit("/1", reader: :old, dimensions: :xyz, axes: { z: :m })

    cost_model = RGeo::WKRep::WKBExchange::CostModel.new(
      split_cost: 100,
      rewrite_cost: 2
    )
    result = plan(archive, manifest,
                  { old: reader(:old, dialect: :wkb12, dimensions: [:xyz]) },
                  cost_model: cost_model)

    assert result.feasible?
    assert_equal 1, result.num_packets
    assert_equal 1, result.packets.first.rewritten_count
    assert_equal 2, result.cost
    assert result.verify!
  end

  def test_split_is_chosen_when_rewriting_is_expensive
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 9], dimensions: :xyz, dialect: :wkb12),
        Builder.point([3, 4, 7], dimensions: :xym, dialect: :wkb12)
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :old)
    manifest.route("/1", to: :old)

    cost_model = RGeo::WKRep::WKBExchange::CostModel.new(
      split_cost: 3,
      rewrite_cost: 50
    )
    result = plan(archive, manifest,
                  { old: reader(:old, dialect: :wkb12, dimensions: %i[xyz xym]) },
                  cost_model: cost_model)

    assert result.feasible?
    assert_equal 2, result.num_packets
    assert_equal [:xyz, :xym], result.packets.map(&:dimensions)
    assert_equal 3, result.cost
    assert_equal 0, result.rewritten_count
    assert result.verify!
  end

  def test_old_reader_z_and_m_in_one_packet_requires_split_or_rewrite
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 3], dimensions: :xyz, dialect: :wkb12),
        Builder.point([4, 5, 6], dimensions: :xym, dialect: :wkb12)
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)

    without_permit = RGeo::WKRep::WKBExchange::Manifest.new
    without_permit.route("/0", to: :old)
    without_permit.route("/1", to: :old)
    infeasible = plan(archive, without_permit,
                      old: reader(:old, dialect: :wkb12, dimensions: [:xyz]))
    refute infeasible.feasible?
    assert_equal "/1", infeasible.infeasibilities.first.path
    assert_equal "M coordinates", infeasible.infeasibilities.first.missing

    with_split = RGeo::WKRep::WKBExchange::Manifest.new
    with_split.route("/0", to: :old)
    with_split.route("/1", to: :old)
    split = plan(archive, with_split,
                 { old: reader(:old, dialect: :wkb12, dimensions: %i[xyz xym]) },
                 cost_model: RGeo::WKRep::WKBExchange::CostModel.new(split_cost: 1))
    assert split.feasible?
    assert_equal 2, split.num_packets
    assert_equal [:xyz, :xym], split.packets.map(&:dimensions)
    assert split.verify!

    with_rewrite = RGeo::WKRep::WKBExchange::Manifest.new
    with_rewrite.route("/0", to: :old)
    with_rewrite.route("/1", to: :old)
    with_rewrite.permit("/1", reader: :old, dimensions: :xyz, axes: { z: :m })
    rewrite = plan(archive, with_rewrite,
                   { old: reader(:old, dialect: :wkb12, dimensions: [:xyz]) },
                   cost_model: RGeo::WKRep::WKBExchange::CostModel.new(rewrite_cost: 1))
    assert rewrite.feasible?
    assert_equal 1, rewrite.num_packets
    assert_equal :xyz, rewrite.packets.first.dimensions
    assert rewrite.verify!
  end

  def test_unrepresentable_member_lists_every_local_failure
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 3], dimensions: :xyz),
        Builder.point([4, 5], srid: 3857)
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :standard)
    manifest.route("/1", to: :standard)

    result = plan(archive, manifest,
                  standard: reader(:standard, dialect: :wkb12, dimensions: [:xy],
                                   srid: 4326))

    refute result.feasible?
    assert_equal ["/0", "/1"], result.infeasibilities.map(&:path)
    assert_equal ["Z coordinates", "SRID"], result.infeasibilities.map(&:missing)
  end

  def test_rewritten_line_string_and_polygon_keep_shape
    line = Builder.line_string(
      [[1, 2, 3], [4, 5, 6]],
      dimensions: :xyz,
      dialect: :wkb12
    )
    polygon = Builder.polygon(
      [
        [[0, 0, 1], [0, 4, 2], [4, 4, 3], [0, 0, 1]],
        [[1, 1, 4], [1, 2, 5], [2, 1, 6], [1, 1, 4]]
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    bytes = Builder.collection([line, polygon], dimensions: :xyz, dialect: :wkb12)
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :ewkb)
    manifest.route("/1", to: :ewkb)
    manifest.permit("/0", reader: :ewkb, dimensions: :xyz, axes: { m: :z },
                    allow_drop: true)
    manifest.permit("/1", reader: :ewkb, dimensions: :xyz, allow_drop: true)

    result = plan(archive, manifest,
                  ewkb: reader(:ewkb, dialect: :ewkb, dimensions: [:xyz]))
    assert result.feasible?
    assert result.verify!

    decoded = RGeo::WKRep::WKBExchange::Decoder.new(
      result.packets.first.bytes,
      dialect: :ewkb,
      prelude_format: nil
    ).read
    assert_equal [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], decoded[0][:shape]
    assert_equal 2, decoded[1][:shape].size
    assert_equal [[0.0, 0.0, 1.0], [0.0, 4.0, 2.0], [4.0, 4.0, 3.0], [0.0, 0.0, 1.0]],
                 decoded[1][:shape][0]
  end

  def test_z_and_m_purpose_are_carried_into_entries
    bytes = Builder.collection(
      [Builder.point([1, 2, 3], dimensions: :xyz, dialect: :wkb12)],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :r)
    manifest.z_purpose("/0", :elevation)
    manifest.m_purpose("/0", :mileage)

    result = plan(archive, manifest, r: reader(:r, dialect: :wkb12, dimensions: [:xyz]))
    entry = result.packets.first.entries.first
    assert_equal :elevation, entry.z_purpose
    assert_equal :mileage, entry.m_purpose
    assert_equal :elevation, result.to_h[:packets][0][:entries][0][:z_purpose]
  end

  def test_permit_with_empty_dimensions_preserves_source_layout
    bytes = Builder.collection(
      [Builder.point([1, 2, 3], dimensions: :xyz, dialect: :wkb12)],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :r)
    manifest.permit("/0", reader: :r, axes: { m: :z }, allow_drop: true)

    result = plan(archive, manifest, r: reader(:r, dialect: :wkb12, dimensions: [:xyz]))
    assert result.feasible?
    entry = result.packets.first.entries.first
    assert_equal :xyz, entry.output_dimensions
    assert result.verify!
  end

  def test_subtree_route_selects_descendants_and_specific_route_wins
    bytes = Builder.collection(
      [
        Builder.collection([Builder.point([1, 2]), Builder.point([3, 4])]),
        Builder.point([5, 6])
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :legacy)
    manifest.route("/0/1", to: :modern)
    manifest.route("/1", to: :modern)

    result = plan(archive, manifest,
                  legacy: reader(:legacy, dialect: :ewkb, dimensions: [:xy], srid: 4326),
                  modern: reader(:modern, dialect: :ewkb, dimensions: [:xy], srid: 4326))
    assert result.feasible?
    assert_equal [[0, 0]], result.packets_for(:legacy).flat_map(&:paths)
    assert_equal [[0, 1], [1]], result.packets_for(:modern).flat_map(&:paths)
    assert result.verify!
  end

  def test_strict_ewkb_splits_distinct_explicit_srids_by_default
    bytes = Builder.collection(
      [
        Builder.point([1, 2], srid: 4326),
        Builder.point([3, 4], srid: 3857)
      ],
      srid: 4326
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :strict)
    manifest.route("/1", to: :strict)

    result = plan(archive, manifest,
                  strict: reader(:strict, dialect: :ewkb, dimensions: [:xy],
                                 srid: 4326, srid_prelude: :ewkb))

    assert result.feasible?
    assert_equal 2, result.num_packets
    assert_equal [4326, 3857], (result.packets.map do |packet|
      entry = packet.entries.first
      entry.expected_srid || packet.srid
    end)
    assert result.verify!
  end

  def test_manifest_srid_override_is_scoped_per_reader
    bytes = Builder.collection([Builder.point([1, 2], srid: 3857)], srid: 4326)
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: %i[first second])
    manifest.srid("/0", 9999, reader: :first)

    result = plan(archive, manifest,
                  first: reader(:first, dialect: :ewkb, dimensions: [:xy], srid: 9999),
                  second: reader(:second, dialect: :ewkb, dimensions: [:xy], srid: 3857))

    assert result.feasible?
    assert_equal 9999, result.packets_for(:first).first.entries.first.written_srid
    assert_equal 3857, result.packets_for(:second).first.entries.first.written_srid
    assert result.verify!
  end

  def test_permitted_widening_fills_absent_axis_with_zero
    bytes = Builder.collection(
      [
        Builder.point([1, 2, 3], dimensions: :xyz, dialect: :wkb12),
        Builder.point([4, 5], dimensions: :xy, dialect: :wkb12)
      ],
      dimensions: :xyz,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :old)
    manifest.route("/1", to: :old)
    manifest.permit("/1", reader: :old, dimensions: :xyz, allow_drop: true)

    result = plan(archive, manifest,
                  { old: reader(:old, dialect: :wkb12, dimensions: [:xyz]) },
                  cost_model: RGeo::WKRep::WKBExchange::CostModel.new(split_cost: 10,
                                                                      rewrite_cost: 1))
    assert result.feasible?
    assert_equal 1, result.num_packets
    assert result.verify!
    decoded = RGeo::WKRep::WKBExchange::Decoder.new(
      result.packets.first.bytes,
      dialect: :wkb12
    ).read
    assert_equal [1.0, 2.0, 3.0], decoded[0][:shape]
    assert_equal [4.0, 5.0, 0.0], decoded[1][:shape]
  end

  def test_axis_remap_carries_measure_value_into_z
    bytes = Builder.collection(
      [Builder.point([4, 5, 6], dimensions: :xym, dialect: :wkb12)],
      dimensions: :xym,
      dialect: :wkb12
    )
    archive = archive(bytes)
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :old)
    manifest.permit("/0", reader: :old, dimensions: :xyz, axes: { z: :m })
    manifest.m_purpose("/0", :mileage)

    result = plan(archive, manifest,
                  old: reader(:old, dialect: :wkb12, dimensions: [:xyz]))
    assert result.feasible?
    assert result.verify!
    decoded = RGeo::WKRep::WKBExchange::Decoder.new(
      result.packets.first.bytes,
      dialect: :wkb12
    ).read
    assert_equal [[4.0, 5.0, 6.0]], [decoded[0][:shape]]
    assert_equal :mileage, result.packets.first.entries.first.m_purpose
  end

  def test_plan_helper_and_single_object_parser_regression
    bytes = Builder.collection([Builder.point([1, 2])])
    manifest = RGeo::WKRep::WKBExchange::Manifest.new
    manifest.route("/0", to: :r)
    result = RGeo::WKRep::WKBExchange.plan(
      bytes,
      manifest: manifest,
      readers: { r: reader(:r, dialect: :ewkb, dimensions: [:xy]) }
    )
    assert result.feasible?
    assert result.verify!

    # The normal single-object parser and generator are unchanged.
    point_bytes = Builder.point([1, 2], dialect: :wkb12)
    parser = RGeo::WKRep::WKBParser.new(
      RGeo::Cartesian.preferred_factory(srid: 0),
      support_wkb12: true
    )
    point = parser.parse(point_bytes)
    assert_equal 1, point.x
    assert_equal 2, point.y
    assert_equal 2, point.coordinate_dimension
  end
end
