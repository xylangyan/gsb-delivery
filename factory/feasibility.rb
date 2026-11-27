# frozen_string_literal: true

require_relative 'catalog'
require_relative 'packer'

# Minimum-feasible-budget analysis. The cheapest package for a cabinet/edition
# is the one the optimizer already builds, so its measured total is the minimum
# feasible cost. Given a tighter target this module explains exactly which
# mandatory piece blocks further removal: a queried zone, a link target that
# must ship with the link, a required country index, or an overlap/gap hour that
# forces the windowed DST module to keep its transitions.
module Feasibility
  module_function

  LOADER_BYTES = 139 + 517 # tzinfo/data.rb + version.rb

  def minimum_for(cabinet, edition)
    result = Packer.build(cabinet, edition)
    {
      cabinet: cabinet.key, edition: edition,
      minimum_bytes: result[:total_bytes], budget: cabinet.budget,
      feasible: result[:total_bytes] <= cabinet.budget,
      result: result
    }
  end

  # Walk the mandatory components in removal-blocking order.
  def blockers(cabinet, edition)
    result = Packer.build(cabinet, edition)
    blocks = []

    cabinet.zones.each do |identifier|
      file = result[:files].find { |f| f[:identifier] == identifier }
      reason =
        if file && file[:form] == 'link'
          "link #{identifier} must ship and resolve to its target module"
        elsif cabinet.samples.any? { |s| s[:id] == identifier && s[:expect] == :overlap }
          "overlap hour for #{identifier} forces the data zone to keep both offsets and its fall-back transition"
        elsif cabinet.windows.any?
          "queried zone #{identifier} must cover the full local window (no per-second samples, but every edge kept)"
        else
          "queried zone #{identifier} is mandatory"
        end
      blocks << { item: identifier, bytes: file&.dig(:bytes), reason: reason }
    end

    cabinet.countries.each do |code|
      file = result[:files].find { |f| f[:identifier] == 'index:countries' }
      blocks << { item: "country index #{code}", bytes: file&.dig(:bytes),
                  reason: "country #{code} query needs its ordered, non-dangling candidate list" }
    end

    blocks << { item: 'timezones index', bytes: result[:files].find { |f| f[:identifier] == 'index:timezones' }&.dig(:bytes),
                reason: 'RubyDataSource requires both indexes before any zone read' }
    blocks << { item: 'loader+version', bytes: LOADER_BYTES,
                reason: "require 'tzinfo/data' entry point is mandatory for standalone opening" }
    blocks
  end

  def explain_target(cabinet, edition, target_bytes)
    info = minimum_for(cabinet, edition)
    return info.merge(blocks: []) if target_bytes >= info[:minimum_bytes]

    info.merge(
      feasible: false,
      target_bytes: target_bytes,
      shortfall: info[:minimum_bytes] - target_bytes,
      blocks: blockers(cabinet, edition)
    )
  end
end
