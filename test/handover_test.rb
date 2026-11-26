GEM_ROOT = File.expand_path("../.gems/ruby/4.0.0", __dir__)
if File.directory?(GEM_ROOT)
  gem_paths = [ENV["GEM_PATH"], GEM_ROOT].compact.reject(&:empty?)
  ENV["GEM_PATH"] = gem_paths.join(File::PATH_SEPARATOR)
end

require "minitest/autorun"
require "tmpdir"
require "dotenv"

class HandoverTest < Minitest::Test
  MANIFEST = File.expand_path("fixtures/handover/manifest.json", __dir__)

  def manifest
    Dotenv::Handover::Manifest.load(MANIFEST)
  end

  def start_environment
    manifest.start_environment
  end

  def rehearse(environment = start_environment, keys = nil)
    Dotenv::Handover.rehearse(manifest: manifest, environment: environment, keys: keys)
  end

  def test_routes_the_full_environment_through_the_west_line
    result = rehearse
    route = result.routes.first
    assert_equal ["check", "west", "normal"], route.checkpoint_names

    normal = route.calls.last
    assert_equal ["BODY"], normal.fields.keys
    assert_equal "west\nstart".b, normal.fields["BODY"].bytes
  end

  def test_missing_marker_falls_back_after_the_export_line
    environment = start_environment.reject { |key, _| key == "PIPELINE_MARKER" }
    result = rehearse(environment)
    route = result.routes.first

    assert_equal ["check", "west", "fallback"], route.checkpoint_names

    west = route.calls[1]
    assert_equal "Dotenv::FormatError", west.exception.class_name
    assert_equal "export PIPELINE_MARKER", west.exception.line
    assert_equal 1, west.exception.line_number

    assert_equal "west\nretry".b, route.calls.last.fields["BODY"].bytes
  end

  def test_route_selection_uses_or_and_and_conditions
    only_fast = {"PIPELINE_FAST" => "1", "PIPELINE_MARKER" => ""}
    assert_equal "west", rehearse(only_fast).routes.first.checkpoint_names[1]

    cache_and_batch = {"PIPELINE_CACHE" => "1", "PIPELINE_BATCH" => "1", "PIPELINE_MARKER" => ""}
    assert_equal "west", rehearse(cache_and_batch).routes.first.checkpoint_names[1]

    neither = {"PIPELINE_CACHE" => "1", "PIPELINE_MARKER" => ""}
    assert_equal ["check"], rehearse(neither).routes.first.checkpoint_names
  end

  def test_selected_files_and_environment_changes_are_recorded
    west = rehearse.routes.first.calls[1]
    assert_equal ["west.env", "marker.env"], west.files
    assert_equal %w[DOTENV_LINEBREAK_MODE PIPELINE_SITE], west.env_diff.keys.sort
    assert_equal "legacy", west.env_diff.added["DOTENV_LINEBREAK_MODE"].value

    assert_equal "west", rehearse.routes.first.calls[1].env_after["PIPELINE_SITE"]
  end

  def test_reference_route_matches_itself
    result = rehearse
    verification = result.verify
    assert verification.ok?, verification.describe
  end

  def test_verification_reports_the_marker_key_as_required
    environment = start_environment.reject { |key, _| key == "PIPELINE_MARKER" }
    result = rehearse(environment)
    refute result.verify.ok?
  end

  def test_minimal_roster_is_fast_and_marker
    solution = rehearse.minimal_keys
    assert solution.feasible?, solution.describe
    assert_equal ["PIPELINE_FAST", "PIPELINE_MARKER"], solution.keys
  end

  def test_minimal_roster_keeps_both_declared_cases
    solution = rehearse.minimal_keys
    scenario = manifest.cases.find { |item| item.name == "missing_marker" }
    run = Dotenv::Handover::Runner.new(manifest)
      .rehearse(environment: scenario.environment.select { |k, _| solution.keys.include?(k) },
        keys: solution.keys)
    assert_equal ["check", "west", "fallback"], run.routes.first.checkpoint_names
  end

  def test_rehearsal_never_touches_the_real_environment
    before = ENV.to_h.dup
    ENV["PIPELINE_FAST"] = "sentinel"
    sentinel = ENV.to_h.dup

    result = rehearse
    assert_equal ["check", "west", "normal"], result.routes.first.checkpoint_names

    assert_equal sentinel, ENV.to_h
    assert_equal "sentinel", ENV["PIPELINE_FAST"]
  ensure
    if before
      ENV.replace(before)
    end
  end

  def test_branch_writes_do_not_leak_between_routes
    blueprint = manifest
    blueprint.send(:instance_variable_set, :@checkpoint_order,
      ["split", "a", "b"])

    split = Dotenv::Handover::Checkpoint.new("split", operation: :parse, files: ["check.env"],
      destinations: [
        Dotenv::Handover::Destination.new("a", conditions: [
          Dotenv::Handover::Condition.new(:field, source: "FAST", op: :eq, value: "1")
        ]),
        Dotenv::Handover::Destination.new("b", conditions: [
          Dotenv::Handover::Condition.new(:field, source: "FAST", op: :eq, value: "1")
        ])
      ])
    a = Dotenv::Handover::Checkpoint.new("a", operation: :parse, files: ["normal.env"])
    b = Dotenv::Handover::Checkpoint.new("b", operation: :load, files: ["west.env"])
    blueprint.instance_variable_set(:@checkpoints,
      {"split" => split, "a" => a, "b" => b})
    blueprint.instance_variable_set(:@entry, "split")

    result = Dotenv::Handover.rehearse(manifest: blueprint, environment: start_environment)
    assert_equal 2, result.routes.size

    a_route = result.routes.find { |r| r.checkpoint_names == ["split", "a"] }
    b_route = result.routes.find { |r| r.checkpoint_names == ["split", "b"] }

    refute a_route.calls.last.env_after.key?("PIPELINE_SITE")
    assert_equal "west", b_route.calls.last.env_after["PIPELINE_SITE"]
  end
