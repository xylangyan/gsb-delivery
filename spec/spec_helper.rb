# frozen_string_literal: true

# coverage setup must come before loading lib/ code
require "support/coverage"

require File.expand_path("#{File.dirname(__FILE__)}/../lib/feedjira")
require "sample_feeds"
require "support/offline_archive_fixtures"

SAXMachine.handler = ENV["HANDLER"].to_sym if ENV["HANDLER"]

RSpec.configure do |c|
  c.include SampleFeeds
  c.include OfflineArchiveFixtures
  c.after { cleanup_temporary_files }
end
