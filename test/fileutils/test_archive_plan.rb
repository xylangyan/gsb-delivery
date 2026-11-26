# frozen_string_literal: true
# $Id$

require 'fileutils'
require 'test/unit'

class TestFileUtilsArchivePlan < Test::Unit::TestCase
  def small_manifest
    {
      "quota" => 6,
      "attachments" => (1..6).to_h { |i| [i.to_s, 1] },
      "verification_size" => 3,
      "queues" => [
        {"name" => "preview", "kind" => "preview",
         "items" => (1..6).map(&:to_s)},
        {"name" => "a", "kind" => "formal",
         "packages" => [%w[3 4], %w[1 2], %w[5 6]]},
        {"name" => "b", "kind" => "formal",
         "packages" => [%w[6 1], %w[2 3], %w[4 5]]}
      ]
    }
  end

  def large_manifest
    odd_preview = (1..30).step(2).map(&:to_s)
    even_preview = (2..30).step(2).map(&:to_s)
    odd_packages = (1..30).step(2).map { |s| [s.to_s, ((s % 30) + 1).to_s] }
    even_packages = (2..30).step(2).map { |s| [s.to_s, ((s % 30) + 1).to_s] }
    {
      "quota" => 30,
      "attachments" => (1..30).to_h { |i| [i.to_s, 1] },
      "queues" => [
        {"name" => "preview-odd", "kind" => "preview",
         "items" => odd_preview,
         "handoffs" => Array.new(4) { |i|
           {"id" => "handoff/#{i}", "verification_size" => 15}
         }},
        {"name" => "preview-even", "kind" => "preview",
         "items" => even_preview},
        {"name" => "formal-odd", "kind" => "formal",
         "packages" => odd_packages},
        {"name" => "formal-even", "kind" => "formal",
         "packages" => even_packages}
      ]
    }
  end

  def test_small_volume_minimum_and_peak
    plan = FileUtils.plan_archive(small_manifest)
    assert_true(plan.feasible?)
    assert_equal(10, plan.minimum_download_bytes)
    assert_equal(6, plan.peak_occupancy)
    assert_equal(6, plan.preview_download_bytes)
    assert_equal(4, plan.formal_download_bytes)
  end

  def test_small_volume_bill_cap_is_rejected_before_download
    plan = FileUtils.plan_archive(small_manifest.merge("bill_limit" => 9))
    assert_false(plan.feasible?)
    assert_equal(10, plan.minimum_download_bytes)
    assert_match(/above bill limit 9/, plan.errors.join("\n"))
    assert_empty(plan.steps)
  end

  def test_small_volume_steps_replay
    plan = FileUtils.plan_archive(small_manifest)
    replay(small_manifest, plan)
  end

  def test_large_volume_is_exact_and_fast
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    plan = FileUtils.plan_archive(large_manifest)
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started

    assert_true(plan.feasible?)
    assert_equal(46, plan.minimum_download_bytes)
    assert_equal(30, plan.peak_occupancy)
    assert_operator(elapsed, :<, 10)
    replay(large_manifest, plan)
  end

  def test_large_volume_staging_budget
    plan = FileUtils.plan_archive(large_manifest)
    assert_operator(plan.retained_items.length, :<=, 15)
    assert_operator(plan.retained_items.length, :>, 0)
    assert_equal(16, plan.formal_download_bytes)
    assert_equal(30, plan.peak_occupancy)
  end

  def test_precondition_can_change_formal_order
    manifest = small_manifest.merge(
      "preconditions" => [{"group" => "a/0", "after" => "b/2"}]
    )
    plan = FileUtils.plan_archive(manifest)
    groups = plan.steps.select { |step| step.phase == "formal" }.map(&:group)
    assert_operator(groups.index("b/2"), :<, groups.index("a/0"))
  end

  def test_aliases_are_counted_once_and_reused
    manifest = {
      "quota" => 10,
      "attachments" => {
        "a" => 4,
        "b" => 3,
        "a-alias" => {"size" => 4, "same_as" => "a"}
      },
      "initial" => ["a-alias"],
      "verification_size" => 0,
      "queues" => [
        {"name" => "p", "kind" => "preview", "items" => %w[a b]},
        {"name" => "f", "kind" => "formal", "packages" => [%w[a b]]}
      ]
    }
    plan = FileUtils.plan_archive(manifest)
    assert_true(plan.feasible?)
    assert_equal(3, plan.minimum_download_bytes)
    assert_equal(7, plan.peak_occupancy)
    first = plan.steps.find { |step| step.phase == "preview" }
    assert_equal("reuse_preview", first.action)
  end

  def test_hash_form_queue_names_are_preserved
    manifest = {
      "quota" => 2,
      "attachments" => {"a" => 1, "b" => 1},
      "queues" => {
        "preview" => {"kind" => "preview", "items" => %w[a b]},
        "formal" => {"kind" => "formal", "packages" => [%w[a b]]}
      }
    }
    plan = FileUtils.plan_archive(manifest)
    assert_true(plan.feasible?)
    assert_equal("preview", plan.steps.first.queue)
    assert_equal("formal", plan.steps.find { |step| step.phase == "formal" }.queue)
  end

  def test_yaml_manifest_reader
    require "tmpdir"
    Dir.mktmpdir do |dir|
      path = File.join(dir, "manifest.yml")
      File.write(path, <<~YAML)
        quota: 4
        attachments:
          a: 1
          b: 2
        verification_size: 1
        queues:
          - name: preview
            kind: preview
            items: [a, b]
          - name: formal
            kind: formal
            packages: [[a, b]]
      YAML
      plan = FileUtils::ArchivePlanner.from_file(path).plan
      assert_true(plan.feasible?)
      assert_equal(3, plan.minimum_download_bytes)
    end
  end

  private

  def replay(manifest, plan)
    sizes = manifest["attachments"]
    held = Array(manifest["initial"]).map(&:to_s).to_set
    staged = Set.new
    billed_preview = 0
    billed_formal = 0
    occupancy = held.sum { |item| sizes.fetch(item) }
    peak = occupancy

    plan.steps.each do |step|
      case step.action
      when "download_preview"
        item = step.items.first
        assert_false(held.include?(item), "duplicate preview #{item}")
        held << item
        billed_preview += sizes.fetch(item)
        occupancy += sizes.fetch(item)
      when "reuse_preview"
        item = step.items.first
        assert_true(held.include?(item), "reused absent preview #{item}")
      when "stage_and_remove_previews"
        staged = step.items.to_set
        assert_empty(staged - held)
        held = Array(manifest["initial"]).map(&:to_s).to_set | staged
        occupancy = held.sum { |item| sizes.fetch(item) }
      when "add_verification"
        occupancy += step.occupancy_after - step.occupancy_before
      when "remove_verification"
        occupancy = step.occupancy_after
      when "download_package", "attach_formal", "attach_formal_from_staging"
        package = step.package || step.items
        missing = package.reject { |item| held.include?(item) }
        if step.action == "download_package"
          assert_false(missing.empty?, "downloaded a fully held package")
          billed_formal += package.sum { |item| sizes.fetch(item) }
          missing.each do |item|
            held << item
            staged.delete(item)
          end
        else
          assert_empty(missing, "attached a package with missing items")
        end
        occupancy = held.sum { |item| sizes.fetch(item) }
      when "close_volume"
        occupancy = 0
      end

      assert_equal(occupancy, step.occupancy_after,
                   "occupancy mismatch at step #{step.number} (#{step.action})")
      assert_operator(occupancy, :<=, manifest["quota"],
                      "quota exceeded at step #{step.number} (#{step.action})")
      peak = [peak, occupancy].max
    end

    assert_equal(plan.minimum_download_bytes, billed_preview + billed_formal)
    assert_equal(plan.preview_download_bytes, billed_preview)
    assert_equal(plan.formal_download_bytes, billed_formal)
    assert_equal(plan.peak_occupancy, peak)
  end
end
