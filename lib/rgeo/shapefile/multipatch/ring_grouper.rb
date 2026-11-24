# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # Groups the raw parts of one multipatch record into face
      # descriptors. The grouping rules follow the ESRI multipatch part
      # sequence: an outer ring owns the following inner rings, a first
      # ring owns the following untyped rings, and triangle strips/fans
      # each stand alone. First Ring groups do not declare which ring is
      # the boundary and which are holes, so the outer ring is found by
      # planar containment in the face's own plane; the first recorded
      # ring may therefore turn out to be a hole.
      class RingGrouper
        FaceSpec = Struct.new(:record_index, :part_indexes, :source_kind,
                              :rings, :ring_roles, keyword_init: true)

        def initialize(tolerance = 1e-9)
          @tolerance = tolerance
        end

        def group(parts)
          specs = []
          index = 0
          while index < parts.size
            part = parts[index]
            if part.triangle_part?
              specs << triangle_spec(part)
              index += 1
            else
              index = consume_ring_group(parts, index, specs)
            end
          end
          specs
        end

        private

        def triangle_spec(part)
          FaceSpec.new(record_index: part.record_index,
                       part_indexes: [part.part_index],
                       source_kind: part.kind,
                       rings: [part.points],
                       ring_roles: [:triangle])
        end

        def consume_ring_group(parts, start, specs)
          first = parts[start]
          group = [first]
          index = start + 1
          if first.kind == :outer_ring
            while index < parts.size && parts[index].kind == :inner_ring
              group << parts[index]
              index += 1
            end
            specs << build_outer_spec(group)
          else
            while index < parts.size && parts[index].kind == :ring
              group << parts[index]
              index += 1
            end
            specs << build_first_spec(group)
          end
          index
        end

        def build_outer_spec(group)
          roles = [:outer] + [:hole] * (group.size - 1)
          build_spec(group, roles)
        end

        def build_first_spec(group)
          roles = identify_roles(group)
          build_spec(group, roles)
        end

        def build_spec(group, roles)
          FaceSpec.new(record_index: group.first.record_index,
                       part_indexes: group.map(&:part_index),
                       source_kind: :ring_group,
                       rings: group.map(&:points),
                       ring_roles: roles)
        end

        # Projects every ring onto the shared plane's 2D basis and picks
        # the ring with the largest area as the boundary; rings contained
        # in it are holes. The plane comes from the first ring, which is
        # safe because the rings of one face are coplanar by spec.
        def identify_roles(group)
          planar = group.map { |part| part.points.map { |point| project(point, part.points) } }
          areas = planar.map { |ring| signed_area_2d(ring).abs }
          outer_index = areas.each_with_index.max_by(&:first).last
          Array.new(group.size) do |index|
            index == outer_index ? :outer : :hole
          end
        end

        def project(point, reference_ring)
          normal = VectorSupport.newell_normal(reference_ring)
          origin = VectorSupport.to_vector(reference_ring.first)
          basis_u = basis_u(normal)
          basis_v = VectorSupport.normalize(VectorSupport.cross(normal, basis_u))
          relative = VectorSupport.subtract(
            VectorSupport.to_vector(point), origin
          )
          [VectorSupport.dot(relative, basis_u),
           VectorSupport.dot(relative, basis_v)]
        end

        def basis_u(normal)
          axis = [1.0, 0.0, 0.0]
          axis = [0.0, 1.0, 0.0] if normal[0].abs > 0.9
          VectorSupport.normalize(VectorSupport.cross(axis, normal))
        end

        def signed_area_2d(ring)
          total = 0.0
          ring.each_cons(2) do |a, b|
            total += (a[0] * b[1]) - (b[0] * a[1])
          end
          total * 0.5
        end
      end
    end
  end
end
