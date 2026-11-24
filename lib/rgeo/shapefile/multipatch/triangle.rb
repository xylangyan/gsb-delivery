# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # A single deliverable triangle. Vertices are RGeo z-points and keep
      # the exact measured coordinates from the source multipatch.
      class Triangle
        attr_reader :a, :b, :c, :face_id

        def initialize(a, b, c, face_id)
          @a = a
          @b = b
          @c = c
          @face_id = face_id
        end

        def vertices
          [@a, @b, @c]
        end

        def area3d
          VectorSupport.triangle_area3d(@a, @b, @c)
        end

        def normal
          VectorSupport.triangle_normal(@a, @b, @c)
        end

        # Returns a triangle whose winding is reversed. The point objects
        # themselves are reused, so measured coordinates are untouched.
        def flip
          Triangle.new(@a, @c, @b, @face_id)
        end

        def vertex_keys(tolerance)
          vertices.map { |point| VectorSupport.coordinate_key(point, tolerance) }
        end
      end
    end
  end
end
