# frozen_string_literal: true

require "minitest/autorun"
require "tmpdir"
require "rgeo/shapefile"

# Builds a minimal shape type 31 (MultiPatch) shapefile in a temp
# directory so the warehouse can be exercised through the real Reader.
class MultipatchFixtureWriter
  def initialize(path, records)
    @path = path
    @records = records
  end

  def write
    write_main
    write_index
  end

  private

  def record_contents
    @record_contents ||= @records.map { |record| encode_record(record) }
  end

  def encode_record(record)
    points = record[:points]
    parts = record[:parts]
    part_types = record[:part_types]
    xs = points.map { |point| point[0] }
    ys = points.map { |point| point[1] }
    zs = points.map { |point| point[2] }
    ms = points.map { |point| point.fetch(3, 0.0) }
    payload = [31].pack("V")
    payload += [xs.min, ys.min, xs.max, ys.max].pack("E4")
    payload += [parts.size, points.size].pack("V2")
    payload += parts.pack("V*")
    payload += part_types.pack("V*")
    payload += points.flat_map { |point| [point[0], point[1]] }.pack("E*")
    payload += [zs.min, zs.max].pack("E2")
    payload += zs.pack("E*")
    payload += [ms.min, ms.max].pack("E2")
    payload += ms.pack("E*")
    payload
  end

  def file_header(length_words)
    header = [9994].pack("N")
    header += ([0] * 5).pack("N5")
    header += [length_words].pack("N")
    header += [1000, 31].pack("V2")
    all_points = @records.flat_map { |record| record[:points] }
    xs = all_points.map(&:first)
    ys = all_points.map { |point| point[1] }
    zs = all_points.map { |point| point[2] }
    header += [xs.min, ys.min, xs.max, ys.max,
               zs.min, zs.max, 0.0, 0.0].pack("E8")
    header
  end

  def write_main
    offset_words = 50
    body = "".b
    record_contents.each_with_index do |content, index|
      length_words = content.bytesize / 2
      body += [index + 1, length_words].pack("N2")
      body += content
      offset_words += 4 + length_words
    end
    File.binwrite("#{@path}.shp", file_header(offset_words) + body)
  end

  def write_index
    body = "".b
    offset_words = 50
    record_contents.each do |content|
      length_words = content.bytesize / 2
      body += [offset_words, length_words].pack("N2")
      offset_words += 4 + length_words
    end
    File.binwrite("#{@path}.shx", file_header(50 + (record_contents.size * 4)) + body)
  end
end

