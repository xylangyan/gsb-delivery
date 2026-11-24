# frozen_string_literal: true

module RGeo
  module GeoJSON
    module Dateline
      # A point on the unwrapped longitude/latitude plane, in integer
      # microdegrees. Longitudes may exceed the [-180, 180] window while an
      # edge or ring is being expanded; they are folded back when pieces are
      # emitted.
      MicroPoint = Struct.new(:lon, :lat) do
        def shift(lon_delta)
          MicroPoint.new(lon + lon_delta, lat)
        end

        def ==(other)
          other.is_a?(MicroPoint) && lon == other.lon && lat == other.lat
        end
        alias eql? ==

        def hash
          [lon, lat].hash
        end

        def to_a
          [lon, lat]
        end
      end

      MICRO_PER_DEGREE = 1_000_000
      CIRCLE = 360 * MICRO_PER_DEGREE
      HALF = 180 * MICRO_PER_DEGREE

      # Exact integer/rational helpers on the unwrapped plane.
      module Geometry
        MICRO_PER_DEGREE = Dateline::MICRO_PER_DEGREE
        CIRCLE = Dateline::CIRCLE
        HALF = Dateline::HALF

        module_function

        # Exact orientation of the ordered triple (a, b, c).
        # Returns -1, 0, or 1.
        def orientation(a, b, c)
          v = (b.lon - a.lon) * (c.lat - a.lat) - (b.lat - a.lat) * (c.lon - a.lon)
          v <=> 0
        end

        def collinear?(a, b, c)
          orientation(a, b, c).zero?
        end

        def on_segment?(a, b, p)
          return false unless collinear?(a, b, p)

          [p.lon, p.lat].min >= [a.lon, b.lon].min &&
            [p.lon, p.lat].max <= [a.lon, b.lon].max
        end

        # Proper or improper segment intersection test.
        def segments_intersect?(a, b, c, d)
          o1 = orientation(a, b, c)
          o2 = orientation(a, b, d)
          o3 = orientation(c, d, a)
          o4 = orientation(c, d, b)

          return true if o1 != o2 && o3 != o4
          return true if o1.zero? && on_segment?(a, b, c)
          return true if o2.zero? && on_segment?(a, b, d)
          return true if o3.zero? && on_segment?(c, d, a)
          return true if o4.zero? && on_segment?(c, d, b)

          false
        end

        def proper_cross?(a, b, c, d)
          o1 = orientation(a, b, c)
          o2 = orientation(a, b, d)
          o3 = orientation(c, d, a)
          o4 = orientation(c, d, b)
          # Strict sign change on both segments; touching at an endpoint or
          # along a collinear overlap is not a proper crossing.
          o1 * o2 < 0 && o3 * o4 < 0
        end

        # Intersection of two segments as [point, t] where point =
        # a + t * (b - a) and t is a Rational in [0, 1]. Returns nil when the
        # segments are parallel or do not cross. For collinear overlap the
        # first shared parameter is returned.
        def segment_intersection(a, b, c, d)
          r_lon = b.lon - a.lon
          r_lat = b.lat - a.lat
          s_lon = d.lon - c.lon
          s_lat = d.lat - c.lat
          denom = r_lon * s_lat - r_lat * s_lon

          if denom.zero?
            return nil unless collinear?(a, b, c)

            candidates = [c, d, a, b].select { |pt| on_segment?(a, b, pt) }
            return nil if candidates.empty?

            pt = candidates.first
            t = r_lon.zero? ? Rational(pt.lat - a.lat, r_lat) : Rational(pt.lon - a.lon, r_lon)
            return [pt, t]
          end

          qp_lon = c.lon - a.lon
          qp_lat = c.lat - a.lat
          t = Rational(qp_lon * s_lat - qp_lat * s_lon, denom)
          u = Rational(qp_lon * r_lat - qp_lat * r_lon, denom)
          return nil unless t >= 0 && t <= 1 && u >= 0 && u <= 1

          x = a.lon + t * r_lon
          y = a.lat + t * r_lat
          [MicroPoint.new(x, y), t]
        end

        # Winding-number point in ring. Boundary membership is reported
        # separately so callers can apply the handover boundary rule.
        def point_location(ring, point)
          ring.each_cons(2) do |a, b|
            return :boundary if on_segment?(a, b, point)
          end

          inside = false
          ring.each_cons(2) do |a, b|
            if (a.lat > point.lat) != (b.lat > point.lat)
              x_int = a.lon + Rational((point.lat - a.lat) * (b.lon - a.lon), b.lat - a.lat)
              inside = !inside if point.lon < x_int
            end
          end
          inside ? :inside : :outside
        end

        def point_in_ring?(ring, point)
          point_location(ring, point) == :inside
        end

        def ring_self_intersects?(ring)
          n = ring.size - 1
          return false if n < 3

          segments = []
          n.times { |i| segments << [ring[i], ring[i + 1]] }

          (0...n).each do |i|
            a, b = segments[i]
            (i + 1...n).each do |j|
              c, d = segments[j]
              next if j == i + 1 || (i.zero? && j == n - 1)

              if segments_intersect?(a, b, c, d)
                shared = [a, b].any? { |p| p == c || p == d }
                next if shared && !proper_cross?(a, b, c, d)

                return true
              end
            end
          end
          false
        end

        def ring_area2(ring)
          sum = 0
          ring.each_cons(2) { |a, b| sum += a.lon * b.lat - b.lon * a.lat }
          sum
        end

        def ring_clockwise?(ring)
          ring_area2(ring) < 0
        end

        def bbox(ring)
          lons = ring.map(&:lon)
          lats = ring.map(&:lat)
          [lons.min, lats.min, lons.max, lats.max]
        end

        def close_ring(points)
          return points if points.empty? || points.first == points.last

          points + [points.first]
        end

        # Canonical description of a closed ring's *territory*, not its
        # vertex list. Consecutive duplicates and collinear intermediate
        # vertices are removed, then the ring is rotated to start at its
        # lexicographically smallest vertex and oriented in the direction
        # whose vertex list sorts first. Two rings describing the same
        # boundary therefore produce the same key even if the registration
        # started at a different vertex or listed the vertices in reverse.
        def canonical_ring_key(points)
          pts = close_ring(points)
          pts = pts[0...-1]
          pts = dedupe_consecutive(pts)
          pts = remove_collinear(pts)
          return [[0, 0]] if pts.empty?

          n = pts.size
          min_index = (0...n).min_by { |i| [pts[i].lon, pts[i].lat] }
          rotated = pts.rotate(min_index)
          forward = rotated.map { |p| [p.lon, p.lat] }
          backward = rotated.reverse.map { |p| [p.lon, p.lat] }
          # A ring and its reverse trace the same boundary; pick one.
          (forward <=> backward) <= 0 ? forward : backward
        end

        def canonical_ring_strict_key(points)
          # Orientation-preserving canonical form, for cases where the
          # direction conveys interior/exterior (GeoJSON winding).
          pts = close_ring(points)
          pts = pts[0...-1]
          pts = dedupe_consecutive(pts)
          pts = remove_collinear(pts)
          return [[0, 0]] if pts.empty?

          n = pts.size
          min_index = (0...n).min_by { |i| [pts[i].lon, pts[i].lat] }
          pts.rotate(min_index).map { |p| [p.lon, p.lat] }
        end

        def dedupe_consecutive(points)
          out = []
          points.each { |p| out << p unless p == out.last }
          out
        end

        def remove_collinear(points)
          return points if points.size < 3

          pts = points.dup
          changed = true
          while changed && pts.size >= 3
            changed = false
            out = []
            pts.each_with_index do |p, i|
              prev = pts[(i - 1) % pts.size]
              nxt = pts[(i + 1) % pts.size]
              if orientation(prev, p, nxt).zero? && between?(prev, nxt, p)
                changed = true
              else
                out << p
              end
            end
            pts = out
          end
          pts
        end

        # Interior representative point. Triangle centroids of consecutive
        # triples are tried first so thin rings still get a point that is
        # genuinely inside.
        def representative_point(ring)
          pts = close_ring(ring)
          n = pts.size - 1
          return pts.first if n <= 0

          n.times do |i|
            a = pts[i]
            b = pts[(i + 1) % n]
            c = pts[(i + 2) % n]
            next if orientation(a, b, c).zero?

            cand = MicroPoint.new(
              Rational(a.lon + b.lon + c.lon, 3),
              Rational(a.lat + b.lat + c.lat, 3)
            )
            return cand if point_location(pts, cand) == :inside
          end

          area2 = ring_area2(pts)
          return pts.first if area2.zero?

          cx = 0r
          cy = 0r
          pts.each_cons(2) do |a, b|
            cross = a.lon * b.lat - b.lon * a.lat
            cx += (a.lon + b.lon) * cross
            cy += (a.lat + b.lat) * cross
          end
          MicroPoint.new(Rational(cx, 3 * area2), Rational(cy, 3 * area2))
        end
      end
    end
  end
end
