# frozen_string_literal: true

require_relative "resumable_downloads"

class Shrine
  module Plugins
    module ResumableDownload
      Store = Shrine::ResumableDownloads unless const_defined?(:Store)
      Manager = Shrine::ResumableDownloads unless const_defined?(:Manager)
      Ticket = Shrine::ResumableDownloads::Ticket unless const_defined?(:Ticket)
      Lease = Shrine::ResumableDownloads::Lease unless const_defined?(:Lease)

      def self.load_dependencies(uploader, **options)
        uploader.plugin :resumable_downloads, **options
      end

      def self.configure(uploader, **options)
        uploader.plugin :resumable_downloads, **options
      end
    end

    register_plugin(:resumable_download, ResumableDownload)
  end
end
