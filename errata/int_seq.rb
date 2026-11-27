# frozen_string_literal: true

module Errata
  # Immutable sets of integers stored as unions of arithmetic progressions.
  #
  # Tables in the long archive use equi-spaced material numbers (start, step,
  # count) and batches may retarget a whole regular run of them.  Keeping the
  # sets symbolic means a million-position table is never expanded while
  # planning the night.
  class IntSeq
    attr_reader :parts

    def initialize(parts = [])
      @parts = parts
                .map { |part| [Integer(part[0]), Integer(part[1]), Integer(part[2])] }
                .reject { |_, _, count| count <= 0 }
    end

    def self.point(value)
      new([[value, 1, 1]])
    end

    def self.seq(first, step, count)
      raise ArgumentError, 'step must be positive' unless step.positive?

      new([[first, step, count]])
    end

    def self.from_a(numbers)
      parts = []
      numbers.sort.uniq.each do |value|
        if !parts.empty? && parts.last[0] + parts.last[2] == value
          parts.last[2] += 1
        else
          parts << [value, 1, 1]
        end
      end
      new(parts)
    end

    def empty?
      parts.empty?
    end

    def size
      parts.sum { |_, _, count| count }
    end

    def include?(value)
      parts.any? do |first, step, count|
        next false if value < first
        next false if value > first + (count - 1) * step

        (value - first) % step == 0
      end
    end

    def union(other)
      IntSeq.new(IntSeq.merge_parts(parts + other.subtract(intersect(other)).parts))
    end

    def subtract(other)
      remaining = parts.dup
      other.parts.each do |removed|
        remaining = remaining.flat_map { |part| IntSeq.subtract_ap(part, removed) }
      end
      IntSeq.new(remaining)
    end

    def intersect(other)
      result = parts.flat_map do |part|
        other.parts.filter_map { |other_part| IntSeq.intersect_ap(part, other_part) }
      end
      IntSeq.new(result)
    end

    def ==(other)
      other.is_a?(IntSeq) &&
        size == other.size &&
        subtract(other).empty? &&
        other.subtract(self).empty?
    end

    alias eql? ==

    def to_a(cap = nil)
      if cap && size > cap
        raise "cannot expand #{size} positions (cap #{cap})"
      end

      parts.flat_map { |first, step, count| count.times.map { |k| first + k * step } }.sort
    end

    def first(limit)
      taken = []
      parts.each do |first, step, count|
        count.times do |k|
          taken << first + k * step
          return taken.sort if taken.size >= limit
        end
      end
      taken.sort
    end

    # Multiplicity-aware materialization: each contribution keeps its own
    # duplicates, mirroring CategoricalIndex#pos which never merges repeated
    # query values.
    def self.flatten_sort(contribs, cap = nil)
      total = contribs.sum(&:size)
      if cap && total > cap
        raise "cannot expand #{total} positions (cap #{cap})"
      end

      contribs.flat_map(&:to_a).sort
    end

    # Solve a + k*d = b + l*e with 0 <= k < n and 0 <= l < m.
    # Returns [first_value, lcm_step, count] or nil.
    def self.intersect_ap(part_a, part_b)
      a, d, n = part_a
      b, e, m = part_b
      g = d.gcd(e)
      diff = b - a
      return nil unless (diff % g).zero?

      d1 = d / g
      e1 = e / g
      r = diff / g
      inverse = e1 == 1 ? 0 : inverse_mod(d1, e1)
      k_period = e1
      k0 = (r * inverse) % k_period

      k_lo = [0, ceil_div(b - a, d)].max
      k_hi = [n - 1, floor_div(b - a + (m - 1) * e, d)].min
      return nil if k_lo > k_hi

      first_index = k0 + (ceil_div(k_lo - k0, k_period) * k_period)
      return nil if first_index > k_hi

      count = (k_hi - first_index).div(k_period) + 1
      [a + first_index * d, d.lcm(e), count]
    end

    # Remove the points of one AP from another, working in the k-index space
    # of the survivor so the cost does not depend on the number of rows.
    def self.subtract_ap(part_a, part_b)
      intersection = intersect_ap(part_a, part_b)
      return [part_a] if intersection.nil?

      a, d, n = part_a
      k0 = (intersection[0] - a) / d
      period = intersection[1] / d
      removed_count = intersection[2]

      result = []
      result << [a, d, k0] if k0.positive?

      inside = period - 1
      if inside.positive?
        inside.times do |offset|
          first_k = k0 + 1 + offset
          next if first_k >= n

          # Interior columns stop before the last removed point; survivors past
          # it belong to the single tail progression.
          count = [removed_count - 1, (n - 1 - first_k).div(period) + 1].min
          result << [a + first_k * d, d * period, count] if count.positive?
        end
      end

      last_removed = k0 + (removed_count - 1) * period
      tail = n - 1 - last_removed
      result << [a + (last_removed + 1) * d, d, tail] if tail.positive?
      result
    end

    def self.inverse_mod(value, modulus)
      g, x, = extended_gcd(value, modulus)
      raise 'no modular inverse' unless g == 1

      x % modulus
    end

    def self.extended_gcd(left, right)
      return [right, 0, 1] if left.zero?

      g, x, y = extended_gcd(right % left, left)
      [g, y - (right / left) * x, x]
    end

    def self.ceil_div(num, den)
      return 0 if num.zero?

      num.negative? ? -((-num) / den) : (num + den - 1) / den
    end

    def self.floor_div(num, den)
      num.negative? ? -ceil_div(-num, den) : num / den
    end

    def self.merge_parts(parts)
      ordered = parts.sort_by { |first, step, _| [step, first] }
      ordered.each_with_object([]) do |part, merged|
        if !merged.empty? && mergeable?(merged.last, part)
          merged.last[2] += part[2]
        else
          merged << part.dup
        end
      end
    end

    def self.mergeable?(left, right)
      left[1] == right[1] && left[0] + left[1] * left[2] == right[0]
    end
  end
end
