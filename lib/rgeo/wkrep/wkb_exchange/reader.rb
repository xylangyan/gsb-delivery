# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# WKB dialect definitions used by the exchange planner
#
# -----------------------------------------------------------------------------

module RGeo
  module WKRep
    module WKBExchange
      # Describes what one downstream reader can re-read.
      #
      # Each packet produced for this reader uses exactly one dialect and one
      # dimension configuration. A reader may accept several configurations
      # separately (for example :xyz and :xym), but a single packet may use only
      # one of them.
      class Reader
        DIALECTS = %i[wkb11 wkb12 ewkb].freeze
        DIMENSIONS = %i[xy xyz xym xyzm].freeze

        attr_reader :name, :dialect, :dimensions, :srid

        # Options:
        #
        # [<tt>:dialect</tt>] :wkb11, :wkb12, or :ewkb.
        # [<tt>:dimensions</tt>] One of :xy, :xyz, :xym, :xyzm, or an array of
        #   allowed configurations.
        # [<tt>:srid</tt>] The SRID this reader assumes when a packet carries no
        #   SRID prelude.
        # [<tt>:srid_prelude</tt>]
        #   How a packet may carry a downstream SRID at its start:
        #   * nil (default): true for EWKB, false otherwise.
        #   * true: the dialect's native convention. EWKB uses the EWKB root
        #     SRID flag; standard WKB uses a 4-byte little-endian prefix.
        #   * :ewkb: EWKB root SRID flag (EWKB only).
        #   * :uint32_le / :uint32_be: a 4-byte prefix before the WKB header.
        #   * false: no packet-level SRID.
        # [<tt>:child_srid</tt>] EWKB only. Whether the reader understands
        #   distinct explicit SRIDs on individual children. Defaults to false:
        #   a strict EWKB reader expects each child to inherit the packet-level
        #   SRID, so a group needing different local SRIDs is split into separate
        #   packets. Set true only for a reader known to honor child SRIDs.
        def initialize(name:, dialect:, dimensions: :xy, srid: nil,
                       srid_prelude: nil, child_srid: false)
          @name = name.to_s
          @dialect = dialect.to_sym
          unless DIALECTS.include?(@dialect)
            raise ArgumentError, "Unknown dialect #{dialect.inspect}"
          end

          @dimensions = Array(dimensions).map do |dim|
            dim = dim.to_sym
            unless DIMENSIONS.include?(dim)
              raise ArgumentError, "Unknown dimension configuration #{dim.inspect}"
            end
            if @dialect == :wkb11 && dim != :xy
              raise ArgumentError, "WKB 1.1 has no Z or M coordinates (requested #{dim.inspect})"
            end
            dim
          end.uniq.freeze

          # A reader that accepts XYZ necessarily accepts XY, and a reader that
          # accepts XYZM accepts every subset. This closure keeps callers from
          # having to list every narrower configuration explicitly.
          @accepted_dimensions = @dimensions.flat_map do |dim|
            case dim
            when :xyzm then %i[xy xyz xym xyzm]
            when :xyz then %i[xy xyz]
            when :xym then %i[xy xym]
            else [dim]
            end
          end.uniq.freeze
          @srid = srid
          @srid_prelude = normalize_prelude(srid_prelude)
          @child_srid = child_srid.nil? ? @dialect == :ewkb : (child_srid ? true : false)
        end

        def supports_dimensions?(dim)
          @accepted_dimensions.include?(dim.to_sym)
        end

        # The explicit configurations this reader can be asked to produce.
        def dimension_options
          @accepted_dimensions
        end

        def supports_dialect?(other)
          dialect == other.to_sym
        end

        # True when a child with the node's detected dialect can be forwarded
        # into this reader's packet unchanged.
        #
        # A plain XY type code has identical bits in WKB 1.1, WKB 1.2, and EWKB.
        # It is therefore readable by any of those readers, even though the
        # parser labels it :wkb11 because no high bits are set. Z/M or SRID flags
        # are not interchangeable between EWKB and standard WKB.
        def compatible_dialect?(node)
          return true if dialect == node.dialect

          standard = %i[wkb11 wkb12]
          return true if standard.include?(dialect) && standard.include?(node.dialect)

          node.dimensions == :xy && !node.explicit_srid?
        end

        def srid_prelude?
          !@srid_prelude.nil?
        end

        def prelude_format
          @srid_prelude
        end

        def child_srid?
          @child_srid
        end

        def accepts_child_srid?
          @dialect == :ewkb && @child_srid
        end

        # True when a member with this effective SRID can be represented at all.
        def supports_srid?(value)
          return true if value.nil?
          return true if !@srid.nil? && @srid == value

          srid_prelude? || accepts_child_srid?
        end

        # Human-readable capability name used in UnsupportedMember errors.
        def capability_for(axis)
          case axis
          when :z then "Z coordinates"
          when :m then "M coordinates"
          when :srid then "SRID"
          else axis.to_s
          end
        end

        def canonical_key
          [@dialect, @dimensions.sort, @srid, @srid_prelude, @child_srid]
        end

        def inspect
          "#<#{self.class} name=#{@name.inspect} dialect=#{@dialect} " \
            "dimensions=#{@dimensions.inspect} srid=#{@srid.inspect} " \
            "srid_prelude=#{@srid_prelude.inspect}>"
        end

        private

        def normalize_prelude(value)
          case value
          when nil
            @dialect == :ewkb ? :ewkb : nil
          when false
            nil
          when true
            @dialect == :ewkb ? :ewkb : :uint32_le
          when :ewkb
            unless @dialect == :ewkb
              raise ArgumentError, ":ewkb SRID prelude requires the EWKB dialect"
            end
            :ewkb
          when :uint32_le, :uint32_be
            value
          else
            raise ArgumentError, "Unknown SRID prelude #{value.inspect}"
          end
        end
      end
    end
  end
end
