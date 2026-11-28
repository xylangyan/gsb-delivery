# frozen_string_literal: true

require "time"
require "securerandom"
require "digest"
require "digest/md5"
require "digest/sha1"
require "digest/sha2"
require "strscan"

module HTTPX
  module Plugins
    module Authentication
      class Digest
        class Error < Error
        end

        # Raised when the peer's "Authentication-Info" header doesn't match the
        # response entity and the authorization round which was actually sent.
        class ResponseError < Error
        end

        # One authorization round: a challenge and the header generated for it.
        # It is kept until its response arrives, so that "rspauth" can be
        # verified against the exact entity and credentials that were sent.
        class Round
          attr_reader :token, :nonce, :nc, :counter, :cnonce, :qop, :algorithm, :ha1, :uri, :realm, :opaque
          attr_reader :challenge, :generation

          def initialize(token:, nonce:, nc:, counter:, cnonce:, qop:, algorithm:, ha1:, uri:, realm:, opaque:, challenge:,
                         generation:)
            @token = token
            @nonce = nonce
            @nc = nc
            @counter = counter
            @cnonce = cnonce
            @qop = qop
            @algorithm = algorithm
            @ha1 = ha1
            @uri = uri
            @realm = realm
            @opaque = opaque
            @challenge = challenge
            @generation = generation
            @committed = false
          end

          def auth_int?
            @qop == "auth-int"
          end

          # only a round whose headers reached the wire consumes a nonce count.
          def commit!
            return if @committed

            @committed = true
          end

          def signature_for(ha2)
            digest = [@ha1, @nonce]
            digest.push(@nc, @cnonce, @qop) if @qop
            digest << ha2
            @algorithm.hexdigest(digest.join(":"))
          end
        end

        # Nonce counts are scoped to a challenge (origin, authority, realm,
        # nonce and opaque), so different interfaces never share counters.
        class Space
          attr_reader :count

          def initialize
            @count = 0
          end

          def reserve
            @count + 1
          end

          def commit(counter)
            @count = counter if counter > @count
          end
        end

        CHALLENGE_ALGORITHMS = {
          "MD5" => ::Digest::MD5,
          "SHA" => ::Digest::SHA1,
          "SHA1" => ::Digest::SHA1,
          "SHA2" => ::Digest::SHA256,
          "SHA256" => ::Digest::SHA256,
          "SHA384" => ::Digest::SHA384,
          "SHA512" => ::Digest::SHA512,
          "RMD160" => ::Digest::RMD160,
        }.freeze

        def initialize(user, password, hashed: false, **)
          @user = user
          @password = password
          @hashed = hashed
          @spaces = {}
          @generations = Hash.new(0)
          @verified_challenges = {}
          @mutex = Thread::Mutex.new
        end

        def can_authenticate?(authenticate)
          authenticate && /\A\s*(?:[^,]*,\s*)*Digest[\s=]/i.match?(authenticate)
        end

        # Generates the authorization header for the given challenge. For
        # "auth-int", the encoded request entity is staged and fully hashed
        # before the header is produced.
        def authenticate(request, authenticate)
          params = parse_challenge(authenticate)

          space = authentication_space(request, params)
          counter = space.reserve
          nc = format("%<nonce>08x", nonce: counter)
          generation = next_generation(request, params)

          qop = select_qop(params["qop"])
          algorithm, sess = parse_algorithm(params["algorithm"])

          cnonce = make_cnonce if qop || sess

          a1 = if sess
            [
              (@hashed ? @password : algorithm.hexdigest("#{@user}:#{params["realm"]}:#{@password}")),
              params["nonce"],
              cnonce,
            ].join ":"
          else
            @hashed ? @password : "#{@user}:#{params["realm"]}:#{@password}"
          end

          ha1 = algorithm.hexdigest(a1)

          ha2 = if qop == "auth-int"
            entity_hash = request.body.digest(algorithm)
            algorithm.hexdigest("#{request.verb}:#{request.path}:#{entity_hash}")
          else
            algorithm.hexdigest("#{request.verb}:#{request.path}")
          end

          request_digest = [ha1, params["nonce"]]
          request_digest.push(nc, cnonce, qop) if qop
          request_digest << ha2

          token = [
            %(username="#{@user}"),
            %(nonce="#{params["nonce"]}"),
            %(uri="#{request.path}"),
            %(response="#{algorithm.hexdigest(request_digest.join(":"))}"),
          ]
          token << %(realm="#{params["realm"]}") if params.key?("realm")
          token << %(algorithm=#{params["algorithm"]}) if params.key?("algorithm")
          token << %(cnonce="#{cnonce}") if cnonce
          token << %(nc=#{nc})
          token << %(qop=#{qop}) if qop
          token << %(opaque="#{params["opaque"]}") if params.key?("opaque")

          round = Round.new(
            token: token.join(", "),
            nonce: params["nonce"],
            nc: nc,
            counter: counter,
            cnonce: cnonce,
            qop: qop,
            algorithm: algorithm,
            ha1: ha1,
            uri: request.path,
            realm: params["realm"],
            opaque: params["opaque"],
            challenge: params,
            generation: generation
          )

          request.digest_auth_round = round if request.respond_to?(:digest_auth_round=)

          "Digest #{round.token}"
        rescue HTTPX::Error => e
          response = ErrorResponse.new(request, e)
          request.response = response
          request.emit_response(response)
          nil
        end

        # Marks the round as submitted and advances the nonce count of its
        # authentication space. Called as the headers are written.
        def submitted(request, round)
          return unless round

          round.commit!

          space = @mutex.synchronize { @spaces[authentication_key(request, round.challenge)] }

          space&.commit(round.counter)
        end

        # Verifies the "Authentication-Info" of a fully received response
        # against the round which was actually sent. Returns its next nonce.
        def verify(response)
          header = response.headers["authentication-info"]

          return unless header

          params = parse_parameters(header)

          # A next nonce is a receipt for this response; it can only be published
          # once the response round has been verified. Responses without
          # "rspauth" do not get a receipt.
          return unless params.key?("rspauth")

          round = find_round(response, params)

          return unless round

          # Keep the legacy "auth" flow unchanged; integrity verification only
          # applies to auth-int rounds.
          return params["nextnonce"] unless round.auth_int?

          entity_hash = response.body.digest_auth_entity_hash
          ha2 = round.algorithm.hexdigest(":#{round.uri}:#{entity_hash}")

          return params["nextnonce"] if round.signature_for(ha2) == params["rspauth"]

          raise ResponseError, "authentication-info rspauth does not match the submitted request"
        end

        # Keeps the verified next nonce for the round's authentication space.
        # It is used to authorize the next request in that same space without
        # another challenge round-trip.
        def remember_next_nonce(response, nonce)
          round = response.digest_auth_round

          return unless round && round.auth_int?

          key = space_key(response.request, round.challenge)

          @mutex.synchronize do
            # a late response for an older challenge must not replace the
            # continuation of the current one.
            next unless round.generation == @generations[key]

            @verified_challenges[key] = {
              challenge: round.challenge.merge("nonce" => nonce),
              generation: round.generation,
            }
          end
        end

        # Returns an already verified challenge for the request's authentication
        # space, or nil. The caller can use it to pre-authorize a request.
        def reusable_challenge(request)
          @mutex.synchronize do
            matches = @verified_challenges.select do |key, entry|
              key[0] == request.origin &&
                key[1] == request.authority &&
                entry[:generation] == @generations[key]
            end

            return unless matches.size == 1

            matches.values.first[:challenge]
          end
        end

        # Clears the verified continuation for a space once a challenge for it
        # has been explicitly rejected by the peer.
        def forget_next_nonce(request, challenge)
          params = challenge.is_a?(Hash) ? challenge : parse_challenge(challenge)

          @mutex.synchronize do
            @verified_challenges.delete(space_key(request, params))
          end
        end

        private

        def parse_challenge(authenticate)
          return authenticate if authenticate.is_a?(Hash)

          auth_info = authenticate[/\bDigest\s+(.*)\z/im, 1]

          raise Error, "unsupported digest header format" unless auth_info

          parse_parameters(auth_info)
        end

        def parse_parameters(value)
          scanner = StringScanner.new(value)

          params = {}
          until scanner.eos?
            key = scanner.scan_until(/=/)
            raise Error, "unsupported digest header format" unless key&.end_with?("=")

            if scanner.peek(1) == "\""
              scanner.skip("\"")
              val = scanner.scan_until(/"/)
              raise Error, "unsupported digest header format" unless val&.end_with?("\"")

              val = val[0..-2]
              scanner.skip_until(/,/)
            else
              val = scanner.scan_until(/,|$/)

              if val&.end_with?(",")
                val = val[0..-2]
              else
                raise Error, "unsupported digest header format" unless scanner.eos?
              end
            end
            params[key[0..-2]] = val
            scanner.skip(/\s+/)
          end

          params
        end

        def select_qop(value)
          return unless value

          # some servers send multiple values wrapped in parentheses
          value = value[/\(?([^)]+)\)?/, 1]
          list = value.split(",").map { |s| s.delete_prefix("'").delete_suffix("'") }.reject(&:empty?)

          # prefer integrity protection when the peer offers it; older peers
          # which only advertise "auth" keep the legacy calculation.
          list.include?("auth-int") ? "auth-int" : list.first
        end

        def parse_algorithm(value)
          value ||= "MD5"

          match = /\A(.*?)(-sess)?\z/i.match(value)

          raise Error, "unsupported digest header format" unless match

          algorithm = CHALLENGE_ALGORITHMS[match[1].upcase.delete("-")]

          raise Error, "unknown algorithm \"#{match[1]}\"" unless algorithm

          [algorithm, match[2]]
        end

        def authentication_space(request, params)
          @mutex.synchronize do
            @spaces[authentication_key(request, params)] ||= Space.new
          end
        end

        def authentication_key(request, params)
          [*space_key(request, params), params["nonce"]]
        end

        def space_key(request, params)
          [
            request.origin,
            request.authority,
            params["realm"],
            params["opaque"],
          ]
        end

        def next_generation(request, params)
          key = space_key(request, params)

          @mutex.synchronize do
            @generations[key] += 1
          end
        end

        def find_round(response, params)
          round = response.digest_auth_round if response.respond_to?(:digest_auth_round)

          return round unless params["cnonce"]

          return round if round && round.cnonce == params["cnonce"]

          return unless response.request.respond_to?(:digest_auth_inflight_rounds)

          # out-of-order response: only a round which actually produced this
          # cnonce may sign it; an unknown one must not fall back to the
          # currently active round.
          request = response.request
          round = request.digest_auth_inflight_rounds.find { |candidate| candidate.cnonce == params["cnonce"] }

          return unless round

          request.digest_auth_retire_round(round)
          response.digest_auth_round = round if response.respond_to?(:digest_auth_round=)

          round
        end

        def make_cnonce
          ::Digest::MD5.hexdigest [
            Time.now.to_i,
            Process.pid,
            SecureRandom.random_number(2**32),
          ].join ":"
        end
      end
    end
  end
end
