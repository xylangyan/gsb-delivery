# frozen_string_literal: true

#--
# Reopens a merged bibliography through the library's existing entry point,
# expands it, and answers the handover's queries.
#
# Usage:
#   ruby tools/bibquery.rb merge-example/merged.bib
#   ruby tools/bibquery.rb merge-example/merged.bib --query 'lovelace2024'
#   ruby tools/bibquery.rb merge-example/merged.bib --field publisher --query turing2024
#++

require 'json'
require 'optparse'

$LOAD_PATH.unshift(File.expand_path('../lib', __dir__))
require 'bibtex'

options = { fields: %w[title booktitle year publisher] }
OptionParser.new do |parser|
  parser.banner = 'Usage: ruby tools/bibquery.rb FILE [--query KEY] [--field NAME]'
  parser.on('--query KEY', 'Entry key to inspect (default: all entries)') { |v| options[:query] = v }
  parser.on('--field NAME', 'Field to read') { |v| options[:fields] = [v] }
  parser.on('--json', 'Print JSON') { options[:json] = true }
end.parse!

path = ARGV.fetch(0) { abort 'missing FILE' }

# The existing entry point: open and expand.
bib = BibTeX.open(path)
bib.replace
bib.join

entries = if options[:query]
            [bib[options[:query]]].compact
          else
            bib.entries.values
          end

rows = entries.map do |entry|
  {
    'key' => entry.key,
    'type' => entry.type,
    'crossref' => entry.fields[:crossref]&.to_s,
    'fields' => options[:fields].to_h { |name| [name, entry.get(name)&.to_s] }
  }
end

if options[:json]
  puts JSON.pretty_generate(rows)
else
  rows.each do |row|
    puts "#{row['key']} (#{row['type']})"
    row['fields'].each do |name, value|
      puts format('  %-10s %s', name, value.nil? ? '<missing>' : value)
    end
  end
end

exit 1 if options[:query] && rows.empty?
