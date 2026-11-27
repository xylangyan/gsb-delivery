# frozen_string_literal: true

# Runs INSIDE an isolated Ruby subprocess whose load path contains only one
# built package's lib/ (plus the tzinfo gem). It proves that the package can be
# opened on its own on a cabinet and exercises every read the auditor asks for.
# Output is a single JSON document on stdout.
require 'json'
require 'time'
require 'tzinfo/data'
require 'tzinfo'

request = JSON.parse(File.read(ARGV.fetch(0)))
TZInfo::DataSource.set(:ruby)
ds = TZInfo::DataSource.get

out = {
  opened: true,
  data_source: ds.to_s,
  tzdata: TZInfo::Data::Version::TZDATA,
  tz_index: {
    data: ds.data_timezone_identifiers.to_a,
    linked: ds.linked_timezone_identifiers.to_a
  },
  zones: {},
  countries: {},
  sample_results: {},
  edge_results: {}
}

request.fetch('zones').each do |identifier|
  zone = TZInfo::Timezone.get(identifier)
  info = zone.send(:info)
  out[:zones][identifier] = {
    kind: info.kind_of?(TZInfo::DataSources::LinkedTimezoneInfo) ? 'linked' : 'data',
    link_to: info.respond_to?(:link_to_identifier) ? info.link_to_identifier : nil
  }
end

request.fetch('countries', []).each do |code|
  out[:countries][code] = TZInfo::Country.get(code).zone_identifiers
end

def local_reading(zone, epoch)
  local = Time.at(epoch, in: 'UTC')
  periods = zone.periods_for_local(local)
  {
    count: periods.size,
    instants: periods.map do |period|
      {
        abbr: period.zone_identifier.to_s,
        base_offset: period.base_utc_offset,
        std_offset: period.std_offset,
        total_offset: period.utc_total_offset,
        utc_epoch: epoch - period.utc_total_offset
      }
    end
  }
end

request.fetch('samples', []).each do |sample|
  zone = TZInfo::Timezone.get(sample.fetch('id'))
  out[:sample_results][sample.fetch('key')] = local_reading(zone, sample.fetch('local_epoch'))
end

request.fetch('edge_probes', []).each do |probe|
  zone = TZInfo::Timezone.get(probe.fetch('id'))
  out[:edge_results][probe.fetch('key')] = local_reading(zone, probe.fetch('local_epoch'))
end

puts JSON.generate(out)
