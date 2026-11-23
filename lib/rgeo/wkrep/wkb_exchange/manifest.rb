# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Business manifest for a WKB exchange
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # A Manifest states which readers receive which atomic members and which
      # output representations are permitted for them.
      #
      # Paths are arrays of zero-based child indexes from the archive root; the
      # string form "/0/2" is also accepted. A rule applies to every atomic
      # member at or below its path.
      #
      # == Routing
      #
      #   manifest.route("/0", to: :legacy)
      #   manifest.route("/1", to: [:legacy, :modern])
      #
      # Routing without a permit means "deliver the source representation
      # unchanged". If that is impossible, the plan is infeasible for that
      # reader; the planner never silently alters data.
      #
      # == Permitted rewrites
      #
      # The business list may allow a member to be rewritten for a reader. Each
      # call describes one alternative representation and they are cumulative:
      #
      #   # Deliver this member's M value as the legacy reader's Z coordinate.
      #   manifest.permit("/1", reader: :legacy, dimensions: :xyz, axes: { z: :m })
      #
      #   # Also permit narrowing to XY rather than failing on a missing axis.
      #   manifest.permit("/2", reader: :modern, dimensions: :xy, allow_drop: true)
      #
      # Permits may be stated before or after routing. The minimum-cost planner
      # chooses among the source representation and these alternatives.
      class Manifest
        # Resolved request for one member and one reader.
        Selection = Struct.new(
          :reader_name, :representations, :srid, :srid_declared,
          :z_purpose, :m_purpose, keyword_init: true
        )

        Rule = Struct.new(:path, :readers, :representations, :srids,
                          :z_purpose, :m_purpose, keyword_init: true)

        attr_reader :rules, :default_srid

        def initialize(default_srid: nil)
          @rules = {}
          @default_srid = default_srid
        end
        # Assign a member (or every atomic member below it) to reader names.
        # Repeated calls merge. This selects a nested collection's leaves; use
        # an explicit path to target one descendant.
        def route(path, to:)
          rule = fetch_rule(path)
          Array(to).each { |reader_name| rule.readers << reader_name.to_sym }
          self
        end

        # Permit one rewrite alternative for a member and reader. +dimensions+
        # is an output configuration (symbol or array). +axes+ maps output axis
        # to source axis. +allow_drop+ permits discarding unlisted source axes.
        def permit(path, reader:, dimensions: nil, axes: nil, allow_drop: false)
          rule = fetch_rule(path)
          representation = Representation.new(
            dimensions: dimensions || [],
            axes: axes || {},
            allow_drop: allow_drop
          )
          (rule.representations[reader.to_sym] ||= []) << representation
          self
        end

        # Declare the effective SRID for a member. With +reader:+ the declaration
        # is scoped to that destination; otherwise it applies to every reader of
        # the path. A member's explicit local SRID always wins over an inherited
        # outer SRID, while an authored declaration wins over both.
        def srid(path, value, reader: nil)
          fetch_rule(path).srids[reader&.to_sym] = value
          self
        end

        # State what the Z coordinate means for a member (:elevation, :measure...).
        def z_purpose(path, value)
          fetch_rule(path).z_purpose = value.to_sym
          self
        end

        # State what the M coordinate means for a member.
        def m_purpose(path, value)
          fetch_rule(path).m_purpose = value.to_sym
          self
        end

        # Resolved selections for the leaf at +path+, outermost rule first.
        #
        # A rule applies to every leaf at or below its path, so routing a nested
        # GeometryCollection selects all of its atomic descendants. The most
        # specific rule that names readers controls the reader set for that
        # subtree; permits, SRIDs, and purposes still merge from every ancestor.
        def selections(path)
          normalized = normalize(path)
          applicable = rules_for(normalized)
          routing = applicable.select { |rule| !rule.readers.empty? }
          reader_names = (routing.last || applicable.last)&.readers || []

          reader_names.map do |reader_name|
            representations = []
            srid_value = @default_srid
            srid_declared = false
            z_purpose = nil
            m_purpose = nil

            applicable.each do |rule|
              candidates = rule.representations[reader_name]
              representations.concat(candidates) if candidates
              if rule.srids.key?(reader_name)
                srid_value = rule.srids[reader_name]
                srid_declared = true
              elsif rule.srids.key?(nil)
                srid_value = rule.srids[nil]
                srid_declared = true
              end
              z_purpose = rule.z_purpose if rule.z_purpose
              m_purpose = rule.m_purpose if rule.m_purpose
            end

            Selection.new(
              reader_name: reader_name,
              representations: representations.uniq,
              srid: srid_value,
              srid_declared: srid_declared,
              z_purpose: z_purpose,
              m_purpose: m_purpose
            )
          end
        end

        # The effective authored SRID for a path, or the manifest default.
        def effective_srid(path, reader: nil)
          value = @default_srid
          found = false
          rules_for(path).each do |rule|
            if reader && rule.srids.key?(reader.to_sym)
              value = rule.srids[reader.to_sym]
              found = true
            elsif rule.srids.key?(nil)
              value = rule.srids[nil]
              found = true
            end
          end
          found ? value : @default_srid
        end

        def purpose(path, axis)
          value = nil
          rules_for(path).each do |rule|
            candidate = axis == :z ? rule.z_purpose : rule.m_purpose
            value = candidate if candidate
          end
          value
        end

        def each_rule(&)
          return enum_for(:each_rule) unless block_given?
          @rules.each_value(&)
          self
        end

        def normalize(path)
          case path
          when Array then path.map(&:to_i)
          when String
            return [] if path.empty? || path == "/"
            path.split("/").reject(&:empty?).map(&:to_i)
          else
            raise ArgumentError, "Unsupported path #{path.inspect}"
          end
        end

        # Build a frozen reader inventory from Reader objects or option hashes.
        def self.readers(definitions)
          definitions.to_h do |name, options|
            reader = options.is_a?(Reader) ? options : Reader.new(name: name, **options)
            [reader.name, reader]
          end.freeze
        end

        private

        # Rules from the root down to the given path, outermost first.
        def rules_for(path)
          normalized = normalize(path)
          (0..normalized.length).filter_map { |length| @rules[normalized[0, length]] }
        end

        def fetch_rule(path)
          key = normalize(path)
          @rules[key] ||= Rule.new(path: key, readers: [],
                                   representations: {}, srids: {},
                                   z_purpose: nil, m_purpose: nil)
        end
      end
    end
  end
end
