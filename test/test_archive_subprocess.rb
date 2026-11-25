# frozen_string_literal: true

require "test/unit"
require "tmpdir"
require "fileutils"
require "rbconfig"
require "open3"
require "json"
require_relative "../lib/archive_handoff"

class ArchiveHandoffSubprocessTest < Test::Unit::TestCase
  RUBY = RbConfig.ruby
  ROOT = File.expand_path("..", __dir__)
  CLI = File.join(ROOT, "exe", "archive-handoff")

  def test_cli_resumes_in_a_separate_process
    Dir.mktmpdir("archive-subprocess-") do |dir|
      state_dir = File.join(dir, "state")
      warehouse_dir = File.join(dir, "warehouse")
      occurrence = "OCC-SUBPROCESS-1"
      warehouse = ArchiveHandoff::FileWarehouse.new(warehouse_dir)

      runner = ArchiveHandoff::Runner.new(state_dir, warehouse: warehouse)
      manifest = {
        order: [41, 42],
        segments: [{ number: 41, digest: "m41" }, { number: 42, digest: "m42" }]
      }
      runner.register_occurrence(occurrence, manifest: manifest)
      runner.verify_manifest!(occurrence, JSON.generate(order: [41, 42], segments: manifest[:segments]))

      config_path = File.join(dir, "config.rb")
      File.write(config_path, <<~RUBY)
        require "archive_handoff"
        { warehouse: ArchiveHandoff::FileWarehouse.new(#{warehouse_dir.dump}) }
      RUBY

      stdout, stderr, status = Open3.capture3(
        RUBY, "-I#{File.join(ROOT, 'lib')}", CLI,
        "resume", "--state", state_dir, "--occurrence", occurrence, "--config", config_path
      )

      assert status.success?, stderr
      snapshot = JSON.parse(stdout)
      assert_equal 1, snapshot["adopted"]["41"]["attempt"]
      assert_equal 1, snapshot["adopted"]["42"]["attempt"]
      assert_equal 1, warehouse.submission_count(occurrence, 41)
      assert_equal 1, warehouse.submission_count(occurrence, 42)

      stdout, _stderr, status = Open3.capture3(
        RUBY, "-I#{File.join(ROOT, 'lib')}", CLI,
        "resume", "--state", state_dir, "--occurrence", occurrence, "--config", config_path
      )
      assert status.success?
      assert_equal 1, warehouse.submission_count(occurrence, 41)
      assert_equal 1, warehouse.submission_count(occurrence, 42)
    end
  end
end
