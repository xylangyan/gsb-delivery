# frozen_string_literal: true

require "test/unit"
require "tmpdir"
require "fileutils"
require "rbconfig"
require_relative "../lib/archive_handoff"

class ArchiveHandoffFullPipelineTest < Test::Unit::TestCase
  RUBY = RbConfig.ruby

  def test_fetch_runs_once_and_second_process_reuses_persisted_volume
    Dir.mktmpdir("archive-full-") do |dir|
      state_dir = File.join(dir, "state")
      count_file = File.join(dir, "fetch-count")
      occurrence = "OCC-FULL-1"
      warehouse = ArchiveHandoff::MemoryWarehouse.new

      fetch_script = <<~RUBY
        count_path = #{count_file.dump}
        count = File.exist?(count_path) ? File.read(count_path).to_i + 1 : 1
        File.write(count_path, count.to_s)
        require "json"
        print JSON.generate(
          order: [71],
          segments: [{ number: 71, digest: "manifest-digest-71" }]
        )
      RUBY
      commands = {
        fetch: [RUBY, "-e", fetch_script],
        normalize: [RUBY, "-e", "print STDIN.read"]
      }

      first = ArchiveHandoff::Runner.new(state_dir, warehouse: warehouse, commands: commands)
      first.run(occurrence)
      snapshot = first.snapshot(occurrence)

      assert_equal "1", File.read(count_file)
      assert_equal 1, snapshot[:adopted][71][:attempt]
      assert_equal 1, warehouse.submission_count(occurrence, 71)

      second = ArchiveHandoff::Runner.new(state_dir, warehouse: warehouse, commands: commands)
      second.run(occurrence)

      assert_equal "1", File.read(count_file)
      assert_equal 1, warehouse.submission_count(occurrence, 71)
      assert second.inspect(occurrence)[:raw_volume][:digest]
      assert second.inspect(occurrence)[:normalized][:digest]
    end
  end

  def test_submission_is_blocked_before_manifest_verification
    Dir.mktmpdir("archive-manifest-") do |dir|
      runner = ArchiveHandoff::Runner.new(dir, warehouse: ArchiveHandoff::MemoryWarehouse.new)
      runner.register_occurrence("OCC-UNVERIFIED", manifest: { order: [1], segments: [{ number: 1 }] })
      runner.package_segment("OCC-UNVERIFIED", 1, previous_digest: nil, bytes: "pkg")

      assert_raise(ArchiveHandoff::ManifestError) do
        runner.submit_attempt("OCC-UNVERIFIED", 1)
      end
    end
  end
end
