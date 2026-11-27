# frozen_string_literal: true

require_relative 'volume_combine/tiny_pdf'
require_relative 'volume_combine/fixtures'
require_relative 'volume_combine/raw_reader'
require_relative 'volume_combine/manifest'
require_relative 'volume_combine/planner'
require_relative 'volume_combine/stable_pdf'
require_relative 'volume_combine/combiner'
require_relative 'volume_combine/auditor'
require_relative 'volume_combine/report'

module VolumeCombine
  # End to end driver: parse the manifest and source volumes, plan every
  # reference, build the combined PDF and verify the reopened delivery.
  class Assembler
    def self.run(manifest_path, output_pdf:, report_md:, report_json:)
      manifest = Manifest.load(manifest_path)
      raw_models = manifest.sources.transform_values do |meta|
        RawReader.read(meta.fetch('path'))
      end
      planner = Planner.new(manifest, raw_models).plan
      combiner = Combiner.new(manifest, raw_models, planner)
      combiner.build.save(output_pdf)

      auditor = Auditor.new(manifest, raw_models, planner, output_pdf,
                            name_tree_rows: combiner.name_tree_rows).audit
      report = Report.new(manifest, planner, auditor, output_pdf)
      IO.binwrite(report_md, report.to_markdown)
      IO.binwrite(report_json, report.to_json)
      { manifest: manifest, planner: planner, auditor: auditor }
    end
  end
end
