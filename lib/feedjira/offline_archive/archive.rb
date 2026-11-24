# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # End to end builder for one archive run.
    #
    #   archive = Feedjira::OfflineArchive::Archive.new(catalog, base_dir: dir)
    #   result = archive.build
    #   result.pages        # Atom pages (written to disk by default)
    #   result.notices      # signatures the reviewer has to see
    #   result.conflicts    # requested groups that could not be placed
    #   result.retractions  # retractions that were actually applied
    class Archive
      PageArtifact = Struct.new(
        :issue, :filename, :xml, :entries, :path, :attachments, keyword_init: true
      ) do
        def to_h
          {
            issue: issue,
            file: filename,
            entries: entries.map { |entry| { source: entry[:source], id: entry[:id], version: entry[:version] } },
            attachments: attachments
          }
        end
      end

      Result = Struct.new(
        :issues, :pages, :notices, :conflicts, :retractions, :retained_sources, :verification,
        keyword_init: true
      ) do
        def to_h
          {
            issues: issues,
            pages: pages.map(&:to_h),
            notices: notices.map(&:to_h),
            conflicts: conflicts.map(&:to_h),
            retractions: retractions.map(&:to_h),
            retained_sources: retained_sources.map(&:to_h),
            verification: verification
          }
        end
      end

      def initialize(catalog, base_dir:, verification: nil, resources: nil, write: true)
        @catalog = catalog.is_a?(Catalog) ? catalog : Catalog.load(catalog)
        @base_dir = base_dir
        @verification = verification || @catalog.verification
        @resources = flatten_resources(resources || @catalog.resources || {})
        @write = write
      end

      def build
        buckets = @catalog.issues.map { |issue| build_issue(issue) }

        pages = buckets.flat_map { |bucket| bucket[:pages] }
        artifacts = @write ? write_pages(pages) : pages

        Result.new(
          issues: @catalog.issues.map(&:id),
          pages: artifacts,
          notices: buckets.flat_map { |bucket| bucket[:notices] },
          conflicts: buckets.flat_map { |bucket| bucket[:conflicts] },
          retractions: buckets.flat_map { |bucket| bucket[:retractions] },
          retained_sources: buckets.flat_map { |bucket| bucket[:retained] },
          verification: verify(pages)
        )
      end

      private

      def build_issue(issue)
        selection = Selector.call(
          issue, load_sources(issue),
          verification: @verification, resources: @resources
        )
        attach_attachments(selection.entries)
        pages, unplaced = Planner.call(selection, page_limit: page_limit(issue, selection))
        rendered = Renderer.call(pages, issue: issue, resources: @resources)

        {
          pages: rendered,
          notices: notices_from(unplaced, issue),
          conflicts: conflicts_from(unplaced, issue),
          retractions: selection.retractions,
          retained: retained_from(rendered, issue)
        }
      end

      def page_limit(issue, selection)
        limit = issue.page_limit
        limit.positive? ? limit : [selection.entries.length, 1].max
      end

      def load_sources(issue)
        grouped = Hash.new { |hash, key| hash[key] = [] }
        issue.sources.each do |spec|
          path = File.expand_path(spec.file, @base_dir)
          grouped[spec.name] << SourceDocument.new(spec.name, path, uri: spec.uri)
        end
        grouped
      end

      # The original URL -> local file pairs for one selected entry, restricted
      # to references the entry actually carries.
      def attach_attachments(entries)
        entries.each do |item|
          resolved = item[:resolved_references] || {}
          item[:attachment_targets] = resolved.filter_map do |reference, absolute|
            local = @resources[reference] || @resources[absolute]
            [absolute, local] if local
          end.to_h
        end
      end

      def flatten_resources(resources)
        resources.each_with_object({}) { |(key, value), memo| memo[key.to_s] = value.to_s }
      end

      def notices_from(unplaced, issue)
        unplaced.select { |item| signature?(item) }.map do |item|
          entry = item.entries.first

          Notice.new(
            kind: item.kind,
            issue: issue.id,
            source: entry[:source],
            id: entry[:id],
            locations: item.constraints[:covered],
            message: item.constraints[:message],
            xml: entry[:entry].xml
          )
        end
      end

      def conflicts_from(unplaced, issue)
        unplaced.reject { |item| signature?(item) }.map do |item|
          Conflict.new(
            issue: issue.id,
            entries: item.entries.map { |entry| conflict_entry(entry) },
            constraints: item.constraints.merge(kind: item.kind)
          )
        end
      end

      def conflict_entry(entry)
        {
          source: entry[:source],
          id: entry[:id],
          version: entry[:version],
          file: entry[:entry].file,
          line_range: entry[:entry].line_range,
          byte_range: entry[:entry].byte_range,
          xml: entry[:entry].xml
        }
      end

      def retained_from(rendered, issue)
        rendered.flat_map do |page|
          page.page.entries.map do |entry|
            RetainedSource.new(
              issue: issue.id,
              source: entry[:source],
              id: entry[:id],
              version: entry[:version],
              file: entry[:entry].file,
              line_range: entry[:entry].line_range,
              byte_range: entry[:entry].byte_range
            )
          end
        end
      end

      def write_pages(pages)
        directory = File.join(@base_dir, "archive")
        FileUtils.mkdir_p(directory)
        pages.map do |rendered|
          path = File.join(directory, rendered.filename)
          File.binwrite(path, rendered.xml)
          PageArtifact.new(
            issue: rendered.issue,
            filename: rendered.filename,
            xml: rendered.xml,
            entries: rendered.page.entries,
            path: path,
            attachments: rendered.attachments
          )
        end
      end

      def verify(pages)
        Verifier.call(pages, selected: pages.flat_map { |page| page.page.entries }).to_h
      end

      def signature?(item)
        item.kind.to_s.start_with?("signature")
      end
    end
  end
end
