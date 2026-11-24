# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # One physical occurrence of a registered seam on one face. The
      # polyline keeps every measured point of that face, including
      # intermediate survey stations; inserted points are the ones
      # fabricated to match the neighbouring face's segmentation.
      class FaceEdge
        attr_reader :face_id, :points, :inserted_flags, :traversal_sign

        def initialize(face_id, points, inserted_flags, traversal_sign)
          @face_id = face_id
          @points = points
          @inserted_flags = inserted_flags
          @traversal_sign = traversal_sign
        end

        def inserted_indices
          @inserted_flags.each_index.select { |index| @inserted_flags[index] }
        end
      end

      # The reconciled view of one registered seam: every participating
      # face carries the same segmentation, plus conflict evidence when
      # the registration disagrees with the actual geometry.
      class SeamReport
        attr_reader :id, :endpoints, :registered_face_ids, :edges,
                    :conflicts, :third_face_ids

        def initialize(id:, endpoints:, registered_face_ids:, edges:,
                       conflicts:, third_face_ids:)
          @id = id
          @endpoints = endpoints
          @registered_face_ids = registered_face_ids
          @edges = edges
          @conflicts = conflicts
          @third_face_ids = third_face_ids
        end

        def consistent?
          @conflicts.empty?
        end

        # All edges must walk the same ordered, equally segmented chain.
        def shared_segmentation?
          return true if @edges.empty?

          reference = @edges.first.points
          @edges.all? do |edge|
            edge.points.size == reference.size &&
              edge.points.each_with_index.all? do |point, index|
                same_coordinates?(point, reference[index])
              end
          end
        end

        private

        def same_coordinates?(a, b)
          VectorSupport.norm(VectorSupport.subtract(
            VectorSupport.to_vector(a), VectorSupport.to_vector(b)
          )) <= 1e-6
        end
      end

      # Applies the supervisor's seam registry to meshed faces: matches
      # each registered seam to physical boundary polylines, unifies their
      # segmentation by inserting vertices, and records third faces that
      # share seam coordinates without being registered to it.
      class SeamRegistry
        def initialize(factory, tolerance = 1e-6)
          @factory = factory
          @tolerance = tolerance
        end

        def reconcile(faces, registration)
          registration.seams.map do |entry|
            reconcile_seam(entry, faces, registration)
          end
        end

        private

        def directed_boundary_edges(face, point_cache = nil)
          directed = face.triangles.flat_map do |triangle|
            verts = triangle.vertices
            keys = triangle.vertex_keys(@tolerance)
            [[keys[0], keys[1]], [keys[1], keys[2]], [keys[2], keys[0]]]
              .map { |ka, kb| [verts[keys.index(ka)], verts[keys.index(kb)]] }
          end
          counts = directed.each_with_object(Hash.new(0)) do |(a, b), hash|
            hash[[key_for(a), key_for(b)].sort] += 1
          end
          directed.select do |a, b|
            counts[[key_for(a), key_for(b)].sort] == 1
          end.each do |a, b|
            point_cache&.[]=(key_for(a), a)
            point_cache&.[]=(key_for(b), b)
          end
        end

        def reconcile_seam(entry, faces, registration)
          endpoints = entry[:endpoints]
          face_ids = entry[:faces]
          expected_size = entry[:expected_faces] ||
                          registration.expected_faces_per_seam || 2
          conflicts = []

          matched = face_ids.map do |face_id|
            face = faces.find { |candidate| candidate.id == face_id }
            chain = face && seam_chain(face, endpoints)
            unless chain
              conflicts << "#{face_id}: no boundary edge matching seam #{entry[:id]}"
              next nil
            end
            [face, chain]
          end.compact

          physical_ids = faces.select { |face| seam_chain(face, endpoints) }.map(&:id)
          intended_ids = face_ids.first(expected_size)
          third_face_ids = physical_ids - intended_ids
          if face_ids.size > expected_size
            conflicts << "seam #{entry[:id]} expects #{expected_size} faces but " \
                         "touches #{physical_ids.inspect}"
          end

          edges = unified_edges(entry[:id], matched, endpoints)
          SeamReport.new(id: entry[:id], endpoints: endpoints,
                         registered_face_ids: face_ids, edges: edges,
                         conflicts: conflicts, third_face_ids: third_face_ids)
        end

        # Follows the face's boundary edges that lie on the seam line
        # from one registered endpoint to the other. Returns the measured
        # point chain in the face's triangle-winding direction together
        # with that direction sign.
        def seam_chain(face, endpoints)
          cache = {}
          directed = directed_boundary_edges(face, cache)
          adjacency = Hash.new { |hash, key| hash[key] = [] }
          directed.each do |a, b|
            adjacency[key_for(a)] << key_for(b)
            adjacency[key_for(b)] << key_for(a)
          end
          start_key = cache.keys.find { |key| key_matches?(key, endpoints[0]) }
          target_key = cache.keys.find { |key| key_matches?(key, endpoints[1]) }
          return nil unless start_key && target_key

          path = walk_seam(adjacency, start_key, target_key, endpoints, cache)
          return nil unless path

          points = path.map { |key| cache[key] }
          sign = edge_sign(directed, path[0], path[1])
          [points, sign]
        end

        def walk_seam(adjacency, start_key, target_key, endpoints, cache)
          pending = [[start_key, [start_key]]]
          seen = {}
          until pending.empty?
            current, path = pending.shift
            return path if current == target_key
            next if seen[current]

            seen[current] = true
            adjacency[current].each do |neighbour|
              next if path.include?(neighbour)
              next unless on_seam_line?(cache[neighbour], endpoints)

              pending << [neighbour, path + [neighbour]]
            end
          end
          nil
        end

        def edge_sign(directed, start_key, end_key)
          directed.each do |a, b|
            next unless key_for(a) == start_key && key_for(b) == end_key

            return 1
          end
          -1
        end

        def on_seam_line?(point, endpoints)
          origin = endpoints[0]
          direction = VectorSupport.normalize(
            VectorSupport.subtract(endpoints[1], endpoints[0])
          )
          relative = VectorSupport.subtract(
            VectorSupport.to_vector(point),
            [origin[0], origin[1], origin[2]]
          )
          along = VectorSupport.dot(relative, direction)
          return false if along < -@tolerance
          return false if along > seam_length(endpoints) + @tolerance

          offset = VectorSupport.subtract(
            relative, VectorSupport.scale(direction, along)
          )
          VectorSupport.norm(offset) <= @tolerance
        end

        def seam_length(endpoints)
          VectorSupport.norm(
            VectorSupport.subtract(endpoints[1], endpoints[0])
          )
        end

        def key_matches?(key, coordinates)
          target = [coordinates[0], coordinates[1], coordinates[2]]
          point = @factory.point(*target)
          key == key_for(point)
        end

        # Builds the common point chain (union of all measured stations)
        # and gives every face that exact segmentation, flagging the
        # points that had to be inserted.
        def unified_edges(seam_id, matched, endpoints)
          return [] if matched.empty?

          union = matched.flat_map { |_, (chain, _)| chain }
                         .uniq { |point| key_for(point) }
          origin = matched.first[1][0].first
          direction = seam_direction(matched.first[1][0])
          union.sort_by! { |point| parameter(point, origin, direction) }

          matched.map do |face, (chain, sign)|
            own = chain.each_with_object({}) { |point, hash| hash[key_for(point)] = true }
            points = union.map do |point|
              if own[key_for(point)]
                [point, false]
              else
                [insert_point(face, point, endpoints), true]
              end
            end
            FaceEdge.new(face.id, points.map(&:first), points.map(&:last), sign)
          end
        end

        # An inserted station must sit on the shared seam line so both
        # faces get the same physical subdivision; it is projected onto
        # the face's own plane only enough to keep the face flat. For a
        # boundary edge of a planar face that projection is the line
        # itself, so no measured geometry is disturbed.
        def insert_point(face, source_point, endpoints)
          line_origin = [endpoints[0][0], endpoints[0][1], endpoints[0][2]]
          line_direction = VectorSupport.normalize(
            VectorSupport.subtract(endpoints[1], endpoints[0])
          )
          relative = VectorSupport.subtract(
            VectorSupport.to_vector(source_point), line_origin
          )
          along = VectorSupport.dot(relative, line_direction)
          on_line = VectorSupport.add(
            line_origin, VectorSupport.scale(line_direction, along)
          )
          normal = face.plane_normal
          plane_origin = VectorSupport.to_vector(face.plane_origin)
          plane_distance = VectorSupport.dot(
            VectorSupport.subtract(on_line, plane_origin), normal
          )
          projected = VectorSupport.subtract(
            on_line, VectorSupport.scale(normal, plane_distance)
          )
          @factory.point(projected[0], projected[1], projected[2])
        end

        def seam_direction(polyline)
          VectorSupport.normalize(VectorSupport.subtract(
            VectorSupport.to_vector(polyline.last),
            VectorSupport.to_vector(polyline.first)
          ))
        end

        def parameter(point, origin, direction)
          VectorSupport.dot(
            VectorSupport.subtract(VectorSupport.to_vector(point),
                                   VectorSupport.to_vector(origin)),
            direction
          )
        end

        def key_for(point)
          VectorSupport.coordinate_key(point, @tolerance)
        end

        def same_coordinates?(point, coordinates)
          target = @factory.point(coordinates[0], coordinates[1], coordinates[2])
          same_point?(point, target)
        end

        def same_point?(a, b)
          VectorSupport.norm(VectorSupport.subtract(
            VectorSupport.to_vector(a), VectorSupport.to_vector(b)
          )) <= @tolerance
        end
      end
    end
  end
end