class MultipatchWarehouseTest < Minitest::Test
  T = RGeo::Shapefile::Multipatch
  TOL = 1e-6

  # Vertical wall at x=0 with a hole, recorded First Ring first (and that
  # first ring is the hole). A folded triangle strip shares the seam
  # A-B but carries an extra station E. A detached shell behind the wall
  # repeats the exact seam coordinates.
  def build_records
    a = [0.0, 0.0, 0.0]
    b = [0.0, 10.0, 0.0]
    c = [0.0, 10.0, 8.0]
    d = [0.0, 0.0, 8.0]
    hole = [[0.0, 3.0, 2.0], [0.0, 7.0, 2.0],
            [0.0, 7.0, 6.0], [0.0, 3.0, 6.0]]
    wall_points = hole + [hole.first] + [a, b, c, d, a]
    wall_record = {
      points: wall_points,
      parts: [0, 5],
      part_types: [T::PART_TYPES[:first_ring], T::PART_TYPES[:ring]]
    }

    e = [0.0, 5.0, 0.0]
    q0 = [4.0, 0.0, 1.0]
    q1 = [4.0, 5.0, 2.0]
    strip_points = [a, q0, e, q1, b]
    strip_record = {
      points: strip_points,
      parts: [0],
      part_types: [T::PART_TYPES[:triangle_strip]]
    }

    h = [-3.0, 10.0, 0.0]
    k = [-3.0, 0.0, 0.0]
    shell_record = {
      points: [a, b, h, k, a],
      parts: [0],
      part_types: [T::PART_TYPES[:outer_ring]]
    }
    [wall_record, strip_record, shell_record]
  end

  def open_warehouse(registration)
    Dir.mktmpdir do |dir|
      path = File.join(dir, "mine")
      MultipatchFixtureWriter.new(path, build_records).write
      RGeo::Shapefile::Reader.open("#{path}.shp") do |reader|
        assert_equal 31, reader.shape_type_code
        raw_parts = (0...reader.num_records).map { |index| reader.multipatch_parts(index) }
        warehouse = T::Warehouse.new(reader.factory)
        return warehouse.build(raw_parts, registration)
      end
    end
  end

  def registration
    T::Registration.new(
      seams: [
        { id: "S1",
          endpoints: [[0.0, 0.0, 0.0], [0.0, 10.0, 0.0]],
          faces: [0, 1],
          expected_faces: 2 }
      ],
      verified_orientations: { 0 => [1.0, 0.0, 0.0] }
    )
  end

  def test_wall_hole_is_identified_and_left_unfilled
    result = open_warehouse(registration)
    wall = result.faces.find { |face| face.id == 0 }

    assert_equal :ring_group, wall.source_kind
    assert_equal [:hole, :outer], wall.ring_roles
    assert_equal 1, wall.hole_count
    assert_in_delta 64.0, wall.area3d, TOL
    assert_in_delta 64.0, wall.source_area3d, TOL

    result.triangles.select { |triangle| triangle.face_id == 0 }.each do |triangle|
      centroid = centroid3d(triangle)
      assert_equal 0.0, centroid[0].round(9)
      refute in_hole?(centroid)
    end
  end

  def test_every_measured_point_is_retained
    result = open_warehouse(registration)
    source = build_records.flat_map { |record| record[:points] }.uniq
    delivered = result.triangles.flat_map(&:vertices).map { |point| [point.x, point.y, point.z] }
    source.each do |coordinates|
      assert delivered.any? { |point| distance(point, coordinates) <= TOL },
             "missing measured point #{coordinates.inspect}"
    end
  end

  def test_seam_segmentation_matches_with_inserted_vertex
    result = open_warehouse(registration)
    seam = result.seam_reports.find { |report| report.id == "S1" }

    assert seam.consistent?
    assert seam.shared_segmentation?
    assert_equal [0, 1], seam.edges.map(&:face_id)
    seam.edges.each do |edge|
      assert_equal 3, edge.points.size
      assert_in_delta 5.0, edge.points[1].y, TOL
    end
    wall_edge, folded_edge = seam.edges
    assert_equal [1], wall_edge.inserted_indices
    assert_equal [], folded_edge.inserted_indices
  end

  def test_inserted_vertex_splits_wall_triangles_without_area_change
    result = open_warehouse(registration)
    wall = result.faces.find { |face| face.id == 0 }
    before = result.triangles.select { |triangle| triangle.face_id == 0 }

    station = wall.triangles.flat_map(&:vertices).find do |point|
      point.x.zero? && (point.y - 5.0).abs <= TOL && point.z.zero?
    end
    refute_nil station
    assert_in_delta 64.0, before.sum(&:area3d), TOL
  end

  def test_detached_shell_is_flagged_as_third_face
    result = open_warehouse(registration)
    seam = result.seam_reports.find { |report| report.id == "S1" }

    assert_equal [2], seam.third_face_ids
    assert seam.conflicts.empty?
  end

  def test_third_face_on_two_face_seam_names_all_physical_objects
    bad = T::Registration.new(
      seams: [
        { id: "S1",
          endpoints: [[0.0, 0.0, 0.0], [0.0, 10.0, 0.0]],
          faces: [0, 1, 2],
          expected_faces: 2 }
      ]
    )
    result = open_warehouse(bad)
    seam = result.seam_reports.first

    refute seam.consistent?
    assert_includes seam.conflicts.join, "0"
    assert_includes seam.conflicts.join, "1"
    assert_includes seam.conflicts.join, "2"
    assert_equal [2], seam.third_face_ids
  end

  def test_orientation_is_one_consistent_assignment_per_component
    result = open_warehouse(registration)
    report = result.orientation_reports.find { |entry| entry.face_ids.include?(0) }
    assert report.ok
    assert_equal [], report.evidence

    wall_normals = result.triangles.select { |triangle| triangle.face_id == 0 }
                               .map { |triangle| dot(triangle.normal, [1.0, 0.0, 0.0]) }
    assert(wall_normals.all? { |value| value > 0.99 })

    wall_edges = directed_seam_edges(result, 0)
    folded_edges = directed_seam_edges(result, 1)
    assert_equal wall_edges.map(&:reverse), folded_edges
  end

  def test_contradicted_component_is_held_back_but_shell_still_delivers
    folded_normal = folded_face_normal.map { |value| -value }
    bad = T::Registration.new(
      seams: [
        { id: "S1",
          endpoints: [[0.0, 0.0, 0.0], [0.0, 10.0, 0.0]],
          faces: [0, 1],
          expected_faces: 2 }
      ],
      verified_orientations: { 0 => [1.0, 0.0, 0.0], 1 => folded_normal }
    )
    result = open_warehouse(bad)
    report = result.orientation_reports.find { |entry| entry.face_ids.include?(0) }

    refute report.ok
    assert report.evidence.any? { |line| line.include?("S1") }
    delivered_ids = result.triangles.map(&:face_id).uniq
    refute_includes delivered_ids, 0
    refute_includes delivered_ids, 1
    assert_includes delivered_ids, 2
    shell = result.faces.find { |face| face.id == 2 }
    assert shell.deliverable
  end

  def test_face_ledger_points_back_to_source_records_and_parts
    result = open_warehouse(registration)
    wall, folded, shell = result.faces

    assert_equal 0, wall.record_index
    assert_equal [0, 1], wall.part_indexes
    assert_equal 1, folded.record_index
    assert_equal [0], folded.part_indexes
    assert_equal :triangle_strip, folded.source_kind
    assert_equal 2, shell.record_index
    assert_equal ["S1"], wall.seam_ids
    assert_equal ["S1"], folded.seam_ids
    assert_equal [], shell.seam_ids
  end

  def test_folded_strip_keeps_its_folds_and_3d_area
    result = open_warehouse(registration)
    folded = result.faces.find { |face| face.id == 1 }

    assert_equal 3, folded.triangles.size
    planes = folded.triangles.map { |triangle| triangle.normal.map { |value| value.round(6) } }
    refute_equal planes.uniq.size, 1
    expected = strip_triangle_areas.sum
    assert_in_delta expected, folded.area3d, TOL
    assert_in_delta expected, folded.source_area3d, TOL
  end

  def test_raw_parts_preserve_every_recorded_point
    Dir.mktmpdir do |dir|
      path = File.join(dir, "mine")
      MultipatchFixtureWriter.new(path, build_records).write
      RGeo::Shapefile::Reader.open("#{path}.shp") do |reader|
        parts = reader.multipatch_parts(1)
        assert_equal [:triangle_strip], parts.map(&:kind)
        assert_equal 5, parts.first.points.size
        wall_parts = reader.multipatch_parts(0)
        assert_equal [:first_ring, :ring], wall_parts.map(&:kind)
        assert_equal 10, wall_parts.sum { |part| part.points.size }
      end
    end
  end

  def test_triangle_fan_is_triangulated_from_recorded_pivot
    factory = RGeo::Cartesian.preferred_factory(has_z_coordinate: true)
    fan_points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                  [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]]
    fan = T::RawPart.new(:triangle_fan,
                         fan_points.map { |coords| factory.point(*coords) },
                         0, 0, [])
    result = T::Warehouse.new(factory).build(
      [[fan]], T::Registration.new
    )
    face = result.faces.first

    assert_equal :triangle_fan, face.source_kind
    assert_equal 2, face.triangles.size
    assert_in_delta 1.0, face.area3d, TOL
    pivot = factory.point(0.0, 0.0, 0.0)
    assert(face.triangles.all? { |triangle| triangle.vertices.include?(pivot) })
  end

  def test_manifold_orientation_without_verified_normals
    plain = T::Registration.new(
      seams: [
        { id: "S1",
          endpoints: [[0.0, 0.0, 0.0], [0.0, 10.0, 0.0]],
          faces: [0, 1],
          expected_faces: 2 }
      ]
    )
    result = open_warehouse(plain)
    report = result.orientation_reports.find { |entry| entry.face_ids.include?(0) }
    assert report.ok

    wall_edges = directed_seam_edges(result, 0)
    folded_edges = directed_seam_edges(result, 1)
    assert_equal wall_edges.map(&:reverse), folded_edges
  end

  def test_two_holes_are_both_cut_without_extra_vertices
    factory = RGeo::Cartesian.preferred_factory(has_z_coordinate: true)
    point = ->(y, z) { factory.point(0.0, y, z) }
    outer = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    hole1 = [[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0]]
    hole2 = [[6.0, 6.0], [9.0, 6.0], [9.0, 9.0], [6.0, 9.0]]
    ring = ->(coords) { (coords + [coords.first]).map { |y, z| point.call(y, z) } }
    parts = [
      T::RawPart.new(:first_ring, ring.call(hole1), 0, 0, []),
      T::RawPart.new(:ring, ring.call(hole2), 0, 1, []),
      T::RawPart.new(:ring, ring.call(outer), 0, 2, [])
    ]
    result = T::Warehouse.new(factory).build([parts], T::Registration.new)
    face = result.faces.first

    assert_equal [:hole, :hole, :outer], face.ring_roles
    assert_equal 2, face.hole_count
    assert_equal [], face.introduced_points
    assert_in_delta 87.0, face.area3d, TOL
    assert_in_delta 87.0, face.source_area3d, TOL
    face.triangles.each do |triangle|
      centroid = triangle.vertices.map { |vertex| [vertex.y, vertex.z] }
                         .transpose.map { |values| values.sum / 3.0 }
      in_hole1 = centroid[0].between?(1, 3) && centroid[1].between?(1, 3)
      in_hole2 = centroid[0].between?(6, 9) && centroid[1].between?(6, 9)
      refute(in_hole1 || in_hole2)
    end
  end

  def test_first_ring_can_be_the_outer_ring
    factory = RGeo::Cartesian.preferred_factory(has_z_coordinate: true)
    point = ->(x, y) { factory.point(x, y, 0.0) }
    ring = ->(coords) { (coords + [coords.first]).map { |x, y| point.call(x, y) } }
    outer = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    hole = [[3.0, 3.0], [7.0, 3.0], [7.0, 7.0], [3.0, 7.0]]
    parts = [
      T::RawPart.new(:first_ring, ring.call(outer), 0, 0, []),
      T::RawPart.new(:ring, ring.call(hole), 0, 1, [])
    ]
    result = T::Warehouse.new(factory).build([parts], T::Registration.new)
    face = result.faces.first

    assert_equal [:outer, :hole], face.ring_roles
    assert_in_delta 84.0, face.area3d, TOL
  end

  private

  def centroid3d(triangle)
    triangle.vertices.map { |point| [point.x, point.y, point.z] }
            .transpose.map { |values| values.sum / 3.0 }
  end

  def in_hole?(coordinates)
    _x, y, z = coordinates
    y > 3.0 && y < 7.0 && z > 2.0 && z < 6.0
  end

  def distance(a, b)
    Math.sqrt(a.zip(b).sum { |left, right| (left - right)**2 })
  end

  def dot(a, b)
    a.zip(b).sum { |left, right| left * right }
  end

  def directed_seam_edges(result, face_id)
    triangles = result.triangles.select { |triangle| triangle.face_id == face_id }
    counts = Hash.new(0)
    directed = triangles.flat_map do |triangle|
      keys = triangle.vertex_keys(TOL)
      [[keys[0], keys[1]], [keys[1], keys[2]], [keys[2], keys[0]]]
    end
    directed.each { |edge| counts[edge.sort] += 1 }
    directed.select { |edge| counts[edge.sort] == 1 }
            .select { |edge| seam_keys.include?(edge.sort) }
            .sort
  end

  def seam_keys
    factory = RGeo::Cartesian.preferred_factory(has_z_coordinate: true)
    [factory.point(0.0, 0.0, 0.0), factory.point(0.0, 10.0, 0.0),
     factory.point(0.0, 5.0, 0.0)].map { |point| T::VectorSupport.coordinate_key(point, TOL) }
                              .combination(2).map(&:sort)
  end

  def folded_face_normal
    a = [0.0, 0.0, 0.0]
    q0 = [4.0, 0.0, 1.0]
    e = [0.0, 5.0, 0.0]
    T::VectorSupport.triangle_normal(
      RGeo::Cartesian.preferred_factory(has_z_coordinate: true).point(*a),
      RGeo::Cartesian.preferred_factory(has_z_coordinate: true).point(*q0),
      RGeo::Cartesian.preferred_factory(has_z_coordinate: true).point(*e)
    )
  end

  def strip_triangle_areas
    factory = RGeo::Cartesian.preferred_factory(has_z_coordinate: true)
    points = [[0.0, 0.0, 0.0], [4.0, 0.0, 1.0], [0.0, 5.0, 0.0],
              [4.0, 5.0, 2.0], [0.0, 10.0, 0.0]].map { |coords| factory.point(*coords) }
    triples = [[points[0], points[1], points[2]],
               [points[2], points[1], points[3]],
               [points[2], points[3], points[4]]]
    triples.map { |triple| T::VectorSupport.triangle_area3d(*triple) }
  end
end
