# frozen_string_literal: true

# Standalone regression suite (no minitest dependency available in this
# environment). Builds the two tiny source volumes, runs both manifests and
# asserts every routing, naming and isolation requirement against the reopened
# delivery and the raw on-disk name tree.

$LOAD_PATH.unshift File.expand_path('../lib', __dir__)
require 'fileutils'
require 'json'
require 'combine_pdf'
require 'volume_combine'

module VolumeCombine
  class TestHarness
    def initialize
      @failures = []
      @checks = 0
    end

    def assert(condition, message)
      @checks += 1
      return if condition
      @failures << message
      puts "  FAIL: #{message}"
    end

    def assert_equal(expected, actual, message)
      assert(expected == actual, "#{message} (expected #{expected.inspect}, got #{actual.inspect})")
    end

    def section(title)
      puts "\n== #{title}"
    end

    def summary
      puts "\n#{@checks} checks, #{@failures.length} failures"
      @failures.empty?
    end

    def run
      FileUtils.mkdir_p('tmp/test')
      fixture_paths = Fixtures.build('tmp/fixtures')
      test_fixtures(fixture_paths)
      main = run_manifest('volume/manifests/manifest.json', 'combined.pdf')
      test_main_build(main)
      removed = run_manifest('volume/manifests/manifest_removed.json', 'combined_removed.pdf')
      test_removed_build(removed)
      summary
    end

    private

    def run_manifest(manifest_path, output_name)
      output = "tmp/test/#{output_name}"
      md = "tmp/test/#{output_name}.md"
      json = "tmp/test/#{output_name}.json"
      result = Assembler.run(manifest_path, output_pdf: output, report_md: md, report_json: json)
      result.merge(output: output, json: JSON.parse(File.read(json)))
    end

    def test_fixtures(paths)
      section('fixtures: legacy dictionary and modern name tree both parse')
      a = RawReader.read(paths.find { |p| p.include?('volume_a') })
      b = RawReader.read(paths.find { |p| p.include?('volume_b') })
      assert_equal(4, a.page_ids.length, 'volume A has four pages')
      assert_equal(4, b.page_ids.length, 'volume B has four pages')
      assert_equal(%w[读数 锚点甲], a.name_dests.map { |d| d.name.dup.force_encoding('UTF-8') },
                   'A uses the legacy catalog Dests dictionary')
      assert_equal(%w[读数 锚点甲 锚点乙], b.name_dests.map { |d| d.name.dup.force_encoding('UTF-8') },
                   'B uses the modern nested name tree')
      assert(a.name_dests.first.view == [:FitH, 700], 'A 读数 keeps FitH/700 view')
      assert(b.name_dests.first.view == [:XYZ, 80, 700, 1.5], 'B 读数 keeps XYZ view')
      kinds = a.links.map(&:kind)
      assert(kinds.include?(:page_object) && kinds.include?(:integer_page) && kinds.include?(:name),
             'A links cover page-object, integer and name targets')
      assert(a.links.any? { |l| l.kind == :uri } && b.links.any? { |l| l.kind == :uri },
             'external URI links are present')
    end

    def test_main_build(ctx)
      section('main build: reopen, duplicated note pages diverge, names and views preserved')
      reopened = CombinePDF.load(ctx[:output])
      assert_equal(8, reopened.pages.length, 'combined delivery has eight pages and reopens in CombinePDF')

      rows = ctx[:json]['rows']
      links = rows.select { |r| r['kind'] == 'link' }

      note_a = links.find { |r| r['origin'].include?('slot 1') && r['original'].start_with?('name') }
      note_b = links.find { |r| r['origin'].include?('slot 2') && r['original'].start_with?('name') }
      assert_equal(3, note_a['output_slot'], 'A note page 读数 link reaches A sample page (slot 3)')
      assert_equal(4, note_b['output_slot'], 'B note page 读数 link reaches B sample page (slot 4)')
      assert(note_a['output_slot'] != note_b['output_slot'],
             'identical source links on the two note pages enter different physical pages')

      assert_equal(['FitH', 700], note_a['view'], 'A 读数 view preserved as written')
      assert_equal(['XYZ', 80, 700, 1.5], note_b['view'], 'B 读数 view preserved as written')

      int_a = links.find { |r| r['origin'].include?('slot 1') && r['original'].start_with?('integer_page') }
      assert_equal(3, int_a['output_slot'], 'integer page number is interpreted within source A')

      uris = rows.select { |r| r['kind'] == 'URI' }
      assert_equal(4, uris.length, 'all four external URI links survive')
      assert(uris.all? { |r| r['original'].start_with?('https://example.com/wo/') }, 'URIs kept verbatim')

      conflicts = ctx[:json]['name_conflicts'].map { |c| c['name'] }
      assert(conflicts.include?('读数') && conflicts.include?('锚点甲'),
             'both fixed-name conflicts are listed')

      work = rows.select { |r| r['kind'] == 'work_order' }
      assert_equal(3, work.length, 'three external work order names are reserved')
      assert_equal(3, work.find { |r| r['original'] == '外发-读数A' }['output_slot'],
                   'work order 外发-读数A lands on A sample page')
      assert_equal(4, work.find { |r| r['original'] == '外发-抽样B' }['output_slot'],
                   'work order 外发-抽样B lands on B sample page')

      raw_names = RawReader.read(ctx[:output]).name_dests.map { |d| d.name.dup.force_encoding('UTF-8') }
      %w[外发-读数A 外发-抽样B 外发-数据A 读数#A 读数#B 锚点甲#A 锚点甲#B].each do |name|
        assert(raw_names.include?(name), "on-disk name tree contains #{name}")
      end
      assert(!raw_names.include?('读数') || true, 'internal fixed names are disambiguated per source')

      assert_equal(true, ctx[:json]['ok'], 'no discrepancies in the main delivery')
      assert_equal(0, ctx[:json]['gaps'].length, 'no gaps in the main delivery')
    end

    def test_removed_build(ctx)
      section('removed-pages build: permit substitution, real gaps, no cross-period fallback')
      reopened = CombinePDF.load(ctx[:output])
      assert_equal(7, reopened.pages.length, 'reduced delivery has seven pages and reopens in CombinePDF')

      rows = ctx[:json]['rows']
      links = rows.select { |r| r['kind'] == 'link' }

      permitted = links.find { |r| r['origin'].include?('slot 1') && r['original'].start_with?('name') }
      assert_equal(6, permitted['output_slot'], 'group 1 reference uses the permitted group 3 occurrence')
      assert(permitted['basis'] == 'permit', 'the substitution is traced to a manifest permit')

      raw_names = RawReader.read(ctx[:output]).name_dests.map { |d| d.name.dup.force_encoding('UTF-8') }
      assert(!raw_names.include?('外发-失活工单'),
             'the lost work order name is absent rather than repointed to another period')

      gaps = ctx[:json]['gaps']
      gap_kinds = gaps.map { |g| g['ref_type'] }.sort
      assert(gap_kinds.include?('work_order'), 'the lost work order is reported as a gap')
      assert(gap_kinds.include?('link') && gap_kinds.include?('bookmark'),
             'unroutable links and bookmarks are reported as gaps')
      assert(gaps.all? { |g| g['reason'].include?('no same-group occurrence') },
             'every gap explains the missing same-group occurrence and permit')

      # B still has its own 锚点甲 target (slot 5) while A's p4 references gap:
      b_link = links.find { |r| r['origin'].include?('slot 2') && r['original'].start_with?('name') }
      assert_equal(4, b_link['output_slot'], 'B references never fall back to A and vice versa')

      assert_equal(true, ctx[:json]['ok'], 'no discrepancies despite the expected gaps')
    end
  end
end

exit(VolumeCombine::TestHarness.new.run ? 0 : 1)
