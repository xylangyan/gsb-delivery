# frozen_string_literal: true

require_relative "test_helper"
require "httpx/plugins/digest_auth"

class DigestAuthAuthIntTest < Minitest::Test
  class Peer
    attr_reader :trace

    def initialize(response_status: 200, bad_rspauth: false, stale_once: false)
      @server, client = Socket.pair(:UNIX, :STREAM, 0)
      @io = UNIXSocket.for_fd(client.fileno)
      @trace = []
      @response_status = response_status
      @bad_rspauth = bad_rspauth
      @stale_once = stale_once
      @nonce = "nonce1"
      @thread = Thread.new { run }
    end

    def client_io
      @io
    end

    def run
      loop do
        request = read_request
        break unless request

        @trace << request
        serve(request)
      end
    rescue IOError, Errno::EPIPE
    end

    def close
      @thread.kill
      @server.close
      @io.close unless @io.closed?
    rescue IOError, Errno::EBADF
    end

    def read_request
      line = @server.gets
      return unless line

      verb, target, = line.split(" ")
      headers = {}
      while (header = @server.gets)
        break if header == "\r\n"

        key, value = header.split(":", 2)
        headers[key.downcase] = value.strip
      end
      body = headers["content-length"] ? @server.read(headers["content-length"].to_i) : ""
      { verb: verb, target: target, headers: headers, body: body }
    end

    def serve(request)
      authorization = request[:headers]["authorization"]

      unless authorization
        challenge
        return
      end

      params = parse(authorization)
      expected = expected_response(request, params)

      if params["response"] != expected
        challenge
        return
      end

      if @stale_once && params["nonce"] == "nonce1"
        @stale_once = false
        @nonce = "nonce2"
        challenge(stale: true)
        return
      end

      body = "received"
      response(body, params, digest: digest(body))
    end

    def challenge(stale: false)
      value = %(Digest realm="realm", nonce="#{@nonce}", qop="auth-int", algorithm=SHA-256, opaque="op")
      value << ", stale=true" if stale
      write(401, "no", "WWW-Authenticate" => value)
    end

    def response(body, params, digest:)
      rspauth = rspauth_for(params, digest)
      rspauth = "0" * 64 if @bad_rspauth
      info = %(qop=#{params["qop"]}, rspauth="#{rspauth}", cnonce="#{params["cnonce"]}", nc=#{params["nc"]})
      write(@response_status, body, "Authentication-Info" => info)
    end

    def write(status, body, headers)
      @server.write("HTTP/1.1 #{status} X\r\n")
      headers.each { |k, v| @server.write("#{k}: #{v}\r\n") }
      @server.write("Content-Length: #{body.bytesize}\r\nConnection: close\r\n\r\n#{body}")
    end

    def parse(header)
      header.sub(/\ADigest /, "").scan(/(\w+)=(?:"([^"]*)"|([^,\s]+))/).to_h do |key, quoted, bare|
        [key, quoted || bare]
      end
    end

    def digest(value)
      Digest::SHA256.hexdigest(value)
    end

    def expected_response(request, params)
      ha1 = digest("user:realm:pass")
      ha2 = digest("#{request[:verb]}:#{params["uri"]}:#{digest(request[:body])}")
      digest("#{ha1}:#{params["nonce"]}:#{params["nc"]}:#{params["cnonce"]}:#{params["qop"]}:#{ha2}")
    end

    def rspauth_for(params, entity_digest)
      ha1 = digest("user:realm:pass")
      ha2 = digest(":#{params["uri"]}:#{entity_digest}")
      digest("#{ha1}:#{params["nonce"]}:#{params["nc"]}:#{params["cnonce"]}:#{params["qop"]}:#{ha2}")
    end
  end

  def session(peer)
    HTTPX.plugin(:digest_auth)
         .digest_auth("user", "pass")
         .with(transport: :unix, io: peer.client_io, fallback_protocol: "http/1.1")
  end

  def test_auth_int_hashes_encoded_entity_without_chunk_framing
    peer = Peer.new
    body = ["hello ", "world"]
    request = HTTPX::Request.new("POST", "http://localhost/upload", session(peer).instance_variable_get(:@options),
                                 body: body, headers: { "transfer-encoding" => "chunked" })
    request.body.enable_replay!

    raw = request.body.each.to_a.join
    assert_includes raw, "\r\n"
    assert_equal Digest::SHA256.hexdigest("hello world"), request.body.digest(Digest::SHA256)
    # now send the non-chunked equivalent through the peer for the wire-level check
    response = session(peer).post("http://localhost/upload", body: "hello world")

    assert_equal 200, response.status
    authorized = peer.trace.find { |request| request[:headers]["authorization"] }
    params = peer.parse(authorized[:headers]["authorization"])

    expected_ha2 = Digest::SHA256.hexdigest("POST:/upload:#{Digest::SHA256.hexdigest("hello world")}")
    ha1 = Digest::SHA256.hexdigest("user:realm:pass")
    expected = Digest::SHA256.hexdigest("#{ha1}:#{params["nonce"]}:#{params["nc"]}:#{params["cnonce"]}:auth-int:#{expected_ha2}")

    assert_equal expected, params["response"]
  ensure
    peer&.close
  end

  def test_rewinds_encoded_entity_after_a_partial_first_response
    peer = Peer.new
    body = OneShotReader.new("abcdefghij")
    http = session(peer)

    response = http.post("http://localhost/upload", body: body)

    assert_equal 200, response.status
    bodies = peer.trace.map { |request| request[:body] }
    assert_equal ["abcdefghij", "abcdefghij"], bodies
  ensure
    peer&.close
  end

  def test_retries_with_a_fresh_nonce_reusing_the_same_entity
    peer = Peer.new(stale_once: true)

    response = session(peer).post("http://localhost/upload", body: "abcdefghij")

    assert_equal 200, response.status
    auths = peer.trace.filter_map { |request| request[:headers]["authorization"] }
    assert_equal 2, auths.size
    assert_match(/nonce="nonce1"/, auths[0])
    assert_match(/nonce="nonce2"/, auths[1])
    bodies = peer.trace.select { |request| request[:headers]["authorization"] }.map { |request| request[:body] }
    assert_equal ["abcdefghij", "abcdefghij"], bodies
  ensure
    peer&.close
  end

  def test_rejects_a_response_with_a_bad_rspauth
    peer = Peer.new(bad_rspauth: true)

    response = session(peer).post("http://localhost/upload", body: "abcdefghij")

    assert_kind_of HTTPX::ErrorResponse, response
    assert_kind_of HTTPX::Plugins::Authentication::Digest::ResponseError, response.error
  ensure
    peer&.close
  end

  def test_refuses_to_sign_a_body_whose_length_does_not_match
    peer = Peer.new
    body = ShortReader.new("abc")

    response = session(peer).post("http://localhost/upload",
                                 body: body,
                                 headers: { "content-length" => "10" })

    assert_kind_of HTTPX::ErrorResponse, response
    assert_kind_of HTTPX::Request::Body::StagingError, response.error
    assert_empty peer.trace
  ensure
    peer&.close
  end

  def test_uses_the_verified_next_nonce_for_the_following_request
    peer = Peer.new
    peer.define_singleton_method(:serve) do |request|
      authorization = request[:headers]["authorization"]
      unless authorization
        challenge
        return
      end

      params = parse(authorization)
      if params["response"] != expected_response(request, params)
        challenge
        return
      end

      nextnonce = request[:target] == "/one" ? "nonce-next" : nil
      headers = {}
      info = %(qop=#{params["qop"]}, rspauth="#{rspauth_for(params, digest("received"))}", cnonce="#{params["cnonce"]}", nc=#{params["nc"]})
      info << %(, nextnonce="#{nextnonce}") if nextnonce
      headers["Authentication-Info"] = info
      write(200, "received", headers)
    end

    http = session(peer)
    assert_equal 200, http.get("http://localhost/one").status
    assert_equal 200, http.get("http://localhost/two").status

    auths = peer.trace.filter_map { |request| request[:headers]["authorization"] }
    assert_equal 2, auths.size
    assert_match(/nonce="nonce-next"/, auths[1])
  ensure
    peer&.close
  end


  def test_cnonce_mismatch_does_not_publish_receipt
    peer = Peer.new
    http = session(peer)
    request = http.build_request("POST", "http://localhost/upload", body: "abcdefghij")

    response = http.request(request)
    assert_equal 200, response.status

    stale_round = request.digest_auth_round
    stale_nonce = stale_round.nonce

    forged = HTTPX::Response.new(request, 200, "1.1",
                                 "authentication-info" => %(qop=auth-int, rspauth="#{stale_round.signature_for(stale_round.algorithm.hexdigest(":/upload:#{Digest::SHA256.hexdigest("other")}"))}", cnonce="not-a-round", nc=#{stale_round.nc}))

    digest = http.instance_variable_get(:@options).digest

    assert_nil digest.verify(forged)
    assert_equal stale_nonce, stale_round.nonce
  ensure
    peer&.close
  end

  class OneShotReader
    def initialize(value)
      @value = value
      @read = false
    end

    def read(_length = nil)
      return if @read

      @read = true
      @value
    end

    def rewind
      raise "this body is not rewindable"
    end

    def bytesize
      @value.bytesize
    end

    def content_type
      "application/octet-stream"
    end
  end

  class ShortReader
    def initialize(value)
      @value = value
      @read = false
    end

    def read(_length = nil)
      return if @read

      @read = true
      @value
    end

    def bytesize
      10
    end

    def content_type
      "application/octet-stream"
    end
  end
end
