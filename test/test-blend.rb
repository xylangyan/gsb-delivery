# frozen_string_literal: false
require_relative "rss-testcase"

require "rss/blend"

module RSS
  class TestBlend < TestCase
    def setup
      @feed_cache = {}
    end

    def test_four_slot_example_chooses_nine_points
      blend = build_four_slot_blend
      result = blend.solve

      assert_equal(["y", "a", "b", "x"], result.order)
      assert_equal(9, result.score)
      assert_predicate(result, :optimal?)
      assert_equal(["y", "a", "b", "x"], result.resources.collect do |resource|
        resource.delete_prefix("http://example.com/")
      end)
    end

    def test_dropped_seven_point_rule_explains_nine_point_conflict
      result = build_four_slot_blend.solve

      dropped = result.dropped_rules
      assert_equal(1, dropped.size)
      assert_equal(["x", "y"], [dropped[0].before, dropped[0].after])
      assert_equal(7, dropped[0].weight)
      assert_equal(:conflicts_with_selected_order, dropped[0].reason)
      assert_equal(9, dropped[0].conflicting_weight)
      assert_match(/worth 9 point/, dropped[0].message)
      assert_match(/worth 7/, dropped[0].message)
    end

    def test_reference_breaks_ab_and_ba_tie
      first = build_four_slot_blend(reference: "morning").solve
      second = build_four_slot_blend(reference: :morning).solve
      third = build_four_slot_blend(reference: %w[x y a b]).solve

      assert_equal(["y", "a", "b", "x"], first.order)
      assert_equal(first.order, second.order)
      assert_equal(first.order, third.order)
      assert_equal(9, second.score)
      assert_equal(9, third.score)
    end

    def test_moving_a_and_b_alone_keeps_score
      blend = RSS::Blend.new
      blend.add_source("one", item_feed(%w[y x a b]), %w[y x a b])
      blend.add_hard("a", "x")
      blend.add_hard("b", "x")
      blend.add_preference("x", "y", 7)
      blend.add_preference("y", "a", 4)
      blend.add_preference("y", "b", 5)
      result = blend.solve
      assert_equal(["y", "a", "b", "x"], result.order)
      assert_equal(9, result.score)
    end

    def test_duplicate_url_occurrences_are_separate_slots
      feed = item_feed(%w[u p u])
      blend = RSS::Blend.new
      blend.add_source("dup", feed, %w[first middle last])
      result = blend.solve

      assert_equal(%w[first middle last], result.order)
      assert_equal(3, result.slots.size)
      first = result.slots.find {|slot| slot.id == "first" }
      middle = result.slots.find {|slot| slot.id == "middle" }
      assert_equal(1, first.resources.size)
      assert_equal(1, middle.resources.size)
      assert_equal("http://example.com/u", first.resource)
      assert_equal("http://example.com/u", result.slot("last").resource)
    end

    def test_duplicate_item_bodies_follow_seq_order
      rss = item_feed(%w[u u], {"u" => "first"})
      second = RSS::RDF::Item.new("http://example.com/u")
      second.title = "second"
      second.link = "http://example.com/u"
      second.description = "second body"
      rss.items << second

      blend = RSS::Blend.new
      blend.add_source("s", rss, %w[first second])
      result = blend.solve

      assert_equal(["first", "second"], result.slots.collect(&:id))
      assert_equal(["first", "second"],
                   result.slots.collect {|slot| slot.item.title })
      # Both slots share one rdf:about, so the output has one item element.
      assert_equal(["http://example.com/u", "http://example.com/u"],
                   result.to_rss.channel.items.Seq.lis.collect(&:resource))
      assert_equal(["first"], result.to_rss.items.collect(&:title))
    end

    def test_duplicate_url_can_be_split_with_resource_mapping
      blend = RSS::Blend.new
      blend.add_source("dup", item_feed(%w[u u v]),
                       {"http://example.com/u" => %w[u1 u2],
                        "http://example.com/v" => "v1"})
      result = blend.solve

      assert_equal(%w[u1 u2 v1], result.order)
      assert_equal(["http://example.com/u", "http://example.com/u",
                    "http://example.com/v"], result.resources)
    end

    def test_resource_mapping_scalar_reuses_one_slot
      blend = RSS::Blend.new
      blend.add_source("dup", item_feed(%w[u u v]),
                       {"http://example.com/u" => "shared",
                        "http://example.com/v" => "v"})
      result = blend.solve

      assert_equal(%w[shared v], result.order)
      assert_equal(2, result.slot("shared").occurrences.size)
      assert_equal(["http://example.com/u", "http://example.com/v"],
                   result.resources)
      assert_equal(["http://example.com/u", "http://example.com/v"],
                   result.to_rss.channel.items.Seq.lis.collect(&:resource))
    end

    def test_mixed_mapping_keys_are_rejected
      blend = RSS::Blend.new
      error = assert_raise(RSS::Blend::MappingError) do
        blend.add_source("dup", item_feed(%w[u v]), {1 => "a", "http://example.com/v" => "b"})
      end
      assert_match(/not mixed/, error.message)
    end

    def test_explicit_position_mapping
      blend = RSS::Blend.new
      blend.add_source("dup", item_feed(%w[u u v]), {1 => "u1", 2 => "u2", 3 => "v1"})
      assert_equal(%w[u1 u2 v1], blend.solve.order)
    end

    def test_mapping_is_required_and_size_checked
      blend = RSS::Blend.new
      error = assert_raise(RSS::Blend::MappingError) do
        blend.add_source("dup", item_feed(%w[u v]))
      end
      assert_match(/explicit occurrence-to-slot mapping/, error.message)

      assert_raise(RSS::Blend::MappingError) do
        blend.add_source("short", item_feed(%w[u v]), %w[u])
      end
    end

    def test_all_pending_feed_needs_placeholder_or_body
      blend = RSS::Blend.new
      blend.add_seq_source("s", ["http://example.com/u", "http://example.com/u"],
                           %w[one two])
      result = blend.solve

      assert_raise(RSS::Blend::NoItemsError) { result.to_rss }
      assert_false(result.readback.ok?)
      rss = result.to_rss(include_pending_placeholder: true)
      assert_equal(2, rss.channel.items.Seq.lis.size)
      assert_equal(1, rss.items.size)
      assert_equal([], result.readback(include_pending_placeholder: true).errors)
    end

    def test_hard_cycle_reported_before_emitting
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b]), %w[a b])
      blend.add_hard("a", "b")
      blend.add_hard("b", "a")

      error = assert_raise(RSS::Blend::HardCycleError) { blend.solve }
      assert_equal(1, error.cycles.size)
      cycle = error.cycles[0]
      assert_equal(%w[a b], cycle.slots.sort)
      assert_equal(2, cycle.rules.size)
      assert_match(/contradictory hard constraints/, error.message)
    end

    def test_hard_cycle_detected_with_three_nodes
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b c]), %w[a b c])
      blend.add_hard("a", "b")
      blend.add_hard("b", "c")
      blend.add_hard("c", "a")

      error = assert_raise(RSS::Blend::HardCycleError) { blend.solve }
      assert_equal(1, error.cycles.size)
      assert_equal(3, error.cycles[0].rules.size)
      assert_equal(%w[a b c], error.cycles[0].slots.sort)
    end

    def test_hard_cycle_preflight
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b]), %w[a b])
      blend.add_hard("a", "b")
      blend.add_hard("b", "a")
      assert_equal(1, blend.check_hard_cycles.size)
      errors = []
      begin
        blend.to_rss
      rescue RSS::Blend::HardCycleError => exception
        errors << exception
      end
      assert_equal(1, errors.size)
    end

    def test_hard_rules_win_over_all_preferences
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b c]), %w[a b c])
      blend.add_hard("c", "a")
      blend.add_preference("a", "c", 100)
      blend.add_preference("c", "b", 1)
      result = blend.solve

      assert_equal(["c", "a", "b"], result.order)
      assert_equal(1, result.score)
      assert_equal(["a", "c"], [result.dropped_rules[0].before,
                                result.dropped_rules[0].after])
    end

    def test_pending_slot_stays_in_seq_position
      feed = item_feed(%w[x p x], "p" => :missing)
      blend = RSS::Blend.new
      blend.add_source("s", feed, %w[x1 p x2])
      result = blend.solve

      assert_equal(%w[x1 p x2], result.order)
      assert_equal(["p"], result.pending_slots.collect(&:id))
      assert_equal([2], result.pending_slots.collect do |slot|
                     result.position(slot.id)
                   end)
      assert_equal(["http://example.com/x", "http://example.com/p",
                    "http://example.com/x"], result.resources)
      assert_equal([], result.dropped_rules)
    end

    def test_pending_slot_does_not_borrow_same_name_other_slot
      feed = item_feed(%w[x p], "p" => :missing)
      blend = RSS::Blend.new
      blend.add_source("s", feed, %w[x p])
      blend.add_source("t", item_feed(%w[x q], "q" => "Q"), %w[x q])
      result = blend.solve

      pending = result.pending_slots
      assert_equal(["p"], pending.collect(&:id))
      assert_equal(["http://example.com/p"], pending.collect(&:resource))
      assert_equal(["http://example.com/x", "http://example.com/p",
                    "http://example.com/q"], result.resources)
    end

    def test_fill_pending_slot_after_fetch
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[x p], "p" => :missing), %w[x p])
      assert_equal(["p"], blend.solve.pending_slots.collect(&:id))

      blend.fill_slot("p", RSS::RDF::Item.new("http://example.com/p").tap do |item|
        item.title = "P"
        item.link = "http://example.com/p"
      end)
      result = blend.solve
      assert_empty(result.pending_slots)
      assert_equal(["x", "p"], result.order)
      assert_equal("P", result.slot("p").item.title)
    end

    def test_output_has_no_placeholder_for_pending_by_default
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[x p], "p" => :missing), %w[x p])
      rss = blend.solve.to_rss

      assert_equal(["http://example.com/x", "http://example.com/p"],
                   rss.channel.items.Seq.lis.collect(&:resource))
      assert_equal(["http://example.com/x"], rss.items.collect(&:about))
    end

    def test_output_can_include_pending_placeholder
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[x p], "p" => :missing), %w[x p])
      result = blend.solve
      rss = result.to_rss(include_pending_placeholder: true)

      assert_equal(["http://example.com/x", "http://example.com/p"],
                   rss.channel.items.Seq.lis.collect(&:resource))
      assert_equal(["http://example.com/x", "http://example.com/p"],
                   rss.items.collect(&:about))
      assert_match(/pending/, rss.items[1].title)
      assert_equal([], result.readback.errors)
    end

    def test_readback_reproduces_seq_positions
      result = build_four_slot_blend.solve
      report = result.verify_readback!

      assert_predicate(report, :ok?)
      assert_predicate(report, :strict?)
      assert_equal(%w[y a b x], report.positions.keys)
      assert_equal(result.order.collect {|id| result.slot(id).resource},
                   report.resources)
      assert_equal(1, report.position("y"))
      assert_equal(4, report.position("x"))
    end

    def test_readback_uses_rss_parser
      result = build_four_slot_blend.solve
      reparsed = RSS::Parser.parse(result.to_s)

      assert_equal(result.order.collect {|id| result.slot(id).resource},
                   reparsed.channel.items.Seq.lis.collect(&:resource))
      assert_equal(["http://example.com/y", "http://example.com/a",
                    "http://example.com/b", "http://example.com/x"],
                   reparsed.items.collect(&:about))
    end

    def test_diagnostics_lists_dropped_and_source_orders
      result = build_four_slot_blend.solve
      diagnostics = result.diagnostics

      assert_equal(["y", "a", "b", "x"], diagnostics[:order])
      assert_equal(9, diagnostics[:score])
      assert_equal(1, diagnostics[:dropped_rules].size)
      assert_equal("x", diagnostics[:dropped_rules][0][:before])
      assert_equal("y", diagnostics[:dropped_rules][0][:after])
      assert_equal(7, diagnostics[:dropped_rules][0][:weight])

      unsatisfied = diagnostics[:source_orders].reject {|check| check[:satisfied] }
      assert_equal(3, unsatisfied.size)
      specific = unsatisfied.find do |check|
        check[:before] == "x" && check[:after] == "y"
      end
      assert_not_nil(specific)
      assert_equal(:conflicts_with_selected_order, specific[:reason])
      assert_match(/not kept/, specific[:message])
    end

    def test_source_order_checks_all_pairs
      result = build_four_slot_blend.solve
      checks = result.source_order_checks(pairs: :all)

      assert_equal(6, checks.size)
      unsatisfied = checks.reject(&:satisfied?)
      # x,y is the rejected 7 point rule; a,x and b,x contradict hard rules.
      assert_equal(3, unsatisfied.size)
    end

    def test_source_order_check_explains_hard_conflict
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[x a]), %w[x a])
      blend.add_hard("a", "x")
      result = blend.solve

      check = result.source_order_checks.first
      assert_predicate(check, :unsatisfied?)
      assert_equal(:hard_conflict, check.reason)
      assert_match(/contradicts hard constraints/, check.message)
    end

    def test_consecutive_source_order_checks
      result = build_four_slot_blend.solve
      checks = result.source_order_checks(pairs: :consecutive)

      assert_equal(3, checks.size)
      assert_equal(["x", "y"], [checks[0].before, checks[0].after])
    end

    def test_preference_weights_must_be_positive
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b]), %w[a b])
      assert_raise(ArgumentError) { blend.add_preference("a", "b", 0) }
      assert_raise(ArgumentError) { blend.add_preference("a", "b", -1) }
    end

    def test_unknown_slot_in_rule_is_reported
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b]), %w[a b])
      blend.add_preference("a", "missing", 1)
      error = assert_raise(RSS::Blend::Error) { blend.solve }
      assert_match(/unknown slot/, error.message)
    end

    def test_three_slot_optimum_by_exact_search
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b c]), %w[a b c])
      blend.add_preference("c", "a", 2)
      blend.add_preference("a", "b", 2)
      blend.add_preference("b", "c", 2)
      result = blend.solve

      # Every pair is a 2-point rule, so any acyclic order scores 4. The
      # reference order decides among those ties.
      assert_equal(["a", "b", "c"], result.order)
      assert_equal(4, result.score)
      assert_equal(1, result.dropped_rules.size)
      assert_equal(["c", "a", "b"],
                   blend.solve(reference: %w[c a b]).order)
    end

    def test_optimal_across_multiple_components
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b c d]), %w[a b c d])
      blend.add_preference("b", "a", 3)
      blend.add_preference("d", "c", 5)
      result = blend.solve(reference: "s")

      assert_equal(["b", "a", "d", "c"], result.order)
      assert_equal(8, result.score)
      assert_predicate(result, :optimal?)
    end

    def test_unconstrained_orders_follow_reference
      blend = RSS::Blend.new
      blend.add_source("ref", item_feed(%w[z y x]), %w[z y x])
      blend.add_source("other", item_feed(%w[a b]), %w[a b])
      result = blend.solve(reference: "ref")

      assert_equal(%w[z y x a b], result.order)
      assert_equal(0, result.score)
    end

    def test_channel_metadata
      blend = build_four_slot_blend
      result = blend.solve(channel: {
                             :about => "http://example.com/blend.rdf",
                             :title => "Blend",
                             :link => "http://example.com/",
                             :description => "blended",
                           })
      rss = RSS::Parser.parse(result.to_s)

      assert_equal("http://example.com/blend.rdf", rss.channel.about)
      assert_equal("Blend", rss.channel.title)
      assert_equal("http://example.com/", rss.channel.link)
      assert_equal("blended", rss.channel.description)
    end

    def test_source_from_string_xml
      blend = RSS::Blend.new
      blend.add_source("s", item_feed(%w[a b]).to_s, %w[a b])
      assert_equal(%w[a b], blend.solve.order)
    end

    def test_source_from_file
      require "tempfile"
      file = Tempfile.new(["rss-blend", ".rdf"])
      begin
        file.write(item_feed(%w[a b]).to_s)
        file.close
        blend = RSS::Blend.new
        blend.add_source("s", file.path, %w[a b])
        assert_equal(%w[a b], blend.solve.order)
      ensure
        file.close unless file.closed?
        file.unlink
      end
    end

    def test_non_rss_one_feed_is_rejected
      rss = RSS::Maker.make("2.0") do |maker|
        maker.channel.title = "t"
        maker.channel.link = "http://example.com/"
        maker.channel.description = "d"
        maker.items.new_item do |item|
          item.title = "i"
          item.link = "http://example.com/i"
        end
      end
      blend = RSS::Blend.new
      assert_raise(RSS::Blend::Error) do
        blend.add_source("s", rss, %w[a])
      end
    end

    def test_exact_limit_and_require_optimal
      blend = RSS::Blend.new(exact_limit: 1)
      blend.add_source("s", item_feed(%w[a b]), %w[a b])
      blend.add_preference("a", "b", 1)
      assert_raise(RSS::Blend::NotOptimalError) do
        blend.solve(require_optimal: true)
      end
    end

    def test_slot_mapped_from_different_resources_warns
      blend = RSS::Blend.new
      blend.add_source("a", item_feed(%w[u]), %w[shared])
      blend.add_source("b", item_feed(%w[v]), %w[shared])
      result = blend.solve

      warnings = result.slots.flat_map(&:warnings)
      assert_equal(1, warnings.size)
      assert_match(/different resources/, warnings[0])
      assert_equal(["http://example.com/u", "http://example.com/v"],
                   result.slot("shared").resources)
      assert_match(/warnings:/, result.report)
    end

    def test_cli_writes_feed_and_reports
      require "json"
      require "open3"
      require "tempfile"
      require "tmpdir"

      Dir.mktmpdir("rss-blend-cli") do |dir|
        feed_path = File.join(dir, "morning.rdf")
        File.write(feed_path, item_feed(%w[x y a b]).to_s)
        manifest = {
          "reference" => "morning",
          "feeds" => [
            {"name" => "morning", "path" => feed_path, "slots" => %w[x y a b]},
          ],
          "hard" => [["a", "x"], ["b", "x"]],
          "preferences" => [
            {"before" => "x", "after" => "y", "weight" => 7},
            {"before" => "y", "after" => "a", "weight" => 4},
            {"before" => "y", "after" => "b", "weight" => 5},
          ],
        }
        manifest_path = File.join(dir, "manifest.json")
        File.write(manifest_path, JSON.generate(manifest))
        rss_path = File.join(dir, "out.rdf")
        report_path = File.join(dir, "report.txt")
        json_path = File.join(dir, "diagnostics.json")
        script = File.expand_path("../sample/blend_daily.rb", __dir__)
        lib = File.expand_path("../lib", __dir__)

        _stdout, stderr, status = Open3.capture3(
          RbConfig.ruby, "-I", lib, script, manifest_path,
          "--rss", rss_path, "--report", report_path,
          "--json-report", json_path, "--verify")

        assert_equal("", stderr)
        assert_predicate(status, :success?)
        assert_match(/order: y a b x/, File.read(report_path))
        diagnostics = JSON.parse(File.read(json_path))
        assert_equal(%w[y a b x], diagnostics["order"])
        assert_equal(9, diagnostics["score"])
        reparsed = RSS::Parser.parse(File.read(rss_path))
        assert_equal(["http://example.com/y", "http://example.com/a",
                      "http://example.com/b", "http://example.com/x"],
                     reparsed.channel.items.Seq.lis.collect(&:resource))
      end
    end

    private
    def build_four_slot_blend(reference: "morning")
      blend = RSS::Blend.new(reference: reference)
      blend.add_source("morning", item_feed(%w[x y a b]), %w[x y a b])
      blend.add_hard("a", "x")
      blend.add_hard("b", "x")
      blend.add_preference("x", "y", 7, source: "morning")
      blend.add_preference("y", "a", 4, source: "morning")
      blend.add_preference("y", "b", 5, source: "morning")
      blend
    end

    def item_feed(resources, titles = {})
      rss = RSS::RDF.new
      channel = RSS::RDF::Channel.new("urn:test:#{resources.join('-')}")
      channel.title = "test"
      channel.link = "http://example.com/"
      channel.description = "test feed"
      items = RSS::RDF::Channel::Items.new
      resources.each do |resource|
        items.Seq.lis << RSS::RDF::Channel::Items::Seq::Li.new(
          "http://example.com/#{resource}")
      end
      channel.items = items
      rss.channel = channel

      resources.uniq.each do |resource|
        title = titles[resource]
        next if title == :missing
        item = RSS::RDF::Item.new("http://example.com/#{resource}")
        item.title = title || resource.upcase
        item.link = "http://example.com/#{resource}"
        item.description = "body #{resource}"
        rss.items << item
      end
      rss
    end
  end
end
