# frozen_string_literal: true

require "json"
require "tempfile"

module OfflineArchiveFixtures
  ROOT = File.expand_path("../fixtures/offline_archive", __dir__)

  def fixture_path(*parts)
    File.join(ROOT, *parts)
  end

  # Writes +xml+ to a temp file that is removed when the example finishes.
  def temporary_atom(xml, basename: "source")
    file = Tempfile.new([basename, ".atom"])
    file.binmode
    file.write(xml)
    file.flush
    @temporary_files ||= []
    @temporary_files << file
    file.path
  end

  def cleanup_temporary_files
    Array(@temporary_files).each(&:close!)
    @temporary_files = []
  end

  def catalog_fixture
    JSON.parse(File.read(fixture_path("catalog.json")))
  end

  def verification_fixture
    JSON.parse(File.read(fixture_path("verification.json")))
  end

  def resources_fixture
    JSON.parse(File.read(fixture_path("resources.json")))
  end

  def build_archive(catalog: catalog_fixture, verification: verification_fixture, resources: resources_fixture)
    Feedjira::OfflineArchive::Archive.new(
      catalog,
      base_dir: ROOT,
      verification: verification,
      resources: resources,
      write: false
    ).build
  end

  def single_issue(overrides = {})
    {
      "id" => "issue-test",
      "cutoff" => "2026-11-21T00:00:00Z",
      "page_limit" => 5,
      "sources" => [
        { "name" => "alpha", "file" => "sources/alpha-1120.atom", "uri" => "https://alpha.example/editions/2026-11-20/" }
      ],
      "entries" => [],
      "must_group" => [],
      "retractions" => []
    }.merge(overrides)
  end

  def alpha_entry(id, **extra)
    { "source" => "alpha", "id" => id }.merge(extra.transform_keys(&:to_s))
  end
end
