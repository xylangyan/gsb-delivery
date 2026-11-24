# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # The supervisor's registration for one warehouse run.
      #
      # seams is an array of entries such as
      #   { id: "S1", endpoints: [[x, y, z], [x, y, z]],
      #     faces: ["wall", "folded"], expected_faces: 2 }
      # where endpoints are the two ends of the physical seam and faces
      # are the original face ids that should meet there.
      # verified_orientations maps face id to the required outward unit
      # normal [x, y, z].
      class Registration
        attr_reader :seams, :verified_orientations,
                    :default_expected_faces_per_seam

        def initialize(seams: [], verified_orientations: {},
                       default_expected_faces_per_seam: 2)
          @seams = seams
          @verified_orientations = verified_orientations
          @default_expected_faces_per_seam = default_expected_faces_per_seam
        end

        def expected_faces_per_seam
          @default_expected_faces_per_seam
        end
      end
    end
  end
end
