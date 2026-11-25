module Groupdate
  class MinuteLedger
    # Pure-Ruby, whole-table reimplementation of the display rules. Used to
    # cross-check the windowed SQL path by rerunning every bucket of a version
    # in one pass.
    module Display
      def self.build(buckets, fill: false)
        data = buckets.to_h
        return [] if data.empty?

        fill ? build_filled(data) : build_sparse(data)
      end

      def self.build_sparse(data)
        order = []
        seen = {}
        totals = {}
        source_minutes = {}

        data.keys.sort.each do |minute|
          label = MinuteLedger.label_for(minute)
          unless seen.key?(label)
            seen[label] = order.size
            order << label
          end
          totals[label] = data[minute]
          source_minutes[label] = minute
        end

        order.each_with_index.map do |label, position|
          minute = source_minutes[label]
          {
            key: label,
            label: label,
            position: position,
            total: totals[label],
            contributed_minute: minute,
            source_minute: minute
          }
        end
      end

      def self.build_filled(data)
        lo, hi = data.keys.minmax
        rows = []
        carry_minute = nil

        (lo..hi).each do |minute|
          present = data.key?(minute)
          carry_minute = minute if present

          rows << {
            key: minute,
            label: MinuteLedger.label_for(minute),
            position: minute - lo,
            total: data[carry_minute],
            contributed_minute: present ? minute : carry_minute,
            source_minute: carry_minute
          }
        end

        rows
      end
    end
  end
end
