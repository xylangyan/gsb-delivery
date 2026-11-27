# frozen_string_literal: true

require 'json'

module VolumeCombine
  # Renders the audit results: a human Markdown table for field use and a
  # machine readable JSON sidecar. Both list every internal link, bookmark and
  # external work order name, its concrete output page, the original reference
  # and the basis used, plus the fixed name conflicts and any gaps.
  class Report
    def initialize(manifest, planner, auditor, output_path)
      @manifest = manifest
      @planner = planner
      @auditor = auditor
      @output_path = output_path
    end

    def to_markdown
      lines = []
      lines << '# 拼卷交付核对报告'
      lines << ''
      lines << "交付件: `#{@output_path}`"
      lines << "重开校验: #{@auditor.ok? ? '通过 (无落点不一致)' : '发现不一致，见文末'}"
      lines << ''
      lines << '## 出页顺序'
      lines << ''
      lines << '| 出页 | 组号 | 原卷 | 原页 | 页面标记 |'
      lines << '| --- | --- | --- | --- | --- |'
      @manifest.slots.each do |slot|
        marker = @auditor.markers[slot.slot - 1]
        lines << "| #{slot.slot} | #{slot.group} | #{slot.source} | #{slot.page_index + 1} | #{marker} |"
      end
      lines << ''

      section(lines, '内链落点', @auditor.rows.select { |row| row.kind == 'link' })
      section(lines, '书签落点', @auditor.rows.select { |row| row.kind == 'bookmark' })
      section(lines, '外部工单名落点', @auditor.rows.select { |row| row.kind == 'work_order' })
      section(lines, '外部 URI (保持原样)', @auditor.rows.select { |row| row.kind == 'URI' })

      lines << '## 固定名冲突'
      lines << ''
      if @planner.name_conflicts.empty?
        lines << '无'
      else
        lines << '| 名称 | 出现在原卷 | 处理 |'
        lines << '| --- | --- | --- |'
        @planner.name_conflicts.each do |conflict|
          lines << "| #{escape(conflict.name.dup.force_encoding('UTF-8'))} | #{conflict.sources.join(', ')} | 内部锚按 `名称#原卷` 改写，各自指向本卷实物页 |"
        end
      end
      lines << ''

      lines << '## 缺项 (无获准替代，未跳转另一期同名)'
      lines << ''
      gaps = @auditor.gaps
      if gaps.empty?
        lines << '无'
      else
        lines << '| 类型 | 来源 | 组号 | 原因 |'
        lines << '| --- | --- | --- | --- |'
        gaps.each do |gap|
          lines << "| #{gap.ref_type} | #{gap.origin_label} | #{gap.group} | #{escape(gap.reason)} |"
        end
      end
      lines << ''

      unless @auditor.discrepancies.empty?
        lines << '## 不一致'
        lines << ''
        @auditor.discrepancies.each do |d|
          lines << "- #{d.where}: 期望 #{d.expected.inspect}，实际 #{d.actual.inspect}"
        end
        lines << ''
      end

      lines.join("\n")
    end

    def to_json(*_args)
      JSON.pretty_generate(
        output: utf8(@output_path),
        ok: @auditor.ok?,
        slots: @manifest.slots.map do |slot|
          { slot: slot.slot, group: slot.group, source: slot.source,
            source_page: slot.page_index + 1, marker: utf8(@auditor.markers[slot.slot - 1]) }
        end,
        rows: @auditor.rows.map { |row| deep_utf8(row.to_h.transform_values { |v| v.is_a?(Symbol) ? v.to_s : v }) },
        name_conflicts: @planner.name_conflicts.map { |c| deep_utf8(c.to_h) },
        gaps: @planner.gaps.map { |gap| deep_utf8(gap.to_h.transform_values { |v| v.is_a?(Symbol) ? v.to_s : v }) },
        discrepancies: @auditor.discrepancies.map { |d| deep_utf8(d.to_h) }
      )
    end

    private

    # PDF strings are ASCII-8BIT; JSON needs UTF-8 for non-ASCII names.
    def utf8(value)
      value.is_a?(String) ? value.dup.force_encoding(Encoding::UTF_8) : value
    end

    def deep_utf8(value)
      case value
      when String then utf8(value)
      when Hash then value.transform_values { |v| deep_utf8(v) }
      when Array then value.map { |v| deep_utf8(v) }
      else value
      end
    end

    def section(lines, title, rows)
      lines << "## #{title}"
      lines << ''
      lines << '| 来源 | 原始指向 | 落到出页 | 页面标记 | 视图 | 采用依据 |'
      lines << '| --- | --- | --- | --- | --- | --- |'
      if rows.empty?
        lines << '| (无) | | | | | |'
      else
        rows.each do |row|
          lines << "| #{escape(row.origin)} | #{escape(row.original.to_s)} | #{row.output_slot || '缺项'} | #{row.output_marker || '-'} | #{escape((row.view || []).inspect)} | #{row.basis}: #{escape(row.detail)} |"
        end
      end
      lines << ''
    end

    def escape(text)
      text.to_s.gsub('|', '\\|').gsub("\n", ' ')
    end
  end
end
