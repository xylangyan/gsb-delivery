# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # One original surface component ("原部件"): a triangle strip, a
      # triangle fan, or a group of rings belonging to one planar face.
      # A Face carries both the deliverable triangles and the ledger data
      # needed to trace every triangle back to its source record and
      # parts.
      class Face
        attr_reader :id, :record_index, :part_indexes, :source_kind,
                    :rings, :triangles, :plane_origin, :plane_normal,
                    :area3d, :source_area3d, :ring_roles, :deliverable,
                    :seam_ids, :introduced_points

        def initialize(id:, record_index:, part_indexes:, source_kind:,
                       rings:, ring_roles:, triangles:, plane_origin:,
                       plane_normal:, area3d:, source_area3d:,
                       deliverable: true, seam_ids: [], introduced_points: [])
          @id = id
          @record_index = record_index
          @part_indexes = part_indexes
          @source_kind = source_kind
          @rings = rings
          @ring_roles = ring_roles
          @triangles = triangles
          @plane_origin = plane_origin
          @plane_normal = plane_normal
          @area3d = area3d
          @source_area3d = source_area3d
          @deliverable = deliverable
          @seam_ids = seam_ids
          @introduced_points = introduced_points
        end

        def hole_count
          @ring_roles.count(:hole)
        end
      end
    end
  end
end
