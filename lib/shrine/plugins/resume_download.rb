# frozen_string_literal: true

require_relative "download_resumption"

class Shrine
  module Plugins
    register_plugin(:resume_download, DownloadResumption)
  end
end