end

class HandoverTieBreakTest < Minitest::Test
  def base_manifest(optional:, cases:, files:)
    {
      "start_environment" => optional.transform_values { "1" },
      "optional_keys" => optional.keys,
      "files" => files,
      "cases" => cases,
      "checkpoints" => [
        {"name" => "step", "operation" => "parse", "files" => [files.keys.first],
         "destinations" => [{"to" => "halt"}]}
      ]
    }
  end

  def test_same_size_rosters_choose_lexicographically_smallest
    files = {"a.env" => "A=\"${AAA}\"\nZ=\"${ZZZ}\"\n"}
    manifest = {
      "start_environment" => {"AAA" => "1", "MID" => "1", "ZZZ" => "1"},
      "optional_keys" => %w[AAA MID ZZZ],
      "files" => files,
      "cases" => [{"name" => "full",
        "environment" => {"AAA" => "1", "MID" => "1", "ZZZ" => "1"}}],
      "checkpoints" => [
        {"name" => "step", "operation" => "parse", "files" => ["a.env"],
         "capture" => %w[A Z],
         "destinations" => [
           {"to" => "next", "when" => {"any" => [
             {"field" => "A", "op" => "eq", "value" => "1"},
             {"field" => "Z", "op" => "eq", "value" => "1"}
           ]}}
         ]},
        {"name" => "next", "operation" => "parse", "files" => [],
         "destinations" => [{"to" => "halt"}]}
      ]
    }
    solution = Dotenv.minimal_keys(manifest: manifest,
      environment: manifest["start_environment"])
    assert solution.feasible?, solution.describe
    assert_equal ["AAA"], solution.keys
  end

  def test_infeasible_roster_names_the_unreachable_step
    files = {"one.env" => "LEFT=\"${AAA}\"\n", "two.env" => "RIGHT=\"${BBB}\"\n"}
    manifest = {
      "start_environment" => {"AAA" => "1", "BBB" => "1"},
      "optional_keys" => %w[AAA BBB],
      "files" => files,
      "cases" => [
        {"name" => "both", "environment" => {"AAA" => "1", "BBB" => "1"},
         "expected" => [{"route" => %w[first second]}]},
        {"name" => "only_bbb", "environment" => {"BBB" => "1"},
         "expected" => [{"route" => %w[first second]}]}
      ],
      "checkpoints" => [
        {"name" => "first", "operation" => "parse", "files" => ["one.env"],
         "destinations" => [
           {"to" => "second", "when" => {"field" => "LEFT", "op" => "eq", "value" => "1"}},
           {"to" => "halt"}
         ]},
        {"name" => "second", "operation" => "parse", "files" => ["two.env"],
         "destinations" => [{"to" => "halt"}]}
      ]
    }
    solution = Dotenv.minimal_keys(manifest: manifest,
      environment: manifest["start_environment"])
    refute solution.feasible?
    conflict = solution.conflicts.find { |item| item.case_name == "only_bbb" }
    refute_nil conflict
    # The roster can still satisfy the first checkpoint from the carried
    # value, but the second cannot be reached without the other key.
    assert_equal "second", conflict.checkpoint
    assert_equal 1, conflict.step_index
    assert_match(/expected route/, conflict.mismatches.first.message)
  end
end

class HandoverSemanticsTest < Minitest::Test
  def manifest_with(operation:, files:, environment:)
    {
      "start_environment" => environment,
      "files" => files,
      "checkpoints" => [
        {"name" => "step", "operation" => operation, "files" => files.keys,
         "destinations" => [{"to" => "halt"}]}
      ]
    }
  end

  def test_parse_leaves_the_environment_untouched
    result = Dotenv.rehearse(
      manifest: manifest_with(operation: "parse", files: {"a.env" => "A=file\n"},
        environment: {"A" => "start"}),
      environment: {"A" => "start"}
    )
    call = result.routes.first.calls.first
    assert call.env_diff.empty?
    assert_equal "start", call.env_after["A"]
  end

  def test_load_preserves_existing_values
    result = Dotenv.rehearse(
      manifest: manifest_with(operation: "load", files: {"a.env" => "A=file\nB=file\n"},
        environment: {"A" => "start"}),
      environment: {"A" => "start"}
    )
    call = result.routes.first.calls.first
    assert_equal "start", call.env_after["A"]
    assert_equal "file", call.env_after["B"]
    assert_equal ["B"], call.env_diff.added.keys
  end

  def test_overwrite_replaces_existing_values
    result = Dotenv.rehearse(
      manifest: manifest_with(operation: "overwrite", files: {"a.env" => "A=file\n"},
        environment: {"A" => "start"}),
      environment: {"A" => "start"}
    )
    call = result.routes.first.calls.first
    assert_equal "file", call.env_after["A"]
    assert_equal ["A"], call.env_diff.changed.keys
  end

  def test_overwrite_checks_files_in_reverse_priority
    files = {"high.env" => "A=high\n", "low.env" => "A=low\n"}
    result = Dotenv.rehearse(
      manifest: {
        "start_environment" => {},
        "files" => files,
        "checkpoints" => [
          {"name" => "step", "operation" => "overwrite",
           "files" => ["high.env", "low.env"], "destinations" => [{"to" => "halt"}]}
        ]
      },
      environment: {}
    )
    assert_equal "high", result.routes.first.calls.first.env_after["A"]
  end
end
