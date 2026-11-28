# frozen_string_literal: true

module HTTPX
  module Plugins
    #
    # This plugin adds helper methods to implement HTTP Digest Auth (https://datatracker.ietf.org/doc/html/rfc7616)
    #
    # https://gitlab.com/os85/httpx/wikis/Auth#digest-auth
    #
    module DigestAuth
      class << self
        def extra_options(options)
          options.merge(max_concurrent_requests: 1)
        end

        def load_dependencies(klass)
          require_relative "auth/digest"
          klass.plugin(:auth)
        end
      end

      # adds support for the following options:
      #
      # :digest :: instance of HTTPX::Plugins::Authentication::Digest, used to authenticate requests in the session.
      module OptionsMethods
        private

        def option_digest(value)
          raise TypeError, ":digest must be a #{Authentication::Digest}" unless value.is_a?(Authentication::Digest)

          value
        end
      end

      module RequestMethods
        attr_accessor :digest_auth_round

        def initialize(*)
          super

          @digest_auth_inflight_rounds = []

          on(:headers) do
            round = @digest_auth_round

            next unless round

            @digest_auth_inflight_rounds << round unless @digest_auth_inflight_rounds.include?(round)

            options.digest&.submitted(self, round)
          end
        end

        def digest_auth_round?
          !@digest_auth_round.nil?
        end

        # the round whose headers are currently on the wire, in request order.
        def digest_auth_inflight_round
          @digest_auth_inflight_rounds.first
        end

        def digest_auth_inflight_rounds
          @digest_auth_inflight_rounds ||= []
        end

        def digest_auth_retire_round(round)
          digest_auth_inflight_rounds.delete(round)
        end

        def response=(response)
          case response
          when Response
            if response.status >= 200
              round = digest_auth_inflight_rounds.shift

              response.digest_auth_round = round if round && response.respond_to?(:digest_auth_round=)
            end
          when ErrorResponse
            digest_auth_inflight_rounds.shift
          end

          super
        end

        def emit_response(response)
          response = verify_digest_auth_response(response) if response.is_a?(Response)

          super
        end

        private

        def verify_digest_auth_response(response)
          digest = options.digest

          return response unless digest

          next_nonce = digest.verify(response)

          digest.remember_next_nonce(response, next_nonce) if next_nonce

          response
        rescue Authentication::Digest::ResponseError => e
          error_response = ErrorResponse.new(self, e)
          @response = error_response
          error_response
        end
      end

      module ResponseMethods
        attr_accessor :digest_auth_round
      end

      module ResponseBodyMethods
        def initialize(response, options)
          super

          request = response.request
          round = if request.respond_to?(:digest_auth_inflight_round)
            request.digest_auth_inflight_round
          end

          @digest_auth_entity_digest = if round && round.auth_int?
            round.algorithm.new
          end
        end

        def write(chunk)
          @digest_auth_entity_digest&.update(chunk)

          super
        end

        def digest_auth_entity_hash
          @digest_auth_entity_digest&.dup&.hexdigest
        end
      end

      module InstanceMethods
        # returns a session which will authenticate requests using HTTP Digest Auth, with
        # +user+ and +password+ as credentials.
        def digest_auth(user, password, hashed: false)
          with(digest: Authentication::Digest.new(user, password, hashed: hashed))
        end

        def build_request(*args)
          request = super

          request.body.enable_replay! if request.options.digest && !request.empty?

          request
        end

        private

        def send_requests(*requests)
          return super unless requests.any? { |request| request.options.digest }

          requests.flat_map do |request|
            send_digest_request(request) { |req| super(req) }
          end
        end

        def send_digest_request(request, &send)
          digest = request.options.digest

          return send.call(request) unless digest

          request.body.enable_replay! unless request.empty?

          if !request.authorized? && request.digest_auth_round.nil? && (challenge = digest.reusable_challenge(request))
            auth_header = digest.authenticate(request, challenge)

            return request.response unless auth_header

            request.authorize(auth_header)
          end

          probe_response = wrap { send.call(request).first }

          return probe_response unless probe_response.is_a?(Response)

          return probe_response unless probe_response.status == 401

          challenge = probe_response.headers["www-authenticate"]

          return probe_response unless digest.can_authenticate?(challenge)

          digest.forget_next_nonce(request, challenge)

          request.transition(:idle)
          request.unauthorize!
          request.digest_auth_round = nil

          attempts = 0
          response = nil

          loop do
            attempts += 1

            auth_header = digest.authenticate(request, challenge)

            break unless auth_header

            request.authorize(auth_header)

            break if request.response

            response = wrap { send.call(request).first }

            break unless response.is_a?(Response)

            next_challenge = response.headers["www-authenticate"]

            break unless response.status == 401 && digest.can_authenticate?(next_challenge)

            digest.forget_next_nonce(request, challenge)

            request.transition(:idle)
            request.unauthorize!

            challenge = next_challenge

            break if attempts >= 2
          end

          response || request.response || probe_response
        end
      end
    end

    register_plugin :digest_auth, DigestAuth
  end
end
