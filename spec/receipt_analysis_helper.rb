# frozen_string_literal: true

$LOAD_PATH.unshift(File.expand_path('../../lib', __dir__))
require 'rspec'
require 'fluent/logger/receipt_analysis'
require 'fluent/logger/receipt_analysis/cli'

RSpec.configure do |config|
  config.expect_with :rspec
  config.mock_with :rspec
end
