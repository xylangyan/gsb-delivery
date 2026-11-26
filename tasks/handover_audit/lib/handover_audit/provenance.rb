# frozen_string_literal: true

module HandoverAudit
  # Translates between the three numbering spaces in an audit:
  # the continuous publish line numbers written into a delivery's receipt,
  # the original material ids archived in the material table, and the ordinal
  # position of a row inside a sealed, already materialized view.
  class Provenance
    def initialize(audit_case)
      @audit_case = audit_case
      @id_output_col = {}
      @view_row_id = {}
      build_indexes
    end

    # Material id that the approval slip declares for publish line +pub_no+
    # of the given delivery.
    def expected_material_id(delivery_id, pub_no)
      map = @audit_case.deliveries.fetch(delivery_id)[:row_map]
      case map[:method]
      when :fixed
        map[:value]
      when :sequence
        map[:start] + ((pub_no - 1) * map[:step])
      when :segment
        seg = map[:source].find { |s| pub_no.between?(s[:pub_first], s[:pub_last]) }
        raise Case::SpecError, "delivery #{delivery_id} publish row #{pub_no} is outside any declared segment" unless seg

        seg[:mat_first] + (pub_no - seg[:pub_first])
      end
    end

    # Name of the view output column that carries the original material id for
    # the given delivery.
    def id_column(delivery_id)
      @id_output_col.fetch(delivery_id)
    end

    # Original material id carried by a materialized view row when read through
    # the given delivery.
    def view_row_material_id(delivery_id, row)
      row.fetch(id_column(delivery_id))
    end

    private

    def build_indexes
      @audit_case.deliveries.each do |did, delivery|
        @id_output_col[did] = resolve_id_column(did, delivery)
      end
    end

    def resolve_id_column(did, delivery)
      explicit = delivery[:id_column]
      return explicit if explicit

      inverted = delivery[:columns].invert
      inferred = inverted[@audit_case.material_id_col]
      return inferred if inferred

      raise Case::SpecError,
        "delivery #{did} cannot locate the material id column :#{@audit_case.material_id_col}; " \
        "add an id_column or a columns mapping to it"
    end
  end
end
