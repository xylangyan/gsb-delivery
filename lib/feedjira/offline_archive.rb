# frozen_string_literal: true

require "fileutils"
require "nokogiri"
require "time"
require "uri"

require_relative "offline_archive/uri_join"
require_relative "offline_archive/raw_document"
require_relative "offline_archive/catalog"
require_relative "offline_archive/source_document"
require_relative "offline_archive/selector"
require_relative "offline_archive/planner"
require_relative "offline_archive/renderer"
require_relative "offline_archive/verifier"
require_relative "offline_archive/archive"

module Feedjira
  # Builds a set of plain Atom pages from already-verified Atom originals, so
  # that another team can open the pages with Feedjira while offline.
  #
  # The builder is deliberately conservative about fidelity:
  #
  # * every excerpted entry is carried as the exact original bytes, so signed
  #   entries survive without re-serialisation,
  # * only direct children of the original +feed+ are treated as entries,
  # * entries are grouped into as few pages as the constraints allow,
  # * a combination that cannot be satisfied is reported with the original
  #   entry bytes and the conflicting constraints instead of being guessed at.
  #
  # See README_OFFLINE_ARCHIVE.md for the catalog schema and the rules applied.
  module OfflineArchive
    # Raised when the catalog cannot be interpreted.
    class CatalogError < StandardError; end

    # Raised when the byte slicer and the XML parser disagree about a source.
    class SlicingError < StandardError; end

    # A signature that has to be shown to the reviewer instead of being filed as
    # an ordinary excerpt. +locations+ points at the content the signature
    # covers, including anything that was not selected.
    Notice = Struct.new(:kind, :issue, :source, :id, :locations, :message, :xml, keyword_init: true) do
      def to_h
        {
          kind: kind,
          issue: issue,
          source: source,
          id: id,
          locations: locations,
          message: message,
          xml: xml
        }
      end
    end

    # A requested page composition that could not be satisfied.
    Conflict = Struct.new(:issue, :entries, :constraints, keyword_init: true) do
      def to_h
        { issue: issue, constraints: constraints, entries: entries }
      end
    end

    # Provenance of a retraction that was actually applied.
    Retraction = Struct.new(
      :issue, :target_source, :target_id, :target_version, :published_by,
      :effective_source, :published_at, :forwarded, :reason, :object, keyword_init: true
    ) do
      def to_h
        {
          issue: issue,
          target_source: target_source,
          target_id: target_id,
          target_version: target_version,
          published_by: published_by,
          effective_source: effective_source,
          published_at: published_at&.utc&.iso8601,
          forwarded: forwarded,
          reason: reason,
          object: object
        }
      end
    end

    # Where an entry on a page came from in the originals, so the checked
    # excerpts can be re-inspected one by one after the move.
    RetainedSource = Struct.new(
      :issue, :source, :id, :version, :file, :line_range, :byte_range, keyword_init: true
    ) do
      def to_h
        {
          issue: issue,
          source: source,
          id: id,
          version: version,
          file: file,
          line_range: line_range,
          byte_range: byte_range
        }
      end
    end
  end
end
