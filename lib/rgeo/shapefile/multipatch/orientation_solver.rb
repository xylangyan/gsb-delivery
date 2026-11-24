# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # Resolves one consistent orientation for every face. Seams tie
      # two faces together through a single flip constraint, and the
      # supervisor's verified normals pin individual faces. Constraints
      # are solved per connected component, so a face is never flipped
      # seam by seam; a component whose constraints disagree is reported
      # with the original seam and orientation evidence instead.
      class OrientationSolver
        Constraint = Struct.new(:left_id, :right_id, :xor_flip, :seam_id,
                                keyword_init: true)
        ComponentReport = Struct.new(:face_ids, :ok, :evidence,
                                     keyword_init: true)

        def initialize(tolerance = 1e-6)
          @tolerance = tolerance
        end

        # faces are the meshed Face objects; seam_reports come from the
        # SeamRegistry; verified_orientations maps face id to the
        # required outward normal as [x, y, z].
        def solve(faces, seam_reports, verified_orientations)
          constraints = seam_constraints(seam_reports)
          graph = build_graph(faces, constraints)
          required = verified_flips(faces, verified_orientations)
          flips = {}
          reports = []

          graph.each do |root, neighbours|
            next if flips.key?(root)

            component = walk_component(root, graph)
            status = assign_component(component, graph, required, flips)
            reports << ComponentReport.new(
              face_ids: component,
              ok: status[:ok],
              evidence: status[:evidence]
            )
          end
          { flips: flips, reports: reports }
        end

        private

        def seam_constraints(seam_reports)
          seam_reports.flat_map do |report|
            report.edges.each_cons(2).map do |left, right|
              same_direction = left.traversal_sign == right.traversal_sign
              Constraint.new(left_id: left.face_id,
                             right_id: right.face_id,
                             xor_flip: same_direction ? 1 : 0,
                             seam_id: report.id)
            end
          end
        end

        def build_graph(faces, constraints)
          graph = faces.each_with_object({}) { |face, hash| hash[face.id] = [] }
          constraints.each do |constraint|
            graph[constraint.left_id] << constraint
            graph[constraint.right_id] << constraint
          end
          graph
        end

        def verified_flips(faces, verified_orientations)
          faces.each_with_object({}) do |face, required|
            target = verified_orientations[face.id]
            next unless target

            alignment = VectorSupport.dot(face.plane_normal, target)
            required[face.id] = alignment.negative?
          end
        end

        def walk_component(root, neighbours)
          component = []
          pending = [root]
          seen = {}
          until pending.empty?
            face_id = pending.shift
            next if seen[face_id]

            seen[face_id] = true
            component << face_id
            neighbours[face_id].each do |constraint|
              other = constraint.left_id == face_id ? constraint.right_id : constraint.left_id
              pending << other unless seen[other]
            end
          end
          component
        end

        def assign_component(component, neighbours, required, flips)
          seed = component.find { |face_id| required.key?(face_id) } || component.first
          flips[seed] = required.key?(seed) ? required[seed] : false
          evidence = []
          ok = true
          pending = [seed]
          visited = {}

          until pending.empty?
            face_id = pending.shift
            next if visited[face_id]

            visited[face_id] = true
            neighbours[face_id].each do |constraint|
              other = constraint.left_id == face_id ? constraint.right_id : constraint.left_id
              expected = flips[face_id] ^ (constraint.xor_flip == 1)
              if flips.key?(other)
                unless flips[other] == expected
                  ok = false
                  evidence << "seam #{constraint.seam_id}: faces " \
                              "#{constraint.left_id} and #{constraint.right_id} " \
                              "demand incompatible windings"
                end
              else
                if required.key?(other) && required[other] != expected
                  ok = false
                  evidence << "seam #{constraint.seam_id}: verified orientation " \
                              "of face #{other} conflicts with face #{face_id}"
                end
                flips[other] = expected
                pending << other
              end
            end

            next unless required.key?(face_id) && flips[face_id] != required[face_id]

            ok = false
            evidence << "orientation of face #{face_id} contradicts the " \
                        "verified normal"
          end
          { ok: ok, evidence: evidence.uniq }
        end
      end
    end
  end
end
