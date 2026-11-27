# frozen_string_literal: true

require 'json'
require 'rbconfig'
require 'fileutils'
require 'open3'
require_relative 'catalog'
require_relative 'generator'
require_relative 'packer'

# Verifies every built package against the full reference gem and against the
# work-order rules. Each package is opened in a separate Ruby process whose
# load path exposes ONLY that package's lib (plus the tzinfo gem), proving a
# cabinet can open the package on its own.
module Auditor
  module_function

  RUBY = File.join(RbConfig::CONFIG['bindir'], RbConfig::CONFIG['ruby_install_name'])
  GEM_HOME = File.expand_path(File.join(__dir__, '..', '.gems', 'ruby', '4.0.0'))

  def reference
    @reference ||= begin
      $LOAD_PATH.unshift(Packer::REF_LIB) unless $LOAD_PATH.include?(Packer::REF_LIB)
      require 'tzinfo/data'
      require 'tzinfo'
      TZInfo::DataSource.set(:ruby)
      TZInfo::DataSource.get
    end
  end

  def readings(zone, local_epoch)
    periods = zone.periods_for_local(Time.at(local_epoch, in: 'UTC'))
    periods.map do |period|
      { 'abbr' => period.zone_identifier.to_s,
        'base_offset' => period.base_utc_offset,
        'std_offset' => period.std_offset,
        'total_offset' => period.utc_total_offset,
        'utc_epoch' => local_epoch - period.utc_total_offset }
    end
  end

  # Reference (full gem) expectations for one zone, including gap/overlap probes
  # that fall inside the supplied UTC window (nil window => no edge probes).
  def reference_zone_data(identifier, window)
    zone = TZInfo::Timezone.get(identifier)
    info = zone.send(:info)
    data = {
      'kind' => info.kind_of?(TZInfo::DataSources::LinkedTimezoneInfo) ? 'linked' : 'data',
      'link_to' => info.respond_to?(:link_to_identifier) ? info.link_to_identifier : nil,
      'edges' => []
    }
    return data unless window

    range = (window.start_epoch..window.end_epoch)
    zone.transitions_up_to(Time.at(window.end_epoch + 86_400, in: 'UTC'),
                           Time.at(window.start_epoch - 86_400, in: 'UTC')).each do |transition|
      utc = transition.at.to_i
      next unless range.cover?(utc)
      before = transition.previous_offset.utc_total_offset
      after = transition.offset.utc_total_offset
      if after > before
        # Spring forward: a local time 30 min after the jump in the old offset
        # lands in the skipped hour and must have no reading.
        data['edges'] << { 'key' => "gap@#{utc}", 'local_epoch' => utc + before + 1800, 'expect_count' => 0 }
      elsif after < before
        # Fall back: 30 min after the jump expressed in the new (shorter) offset
        # is inside the repeated hour and must give two UTC instants.
        data['edges'] << { 'key' => "overlap@#{utc}", 'local_epoch' => utc + after + 1800, 'expect_count' => 2 }
      end
    end
    data
  end

  def build_request(cabinet, edition, result)
    cov_start, cov_end = Catalog.coverage_bounds(cabinet)
    coverage = Catalog::Window.new(name: 'coverage', start_epoch: cov_start, end_epoch: cov_end)
    shipped = result[:files].select { |f| f[:form] != 'index' && f[:form] != 'loader' }

    samples = cabinet.samples.select { |s| s[:version] == edition }.map do |sample|
      { 'key' => "#{sample[:ledger]}:#{sample[:id]}", 'id' => sample[:id],
        'local_epoch' => sample[:local].to_i, 'expect' => sample[:expect].to_s }
    end

    edge_probes = []
    shipped.map { |f| f[:identifier] }.uniq.each do |identifier|
      candidate = Catalog.candidate(shipped.find { |f| f[:identifier] == identifier }[:candidate])
      next if candidate.form == :link
      zone_window = candidate.form == :window_rule ? coverage : nil
      ref = reference_zone_data(identifier, zone_window)
      edge_probes.concat(ref['edges'].map { |e| e.merge('id' => identifier) })
      if zone_window
        # Fold-in probes at BOTH edges. The retained initial/terminal offsets
        # must still answer a local time just outside the window (no UTC-edge
        # truncation, no missed item folding in from outside).
        edge_probes << { 'key' => "fold-in-start:#{identifier}", 'id' => identifier,
                         'local_epoch' => zone_window.start_epoch - 1800, 'expect_count' => nil }
        edge_probes << { 'key' => "fold-in-end:#{identifier}", 'id' => identifier,
                         'local_epoch' => zone_window.end_epoch + 1800, 'expect_count' => nil }
      end
    end

    {
      'zones' => shipped.map { |f| f[:identifier] }.uniq,
      'countries' => cabinet.countries,
      'samples' => samples,
      'edge_probes' => edge_probes
    }
  end

  def run_probe(package_root, request)
    req_path = File.join(package_root, 'audit_request.json')
    File.write(req_path, JSON.generate(request))
    env = { 'GEM_HOME' => GEM_HOME, 'GEM_PATH' => GEM_HOME,
            'RUBYLIB' => File.join(package_root, 'lib'), 'RUBYOPT' => '' }
    stdout, stderr, status = Open3.capture3(env, RUBY, File.join(__dir__, 'audit_probe.rb'), req_path,
                                           unsetenv_others: true)
    raise "probe failed for #{package_root}: #{stderr}" unless status.success?
    JSON.parse(stdout)
  end

  def assert(condition, message, failures)
    failures << message unless condition
  end

  def audit_package(cabinet, edition, result)
    failures = []
    request = build_request(cabinet, edition, result)
    probe = run_probe(result[:root], request)
    ds = reference

    # 1. Opens standalone.
    assert(probe['opened'] == true, 'package did not report opened', failures)

    # 2. Timezone index. Each category is in canonical sorted order and matches
    #    the edition's own data/link classification. For identifiers classified
    #    the same way in the full reference, their relative order equals the
    #    full-edition visible order (recovery edition A may keep an old name as
    #    a data zone where the current reference already lists a link).
    shipped_data = probe.dig('tz_index', 'data')
    shipped_linked = probe.dig('tz_index', 'linked')
    all_shipped = shipped_data + shipped_linked
    parsed = Packer.parsed_sources.fetch(edition)

    assert(shipped_data == shipped_data.sort, "data timezone index not canonically sorted: #{shipped_data.inspect}", failures)
    assert(shipped_linked == shipped_linked.sort, "linked timezone index not canonically sorted: #{shipped_linked.inspect}", failures)
    all_shipped.each do |identifier|
      expect_link = parsed.links.key?(identifier)
      assert(expect_link ? shipped_linked.include?(identifier) : shipped_data.include?(identifier),
             "index classification of #{identifier} disagrees with edition #{edition} rules", failures)
    end
    same_data = ds.data_timezone_identifiers.to_a & shipped_data
    same_linked = ds.linked_timezone_identifiers.to_a & shipped_linked
    assert(same_data == (shipped_data & ds.data_timezone_identifiers.to_a),
           "shared data zones lost full-edition relative order: #{same_data.inspect}", failures)
    assert(same_linked == (shipped_linked & ds.linked_timezone_identifiers.to_a),
           "shared linked zones lost full-edition relative order: #{same_linked.inspect}", failures)

    # 3. Per-zone kind/link target matches the EDITION rule source (rename kept
    #    distinct: old data zone vs new link), and links never dangle.
    request['zones'].each do |identifier|
      actual = probe['zones'][identifier]
      if parsed.links.key?(identifier)
        assert(actual['kind'] == 'linked', "#{identifier} should be linked in edition #{edition}", failures)
        assert(actual['link_to'] == parsed.links[identifier],
               "#{identifier} link target wrong in edition #{edition}", failures)
        assert(all_shipped.include?(parsed.links[identifier]),
               "#{identifier} link dangles (target not shipped)", failures)
      else
        assert(actual['kind'] == 'data', "#{identifier} should be a data zone in edition #{edition}", failures)
      end
    end

    # 4. Country index: shipped candidates keep their full-edition visible
    #    position; nothing points at a module that was not shipped.
    cabinet.countries.each do |code|
      ref_order = TZInfo::Country.get(code).zone_identifiers
      got = probe['countries'][code]
      assert(got == (ref_order & all_shipped),
             "country #{code} order/filter wrong: #{got.inspect}", failures)
      got.each { |id| assert(all_shipped.include?(id), "country #{code} dangles at #{id}", failures) }
    end

    # 5. Samples recomputed in this edition match the full reference, including
    #    the two-UTC overlap and the zero-reading spring gap.
    request['samples'].each do |sample|
      got = probe['sample_results'][sample['key']]
      expected = readings(TZInfo::Timezone.get(sample['id']), sample['local_epoch'])
      assert(got['instants'] == expected,
             "sample #{sample['key']} mismatch: got #{got['instants'].inspect} want #{expected.inspect}", failures)
      if sample['expect'] == 'overlap'
        assert(got['count'] == 2, "overlap sample #{sample['key']} did not yield two UTC instants", failures)
      end
    end

    # 6. Edge probes (gap/overlap inside window + fold-in at the window edge).
    request['edge_probes'].each do |probe_req|
      got = probe['edge_results'][probe_req['key']]
      expected = readings(TZInfo::Timezone.get(probe_req['id']), probe_req['local_epoch'])
      unless probe_req['expect_count'].nil?
        assert(got['count'] == probe_req['expect_count'],
               "edge #{probe_req['key']} count=#{got['count']} want #{probe_req['expect_count']}", failures)
      end
      assert(got['instants'] == expected,
             "edge #{probe_req['key']} mismatch: #{got['instants'].inspect} vs #{expected.inspect}", failures)
    end

    # 7. Whole-window coverage: every reference transition inside the coverage
    #    window must be present in the clipped window module (no per-second
    #    samples, but no transition in the window may be missing).
    cov_start, cov_end = Catalog.coverage_bounds(cabinet)
    result[:files].select { |f| f[:form] == 'window_rule' }.each do |file|
      source = File.read(File.join(result[:root], file[:path]))
      clipped_epochs = source.scan(/tz\.transition\s+\d+,\s+\d+,\s+:\w+,\s+(-?\d+)/).flatten.map(&:to_i).sort
      ref_zone = TZInfo::Timezone.get(file[:identifier])
      ref_epochs = ref_zone.transitions_up_to(Time.at(cov_end + 86_400, in: 'UTC'),
                                              Time.at(cov_start - 86_400, in: 'UTC'))
                              .map { |t| t.at.to_i }
                              .select { |epoch| epoch >= cov_start && epoch <= cov_end }.sort
      assert(clipped_epochs == ref_epochs,
             "window coverage broken for #{file[:identifier]}: clipped #{clipped_epochs.inspect} " \
             "must keep every in-window transition #{ref_epochs.inspect}", failures)
    end

    failures
  end

  def audit_all
    reference
    baselines = %w[A B].to_h { |edition| [edition, Packer.build_baseline(edition)] }
    report = []
    Catalog::CABINETS.each do |cabinet|
      %w[A B].each do |edition|
        result = Packer.build(cabinet, edition)
        failures = audit_package(cabinet, edition, result)
        failures.concat(baseline_cross_check(cabinet, edition, result, baselines.fetch(edition)))
        report << { cabinet: cabinet.key, edition: edition, result: result, failures: failures }
      end
    end
    report
  end

  # Every reading the clipped package returns for the cabinet's samples and
  # window edges must be identical to the unclipped full baseline generated
  # from the same short rule source.
  def baseline_cross_check(cabinet, edition, result, baseline_root)
    request = build_request(cabinet, edition, result)
    request['countries'] = []
    clipped = run_probe(result[:root], request)
    baseline = run_probe(baseline_root, request)
    failures = []
    %w[sample_results edge_results].each do |section|
      clipped.fetch(section).each do |key, value|
        expected = baseline.dig(section, key)
        if expected.nil?
          failures << "baseline lacks #{section}/#{key} (zone not in full source)"
        elsif value['instants'] != expected['instants']
          failures << "#{section}/#{key} differs from full baseline: " \
                      "#{value['instants'].inspect} vs #{expected['instants'].inspect}"
        end
      end
    end
    failures
  end

  # Cross-edition rollback: the same audit sample must give identical UTC
  # instants whether read through the old (A) or new (B) package, so an
  # already-booked reading can be re-verified after a swap and switched back.
  def rollback_equivalence
    reference
    notes = []
    Catalog::CABINETS.each do |cabinet|
      probes = %w[A B].to_h do |edition|
        result = Packer.build(cabinet, edition)
        request = build_request(cabinet, edition, result).merge(
          'samples' => cabinet.samples.map do |s|
            { 'key' => s[:id], 'id' => s[:id], 'local_epoch' => s[:local].to_i }
          end
        )
        [edition, run_probe(result[:root], request)]
      end
      cabinet.samples.each do |sample|
        a = probes['A']['sample_results'][sample[:id]]['instants']
        b = probes['B']['sample_results'][sample[:id]]['instants']
        notes << [cabinet.key, sample[:id], a == b]
      end
    end
    notes
  end
end
