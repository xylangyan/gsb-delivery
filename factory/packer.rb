# frozen_string_literal: true

require 'json'
require 'fileutils'
require_relative 'catalog'
require_relative 'generator'

# Builds a minimal, independently openable TZInfo::Data package for one cabinet
# and one rule edition (A = old/recovered, B = new/current). It:
#   * resolves the work-order swap plan (prefer) plus shared blocks to concrete
#     candidates,
#   * refuses any candidate that is not on the cabinet allowlist or that belongs
#     to another edition,
#   * generates each module through Generator (approved rewrite forms only),
#   * rebuilds the two indexes filtered to shipped identifiers while keeping
#     the full-edition visible order (no dangling entries, no renamed aliases
#     collapsed into the canonical entry),
#   * measures the real on-disk byte cost and enforces the budget.
module Packer
  module_function

  REF_LIB = File.expand_path(File.join(__dir__, '..', 'lib'))

  def parsed_sources
    @parsed_sources ||= Catalog::RULE_SOURCES.to_h { |edition, path| [edition, Generator.parse(edition, path)] }
  end

  # Cheapest-feasible candidate codes for a cabinet + edition. Feasibility for
  # a window_rule candidate is that it is clipped to the cabinet window that
  # covers the audit samples for that edition; full_rule and link candidates are
  # always feasible. The optimizer independently chooses the minimum measured
  # byte candidate and then cross-checks it against the work-order swap plan
  # (cabinet.prefer) so the documented plan is also the cheapest one.
  def chosen_candidates(cabinet, edition)
    ledger_key = edition == 'A' ? :old : :new

    shared_provided = cabinet.shared_blocks.map { |name| Catalog::SHARED_BLOCKS.fetch(name).fetch('provides') }

    chosen = {}

    cabinet.zones.reject { |id| shared_provided.include?(id) }.each do |identifier|
      feasible = cabinet.candidate_allowlist
                        .map { |code| Catalog.candidate(code) }
                        .select { |c| c.identifier == identifier && c.version == edition }
      raise "no feasible candidate for #{identifier} in #{cabinet.key}/#{edition}" if feasible.empty?

      cheapest = feasible.min_by(&:bytes)
      planned_code = cabinet.prefer.fetch(identifier).fetch(ledger_key)
      unless feasible.map(&:code).include?(planned_code)
        raise "work-order plan #{planned_code} is not feasible for #{identifier}/#{edition}"
      end
      unless cheapest.code == planned_code
        raise "work-order plan #{planned_code} for #{identifier}/#{edition} is not the " \
              "cheapest feasible (#{cheapest.code}, #{cheapest.bytes} B)"
      end
      chosen[cheapest.code] = cheapest
    end

    cabinet.shared_blocks.each do |block_name|
      block = Catalog::SHARED_BLOCKS.fetch(block_name)
      code = block.fetch('candidate').fetch(edition)
      candidate = Catalog.candidate(code)
      raise "shared block #{block_name} resolves to wrong edition candidate" unless candidate.version == edition
      chosen[code] = candidate
    end

    chosen
  end

  def window_for(cabinet, edition, candidate)
    return nil unless candidate.form == :window_rule
    # All window_rule modules for a cabinet share the cabinet's coverage span
    # (union of its named local windows), independent of edition.
    Catalog.coverage_bounds(cabinet)
  end

  # Reads the full reference gem's visible order and the country metadata.
  def reference
    @reference ||= begin
      $LOAD_PATH.unshift(REF_LIB) unless $LOAD_PATH.include?(REF_LIB)
      require 'tzinfo/data'
      require 'tzinfo'
      TZInfo::DataSource.set(:ruby)
      ds = TZInfo::DataSource.get
      {
        tz_order: ds.data_timezone_identifiers + ds.linked_timezone_identifiers,
        data_ids: ds.data_timezone_identifiers.to_a,
        linked_ids: ds.linked_timezone_identifiers.to_a
      }
    end
  end

  def render_timezones_index(entries)
    lines = Generator::HEADER.lines(chomp: true) + ['']
    lines << 'module TZInfo'
    lines << '  module Data'
    lines << '    module Indexes'
    lines << '      module Timezones'
    lines << '        include TimezoneIndexDefinition'
    lines << ''
    ordered = entries.sort_by { |e| reference[:tz_order].index(e[:identifier]) || raise }
    ordered.each do |entry|
      meth = entry[:linked] ? 'linked_timezone' : 'timezone'
      lines << "        #{meth} '#{entry[:identifier]}'"
    end
    lines << '      end'
    lines << '    end'
    lines << '  end'
    lines << 'end'
    lines.join("\n") + "\n"
  end

  def render_countries_index(cabinet, shipped_ids)
    lines = Generator::HEADER.lines(chomp: true) + ['']
    lines << 'module TZInfo'
    lines << '  module Data'
    lines << '    module Indexes'
    lines << '      module Countries'
    lines << '        include CountryIndexDefinition'
    lines << ''
    cabinet.countries.each do |code|
      country = TZInfo::Country.get(code)
      kept = country.zone_info.select { |cz| shipped_ids.include?(cz.identifier) }
      dangling = country.zone_info.map(&:identifier) - shipped_ids
      if kept.empty?
        raise "country #{code} has no shipped zone in #{cabinet.key}"
      end
      lines << "        country '#{code}', #{country.name.inspect} do |c|"
      kept.each do |cz|
        lat = cz.latitude
        lon = cz.longitude
        line = "          c.timezone '#{cz.identifier}', #{lat.numerator}, #{lat.denominator}, #{lon.numerator}, #{lon.denominator}"
        line += ", #{cz.description.inspect}" if cz.description
        lines << line
      end
      lines << '        end'
      lines << "        # #{code}: dropped #{dangling.size} index entries whose modules were not shipped" unless dangling.empty?
    end
    lines << '      end'
    lines << '    end'
    lines << '  end'
    lines << 'end'
    lines.join("\n") + "\n"
  end

  def build(cabinet, edition)
    candidates = chosen_candidates(cabinet, edition)
    parsed = parsed_sources.fetch(edition)
    pkg = File.join(Catalog::BUILD_DIR, cabinet.key, edition)
    data_dir = File.join(pkg, 'lib', 'tzinfo', 'data')
    FileUtils.rm_rf(pkg)
    FileUtils.mkdir_p(data_dir)

    files = []
    entries = []
    candidates.each do |code, candidate|
      source = Generator.source_for(candidate, parsed, window: window_for(cabinet, edition, candidate))
      rel = File.join('tzinfo', 'data', Generator.definition_path(candidate.identifier))
      path = File.join(pkg, 'lib', rel)
      FileUtils.mkdir_p(File.dirname(path))
      File.write(path, source)
      bytes = source.bytesize
      files << { candidate: code, identifier: candidate.identifier, form: candidate.form.to_s,
                 edition: edition, path: "lib/#{rel}", bytes: bytes,
                 shared: cabinet.shared_blocks.any? { |b| Catalog::SHARED_BLOCKS.dig(b, 'candidate', edition) == code } }
      entries << { identifier: candidate.identifier, linked: candidate.form == :link }
    end

    shipped_ids = entries.map { |e| e[:identifier] }

    # Indexes (filtered, full-edition order preserved).
    FileUtils.mkdir_p(File.join(data_dir, 'indexes'))
    tz_index = render_timezones_index(entries)
    File.write(File.join(data_dir, 'indexes', 'timezones.rb'), tz_index)
    files << { candidate: '-', identifier: 'index:timezones', form: 'index', edition: edition,
               path: 'lib/tzinfo/data/indexes/timezones.rb', bytes: tz_index.bytesize, shared: false }

    country_index = render_countries_index(cabinet, shipped_ids)
    File.write(File.join(data_dir, 'indexes', 'countries.rb'), country_index)
    files << { candidate: '-', identifier: 'index:countries', form: 'index', edition: edition,
               path: 'lib/tzinfo/data/indexes/countries.rb', bytes: country_index.bytesize, shared: false }

    # Loader and version (copied verbatim so require 'tzinfo/data' works and the
    # original data entry point is preserved).
    FileUtils.mkdir_p(File.join(pkg, 'lib', 'tzinfo'))
    FileUtils.cp(File.join(REF_LIB, 'tzinfo', 'data.rb'), File.join(pkg, 'lib', 'tzinfo', 'data.rb'))
    FileUtils.mkdir_p(data_dir)
    FileUtils.cp(File.join(REF_LIB, 'tzinfo', 'data', 'version.rb'), File.join(data_dir, 'version.rb'))
    files << { candidate: '-', identifier: 'loader:data.rb', form: 'loader', edition: edition,
               path: 'lib/tzinfo/data.rb', bytes: File.size(File.join(pkg, 'lib', 'tzinfo', 'data.rb')), shared: false }
    files << { candidate: '-', identifier: 'loader:version.rb', form: 'loader', edition: edition,
               path: 'lib/tzinfo/data/version.rb', bytes: File.size(File.join(data_dir, 'version.rb')), shared: false }

    total = files.sum { |f| f[:bytes] }
    result = {
      cabinet: cabinet.key, edition: edition, root: pkg,
      rule_source: File.basename(Catalog::RULE_SOURCES.fetch(edition)),
      rule_label: Catalog::RULE_LABEL.fetch(edition),
      budget: cabinet.budget, total_bytes: total, within_budget: total <= cabinet.budget,
      files: files
    }
    File.write(File.join(pkg, 'manifest.json'), JSON.pretty_generate(result))
    result
  end

  def build_all
    results = []
    Catalog::CABINETS.each do |cabinet|
      %w[A B].each { |edition| results << build(cabinet, edition) }
    end
    results
  end

  # The "full edition baseline" generated unclipped from one short rule source:
  # every data zone and link the source defines, with all offsets and
  # transitions. It is independently openable and is what the clipped cabinet
  # packages are cross-checked against.
  def build_baseline(edition)
    parsed = parsed_sources.fetch(edition)
    pkg = File.join(Catalog::BUILD_DIR, 'baseline', edition)
    data_dir = File.join(pkg, 'lib', 'tzinfo', 'data')
    FileUtils.rm_rf(pkg)
    FileUtils.mkdir_p(File.join(data_dir, 'indexes'))

    entries = []
    parsed.zones.each_key do |identifier|
      candidate = Catalog::Candidate.new(code: "BASE-#{identifier}", form: :full_rule,
                                         identifier: identifier, version: edition, target: nil, bytes: 0)
      source = Generator.source_for(candidate, parsed)
      rel = File.join('tzinfo', 'data', Generator.definition_path(identifier))
      path = File.join(pkg, 'lib', rel)
      FileUtils.mkdir_p(File.dirname(path))
      File.write(path, source)
      entries << { identifier: identifier, linked: false }
    end
    parsed.links.each do |identifier, target|
      source = Generator.emit_link_module(identifier, target)
      rel = File.join('tzinfo', 'data', Generator.definition_path(identifier))
      path = File.join(pkg, 'lib', rel)
      FileUtils.mkdir_p(File.dirname(path))
      File.write(path, source)
      entries << { identifier: identifier, linked: true }
    end

    File.write(File.join(data_dir, 'indexes', 'timezones.rb'), render_timezones_index(entries))
    # Baseline carries an empty country index; it still satisfies the data
    # source, which requires both index files to load.
    File.write(File.join(data_dir, 'indexes', 'countries.rb'),
               "#{Generator::HEADER}\nmodule TZInfo\n  module Data\n    module Indexes\n" \
               "      module Countries\n        include CountryIndexDefinition\n" \
               "      end\n    end\n  end\nend\n")
    FileUtils.mkdir_p(File.join(pkg, 'lib', 'tzinfo'))
    FileUtils.cp(File.join(REF_LIB, 'tzinfo', 'data.rb'), File.join(pkg, 'lib', 'tzinfo', 'data.rb'))
    FileUtils.cp(File.join(REF_LIB, 'tzinfo', 'data', 'version.rb'), File.join(data_dir, 'version.rb'))
    pkg
  end
end
