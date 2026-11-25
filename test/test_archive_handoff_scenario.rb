# frozen_string_literal: true

require "test/unit"
require "tmpdir"
require "fileutils"
require_relative "../lib/archive_handoff"

class ArchiveHandoffScenarioTest < Test::Unit::TestCase
  OCCURRENCE = "OCC-20261126-OLD"
  CORRECTION = "OCC-20261126-CORR-1"

  def setup
    @dir = Dir.mktmpdir("archive-handoff-")
    @warehouse = ArchiveHandoff::MemoryWarehouse.new(auto_commit: true)
    @old_request_42 = nil
    @first_42_query = nil
  end

  def teardown
    FileUtils.remove_entry(@dir) if @dir && File.directory?(@dir)
  end

  def test_old_attempt_commits_before_settle_and_lost_settle_response_can_be_recovered
    loss = 1
    gateway = controlled_gateway do |key|
      if key[:segment] == 42 && key[:attempt] == 1
        @warehouse.deliver_submission(@old_request_42)
        result = @warehouse.settle_attempt(key)
        if loss.positive?
          loss -= 1
          raise ArchiveHandoff::SettlementResponseLost, "simulated lost settle response"
        end
        result
      else
        @warehouse.settle_attempt(key)
      end
    end

    seed_old_volume(gateway)
    runner = ArchiveHandoff::Runner.new(@dir, gateway: gateway)

    assert_raise(ArchiveHandoff::SettlementResponseLost) do
      runner.resume(OCCURRENCE)
    end

    recovery = ArchiveHandoff::Runner.new(@dir, warehouse: @warehouse)
    recovery.resume(OCCURRENCE)
    snapshot = recovery.snapshot(OCCURRENCE)

    assert_equal 1, @first_42_query[:attempt]
    assert_nil @first_42_query[:result]
    assert_equal 1, @warehouse.submission_count(OCCURRENCE, 41)
    assert_equal 1, @warehouse.submission_count(OCCURRENCE, 42)
    assert_equal 1, @warehouse.submission_count(OCCURRENCE, 43)

    adopted42 = snapshot[:adopted][42]
    assert_equal 1, adopted42[:attempt]
    assert_equal old_package42_digest, adopted42[:package_digest]
    assert_equal old_package42_digest, adopted42[:actual_digest]
    assert_equal "receipt-42-1", adopted42[:receipt][:id]

    adopted43 = snapshot[:adopted][43]
    assert_equal prebuilt_package43_digest, adopted43[:package_digest]
    package_bytes = File.binread(adopted_package_path(recovery, 43))
    assert_equal "43-prebuilt-original-bytes", package_bytes
  end

  def test_old_attempt_is_settled_unpostable_before_late_request_and_retry_uses_new_digests
    loss = 1
    gateway = controlled_gateway do |key|
      if key[:segment] == 42 && key[:attempt] == 1
        result = @warehouse.settle_attempt(key)
        @warehouse.submit_package(@old_request_42)
        if loss.positive?
          loss -= 1
          raise ArchiveHandoff::SettlementResponseLost, "simulated lost settle response"
        end
        result
      else
        @warehouse.settle_attempt(key)
      end
    end

    seed_old_volume(gateway)
    runner = ArchiveHandoff::Runner.new(@dir, gateway: gateway)

    assert_raise(ArchiveHandoff::SettlementResponseLost) do
      runner.resume(OCCURRENCE)
    end

    recovery = ArchiveHandoff::Runner.new(@dir, warehouse: @warehouse)
    recovery.resume(OCCURRENCE)
    snapshot = recovery.snapshot(OCCURRENCE)

    assert_equal 1, @warehouse.submission_count(OCCURRENCE, 41)
    assert_equal 2, @warehouse.submission_count(OCCURRENCE, 42)
    assert_equal 1, @warehouse.submission_count(OCCURRENCE, 43)
    assert_equal 1, @warehouse.late_calls.length
    assert_equal old_package42_digest, @warehouse.late_calls.first[:package_digest]

    adopted42 = snapshot[:adopted][42]
    assert_equal 2, adopted42[:attempt]
    assert_not_equal old_package42_digest, adopted42[:package_digest]
    assert_equal adopted42[:package_digest], adopted42[:actual_digest]

    adopted43 = snapshot[:adopted][43]
    assert_not_equal prebuilt_package43_digest, adopted43[:package_digest]
    segment43 = recovery.inspect(OCCURRENCE)[:segments].find { |segment| segment[:number] == 43 }
    new_package43 = segment43[:packages].find { |pkg| pkg[:id] == segment43[:adopted_package_id] }
    assert_equal adopted42[:actual_digest], new_package43[:previous_digest]

    correction = ArchiveHandoff::Runner.new(@dir, warehouse: @warehouse)
    correction.register_occurrence(CORRECTION, manifest: manifest, correction: true)
    correction_volume = JSON.generate(order: manifest[:order], segments: manifest[:segments])
    correction.verify_manifest!(CORRECTION, correction_volume)
    correction.resume(CORRECTION)
    correction_snapshot = correction.snapshot(CORRECTION)
    assert_equal true, correction_snapshot[:correction]
    assert_equal 1, correction_snapshot[:adopted][42][:attempt]

    stale_result = recovery.apply_receipt(
      occurrence_id: OCCURRENCE,
      segment: 42,
      attempt: 1,
      id: "late-old-receipt",
      digest: old_package42_digest
    )
    assert_equal "ignored_stale", stale_result[:status]
    assert_equal 2, recovery.snapshot(OCCURRENCE)[:adopted][42][:attempt]
    assert_equal 1, correction.snapshot(CORRECTION)[:adopted][42][:attempt]
  end

  private

  def manifest
    {
      order: [41, 42, 43],
      segments: [
        { number: 41, digest: "manifest-41" },
        { number: 42, digest: "manifest-42" },
        { number: 43, digest: "manifest-43" }
      ]
    }
  end

  def controlled_gateway(&settle_hook)
    ArchiveHandoff::Gateway.new(
      submit: lambda { |submission|
        if submission[:segment] == 42 && submission[:attempt] == 1
          @old_request_42 = submission
          nil
        else
          @warehouse.submit_package(submission)
        end
      },
      query: lambda { |key|
        result = @warehouse.find_attempt(key)
        if key[:segment] == 42 && key[:attempt] == 1 && @first_42_query.nil?
          @first_42_query = key.merge(result: result)
        end
        result
      },
      settle: settle_hook
    )
  end

  def seed_old_volume(gateway)
    seed = ArchiveHandoff::Runner.new(@dir, gateway: gateway)
    seed.register_occurrence(OCCURRENCE, manifest: manifest)
    raw_manifest_volume = JSON.generate(order: manifest[:order], segments: manifest[:segments])
    seed.verify_manifest!(OCCURRENCE, raw_manifest_volume)

    package41 = seed.package_segment(OCCURRENCE, 41, previous_digest: nil, bytes: "41-committed-package")
    seed.submit_attempt(OCCURRENCE, 41)
    seed.reconcile_segment(OCCURRENCE, 41)

    @package42 = seed.package_segment(
      OCCURRENCE, 42,
      previous_digest: package41[:digest],
      bytes: "42-old-in-flight-package"
    )
    seed.submit_attempt(OCCURRENCE, 42)

    @package43 = seed.package_segment(
      OCCURRENCE, 43,
      previous_digest: @package42[:digest],
      bytes: "43-prebuilt-original-bytes"
    )
  end

  def old_package42_digest
    @package42[:digest]
  end

  def prebuilt_package43_digest
    @package43[:digest]
  end

  def adopted_package_path(runner, segment_number)
    record = runner.inspect(OCCURRENCE)
    segment = record[:segments].find { |item| item[:number] == segment_number }
    segment[:packages].find { |pkg| pkg[:id] == segment[:adopted_package_id] }[:path]
  end
end
