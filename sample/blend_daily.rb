#!/usr/bin/env ruby
# frozen_string_literal: false

require "json"
require "optparse"

require "rss/blend"

##
# Daily driver for RSS::Blend.
#
# Reads a JSON manifest that names the input feeds, the occurrence-to-slot
# mapping, hard rules and weighted preferences, then writes one RSS 1.0 feed
# and a verification report.
#
#   ruby sample/blend_daily.rb manifest.json --rss blended.rdf
#
# Manifest shape:
#
#   {
#     "channel": {"about": "...", "title": "...", "link": "...",
#                 "description": "..."},
#     "reference": "morning",
#     "include_pending_placeholder": false,
#     "feeds": [
#       {"name": "morning", "path": "morning.rdf",
#        "slots": ["x", "y", "a", "b"]},
#       {"name": "evening", "path": "evening.rdf",
#        "slots": {"http://example.com/a": "a"}}
#     ],
#     "hard": [["a", "x"], ["b", "x"]],
#     "preferences": [
#       {"before": "x", "after": "y", "weight": 7, "source": "morning"}
#     ]
#   }
class BlendDaily
  def initialize(manifest)
    @manifest = manifest
    @blend = RSS::Blend.new(
      :channel => @manifest["channel"] || {},
      :reference => @manifest["reference"],
      :exact_limit => @manifest.fetch("exact_limit", 20),
      :allow_heuristic => @manifest.fetch("allow_heuristic", true),
      :require_optimal => @manifest.fetch("require_optimal", false),
      :include_pending_placeholder =>
        @manifest.fetch("include_pending_placeholder", false)
    )
  end

  def run
    add_feeds
    add_rules
    result = @blend.solve
    [result, result.to_rss]
  end

  attr_reader :blend

  private
  def add_feeds
    feeds = @manifest["feeds"] || []
    if feeds.empty?
      raise RSS::Blend::Error, "manifest has no feeds"
    end
    feeds.each do |feed|
      name = feed["name"]
      path = feed["path"]
      if name.nil? or name.empty?
        raise RSS::Blend::Error, "feed entry needs a name: #{feed.inspect}"
      end
      if path.nil? or path.empty?
        raise RSS::Blend::Error, "feed #{name} needs a path"
      end
      mapping = feed["slots"] || feed["mapping"]
      @blend.add_source(name, path, mapping)
    end
  end

  def add_rules
    (@manifest["hard"] || []).each do |rule|
      if rule.is_a?(Hash)
        @blend.add_hard(rule["before"], rule["after"],
                        :source => rule["source"], :id => rule["id"])
      else
        @blend.add_hard(rule[0], rule[1])
      end
    end

    (@manifest["preferences"] || []).each do |rule|
      if rule.is_a?(Hash)
        @blend.add_preference(rule["before"], rule["after"], rule["weight"],
                              :source => rule["source"], :id => rule["id"])
      else
        @blend.add_preference(rule[0], rule[1], rule[2])
      end
    end
  end
end

def main(argv)
  options = {
    :rss => nil,
    :report => nil,
    :json_report => nil,
  }
  parser = OptionParser.new do |opts|
    opts.banner = "Usage: #{File.basename($0)} MANIFEST [options]"
    opts.on("--rss PATH", "Write the blended RSS 1.0 feed") do |path|
      options[:rss] = path
    end
    opts.on("--report PATH", "Write the human-readable report") do |path|
      options[:report] = path
    end
    opts.on("--json-report PATH", "Write machine-readable diagnostics") do |path|
      options[:json_report] = path
    end
    opts.on("--verify", "Re-read the generated feed and fail on mismatch") do
      options[:verify] = true
    end
    opts.on("-h", "--help", "Show this help") do
      puts opts
      exit 0
    end
  end
  parser.parse!(argv)

  manifest_path = argv.shift
  if manifest_path.nil?
    warn(parser.banner)
    return 2
  end

  manifest = JSON.parse(File.read(manifest_path))
  daily = BlendDaily.new(manifest)
  result, rss = daily.run

  if options[:verify]
    result.verify_readback!
  end

  if options[:rss]
    File.write(options[:rss], rss.to_s)
  else
    puts rss
  end

  if options[:report]
    File.write(options[:report], result.report + "\n")
  end
  if options[:json_report]
    File.write(options[:json_report],
               JSON.pretty_generate(result.diagnostics) + "\n")
  end

  0
rescue RSS::Blend::HardCycleError => exception
  warn "hard constraint conflict:"
  exception.cycles.each {|cycle| warn "  #{cycle}" }
  1
rescue RSS::Blend::Error, JSON::ParserError, SystemCallError => exception
  warn "#{exception.class}: #{exception.message}"
  1
end

exit(main(ARGV)) if $0 == __FILE__
