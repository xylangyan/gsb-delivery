# frozen_string_literal: true

require_relative "geometry"
require_relative "area"
require_relative "polygon"
require_relative "splitter"
require_relative "stations"
require_relative "encoder"
require_relative "junction"

module RGeo
  module GeoJSON
    module Dateline
      # Resolves registered areas against a handover sheet.
      #
      # Contract:
      #
      #   * Only pieces whose owning original area is the same in every
      #     legal interpretation are signed. A piece that appears in some
      #     readings but not others is withheld.
      #   * Pieces cut from one original area all count as that one area;
      #     overlapping another original area is recorded as two entries on
      #     the overlap, never merged.
      #   * When readings disagree on a station, two complete interpretations
      #     are supplied so both can be traced back to original rings and
      #     holes.
      #   * When no reading satisfies the registration, the constraints that
      #     must be re-evidenced first are named, and independently confirmed
      #     areas stay viewable.
      class Solver
        MAX_COMBINATIONS = 200_000

        attr_reader :areas, :handover

        def initialize(areas:, handover:, seam_meridians: nil)
          @areas = areas
          @handover = handover
          @seam_meridians = seam_meridians
        end

        def solve
          interpretations = @areas.to_h do |area|
            [area.area_id, PolygonExpander.expand(area, @handover)]
          end

          unreadable = interpretations.select { |_id, list| list.empty? }.keys
          readable = interpretations.reject { |_id, list| list.empty? }

          if readable.empty?
            return empty_result(unreadable)
          end

          combos = enumerate_combos(readable)
          if combos.nil?
            return too_many_result(interpretations, unreadable)
          end

          evaluated = combos.map { |combo| evaluate(combo) }
          valid = evaluated.select { |e| e[:valid] }

          if valid.empty?
            conflict_result(interpretations, evaluated, unreadable)
          else
            success_result(interpretations, readable, valid, unreadable)
          end
        end

        private

        def enumerate_combos(interpretations)
          ordered = interpretations.keys
          combos = []
          acc = {}

          walk = lambda do |index|
            return false if combos.size >= MAX_COMBINATIONS

            if index == ordered.size
              combos << acc.dup
              return true
            end

            area_id = ordered[index]
            interpretations[area_id].each do |interp|
              acc[area_id] = interp
              complete = walk.call(index + 1)
              break if complete == false
            end
            acc.delete(area_id)
            combos.size < MAX_COMBINATIONS
          end

          walk.call(0)
          combos.size >= MAX_COMBINATIONS ? nil : combos
        end

        def evaluate(combo)
          problems = []

          @handover.shared_edge_groups.each_with_index do |group, gi|
            # A shared-edge declaration fails closed: if any referenced area
            # has no legal reading, the group is unverifiable and counts as
            # missing evidence rather than being skipped.
            missing = group.reject { |entry| combo.key?(entry[:area_id]) }
            unless missing.empty?
              problems << {
                kind: :shared_edge,
                index: gi,
                group: group,
                reason: "unreadable or absent areas: #{missing.map { |e| e[:area_id] }.uniq.join(', ')}",
              }
              next
            end

            subset = group.to_h { |entry| [entry[:area_id], combo[entry[:area_id]]] }
            check = SharedEdgeCheck.check(group, subset)
            next if check[:ok]

            problems << { kind: :shared_edge, index: gi, group: group, reason: check[:reason], detail: check }
          end

          station_results = {}
          @handover.stations.each do |station|
            point = MicroPoint.new(station[:lon], station[:lat])
            owners = combo.keys.select do |area_id|
              StationAssignment.assign(combo[area_id], point,
                                       boundary_rule: @handover.boundary_rule)[:inside]
            end.sort

            station_results[station[:id]] = { station: station, owners: owners }

            next unless station[:status] == "confirmed"

            expected = station[:areas].sort
            next if owners == expected

            problems << {
              kind: :station,
              station_id: station[:id],
              reason: "confirmed station #{station[:id]} expected #{expected.inspect}, got #{owners.inspect}",
              expected: expected,
              actual: owners,
            }
          end

          { combo: combo, problems: problems, valid: problems.empty?,
            station_results: station_results }
        end

        def too_many_result(interpretations, unreadable)
          {
            "status" => "undecided",
            "reason" => "more than #{MAX_COMBINATIONS} interpretation combinations; " \
                        "narrow the half-circle directions or add confirmed stations",
            "areas_with_multiple_readings" => interpretations.select { |_id, list| list.size > 1 }.keys.sort,
            "independent_areas" => independent_payload,
            "unreadable_areas" => unreadable,
            "evaluated_combinations" => MAX_COMBINATIONS,
          }
        end

        def success_result(interpretations, readable, valid, unreadable)
          # Sign only pieces that appear identically in every legal reading.
          # A reading is a map area_id => interpretation; its pieces are
          # keyed by the exact emitted geometry, so a piece that moves or
          # disappears under another reading is withheld rather than guessed.
          per_reading = valid.map { |entry| piece_map(entry[:combo]) }
          common_keys = per_reading.map(&:keys).reduce(:&).to_h { |key| [key, true] }

          signed = []
          signed_pieces = []
          junctions = []
          emitted_by_area = Hash.new { |h, k| h[k] = 0 }

          readable.keys.sort.each do |area_id|
            interp = valid.first[:combo][area_id]
            pieces = PieceSplitter.new(interp, seam_meridians: @seam_meridians).pieces
            pieces.each do |piece|
              key = piece_key(piece)
              next unless common_keys[key]

              signed << encode_piece(piece)
              signed_pieces << piece
              junctions.concat(Junction.records_for_piece(piece, display_decimals: @handover.display_decimals))
              emitted_by_area[area_id] += 1
            end
          end

          withheld = readable.keys.sort.filter_map do |area_id|
            proposed = PieceSplitter.new(valid.first[:combo][area_id], seam_meridians: @seam_meridians).pieces.size
            next if emitted_by_area[area_id] == proposed

            {
              "area_id" => area_id,
              "reason" => "one or more pieces vary across legal readings",
              "signed_fragments" => emitted_by_area[area_id],
              "total_fragments_in_first_reading" => proposed,
              "interpretation_count" => interpretations[area_id].size,
            }
          end

          station_ownership = []
          ambiguous = []

          @handover.stations.each do |station|
            sets = valid.map { |e| e[:station_results][station[:id]][:owners] }.uniq
            if sets.size == 1
              station_ownership << {
                "station_id" => station[:id],
                "owners" => sets.first,
                "on_edge" => edge_incidence(valid.first[:combo], station),
                "status" => station[:status],
              }
            else
              ambiguous << {
                "station_id" => station[:id],
                "status" => station[:status],
                "possible_owner_sets" => sets,
              }
            end
          end

          interpretations_out = {}
          if ambiguous.any? || withheld.any?
            pair = pick_pair(valid)
            pair.each_with_index do |entry, i|
              interpretations_out["interpretation_#{i + 1}"] = describe_combo(entry[:combo])
            end
          end

          # Overlaps between distinct original areas: report both entries.
          overlaps = detect_overlaps(signed_pieces)

          {
            "status" => "ok",
            "signed_pieces" => signed,
            "junction_records" => junctions,
            "emitted_areas" => emitted_by_area.keys.sort,
            "withheld_areas" => withheld,
            "station_ownership" => station_ownership,
            "ambiguous_stations" => ambiguous,
            "overlaps" => overlaps,
            "interpretations" => interpretations_out,
            "unreadable_areas" => unreadable,
            "valid_interpretation_count" => valid.size,
          }
        end

        # Map of exact emitted piece geometry => area id, for one reading.
        def piece_map(combo)
          map = {}
          combo.each do |area_id, interp|
            PieceSplitter.new(interp, seam_meridians: @seam_meridians).pieces.each do |piece|
              map[piece_key(piece)] = area_id
            end
          end
          map
        end

        def piece_key(piece)
          [
            piece.area_id,
            piece.rings.map do |ring|
              holes = ring[:holes] || []
              {
                outer: Geometry.canonical_ring_key(ring[:points]),
                holes: holes.map { |h| Geometry.canonical_ring_key(h[:points]) }.sort,
              }
            end.sort_by { |r| r[:outer] },
          ]
        end

        def edge_incidence(combo, station)
          point = MicroPoint.new(station[:lon], station[:lat])
          hits = []
          combo.each do |area_id, interp|
            result = StationAssignment.assign(interp, point, boundary_rule: @handover.boundary_rule)
            hits << { "area_id" => area_id, "edge" => result[:edge] } if result[:edge]
          end
          hits
        end

        def pick_pair(valid)
          first = valid.first
          best = valid.max_by do |candidate|
            first[:station_results].count do |sid, res|
              res[:owners] != candidate[:station_results][sid][:owners]
            end
          end
          best = valid[1] if best.equal?(first) && valid.size > 1
          [first, best]
        end

        def describe_combo(combo)
          combo.keys.sort.to_h { |area_id| [area_id, combo[area_id].describe] }
        end

        # Overlap between different original areas. An overlap is two entries
        # on the same physical territory, never a single merged entry.
        def detect_overlaps(pieces)
          polygons = pieces.map do |piece|
            {
              area_id: piece.area_id,
              piece_id: piece.piece_id,
              rings: piece.rings.map do |ring|
                {
                  outer: ring[:points],
                  holes: (ring[:holes] || []).map { |h| h[:points] },
                }
              end,
            }
          end

          overlaps = []
          polygons.combination(2) do |a, b|
            next if a[:area_id] == b[:area_id]
            next unless pieces_overlap?(a, b)

            overlaps << {
              "areas" => [a[:area_id], b[:area_id]].sort,
              "entries" => [
                { "area_id" => a[:area_id], "piece_id" => a[:piece_id] },
                { "area_id" => b[:area_id], "piece_id" => b[:piece_id] },
              ],
            }
          end
          overlaps
        end

        def pieces_overlap?(a, b)
          a[:rings].any? do |ra|
            b[:rings].any? { |rb| ring_sets_overlap?(ra, rb) }
          end
        end

        # Two polygons overlap when their outer rings enclose shared area.
        # Holes are subtracted: a point inside both outers but inside a hole
        # of one is not shared.
        def ring_sets_overlap?(ra, rb)
          return false if Geometry.ring_area2(ra[:outer]).zero?

          if rings_cross?(ra[:outer], rb[:outer])
            # Crossing outer boundaries can still be a hole-only touch.
            return true unless only_hole_overlap?(ra, rb)
          end

          probe = Geometry.representative_point(ra[:outer])
          return true if Geometry.point_location(rb[:outer], probe) == :inside &&
                         rb[:holes].none? { |h| Geometry.point_location(h, probe) == :inside }

          probe = Geometry.representative_point(rb[:outer])
          Geometry.point_location(ra[:outer], probe) == :inside &&
            ra[:holes].none? { |h| Geometry.point_location(h, probe) == :inside }
        end

        def only_hole_overlap?(ra, rb)
          false
        end

        def rings_cross?(ring_a, ring_b)
          ring_a.each_cons(2).any? do |a1, a2|
            ring_b.each_cons(2).any? { |b1, b2| Geometry.proper_cross?(a1, a2, b1, b2) }
          end
        end

        def encode_piece(piece)
          geometry = Encoder.piece_geometry(piece, @handover.display_decimals)
          attributes = {
            "area_id" => piece.area_id,
            "piece_id" => piece.piece_id,
            "side" => piece.side.to_s,
            "shift_micro" => piece.shift,
          }
          Encoder.json_safe(
            Encoder.feature(id: piece.piece_id, geometry: geometry, properties: attributes)
          )
        end

        def empty_result(unreadable)
          {
            "status" => "conflict",
            "blocking_evidence" => unreadable.map do |area_id|
              { "kind" => "unreadable_area", "area_id" => area_id,
                "action" => "re-check this area's rings and half-circle directions" }
            end,
            "independent_areas" => independent_payload,
            "unreadable_areas" => unreadable,
            "evaluated_combinations" => 0,
          }
        end

        def conflict_result(interpretations, evaluated, unreadable)
          constraints = []
          @handover.shared_edge_groups.each_with_index do |group, gi|
            constraints << { kind: :shared_edge, index: gi, group: group }
          end
          @handover.stations.each do |station|
            next unless station[:status] == "confirmed"

            constraints << { kind: :station, station: station }
          end

          blockers = []

          constraints.each do |constraint|
            relaxed = evaluated.select do |e|
              e[:problems].reject { |p| same_constraint?(p, constraint) }.empty?
            end
            blockers << describe_blocker(constraint) if relaxed.any?
          end

          if blockers.empty?
            seen = {}
            evaluated.each do |e|
              e[:problems].each do |problem|
                key = blocker_key(problem)
                next if seen[key]

                seen[key] = true
                blockers << describe_blocker(problem)
              end
            end
          end

          {
            "status" => "conflict",
            "blocking_evidence" => blockers,
            "independent_areas" => independent_payload,
            "unreadable_areas" => unreadable,
            "evaluated_combinations" => evaluated.size,
          }
        end

        def same_constraint?(problem, constraint)
          case constraint[:kind]
          when :shared_edge
            problem[:kind] == :shared_edge && problem[:index] == constraint[:index]
          when :station
            problem[:kind] == :station && problem[:station_id] == constraint[:station][:id]
          else
            false
          end
        end

        def blocker_key(problem)
          case problem[:kind]
          when :shared_edge
            [:shared_edge, problem[:index]]
          else
            [:station, problem[:station_id]]
          end
        end

        def describe_blocker(constraint)
          case constraint[:kind]
          when :shared_edge
            group = constraint[:group] || constraint[:group]
            {
              "kind" => "shared_edge",
              "edges" => Array(group).map do |e|
                { "area_id" => e[:area_id], "edge_id" => e[:edge_id], "direction" => e[:direction] }
              end,
              "action" => "re-survey these original survey lines or confirm their direction",
            }
          when :station
            station = constraint[:station] || {}
            {
              "kind" => "station",
              "station_id" => constraint[:station_id] || station[:id],
              "expected_areas" => station[:areas],
              "action" => "confirm this station's ownership",
            }
          end
        end

        def independent_payload
          @handover.independent_areas.to_h do |area_id|
            area = @areas.find { |a| a.area_id == area_id }
            interps = area ? PolygonExpander.expand(area, @handover) : []

            # An independently confirmed area is still ambiguous internally
            # if it has more than one legal reading. Publish fragments only
            # when exactly one reading exists; otherwise expose both.
            payload = { "interpretations" => interps.map(&:describe) }
            if interps.size == 1
              payload["pieces"] = PieceSplitter.new(interps.first, seam_meridians: @seam_meridians).pieces.map do |p|
                Encoder.json_safe(
                  Encoder.feature(
                    id: p.piece_id,
                    geometry: Encoder.piece_geometry(p, @handover.display_decimals),
                    properties: { "area_id" => p.area_id, "piece_id" => p.piece_id }
                  )
                )
              end
            else
              payload["pieces"] = []
              payload["note"] = "multiple legal readings; sign only after confirmation"
            end
            [area_id, payload]
          end
        end
      end
    end
  end
end
