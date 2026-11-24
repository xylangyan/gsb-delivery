# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # Triangulates the face descriptors produced by RingGrouper.
      #
      # Triangle strips and fans are expanded directly from their recorded
      # vertex order, so folded (non-planar) surfaces keep every fold.
      # Ring groups are planar: all rings are projected onto the face's
      # own plane, holes are cut in by bridge edges, and the resulting
      # simple contour is ear-clipped. Hole interiors are never filled:
      # the bridge edges only guide the cut and contribute zero area.
      class PlanarMesher
        Node = Struct.new(:point, :x, :y)

        def initialize(factory, tolerance = 1e-9)
          @factory = factory
          @tolerance = tolerance
        end

        # Returns a result struct with the triangle point triples, the
        # plane origin/normal, the net source area of the face, and the
        # points that had to be introduced by bridge edges (they are not
        # measured points of the source part).
        Result = Struct.new(:triangles, :plane_origin, :plane_normal,
                            :source_area3d, :introduced_points, keyword_init: true)

        def mesh(face_spec)
          if face_spec.source_kind == :triangle_strip
            mesh_strip(face_spec)
          elsif face_spec.source_kind == :triangle_fan
            mesh_fan(face_spec)
          else
            mesh_ring_group(face_spec)
          end
        end

        private

        def mesh_strip(face_spec)
          points = face_spec.rings.first
          triangles = (0...(points.size - 2)).map do |index|
            if index.even?
              [points[index], points[index + 1], points[index + 2]]
            else
              [points[index + 1], points[index], points[index + 2]]
            end
          end
          build_triangle_result(face_spec, triangles)
        end

        def mesh_fan(face_spec)
          points = face_spec.rings.first
          triangles = (1...(points.size - 1)).map do |index|
            [points[0], points[index], points[index + 1]]
          end
          build_triangle_result(face_spec, triangles)
        end

        def mesh_ring_group(face_spec)
          outer_index = face_spec.ring_roles.index(:outer)
          outer_ring = face_spec.rings[outer_index]
          normal = VectorSupport.newell_normal(outer_ring)
          origin = VectorSupport.to_vector(outer_ring.first)
          basis_u = basis_u(normal)
          basis_v = VectorSupport.normalize(VectorSupport.cross(normal, basis_u))

          contours = face_spec.rings.each_with_index.map do |ring, index|
            nodes = unique_points(ring).map { |point| node_for(point, origin, basis_u, basis_v) }
            orient!(nodes, face_spec.ring_roles[index] == :outer)
            nodes
          end

          boundary = contours[outer_index]
          holes = contours.each_with_index.reject { |_, index| index == outer_index }.map(&:first)
          contour = cut_holes(boundary, holes)
          indices = ear_clip(contour)
          triangles = indices.map do |tri|
            tri.map { |node_index| contour[node_index].point || point_from_xy(contour[node_index], origin, basis_u, basis_v) }
          end

          source_area = net_planar_area(boundary, holes)
          measured_keys = unique_ring_points(face_spec).map { |point| key_for(point) }
          introduced = triangles.flatten.reject do |point|
            measured_keys.include?(key_for(point))
          end
          Result.new(triangles: triangles,
                     plane_origin: outer_ring.first,
                     plane_normal: normal,
                     source_area3d: source_area,
                     introduced_points: introduced)
        end

        def unique_ring_points(face_spec)
          face_spec.rings.flat_map(&:itself).uniq { |point| key_for(point) }
        end

        def key_for(point)
          VectorSupport.coordinate_key(point, @tolerance)
        end

        def build_triangle_result(face_spec, triangles)
          points = face_spec.rings.first
          Result.new(triangles: triangles,
                     plane_origin: points.first,
                     plane_normal: average_normal(triangles),
                     source_area3d: triangles.sum do |tri|
                       VectorSupport.triangle_area3d(*tri)
                     end,
                     introduced_points: [])
        end

        def average_normal(triangles)
          vector = triangles.each_with_object([0.0, 0.0, 0.0]) do |tri, sum|
            VectorSupport.newell_vector(tri).each_with_index do |value, index|
              sum[index] += value
            end
          end
          VectorSupport.normalize(vector)
        end

        def unique_points(ring)
          points = ring.dup
          points.pop if points.size > 1 && same_point?(points.first, points.last)
          points
        end

        def same_point?(a, b)
          VectorSupport.norm(VectorSupport.subtract(
            VectorSupport.to_vector(a), VectorSupport.to_vector(b)
          )) <= @tolerance
        end

        def node_for(point, origin, basis_u, basis_v)
          relative = VectorSupport.subtract(VectorSupport.to_vector(point), origin)
          Node.new(point,
                   VectorSupport.dot(relative, basis_u),
                   VectorSupport.dot(relative, basis_v))
        end

        def point_from_xy(node, origin, basis_u, basis_v)
          vector = VectorSupport.add(
            origin,
            VectorSupport.add(VectorSupport.scale(basis_u, node.x),
                              VectorSupport.scale(basis_v, node.y))
          )
          @factory.point(vector[0], vector[1], vector[2])
        end

        def basis_u(normal)
          axis = [1.0, 0.0, 0.0]
          axis = [0.0, 1.0, 0.0] if normal[0].abs > 0.9
          VectorSupport.normalize(VectorSupport.cross(axis, normal))
        end

        # Outer contours become counter-clockwise, holes clockwise.
        def orient!(nodes, outer)
          area = signed_area(nodes)
          reverse = outer ? area < 0 : area > 0
          nodes.reverse! if reverse
        end

        def signed_area(nodes)
          total = 0.0
          nodes.each_with_index do |node, index|
            nxt = nodes[(index + 1) % nodes.size]
            total += (node.x * nxt.y) - (nxt.x * node.y)
          end
          total * 0.5
        end

        def net_planar_area(boundary, holes)
          area = signed_area(boundary).abs
          holes.each { |hole| area -= signed_area(hole).abs }
          area
        end

        # Punch every hole into the boundary contour through mutually
        # visible vertex pairs. Bridges always reuse measured vertices, so
        # hole cutting never introduces extra stations on outer edges.
        def cut_holes(boundary, holes)
          remaining = holes.dup
          contour = boundary
          until remaining.empty?
            hole = remaining.shift
            start_node = hole.max_by { |node| node.x }
            target = nearest_visible_vertex(start_node, contour, hole, remaining)
            if target[:contour_index]
              other = remaining.delete_at(target[:contour_index])
              hole = splice(hole, start_node, other, target[:node])
              remaining.unshift(hole)
            else
              contour = splice(contour, target[:node], hole, start_node)
            end
          end
          contour
        end

        # Searches boundary and remaining-hole vertices to the right of
        # the hole vertex for the nearest one reachable by a segment that
        # does not cross any contour edge. Returns the target node and,
        # when it belongs to another hole, that hole's index.
        def nearest_visible_vertex(start_node, boundary, own_hole, remaining)
          candidates = boundary.map { |node| [node, nil] }
          remaining.each_with_index do |other, index|
            other.each { |node| candidates << [node, index] }
          end
          usable = candidates.reject { |node, _| node.x < start_node.x - @tolerance }
                             .reject { |node, _| same_node?(node, start_node) }
                             .sort_by { |node, _| distance2d(start_node, node) }
          usable.each do |node, contour_index|
            next unless visible?(start_node, node, boundary, own_hole, remaining)

            return { node: node, contour_index: contour_index }
          end
          fallback = boundary.min_by { |node| distance2d(start_node, node) }
          { node: fallback, contour_index: nil }
        end

        def visible?(start_node, target, boundary, own_hole, remaining)
          blockers = [boundary, own_hole] + remaining
          blockers.each do |contour|
            collect_edges(contour).each do |a, b|
              next if [a, b].include?(start_node) || [a, b].include?(target)
              return false if segments_cross?(start_node, target, a, b)
            end
          end
          true
        end

        def segments_cross?(p, q, a, b)
          orientations = [orientation2d(p, q, a), orientation2d(p, q, b),
                          orientation2d(a, b, p), orientation2d(a, b, q)]
          return true if orientations[0] * orientations[1] < -@tolerance &&
                         orientations[2] * orientations[3] < -@tolerance

          false
        end

        def orientation2d(a, b, c)
          ((b.x - a.x) * (c.y - a.y)) - ((b.y - a.y) * (c.x - a.x))
        end

        def distance2d(a, b)
          dx = a.x - b.x
          dy = a.y - b.y
          Math.sqrt((dx * dx) + (dy * dy))
        end

        # Joins contour_b into contour_a at the shared bridge nodes.
        # Both contours keep their existing orientation; the bridge
        # nodes occur twice, which the ear clipper treats as zero-area
        # connectors.
        def splice(contour_a, node_a, contour_b, node_b)
          start_a = contour_a.index { |node| same_node?(node, node_a) }
          start_b = contour_b.index { |node| same_node?(node, node_b) }
          ordered_a = contour_a.rotate(start_a)
          ordered_b = contour_b.rotate(start_b)
          ordered_a + [ordered_a.first] + ordered_b + [ordered_b.first]
        end

        def collect_edges(nodes)
          nodes.each_with_index.map do |node, index|
            [node, nodes[(index + 1) % nodes.size]]
          end
        end

        def same_node?(a, b)
          (a.x - b.x).abs <= @tolerance && (a.y - b.y).abs <= @tolerance
        end

        # Classic ear clipping on a simple CCW contour that may contain
        # repeated bridge vertices and collinear points.
        def ear_clip(nodes)
          indices = (0...nodes.size).to_a
          triangles = []
          guard = nodes.size
          while indices.size > 3
            ear = find_ear(nodes, indices)
            ear ||= fallback_ear(nodes, indices)
            position = indices.index(ear)
            prev_node = indices[(position - 1) % indices.size]
            next_node = indices[(position + 1) % indices.size]
            triangles << [prev_node, ear, next_node]
            indices.delete_at(position)
            guard -= 1
            raise "ear clipping failed to make progress" if guard.negative?
          end
          triangles << indices.dup if indices.size == 3
          triangles.reject do |tri|
            area2d(nodes[tri[0]], nodes[tri[1]], nodes[tri[2]]).abs <= @tolerance
          end
        end

        def find_ear(nodes, indices)
          indices.each do |candidate|
            next unless convex?(nodes, indices, candidate)
            next unless visible_ear?(nodes, indices, candidate)

            return candidate
          end
          nil
        end

        def fallback_ear(nodes, indices)
          convex = indices.select { |candidate| convex?(nodes, indices, candidate) }
          raise "no convex vertex remains for ear clipping" if convex.empty?

          convex.min_by do |candidate|
            position = indices.index(candidate)
            prev_node = nodes[indices[(position - 1) % indices.size]]
            next_node = nodes[indices[(position + 1) % indices.size]]
            area2d(prev_node, nodes[candidate], next_node).abs
          end
        end

        def convex?(nodes, indices, candidate)
          position = indices.index(candidate)
          prev_node = nodes[indices[(position - 1) % indices.size]]
          current = nodes[candidate]
          next_node = nodes[indices[(position + 1) % indices.size]]
          cross = ((current.x - prev_node.x) * (next_node.y - current.y)) -
                  ((current.y - prev_node.y) * (next_node.x - current.x))
          cross > @tolerance
        end

        def visible_ear?(nodes, indices, candidate)
          position = indices.index(candidate)
          prev_node = nodes[indices[(position - 1) % indices.size]]
          current = nodes[candidate]
          next_node = nodes[indices[(position + 1) % indices.size]]
          indices.each do |other|
            next if [prev_node, current, next_node].include?(nodes[other])

            return false if point_in_triangle?(nodes[other], prev_node, current, next_node)
          end
          true
        end

        def point_in_triangle?(point, a, b, c)
          c1 = area2d(point, a, b)
          c2 = area2d(point, b, c)
          c3 = area2d(point, c, a)
          # Points on an ear edge (zero cross product) do not block it.
          c1 > @tolerance && c2 > @tolerance && c3 > @tolerance
        end

        def area2d(a, b, c)
          ((b.x - a.x) * (c.y - a.y)) - ((b.y - a.y) * (c.x - a.x))
        end
      end
    end
  end
end
