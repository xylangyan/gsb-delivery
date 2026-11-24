# frozen_string_literal: true

require_relative "resumable_downloads"

class Shrine
  module Plugins
    module ResumableDownloadEndpoint
      def self.load_dependencies(uploader, **options)
        uploader.plugin :resumable_downloads, **options
      end

      def self.configure(uploader, **options)
        uploader.opts[:resumable_download_endpoint] ||= {}
        uploader.opts[:resumable_download_endpoint].merge!(options)
      end

      module ClassMethods
        def resumable_download_endpoint(**options)
          Shrine::Plugins::ResumableDownloads::Endpoint.new(
            coordinator: resumable_downloads(options.delete(:storage_key), **options),
            shrine_class: self,
            **options
          )
        end
      end
    end

    register_plugin(:resumable_download_endpoint, ResumableDownloadEndpoint)
  end
end
