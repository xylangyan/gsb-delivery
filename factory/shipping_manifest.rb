# frozen_string_literal: true

require 'json'
require_relative 'catalog'

# Renders the consolidated shipping manifest (machine JSON + human change list)
# from the audited build report. Nothing here recomputes selections; it only
# records what the packer chose and the auditor already verified.
module ShippingManifest
  module_function

  def build(report)
    packages = report.map do |entry|
      cabinet = Catalog.cabinet(entry[:cabinet])
      result = entry[:result]
      {
        cabinet: entry[:cabinet],
        title: cabinet.title,
        edition: entry[:edition],
        rule_source: result[:rule_source],
        rule_label: result[:rule_label],
        package_root: result[:root],
        budget: result[:budget],
        total_bytes: result[:total_bytes],
        within_budget: result[:within_budget],
        audit: entry[:failures].empty? ? 'PASS' : 'FAIL',
        shared_blocks: cabinet.shared_blocks,
        swap_plan: cabinet.prefer.transform_values(&:itself),
        files: result[:files]
      }
    end

    {
      global_budget: Catalog::GLOBAL_BUDGET,
      global_total_bytes: packages.sum { |p| p[:total_bytes] },
      rule_sources: Catalog::RULE_SOURCES.transform_values { |path| File.basename(path) },
      packages: packages
    }
  end

  def write(report)
    data = build(report)
    json_path = File.join(Catalog::BUILD_DIR, 'SHIPPING_MANIFEST.json')
    File.write(json_path, JSON.pretty_generate(data))

    md = +"# Cabinet TZInfo data change list (换包清单)\n\n"
    md << "Global shipped total: **#{data[:global_total_bytes]} B** / cap #{data[:global_budget]} B.\n"
    md << "Rule editions: A=#{Catalog::RULE_LABEL['A']} (#{data[:rule_sources]['A']}), "
    md << "B=#{Catalog::RULE_LABEL['B']} (#{data[:rule_sources]['B']}).\n\n"

    data[:packages].each do |pkg|
      md << "## #{pkg[:cabinet]} — #{pkg[:title]} — edition #{pkg[:edition]} (#{pkg[:rule_label]}) [#{pkg[:audit]}]\n"
      md << "- Rule source: `#{pkg[:rule_source]}`\n"
      md << "- Package root: `#{pkg[:package_root]}`\n"
      md << "- Bytes: #{pkg[:total_bytes]} / budget #{pkg[:budget]} (#{pkg[:within_budget] ? 'within' : 'OVER'})\n"
      md << "- Shared blocks: #{pkg[:shared_blocks].join(', ')}\n\n"
      md << "| bytes | form | candidate | identifier | shared |\n|---:|---|---|---|---|\n"
      pkg[:files].each do |f|
        md << "| #{f[:bytes]} | #{f[:form]} | #{f[:candidate]} | #{f[:identifier]} | #{f[:shared] ? 'yes' : ''} |\n"
      end
      md << "\n"
    end

    md_path = File.join(Catalog::BUILD_DIR, 'SHIPPING_MANIFEST.md')
    File.write(md_path, md)
    [json_path, md_path]
  end
end
