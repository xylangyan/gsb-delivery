# frozen_string_literal: true

# -----------------------------------------------------------------------------
#
# Byte-preserving WKB archive and exchange planner
#
# -----------------------------------------------------------------------------

require_relative "../error" unless defined?(RGeo::Error::RGeoError)

module RGeo
  module WKRep
    # == Overview
    #
    # WKBExchange reads a WKB or EWKB geometry as an unmodified byte archive,
    # routes its atomic members to downstream readers, and produces the cheapest
    # set of self-contained packets that each reader can re-read with exactly
    # the requested coordinates, dimensions, SRIDs, and collection order.
    #
    # The archive does not construct geometry objects. It retains each node's
    # original byte span together with its byte order, dialect, explicit or
    # inherited SRID, and Z/M layout. As a result an atom can be forwarded
    # byte-for-byte whenever its destination reader accepts the original
    # representation.
    #
    # Atomic members are Point, LineString, and Polygon leaves. GeometryCollection
    # and Multi* wrappers are traversed, and their atomic leaves are routed in
    # original preorder.
    #
    # A packet is a WKB GeometryCollection whose root type carries the packet's
    # fixed dimension configuration. It may carry one downstream SRID at its
    # start, in the convention the destination reader defines; alternatively an
    # EWKB packet may let children retain distinct explicit SRIDs. Children may
    # keep different byte orders, which WKB permits.
    #
    # == Example
    #
    #   archive = RGeo::WKRep::WKBExchange::Archive.parse(bytes, srid: 4326)
    #
    #   manifest = RGeo::WKRep::WKBExchange::Manifest.new
    #   manifest.route("/0", to: :legacy)
    #   manifest.route("/1", to: :modern)
    #   # The legacy reader has no M axis, but the business allows this member's
    #   # measure to be delivered as Z.
    #   manifest.permit("/1", reader: :legacy, dimensions: :xyz, axes: { z: :m })
    #
    #   readers = {
    #     legacy: { dialect: :wkb12, dimensions: [:xyz, :xym], srid: 4326 },
    #     modern: { dialect: :ewkb, dimensions: [:xy, :xyz, :xym, :xyzm],
    #               srid: 4326, srid_prelude: :ewkb }
    #   }
    #
    #   plan = RGeo::WKRep::WKBExchange::Planner.new(
    #     archive: archive,
    #     manifest: manifest,
    #     readers: readers,
    #     cost_model: RGeo::WKRep::WKBExchange::CostModel.new(split_cost: 8, rewrite_cost: 3)
    #   ).plan
    #
    #   plan.cost       # minimum total cost
    #   plan.packets    # the chosen packets, in collection order
    #   plan.verify!    # true, or raises with the exact offending path
    #
    # If a reader cannot express a selected member, the returned Plan is
    # infeasible and exposes UnsupportedMember records containing the original
    # collection path and the missing capability. An outer SRID never masks a
    # member's local SRID fact.
    module WKBExchange
      # Build an archive and plan an exchange in one call.
      #
      #   plan = RGeo::WKRep::WKBExchange.plan(
      #     bytes, srid: 4326, manifest: manifest, readers: readers,
      #     cost_model: cost_model
      #   )
      def self.plan(data, manifest:, readers:, cost_model: CostModel.new, **archive_options)
        archive = Archive.parse(data, **archive_options)
        Planner.new(archive: archive, manifest: manifest, readers: readers,
                    cost_model: cost_model).plan
      end

      # Raised when a downstream reader cannot express a selected member.
      class UnsupportedMember < RGeo::Error::RGeoError
        attr_reader :path, :reader, :missing, :node

        def initialize(path, reader, missing, message = nil, node: nil)
          @path = path
          @reader = reader
          @missing = missing
          @node = node
          super(message || "Reader #{reader} cannot express member at #{path}: #{missing}")
        end

        def to_h
          {
            path: @path,
            reader: @reader.to_s,
            missing: @missing,
            message: message
          }
        end
        alias capability missing
        alias collection_path path
      end
    end
  end
end

require_relative "wkb_exchange/node"
require_relative "wkb_exchange/parser"
require_relative "wkb_exchange/archive"
require_relative "wkb_exchange/reader"
require_relative "wkb_exchange/representation"
require_relative "wkb_exchange/manifest"
require_relative "wkb_exchange/cost_model"
require_relative "wkb_exchange/serializer"
require_relative "wkb_exchange/delivery"
require_relative "wkb_exchange/packet"
require_relative "wkb_exchange/decoder"
require_relative "wkb_exchange/plan"
require_relative "wkb_exchange/planner"
