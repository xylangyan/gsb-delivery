# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # Minimal 3D vector helpers used by the multipatch mesher. Vectors
      # are plain [x, y, z] arrays so the helpers stay independent of any
      # particular RGeo factory.
      module VectorSupport
        module_function

        def subtract(a, b)
          [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
        end

        def add(a, b)
          [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
        end

        def scale(vector, factor)
          [vector[0] * factor, vector[1] * factor, vector[2] * factor]
        end

        def dot(a, b)
          (a[0] * b[0]) + (a[1] * b[1]) + (a[2] * b[2])
        end

        def cross(a, b)
          [
            (a[1] * b[2]) - (a[2] * b[1]),
            (a[2] * b[0]) - (a[0] * b[2]),
            (a[0] * b[1]) - (a[1] * b[0])
          ]
        end

        def norm(vector)
          Math.sqrt(dot(vector, vector))
        end

        def normalize(vector)
          length = norm(vector)
          return vector if length.zero?

          scale(vector, 1.0 / length)
        end

        def to_vector(point)
          [point.x, point.y, point.z]
        end

        def coordinate_key(point, tolerance)
          [point.x, point.y, point.z].map { |value| (value / tolerance).round }
        end

        # Newell's method yields the area vector of a (planar) 3D polygon.
        # Its magnitude is the polygon area and its direction follows the
        # recorded winding.
        def newell_vector(points)
          vector = [0.0, 0.0, 0.0]
          size = points.size
          points.each_with_index do |point, index|
            nxt = points[(index + 1) % size]
            crossed = cross(to_vector(point), to_vector(nxt))
            vector = add(vector, crossed)
          end
          scale(vector, 0.5)
        end

        def newell_normal(points)
          normalize(newell_vector(points))
        end

        def triangle_area3d(a, b, c)
          0.5 * norm(cross(subtract(to_vector(b), to_vector(a)),
                              subtract(to_vector(c), to_vector(a))))
        end

        def triangle_normal(a, b, c)
          normalize(cross(subtract(to_vector(b), to_vector(a)),
                          subtract(to_vector(c), to_vector(a))))
        end
      end
    end
  end
end
