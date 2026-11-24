# frozen_string_literal: true

require_relative "../plugins"
require_relative "../resumable_downloads"

class Shrine
  module Plugins
    module ResumableDownloads
      Store = Shrine::ResumableDownloads unless const_defined?(:Store)
      Manager = Shrine::ResumableDownloads unless const_defined?(:Manager)
      Coordinator = Shrine::ResumableDownloads unless const_defined?(:Coordinator)
      Ticket = Shrine::ResumableDownloads::Ticket unless const_defined?(:Ticket)
      Lease = Shrine::ResumableDownloads::Lease unless const_defined?(:Lease)
      ResponseBody = Shrine::ResumableDownloads::ResponseBody unless const_defined?(:ResponseBody)

      def self.configure(uploader, **options)
        uploader.opts[:resumable_downloads] ||= {
          state_path: nil,
          clock: Time,
          recipient_resolver: nil,
          default_resume_grace: 300
        }
        uploader.opts[:resumable_downloads].merge!(options)
      end

      module ClassMethods
        def resumable_downloads(storage_key = nil, **options)
          storage_key ||= options.delete(:storage) || options.delete(:storage_key) ||
                          opts[:resumable_downloads][:storage] || :store
          storage = storages[storage_key] || find_storage(storage_key)
          configuration = opts[:resumable_downloads].merge(options)

          @resumable_downloads ||= {}
          @resumable_downloads[storage_key] ||= Shrine::ResumableDownloads.new(
            storage,
            shrine_class: self,
            storage_key: storage_key,
            **configuration
          )
        end
        alias resumable_download_store resumable_downloads
        alias download_sessions resumable_downloads

        def resumable_download_endpoint(storage_key = nil, **options)
          Endpoint.new(
            coordinator: resumable_downloads(storage_key, **options),
            shrine_class: self,
            **opts[:resumable_downloads].merge(options)
          )
        end
      end

      module FileMethods
        def resumable_downloads(**options)
          shrine_class.resumable_downloads(storage_key, **options)
        end

        def strong_etag
          Shrine::ResumableDownloads.new(shrine_class.storages[storage_key],
                                        shrine_class: shrine_class,
                                        storage_key: storage_key)
                                  .register_file(id, size: size)["etag"]
        end
      end

      class Endpoint
        attr_reader :coordinator

        def initialize(coordinator: nil, shrine_class: nil, recipient_resolver: nil, **options)
          @coordinator = coordinator || Shrine::ResumableDownloads.new(**options)
          @shrine_class = shrine_class
          @recipient_resolver = recipient_resolver
        end

        def call(env)
          request = rack_request(env)
          return method_not_allowed unless request.get? || request.head?

          recipient = resolve_recipient(request, env)
          request_id = header(env, "HTTP_X_REQUEST_ID") || request.params["request_id"]
          coordinator.call_env(env.merge(
            "resumable.recipient" => recipient,
            "resumable.request_id" => request_id
          ))
        rescue StandardError => error
          [400, { "Content-Type" => "text/plain", "Cache-Control" => "no-store" }, [error.message]]
        end

        private

        def rack_request(env)
          if defined?(Rack::Request)
            Rack::Request.new(env)
          else
            SimpleRequest.new(env)
          end
        end

        def resolve_recipient(request, env)
          return @recipient_resolver.call(request) if @recipient_resolver&.arity == 1
          return @recipient_resolver.call(request, env) if @recipient_resolver

          env["resumable.recipient"] ||
            request.params["recipient"] ||
            env["REMOTE_USER"]
        end

        def header(env, name)
          env[name]
        end

        def method_not_allowed
          [405, { "Content-Type" => "text/plain" }, ["Method Not Allowed"]]
        end
      end

      class SimpleRequest
        attr_reader :env

        def initialize(env)
          @env = env
        end

        def get?
          env["REQUEST_METHOD"].to_s.upcase != "POST"
        end

        def head?
          env["REQUEST_METHOD"].to_s.upcase == "HEAD"
        end

        def params
          query = env["QUERY_STRING"].to_s
          URI.decode_www_form(query).to_h
        rescue ArgumentError
          {}
        end
      end
    end

    register_plugin(:resumable_downloads, ResumableDownloads)
    register_plugin(:resumable_download, ResumableDownloads)
  end

  ResumableDownloadEndpoint = Plugins::ResumableDownloads::Endpoint unless const_defined?(:ResumableDownloadEndpoint)
end
