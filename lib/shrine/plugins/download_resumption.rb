# frozen_string_literal: true

require_relative "resumable_downloads"

class Shrine
  module Plugins
    module DownloadResumption
      def self.load_dependencies(uploader, **options)
        uploader.plugin :resumable_downloads, **options
      end

      def self.configure(uploader, **options)
        uploader.opts[:download_resumption] = options
      end
    end

    register_plugin(:download_resumption, DownloadResumption)
    register_plugin(:download_leases, DownloadResumption)
    register_plugin(:download_sessions, DownloadResumption)
    register_plugin(:resume_downloads, DownloadResumption)
  end
end
