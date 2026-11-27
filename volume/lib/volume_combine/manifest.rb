# frozen_string_literal: true

require 'json'

module VolumeCombine
  # Loads and validates the assembly manifest (拼卷单) and the external work
  # order address list (收件单). Both live in one JSON document.
  #
  # Layout entries use 1 based source page numbers for human authors; the rest
  # of the system works with 0 based page indexes.
  class Manifest
    Slot = Struct.new(:slot, :source, :page_index, :group, keyword_init: true)
    Permit = Struct.new(:source, :page_index, :group, :target_group, keyword_init: true)
    WorkOrder = Struct.new(:name, :source, :page_index, :group, :view, keyword_init: true)

    attr_reader :raw, :sources, :slots, :permits, :work_orders

    def self.load(path)
      new(JSON.parse(IO.read(path, encoding: 'UTF-8')))
    end

    def initialize(raw)
      @raw = raw
      @sources = raw.fetch('sources')
      @slots = raw.fetch('layout').map do |entry|
        Slot.new(
          slot: entry.fetch('slot'),
          source: entry.fetch('source'),
          page_index: entry.fetch('page') - 1,
          group: entry.fetch('group')
        )
      end
      @permits = (raw['allowed_substitutes'] || []).map do |entry|
        Permit.new(
          source: entry.fetch('source'),
          page_index: entry.fetch('page') - 1,
          group: entry.fetch('group'),
          target_group: entry.fetch('target_group')
        )
      end
      @work_orders = (raw['work_orders'] || []).map do |entry|
        WorkOrder.new(
          name: entry.fetch('name'),
          source: entry.fetch('source'),
          page_index: entry.fetch('page') - 1,
          group: entry.fetch('group'),
          view: symbolize_view(entry['view'] || [])
        )
      end
      validate!
    end

    # PDF destination view keywords arrive as JSON strings (/FitH etc.); the
    # numeric coordinates are left untouched.
    def symbolize_view(view)
      view.map { |part| part.is_a?(String) ? part.to_sym : part }
    end

    def source_path(source_id)
      @sources.fetch(source_id).fetch('path')
    end

    def slots_for(source, page_index)
      @slots.select { |s| s.source == source && s.page_index == page_index }
    end

    private

    def validate!
      slot_numbers = @slots.map(&:slot)
      raise 'layout slot numbers must be a 1-based sequence' unless slot_numbers == (1..@slots.length).to_a
      @slots.each do |slot|
        raise "unknown source #{slot.source}" unless @sources.key?(slot.source)
      end
      @permits.each do |permit|
        raise "permit references unknown source #{permit.source}" unless @sources.key?(permit.source)
      end
      names = @work_orders.map(&:name)
      duplicates = names.select { |name| names.count(name) > 1 }.uniq
      raise "duplicate external work order names: #{duplicates.join(', ')}" unless duplicates.empty?
    end
  end
end
