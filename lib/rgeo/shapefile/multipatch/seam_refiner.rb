# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # Turns the seam registry's inserted vertices into real triangle
      # segmentation. An inserted station lies on a face's boundary edge,
      # so the triangle owning that edge is split in two; points landing
      # inside a triangle fan-split it into three. Every split reuses the
      # registry's point object, which keeps both sides of a seam on the
      # same physical subdivision.
      class SeamRefiner
        def initialize(tolerance = 1e-6)
          @tolerance = tolerance
        end

        # edges_by_face maps face id to its reconciled FaceEdges. Returns
        # a new list of triangles for the face with identical total area.
        def refine(face, face_edges)
          existing = face.triangles.flat_map do |triangle|
            triangle.vertex_keys(@tolerance)
          end
          inserted = face_edges.flat_map do |edge|
            edge.points.values_at(*edge.inserted_indices)
          end.uniq { |point| key(point) }
           .reject { |point| existing.include?(key(point)) }
          return face.triangles if inserted.empty?

          subtriangles = face.triangles.map(&:vertices)
          inserted.each do |point|
            subtriangles = split_where_containing(subtriangles, point)
          end
          subtriangles.map do |vertices|
            Triangle.new(vertices[0], vertices[1], vertices[2], face.id)
          end
        end

        private

        def split_where_containing(subtriangles, point)
          result = []
          subtriangles.each do |triangle|
            location = locate(point, triangle)
            if location.nil?
              result << triangle
            elsif location == :interior
              result.concat(fan_split(triangle, point))
            else
              result.concat(edge_split(triangle, point, location))
            end
          end
          result
        end

        # :interior, the index of the opposite edge, or nil when outside.
        def locate(point, triangle)
          a, b, c = triangle
          total = VectorSupport.triangle_area3d(a, b, c)
          return nil if total <= @tolerance

          areas = [
            VectorSupport.triangle_area3d(point, b, c),
            VectorSupport.triangle_area3d(a, point, c),
            VectorSupport.triangle_area3d(a, b, point)
          ]
          return nil if areas.sum > total + (@tolerance * 10)

          zero_index = areas.index { |value| value <= @tolerance }
          zero_index || :interior
        end

        def fan_split(triangle, point)
          a, b, c = triangle
          [[point, b, c], [a, point, c], [a, b, point]]
        end

        # edge_index 0 means point sits on edge b-c, 1 on a-c, 2 on a-b.
        def edge_split(triangle, point, edge_index)
          a, b, c = triangle
          case edge_index
          when 0
            [[a, b, point], [a, point, c]]
          when 1
            [[a, b, point], [point, b, c]]
          else
            [[a, point, c], [point, b, c]]
          end
        end

        def key(point)
          VectorSupport.coordinate_key(point, @tolerance)
        end
      end
    end
  end
end
