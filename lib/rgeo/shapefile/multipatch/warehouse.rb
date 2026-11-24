# frozen_string_literal: true

module RGeo
  module Shapefile
    module Multipatch
      # End-to-end warehouse pipeline for one MultiPatch shapefile.
      #
      # The output keeps three layers separate:
      # * deliverable triangles carrying their source face id,
      # * per-face ledgers (record/part provenance, plane, source and
      #   delivered 3D area, seam ids),
      # * seam and orientation reports with the original evidence for
      #   anything the registry contradicts.
      class Warehouse
        Result = Struct.new(:triangles, :faces, :seam_reports,
                            :orientation_reports, keyword_init: true)

        def initialize(factory, tolerance = 1e-6)
          @factory = factory
          @tolerance = tolerance
          @grouper = RingGrouper.new(tolerance)
          @mesher = PlanarMesher.new(factory, tolerance)
          @registry = SeamRegistry.new(factory, tolerance)
          @refiner = SeamRefiner.new(tolerance)
          @solver = OrientationSolver.new(tolerance)
        end

        # raw_parts is an array of RawPart arrays (one per shapefile
        # record); registration is the supervisor's seam and verified
        # orientation data.
        def build(raw_parts, registration)
          faces = mesh_faces(raw_parts)
          seam_reports = @registry.reconcile(faces, registration)
          faces = refine_faces(faces, seam_reports)
          orientation = @solver.solve(
            faces, seam_reports, registration.verified_orientations || {}
          )
          finalize(faces, seam_reports, orientation)
        end

        private

        def mesh_faces(raw_parts)
          face_id = 0
          raw_parts.flat_map do |parts|
            @grouper.group(parts).map do |spec|
              result = @mesher.mesh(spec)
              triangles = result.triangles.map do |triple|
                Triangle.new(triple[0], triple[1], triple[2], face_id)
              end
              face = Face.new(
                id: face_id,
                record_index: spec.record_index,
                part_indexes: spec.part_indexes,
                source_kind: spec.source_kind,
                rings: spec.rings,
                ring_roles: spec.ring_roles,
                triangles: triangles,
                plane_origin: result.plane_origin,
                plane_normal: result.plane_normal,
                area3d: triangles.sum(&:area3d),
                source_area3d: result.source_area3d,
                introduced_points: result.introduced_points
              )
              face_id += 1
              face
            end
          end
        end

        def refine_faces(faces, seam_reports)
          edges_by_face = seam_reports.each_with_object(Hash.new { |hash, key| hash[key] = [] }) do |report, mapping|
            report.edges.each { |edge| mapping[edge.face_id] << edge }
          end
          seam_ids_by_face = seam_reports.each_with_object(Hash.new { |hash, key| hash[key] = [] }) do |report, mapping|
            report.registered_face_ids.each { |face_id| mapping[face_id] << report.id }
          end

          faces.map do |face|
            edges = edges_by_face[face.id]
            triangles = edges.empty? ? face.triangles : @refiner.refine(face, edges)
            Face.new(
              id: face.id,
              record_index: face.record_index,
              part_indexes: face.part_indexes,
              source_kind: face.source_kind,
              rings: face.rings,
              ring_roles: face.ring_roles,
              triangles: triangles,
              plane_origin: face.plane_origin,
              plane_normal: face.plane_normal,
              area3d: triangles.sum(&:area3d),
              source_area3d: face.source_area3d,
              seam_ids: seam_ids_by_face[face.id],
              introduced_points: face.introduced_points
            )
          end
        end

        def finalize(faces, seam_reports, orientation)
          blocked = blocked_face_ids(orientation[:reports])
          delivered = faces.reject { |face| blocked.include?(face.id) }
          triangles = delivered.flat_map do |face|
            flip = orientation[:flips][face.id]
            face.triangles.map { |triangle| flip ? triangle.flip : triangle }
          end
          Result.new(
            triangles: triangles,
            faces: faces.map { |face| face_with_status(face, blocked) },
            seam_reports: seam_reports,
            orientation_reports: orientation[:reports]
          )
        end

        def blocked_face_ids(reports)
          reports.reject(&:ok).flat_map(&:face_ids)
        end

        def face_with_status(face, blocked)
          return face unless blocked.include?(face.id)

          Face.new(
            id: face.id,
            record_index: face.record_index,
            part_indexes: face.part_indexes,
            source_kind: face.source_kind,
            rings: face.rings,
            ring_roles: face.ring_roles,
            triangles: face.triangles,
            plane_origin: face.plane_origin,
            plane_normal: face.plane_normal,
            area3d: face.area3d,
            source_area3d: face.source_area3d,
            deliverable: false,
            seam_ids: face.seam_ids,
            introduced_points: face.introduced_points
          )
        end
      end
    end
  end
end
