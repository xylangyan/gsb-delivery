# frozen_string_literal: true

module Roo
  class Excelx
    # Builds and writes a local, self-contained workbook containing only the
    # selected sheets. Every included part is reached by following the OOXML
    # relationship graph from the workbook down, so parts that merely share a
    # file-name number with a selected sheet are never borrowed.
    class Exporter
      Part = Struct.new(
        :part, :source, :relationship, :chain, :required, :reason, :content_type,
        :sha256, :missing, :external,
        keyword_init: true
      ) do
        def external?
          external || relationship&.external? || source.to_s.match?(%r{\A[a-z][a-z0-9+.-]*://}i)
        end

        def to_h
          {
            part: part,
            source: source,
            required: required,
            reason: reason,
            content_type: content_type,
            sha256: sha256,
            missing: !!missing,
            external: external?,
            relationship: relationship&.to_h,
            provenance_chain: chain
          }
        end
      end

      SheetPlan = Struct.new(
        :name, :index, :kind, :state, :sheet_id, :relationship_id, :part, :parts,
        keyword_init: true
      ) do
        def to_h
          {
            name: name,
            index: index,
            kind: kind,
            state: state,
            sheet_id: sheet_id,
            relationship_id: relationship_id,
            part: part,
            parts: parts.map(&:to_h)
          }
        end
      end

      attr_reader :package, :source_path, :scope

      SCOPES = %i[complete strict].freeze

      def initialize(path_or_io, sheet_names:, prune_shared_strings: true, scope: :complete, source: nil)
        unless sheet_names.is_a?(Array) && !sheet_names.empty?
          raise ArgumentError, 'sheet_names is required: pass the authorized sheet names as an Array'
        end
        unless SCOPES.include?(scope)
          raise ArgumentError, "scope must be one of #{SCOPES.join(', ')}"
        end

        if path_or_io.is_a?(Package)
          @package = path_or_io
          @owns_package = false
        else
          @package = Package.new(path_or_io)
          @owns_package = true
        end

        @source_path = source || @package.path
        @source_description = @source_path || 'workbook (opened from a stream)'
        @requested_sheet_names = sheet_names.map(&:to_s)
        @prune_shared_strings = prune_shared_strings
        @scope = scope
      end

      def close
        @package.close if @owns_package
      end

      def plan
        @plan ||= build_plan
      end

      def self.plan(path_or_io, sheet_names:, prune_shared_strings: true, scope: :complete)
        exporter = new(path_or_io, sheet_names: sheet_names, prune_shared_strings: prune_shared_strings, scope: scope)
        begin
          exporter.plan
        ensure
          exporter.close
        end
      end

      def self.write(path_or_io, destination, sheet_names:, prune_shared_strings: true, scope: :complete)
        exporter = new(path_or_io, sheet_names: sheet_names, prune_shared_strings: prune_shared_strings, scope: scope)
        begin
          plan = exporter.plan
          result = exporter.write(destination, plan)
          {
            plan: plan,
            write: result,
            audit: Auditor.new(plan[:source], destination, sheet_names: plan[:selected_sheets]).report
          }
        ensure
          exporter.close
        end
      end

      def write(destination, plan_object = plan)
        missing = required_parts(plan_object).select { |part| part[:missing] }
        unless missing.empty?
          raise ExportError, "selected parts are missing: #{missing.map { |part| part[:part] }.join(', ')}"
        end

        strict = plan_object[:scope][:strict]
        if @scope == :strict && strict[:status] != 'possible'
          breaches = strict[:blocking_parts]
          raise ExportError,
                'a strict copy is not possible without carrying unselected content: ' \
                "#{breaches.map { |part| "#{part[:part]} (#{part[:verdict]})" }.join(', ')}"
        end

        Writer.new(package, plan_object, prune_shared_strings: @prune_shared_strings).write(destination)
      end

      private

      def build_plan
        sheets = @package.sheets
        selected = select_sheets(sheets)
        selected_names = selected.map(&:name)
        excluded_sheets = sheets.reject { |sheet| selected_names.include?(sheet.name) }

        root_parts = root_level_parts
        workbook_parts = workbook_level_parts
        sheet_plans = selected.map { |sheet| build_sheet_plan(sheet, sheets) }

        all_parts = root_parts + workbook_parts + sheet_plans.flat_map(&:parts)
        sharing = SharingAnalysis.new(@package, sheet_plans, excluded_sheets, root_parts + workbook_parts)
        shared_parts = sharing.assessments
        strict_blockers = shared_parts.select { |part| part[:content_bearing] && !part[:strict_subset_copy_self_consistent] }

        {
          source: @source_path,
          selected_sheets: selected_names,
          excluded_sheets: excluded_sheets.map(&:name),
          workbook_part: @package.workbook_part,
          workbook_relationships_part: @package.relationships_part_name(@package.workbook_part),
          root_parts: root_parts.map(&:to_h),
          workbook_parts: workbook_parts.map(&:to_h),
          sheets: sheet_plans.map(&:to_h),
          shared_parts: shared_parts,
          unauthorized_parts: sharing.unauthorized_parts,
          consistent_copy_possible: sharing.consistent_copy_possible?,
          scope: {
            requested: @scope,
            strict: {
              status: strict_blockers.empty? ? 'possible' : 'not_possible',
              blocking_parts: strict_blockers,
              meaning: 'A strict copy contains only selected-sheet content; shared content-bearing parts must be rewritable into a per-sheet subset and must not carry unselected content.'
            },
            complete: {
              status: 'possible',
              meaning: 'A complete copy follows all relationships the selected sheets actually need. Whole shared parts are retained where they cannot be split; the plan records every such part.'
            }
          },
          missing_parts: all_parts.select { |part| part.missing }.map(&:part).uniq,
          external_references: external_reference_summary,
          provenance: 'each part is keyed by its relationship owner and resolved target; no file-name or sheet-position fallback is used'
        }
      end

      def select_sheets(sheets)
        missing = @requested_sheet_names - sheets.map(&:name)
        unless missing.empty?
          raise ArgumentError, "unknown sheet(s): #{missing.join(', ')}"
        end

        sheets.select { |sheet| @requested_sheet_names.include?(sheet.name) }
      end

      def root_level_parts
        parts = []

        @package.relationships(nil).each do |relationship|
          next if relationship.external?

          case relationship.type_name
          when 'core-properties'
            parts << part_for(relationship.resolved_target, relationship, 'package core properties', [])
          end
        end

        parts.compact
      end

      def workbook_level_parts
        workbook_part = @package.workbook_part
        parts = []
        parts << part_for(workbook_part, nil, 'workbook definition', [])
        parts << part_for(@package.relationships_part_name(workbook_part), nil, 'workbook relationships', [])

        @package.relationships(workbook_part).each do |relationship|
          next if relationship.external?

          case relationship.type_name
          when 'sharedStrings', 'styles', 'theme'
            parts << part_for(relationship.resolved_target, relationship, "workbook-level #{relationship.type_name}", [])
          end
        end

        @package.external_references.each do |reference|
          next unless reference[:part]

          parts << part_for(reference[:part], reference[:relationship], 'external workbook link', [])
          parts << part_for(@package.relationships_part_name(reference[:part]), nil, 'external workbook link relationships', [])
        end

        parts.compact
      end

      def build_sheet_plan(sheet, all_sheets)
        relationship = sheet.relationship
        unless relationship
          raise ArgumentError, "sheet #{sheet.name.inspect} has no resolvable relationship"
        end

        if relationship.external?
          raise ArgumentError, "sheet #{sheet.name.inspect} points to an external target #{relationship.target.inspect}"
        end

        parts = []
        traverse_part(relationship, 'sheet payload', [], parts)

        SheetPlan.new(
          name: sheet.name,
          index: all_sheets.index(sheet),
          kind: sheet.kind,
          state: sheet.state,
          sheet_id: sheet.sheet_id,
          relationship_id: sheet.relationship_id,
          part: relationship.resolved_target,
          parts: parts
        )
      end

      def traverse_part(relationship, reason, chain, parts)
        part_name = relationship.resolved_target
        part = part_for(part_name, relationship, reason, chain)
        return unless part

        parts << part
        return if relationship.external? || part.missing

        child_chain = chain + [relationship.to_h]
        rels_part = @package.relationships_part_name(part_name)
        if @package.exist?(rels_part)
          parts << part_for(rels_part, nil, "#{reason} relationships", child_chain)
        end

        @package.relationships(part_name).each do |child|
          unless child.external?
            traverse_part(child, child_reason(child), child_chain, parts)
            next
          end

          parts << Part.new(
            part: child.target,
            source: child.target,
            relationship: child,
            chain: child_chain + [child.to_h],
            required: false,
            reason: "#{child_reason(child)} (external target retained, not copied)",
            content_type: nil,
            sha256: nil,
            missing: false,
            external: true
          )
        end
      end

      def child_reason(relationship)
        case relationship.type_name
        when 'comments' then 'sheet comments'
        when 'vmlDrawing' then 'comment display shape'
        when 'drawing' then 'sheet drawing'
        when 'image' then 'visible image'
        when 'chart' then 'chart'
        when 'chartStyle', 'chartColorStyle' then 'chart style'
        when 'hyperlink' then 'hyperlink'
        when 'printerSettings' then 'printer settings'
        else relationship.type_name
        end
      end

      def part_for(part_name, relationship, reason, chain)
        return nil unless part_name

        exists = @package.exist?(part_name)
        Part.new(
          part: part_name,
          source: @source_description,
          relationship: relationship,
          chain: relationship ? chain + [relationship.to_h] : chain,
          required: true,
          reason: reason,
          content_type: @package.content_type_for(part_name),
          sha256: exists ? @package.digest_for(part_name) : nil,
          missing: !exists,
          external: false
        )
      end

      def required_parts(plan_object)
        (plan_object[:root_parts] || []) + plan_object[:workbook_parts] +
          plan_object[:sheets].flat_map { |sheet| sheet[:parts] }
      end

      def external_reference_summary
        @package.external_references.map do |reference|
          {
            index: reference[:index],
            relationship: reference[:relationship]&.to_h,
            part: reference[:part],
            included: !reference[:part].nil? && @package.exist?(reference[:part]),
            external_target: reference[:relationship]&.target,
            external_target_copied: false,
            reason: 'external workbook target is kept as an external relationship; the target workbook is not copied'
          }
        end
      end

      # Determines which included parts are also reachable from sheets that are
      # not selected, and whether a self-consistent partial copy exists.
      class SharingAnalysis
        def initialize(package, sheet_plans, excluded_sheets, workbook_parts)
          @package = package
          @sheet_plans = sheet_plans
          @excluded_sheets = excluded_sheets
          @workbook_parts = workbook_parts
          @excluded_closures = excluded_sheets.each_with_object({}) do |sheet, result|
            result[sheet.name] = closure_for(sheet)
          end
        end

        def assessments
          selected_names = @sheet_plans.map(&:name)

          included = (@workbook_parts + @sheet_plans.flat_map(&:parts))
                     .reject(&:external?)
                     .reject(&:missing)
                     .uniq { |part| part.part }

          included.filter_map do |part|
            users = users_for_part(part, selected_names).uniq
            next if users.length < 2

            classify(part.part, users, selected_names, users - selected_names)
          end
        end

        def unauthorized_parts
          selected_names = @sheet_plans.map(&:name)
          included_names = (@workbook_parts + @sheet_plans.flat_map(&:parts)).map(&:part).to_set

          @excluded_closures.flat_map do |sheet_name, part_names|
            part_names.reject { |part| included_names.include?(part) }.map do |part|
              {
                part: part,
                owner_sheet: sheet_name,
                selected_sheets: selected_names,
                reason: "reachable only from unselected sheet #{sheet_name}; it is not used to fill a missing part of #{selected_names.join(', ')}"
              }
            end
          end.uniq { |entry| entry[:part] }
        end

        def consistent_copy_possible?
          unique_selected = @sheet_plans.map(&:part).compact.uniq
          return false unless unique_selected.length == @sheet_plans.length

          missing_required = (@workbook_parts + @sheet_plans.flat_map(&:parts)).any?(&:missing)
          !missing_required
        end

        private

        def users_for_part(part, selected_names)
          if @workbook_parts.any? { |workbook_part| workbook_part.part == part.part }
            selected_names + @excluded_sheets.map(&:name)
          else
            users = @sheet_plans.select { |sheet| sheet.parts.any? { |sheet_part| sheet_part.part == part.part } }
                                .map(&:name)
            users + @excluded_closures.select { |_, names| names.include?(part.part) }.keys
          end
        end

        def selected_users_for(part, selected_names)
          users_for_part(part, selected_names) & selected_names
        end

        def closure_for(sheet)
          names = []
          visited = {}
          walk(sheet.part, names, visited) if sheet.part && !sheet.relationship&.external?
          names
        end

        def walk(part_name, names, visited)
          return if visited[part_name]
          return unless @package.exist?(part_name)

          visited[part_name] = true
          names << part_name
          names << @package.relationships_part_name(part_name) if @package.exist?(@package.relationships_part_name(part_name))

          @package.relationships(part_name).each do |relationship|
            walk(relationship.resolved_target, names, visited) unless relationship.external?
          end
        end

        def classify(part_name, users, selected_names, excluded_users)
          kind = classify_kind(part_name)
          verdict = verdict_for(kind, part_name, excluded_users)
          {
            part: part_name,
            shared_with_sheets: users,
            unselected_users: excluded_users,
            selected_sheets: selected_names,
            kind: kind,
            content_bearing: verdict[:content_bearing],
            split_required: users.length > 1,
            whole_part_structural_copy_valid: verdict[:whole_part_valid],
            whole_part_carries_unselected_content: verdict[:whole_part_leaks],
            strict_subset_copy_self_consistent: verdict[:strict_subset],
            strict_subset_requires_rewrite: verdict[:requires_rewrite],
            verdict: verdict[:verdict],
            strategy: verdict[:strategy],
            rationale: verdict[:rationale]
          }
        end

        def classify_kind(part_name)
          basename = File.basename(part_name)
          if basename.end_with?('sharedStrings.xml')
            'shared_strings'
          elsif basename.end_with?('styles.xml')
            'styles'
          elsif part_name.include?('theme/')
            'theme'
          elsif basename.end_with?('.vml')
            'vml'
          elsif part_name.include?('/drawings/') && basename.end_with?('.xml')
            'drawing'
          elsif basename.start_with?('comments')
            'comments'
          elsif part_name.include?('/media/')
            'media'
          elsif part_name.include?('/externalLinks/') && basename.end_with?('.xml')
            'external_link'
          elsif part_name.include?('/charts/')
            'chart'
          elsif part_name.end_with?('.xml.rels')
            'relationships'
          elsif part_name.start_with?('docProps/')
            'package_metadata'
          elsif basename == 'workbook.xml'
            'workbook'
          else
            'other'
          end
        end

        def verdict_for(kind, part_name, excluded_users)
          case kind
          when 'shared_strings'
            {
              content_bearing: true,
              whole_part_valid: true,
              whole_part_leaks: true,
              strict_subset: true,
              requires_rewrite: true,
              verdict: 'strict_subset_possible',
              strategy: 'prune_to_referenced_entries_and_remap_cell_indices',
              rationale: 'selected sheets address entries by index; unused entries are dropped and every cell index in the selected sheets is remapped. A self-consistent A-only copy is achievable and carries no text used only by unselected sheets.'
            }
          when 'styles'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: true,
              requires_rewrite: true,
              verdict: 'strict_subset_possible',
              strategy: 'retain_whole_table_or_remap_style_ids',
              rationale: 'style ids are positional. Retaining the table whole is valid and exposes no cell values; a strict style subset is also possible but requires remapping every s= attribute plus font/fill/border ids.'
            }
          when 'theme'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: false,
              requires_rewrite: false,
              verdict: 'whole_part_only_safe',
              strategy: 'retain_whole_part',
              rationale: 'a theme is one XML document with no addressable sub-parts. It carries no sheet cell data, so retaining it whole is safe and keeps emitted style references valid.'
            }
          when 'media'
            {
              content_bearing: true,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: false,
              requires_rewrite: false,
              verdict: excluded_users.any? ? 'whole_part_required_no_partial_slice' : 'part_optional_per_drawing_reference',
              strategy: excluded_users.any? ? 'retain_whole_part_when_referenced' : 'drop_if_no_kept_drawing_relationship_references_it',
              rationale: excluded_users.any? ?
                "#{part_name} is a single image target also referenced by #{excluded_users.join(', ')}. An image cannot be half-copied; if a kept drawing references it, the whole media part must be retained. That is valid and does not add unselected cell values." :
                'no unselected sheet references this media part; it can be dropped whenever no kept drawing relationship refers to it.'
            }
          when 'comments'
            {
              content_bearing: true,
              whole_part_valid: true,
              whole_part_leaks: true,
              strict_subset: false,
              requires_rewrite: true,
              verdict: 'strict_subset_not_derivable',
              strategy: 'rebuild_sheet_scoped_comments_part',
              rationale: 'a comments part has one relationship target and its entries carry cell references but no sheet tag. When A and B share it, the part itself cannot tell which comments belong to A. A strict A-only subset cannot be derived from this part; making one requires authoring a new sheet-scoped comments part and a new relationship, not a partial copy.'
            }
          when 'drawing'
            {
              content_bearing: true,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: false,
              requires_rewrite: true,
              verdict: excluded_users.any? ? 'whole_part_required_no_partial_slice' : 'part_optional_per_drawing_reference',
              strategy: excluded_users.any? ? 'retain_whole_part_when_referenced' : 'rebuild_sheet_scoped_drawing',
              rationale: excluded_users.any? ?
                "a shared drawing is displayed as a whole on every sheet that references it. All of its anchors are therefore part of A's visible content; an anchor cannot be attributed to A alone, so the whole drawing must be retained and a strict partial slice is not meaningful." :
                'the selected sheet has its own drawing part. It can be rewritten anchor by anchor, but that requires a new drawing part plus updated relationship ids.'
            }
          when 'external_link'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: true,
              requires_rewrite: false,
              verdict: 'whole_part_only_safe',
              strategy: 'retain_external_link_part_with_external_target',
              rationale: 'the external-link part records the reference and cached names; it carries no selected-sheet cell value. It is retained whole while the workbook target itself stays an external relationship and is not copied.'
            }
          when 'chart'
            {
              content_bearing: true,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: false,
              requires_rewrite: true,
              verdict: 'whole_part_required_no_partial_slice',
              strategy: 'retain_whole_part',
              rationale: 'chart XML is a single document; a partial chart would not be a valid chart part and its cached values cannot be split by sheet.'
            }
          when 'relationships'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: true,
              requires_rewrite: true,
              verdict: 'strict_subset_possible',
              strategy: 'rebuild_relationships_for_the_copy',
              rationale: 'the .rels part lists ids and targets for an owner; it is rebuilt with only the kept targets, so no retained relationship points at an excluded part.'
            }
          when 'workbook'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: true,
              requires_rewrite: true,
              verdict: 'strict_subset_possible',
              strategy: 'rebuild_sheet_list_and_drop_references_to_excluded_sheets',
              rationale: 'the workbook part is rewritten to list only selected sheets and to remove defined names that point at unselected sheets.'
            }
          when 'package_metadata'
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: true,
              requires_rewrite: false,
              verdict: 'whole_part_only_safe',
              strategy: 'retain_whole_part',
              rationale: 'package metadata documents describe the package as a whole and are not split per sheet; retaining them exposes no sheet cell values.'
            }
          else
            {
              content_bearing: false,
              whole_part_valid: true,
              whole_part_leaks: false,
              strict_subset: false,
              requires_rewrite: false,
              verdict: 'whole_part_only_safe',
              strategy: 'retain_whole_part',
              rationale: 'the part has no sheet-scoped payload; retaining it whole is valid and does not expose unselected content.'
            }
          end
        end

      end
    end
  end
end
