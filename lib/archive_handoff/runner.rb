# frozen_string_literal: true

require "securerandom"
require "time"
require_relative "errors"
require_relative "store"
require_relative "progress"
require_relative "command_runner"
require_relative "warehouse"

module ArchiveHandoff
  class Runner
    attr_reader :store, :gateway, :progress, :commands, :command_runner, :occurrence_id

    def initialize(root = nil, warehouse_or_gateway = nil, state_dir: nil, store: nil,
                   warehouse: nil, gateway: nil, commands: {}, progress: nil,
                   command_runner: nil, packager: nil, occurrence_id: nil, **options)
      @store = store || (root if root.respond_to?(:load_state)) || Store.new(state_dir || (root unless root.respond_to?(:load_state)) || Dir.pwd)
      warehouse ||= warehouse_or_gateway if warehouse_or_gateway && !warehouse_or_gateway.is_a?(Gateway)
      gateway ||= warehouse_or_gateway if warehouse_or_gateway.is_a?(Gateway)
      gateway ||= Gateway.new(
        object: warehouse,
        submit: options[:submit],
        query: options[:query] || options[:find],
        settle: options[:settle],
        ack_volume: options[:ack_volume]
      )
      @gateway = gateway
      @progress = progress || Progress.new
      @commands = normalize_commands(commands, options)
      @command_runner = command_runner || CommandRunner.new(@store, progress: @progress)
      @packager = packager || options[:package_builder]
      @occurrence_id = occurrence_id
    end

    def self.open(root, **options)
      new(root, **options)
    end

    def register_occurrence(occurrence_id, manifest: nil, segments: nil, volume_key: nil,
                            correction: false, **attributes)
      manifest = Manifest.coerce(manifest, segments) if manifest || segments
      @store.mutate do |state|
        record = @store.find_occurrence(state, occurrence_id)
        if record
          record[:manifest] = manifest.to_h if manifest
          record[:updated_at] = now
          next record
        end

        record = {
          occurrence_id: occurrence_id,
          volume_key: volume_key,
          correction: correction,
          status: "registered",
          created_at: now,
          updated_at: now,
          manifest: manifest&.to_h,
          raw_volume: nil,
          volume_ack: nil,
          normalized: nil,
          segments: build_segment_records(manifest),
          packages: [],
          stale_receipts: []
        }.merge(attributes)
        @store.put_occurrence(state, record)
        record
      end
    end

    def receive_raw_volume(occurrence_id, bytes, manifest: nil, verify: true)
      existing = inspect(occurrence_id)
      manifest ||= existing&.dig(:manifest)
      manifest ||= Manifest.from_volume(bytes).to_h
      register_occurrence(occurrence_id, manifest: manifest) unless existing
      verified_manifest = verify ? verify_manifest!(occurrence_id, bytes, manifest) : Manifest.coerce(manifest).to_h

      @store.mutate do |state|
        record = @store.occurrence(state, occurrence_id)
        path, digest = @store.write_artifact(occurrence_id, "raw-volume.bin", bytes)
        record[:raw_volume] = { path: path, digest: digest, byte_length: bytes.bytesize, received_at: now }
        record[:manifest] = verified_manifest
        record[:status] = "volume_received"
        record[:updated_at] = now
        record
      end
    end

    def fetch_volume(occurrence_id, command: @commands[:fetch], stdin_data: nil)
      raise ArgumentError, "fetch command is required" unless command

      result = @command_runner.run("fetch", command, stdin_data: stdin_data)
      receive_raw_volume(occurrence_id, result.stdout)
    end

    def run(occurrence_id = nil)
      occurrence_id ||= @occurrence_id
      raise ArgumentError, "occurrence_id is required" unless occurrence_id

      record = inspect(occurrence_id)
      register_occurrence(occurrence_id) unless record

      record = inspect(occurrence_id)
      unless record[:raw_volume]
        if @commands[:fetch]
          fetch_volume(occurrence_id)
        else
          raise NotFound, "no persisted raw volume and no fetch command for #{occurrence_id}"
        end
      end

      record = inspect(occurrence_id)
      verify_manifest!(occurrence_id) unless record.dig(:manifest, :verified)
      acknowledge_volume!(occurrence_id) unless record[:volume_ack]&.dig(:confirmed)
      normalize_volume(occurrence_id) if @commands[:normalize] && !record[:normalized]

      resume(occurrence_id)
    end
    alias start run
    alias execute run
    alias call run
    alias process run

    def verify_manifest!(occurrence_id, bytes = nil, manifest = nil)
      record = inspect(occurrence_id)
      manifest ||= record[:manifest]
      raise ManifestError, "volume trailer manifest is missing" unless manifest

      manifest = Manifest.coerce(manifest)
      raw = bytes || @store.read_artifact(record[:raw_volume][:path])
      manifest.verify!(raw)

      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        occurrence[:manifest] = manifest.to_h.merge(verified: true, verified_at: now)
        if occurrence[:segments].nil? || occurrence[:segments].empty?
          occurrence[:segments] = build_segment_records(manifest)
        end
        occurrence[:updated_at] = now
        occurrence
      end
      manifest.to_h.merge(verified: true)
    end

    def acknowledge_volume!(occurrence_id)
      record = inspect(occurrence_id)
      raise ManifestError, "manifest has not been completely verified" unless record.dig(:manifest, :verified)

      ack = { occurrence_id: occurrence_id, acknowledged_at: now }
      @gateway.ack_volume(ack)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        occurrence[:volume_ack] = ack.merge(confirmed: true)
        occurrence[:status] = "volume_acknowledged"
        occurrence[:updated_at] = now
        occurrence
      end
    end

    def normalize_volume(occurrence_id, command: @commands[:normalize])
      record = inspect(occurrence_id)
      raw_path = record.dig(:raw_volume, :path)
      raise NotFound, "persisted raw volume is required for handoff" unless raw_path

      raw = @store.read_artifact(raw_path)
      result = if command
                 @command_runner.run("normalize", command, stdin_data: raw)
               else
                 CommandRunner::Result.new(step: "normalize", stdout: raw, stderr: "", status: successful_status)
               end
      path, digest = @store.write_artifact(occurrence_id, "normalized-volume.bin", result.stdout)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        occurrence[:normalized] = { path: path, digest: digest, normalized_at: now }
        occurrence[:updated_at] = now
        occurrence
      end
      result.stdout
    end

    def package_segment(occurrence_id, segment_number, previous_digest: :actual, bytes: nil,
                        issue_number: nil, command: @commands[:package])
      record = inspect(occurrence_id)
      segment = find_segment(record, segment_number)
      previous = resolve_previous_digest(record, segment_number, previous_digest)
      package_number = segment.fetch(:package_count, 0) + 1
      issue_number ||= "issue-#{package_number}-#{SecureRandom.hex(4)}"

      package_bytes = if bytes
                        bytes.b
                      elsif @packager
                        @packager.call(
                          occurrence_id: occurrence_id,
                          segment: segment_number,
                          previous_digest: previous,
                          issue_number: issue_number,
                          package_number: package_number,
                          segment_record: segment
                        ).b
                      elsif command
                        payload = JSON.generate(
                          occurrence_id: occurrence_id,
                          segment: segment_number,
                          previous_digest: previous,
                          issue_number: issue_number,
                          package_number: package_number
                        )
                        @command_runner.run("package", command, stdin_data: payload).stdout.b
                      else
                        default_package_bytes(occurrence_id, segment_number, previous, issue_number, package_number)
                      end

      filename = "segment-#{segment_number}-package-#{package_number}.bin"
      path, digest = @store.write_artifact(occurrence_id, filename, package_bytes)
      package = {
        id: "pkg-#{segment_number}-#{package_number}",
        version: package_number,
        issue_number: issue_number,
        segment: segment_number,
        previous_digest: previous,
        digest: digest,
        path: path,
        byte_length: package_bytes.bytesize,
        created_at: now
      }

      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        target = find_segment(occurrence, segment_number)
        target[:package_count] = package_number
        target[:current_package_id] = package[:id]
        target[:proposed_digest] = digest
        target[:packages] ||= []
        target[:packages] << package
        occurrence[:packages] << package
        occurrence[:updated_at] = now
      end
      package
    end

    def submit_attempt(occurrence_id, segment_number, package_id: nil)
      record = inspect(occurrence_id)
      raise ManifestError, "volume trailer manifest has not been verified" unless record.dig(:manifest, :verified)

      package = package_id ? find_package(record, package_id) : current_package(record, segment_number)
      raise NotFound, "package is required before submission" unless package

      attempt = create_attempt(occurrence_id, segment_number, package)
      package_bytes = File.binread(package[:path])
      submission = {
        occurrence_id: occurrence_id,
        occurrence: occurrence_id,
        segment: segment_number,
        segment_number: segment_number,
        attempt: attempt[:number],
        attempt_number: attempt[:number],
        attempt_id: attempt[:id],
        package_id: package[:id],
        package_version: package[:version],
        issue_number: package[:issue_number],
        package_digest: package[:digest],
        digest: package[:digest],
        previous_digest: package[:previous_digest],
        artifact_path: package[:path],
        package_bytes: package_bytes,
        bytes: package_bytes,
        body: package_bytes,
        content: package_bytes
      }

      begin
        response = @gateway.submit(submission)
        mark_attempt(occurrence_id, segment_number, attempt[:number]) do |candidate|
          candidate[:status] = "in_flight"
          candidate[:submitted_at] ||= now
          candidate[:submit_response] = stringify(response)
        end
      rescue StandardError => error
        mark_attempt(occurrence_id, segment_number, attempt[:number]) do |candidate|
          candidate[:status] = "submit_uncertain"
          candidate[:error] = error.class.name
        end
        raise SubmissionUncertain, error.message
      end

      attempt.merge(status: "in_flight")
    end

    def reconcile_segment(occurrence_id, segment_number)
      record = inspect(occurrence_id)
      segment = find_segment(record, segment_number)
      return :committed if segment[:status] == "committed" && segment[:adopted_attempt]

      attempt = current_attempt(segment)
      return :no_attempt unless attempt

      key = attempt_key(occurrence_id, segment_number, attempt[:number])
      outcome = normalize_outcome(safe_query(key))

      if outcome.nil? || unseen?(outcome)
        outcome = normalize_outcome(@gateway.settle(key))
      end

      if committed?(outcome)
        adopt_committed_outcome!(occurrence_id, segment_number, attempt, outcome)
        :committed
      elsif unpostable?(outcome)
        mark_unpostable!(occurrence_id, segment_number, attempt, outcome)
        :unpostable
      else
        :unknown
      end
    rescue SettlementResponseLost
      raise
    end

    def resume(occurrence_id = nil)
      occurrence_id ||= @occurrence_id
      raise ArgumentError, "occurrence_id is required" unless occurrence_id

      record = inspect(occurrence_id)
      raise NotFound, "unknown occurrence: #{occurrence_id}" unless record

      numbers = manifest_order(record)
      numbers.each do |segment_number|
        deliver_segment(occurrence_id, segment_number)
      end
      inspect(occurrence_id)
    end
    alias handoff resume
    alias take_over resume
    alias continue resume

    def deliver_segment(occurrence_id, segment_number)
      loop do
        record = inspect(occurrence_id)
        segment = find_segment(record, segment_number)
        return segment if segment[:status] == "committed" && segment[:adopted_attempt]

        outcome = reconcile_segment(occurrence_id, segment_number)
        if outcome == :committed
          return find_segment(inspect(occurrence_id), segment_number)
        elsif outcome == :unknown
          raise OutcomeUnknown, "settlement outcome for #{occurrence_id}/#{segment_number} is not terminal"
        end

        previous_number = previous_segment_number(record, segment_number)
        if previous_number
          previous = find_segment(record, previous_number)
          unless previous[:status] == "committed" && previous[:actual_digest]
            raise Error, "segment #{segment_number} cannot be submitted before segment #{previous_number} is confirmed"
          end

          matching_package = find_package_by_previous_digest(record, segment_number, previous[:actual_digest])
          if matching_package
            use_package!(occurrence_id, segment_number, matching_package[:id])
          else
            package_segment(occurrence_id, segment_number, previous_digest: previous[:actual_digest])
          end
        else
          package_segment(occurrence_id, segment_number, previous_digest: nil) unless current_package(record, segment_number)
        end

        if outcome == :unpostable
          package_segment(occurrence_id, segment_number, previous_digest: :actual)
        end

        submit_attempt(occurrence_id, segment_number)
      end
    end

    def apply_receipt(receipt)
      receipt = symbolize(receipt)
      occurrence_id = receipt[:occurrence_id]
      segment_number = receipt[:segment] || receipt[:segment_number]
      attempt_number = receipt[:attempt] || receipt[:attempt_number]

      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        current_adopted = segment[:adopted_attempt]

        if current_adopted && attempt_number && attempt_number < current_adopted
          occurrence[:stale_receipts] ||= []
          occurrence[:stale_receipts] << receipt.merge(ignored_at: now, reason: "older_than_adopted_attempt")
          next { status: "ignored_stale", adopted_attempt: current_adopted }
        end

        package = current_package(@store.occurrence(state, occurrence_id), segment_number)
        digest = receipt[:digest] || receipt.dig(:receipt, :digest) || package&.dig(:digest)
        if package && digest && digest != package[:digest] && segment[:adopted_attempt] != attempt_number
          raise DigestMismatch, "receipt digest does not match packaged bytes"
        end

        segment[:status] = "committed"
        segment[:adopted_attempt] = attempt_number
        segment[:adopted_submission] = attempt_number
        segment[:actual_digest] = digest
        segment[:receipt] = receipt[:receipt] || receipt
        segment[:committed_at] = now
        if attempt_number
          attempt = (segment[:attempts] || []).find { |candidate| candidate[:number] == attempt_number }
          if attempt
            attempt[:status] = "committed"
            attempt[:receipt] = segment[:receipt]
          end
        end
        { status: "committed", adopted_attempt: attempt_number }
      end
    end

    def inspect(occurrence_id)
      state = @store.load_state
      @store.find_occurrence(state, occurrence_id)
    end
    alias query_by_occurrence inspect
    alias status inspect
    alias occurrence inspect

    def snapshot(occurrence_id)
      record = inspect(occurrence_id)
      return nil unless record

      {
        occurrence_id: record[:occurrence_id],
        correction: record[:correction],
        status: record[:status],
        adopted: record[:segments].each_with_object({}) do |segment, result|
          result[segment[:number]] = {
              submission: segment[:adopted_submission] || segment[:adopted_attempt],
              attempt: segment[:adopted_attempt],
              receipt: segment[:receipt],
              package_id: segment[:adopted_package_id] || segment[:current_package_id],
              package_version: adopted_package(segment)&.dig(:version),
              issue_number: adopted_package(segment)&.dig(:issue_number),
              package_digest: adopted_package(segment)&.dig(:digest),
              actual_digest: segment[:actual_digest]
            }
          end
      }
    end

    def stderr_events(command = nil)
      events = @store.read_log.select { |event| event["type"] == "stderr" }
      events = events.select { |event| event["command"] == command.to_s } if command
      events.sort_by { |event| event["seq"] || 0 }
    end

    def stderr_text(command = nil)
      stderr_events(command).map do |event|
        if event["chunk_hex"]
          [event["chunk_hex"]].pack("H*")
        else
          event["chunk"].to_s
        end
      end.join
    end
    alias stderr_history stderr_text

    def stop!
      @command_runner.stop_all
      self
    end

    private

    def normalize_commands(commands, options)
      normalized = (commands || {}).dup
      {
        fetch: %i[fetch_command fetch_volume_command fetch],
        normalize: %i[normalize_command normalization_command normalize],
        package: %i[package_command packaging_command package]
      }.each do |canonical, aliases|
        aliases.each do |name|
          value = options[name]
          normalized[canonical] ||= value if value && !(canonical == name && value.is_a?(Hash))
        end
      end
      normalized
    end

    def safe_query(key)
      @gateway.query(key)
    rescue StandardError
      nil
    end

    def build_segment_records(manifest)
      return [] unless manifest

      manifest.segments.map do |segment|
        number = segment[:number] || segment["number"]
        {
          number: number,
          manifest_digest: segment[:digest] || segment["digest"],
          status: "pending",
          package_count: 0,
          attempts: [],
          packages: []
        }
      end
    end

    def manifest_order(record)
      order = record.dig(:manifest, :order) || record[:segments].map { |segment| segment[:number] }
      order.map(&:to_i)
    end

    def previous_segment_number(record, segment_number)
      order = manifest_order(record)
      index = order.index(segment_number.to_i)
      index&.positive? ? order[index - 1] : nil
    end

    def find_segment(record, segment_number)
      number = segment_number.to_i
      record[:segments].find { |segment| segment[:number] == number } ||
        raise(NotFound, "segment #{segment_number} not found for #{record[:occurrence_id]}")
    end

    def find_package(record, package_id)
      record[:packages].find { |package| package[:id] == package_id } ||
        raise(NotFound, "package #{package_id} not found")
    end

    def find_package_by_previous_digest(record, segment_number, digest)
      segment = find_segment(record, segment_number)
      (segment[:packages] || []).reverse.find { |package| package[:previous_digest] == digest }
    end

    def current_package(record, segment_number)
      segment = find_segment(record, segment_number)
      id = segment[:current_package_id]
      id ? (segment[:packages] || []).find { |package| package[:id] == id } : (segment[:packages] || []).last
    end

    def adopted_package(segment)
      id = segment[:adopted_package_id]
      return nil unless id

      (segment[:packages] || []).find { |package| package[:id] == id }
    end

    def use_package!(occurrence_id, segment_number, package_id)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        segment[:current_package_id] = package_id
        package = segment[:packages].find { |candidate| candidate[:id] == package_id }
        segment[:proposed_digest] = package[:digest] if package
      end
    end

    def create_attempt(occurrence_id, segment_number, package)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        number = segment[:attempt_count].to_i + 1
        attempt = {
          id: "#{occurrence_id}-#{segment_number}-attempt-#{number}",
          number: number,
          package_id: package[:id],
          package_digest: package[:digest],
          status: "prepared",
          created_at: now
        }
        segment[:attempt_count] = number
        segment[:current_attempt_number] = number
        segment[:attempts] ||= []
        segment[:attempts] << attempt
        segment[:status] = "submitting"
        attempt
      end
    end

    def current_attempt(segment)
      number = segment[:current_attempt_number]
      (segment[:attempts] || []).find { |attempt| attempt[:number] == number } || (segment[:attempts] || []).last
    end

    def attempt_key(occurrence_id, segment_number, attempt_number)
      {
        occurrence_id: occurrence_id,
        segment: segment_number.to_i,
        segment_number: segment_number.to_i,
        attempt: attempt_number.to_i,
        attempt_number: attempt_number.to_i
      }
    end

    def mark_attempt(occurrence_id, segment_number, attempt_number)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        attempt = segment[:attempts].find { |candidate| candidate[:number] == attempt_number }
        yield attempt
        attempt
      end
    end

    def adopt_committed_outcome!(occurrence_id, segment_number, attempt, outcome)
      receipt = outcome[:receipt] || outcome["receipt"] || outcome
      digest = outcome[:digest] || outcome["digest"] || receipt[:digest] || receipt["digest"] || attempt[:package_digest]
      if attempt[:package_digest] && digest && digest != attempt[:package_digest]
        raise DigestMismatch, "settled digest does not match packaged bytes for #{occurrence_id}/#{segment_number}/#{attempt[:number]}"
      end

      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        candidate = segment[:attempts].find { |item| item[:number] == attempt[:number] }
        candidate[:status] = "committed"
        candidate[:receipt] = stringify(receipt)
        candidate[:committed_at] = now
        segment[:status] = "committed"
        segment[:adopted_attempt] = attempt[:number]
        segment[:adopted_submission] = attempt[:number]
        segment[:adopted_package_id] = attempt[:package_id]
        segment[:actual_digest] = digest
        segment[:receipt] = stringify(receipt)
        segment[:committed_at] = now
        occurrence[:status] = "delivering"
      end
      @progress.publish(type: "segment_committed", occurrence_id: occurrence_id, segment: segment_number, attempt: attempt[:number])
    end

    def mark_unpostable!(occurrence_id, segment_number, attempt, outcome)
      @store.mutate do |state|
        occurrence = @store.occurrence(state, occurrence_id)
        segment = find_segment(@store.occurrence(state, occurrence_id), segment_number)
        candidate = segment[:attempts].find { |item| item[:number] == attempt[:number] }
        candidate[:status] = "unpostable"
        candidate[:settlement] = stringify(outcome)
        candidate[:settled_at] = now
        segment[:status] = "attempt_unpostable"
      end
    end

    def resolve_previous_digest(record, segment_number, previous_digest)
      return nil if previous_digest.nil?
      return previous_digest unless previous_digest == :actual || previous_digest == :proposed

      number = previous_segment_number(record, segment_number)
      raise NotFound, "no previous segment for #{segment_number}" unless number

      segment = find_segment(record, number)
      if previous_digest == :actual
        segment[:actual_digest] || raise(Error, "previous segment #{number} is not committed")
      else
        segment[:proposed_digest] || current_package(record, number)&.dig(:digest)
      end
    end

    def default_package_bytes(occurrence_id, segment_number, previous_digest, issue_number, package_number)
      JSON.generate(
        occurrence_id: occurrence_id,
        segment: segment_number,
        previous_digest: previous_digest,
        issue_number: issue_number,
        package_number: package_number,
        issued_at: now
      )
    end

    def normalize_outcome(value)
      symbolize(value)
    end

    def committed?(outcome)
      %w[committed posted settled_committed ok success].include?(outcome[:status].to_s) || outcome[:receipt]
    end

    def unpostable?(outcome)
      %w[unpostable rejected fenced settled_unpostable never_post].include?(outcome[:status].to_s)
    end

    def unseen?(outcome)
      %w[unseen not_found missing pending in_flight].include?(outcome[:status].to_s)
    end

    def symbolize(object)
      case object
      when Hash
        object.each_with_object({}) do |(key, value), result|
          result[key.to_sym] = symbolize(value)
        end
      when Array
        object.map { |item| symbolize(item) }
      else
        object
      end
    end

    def stringify(object)
      JSON.parse(JSON.generate(object))
    rescue JSON::GeneratorError
      object.to_s
    end

    def now
      Time.now.utc.iso8601(6)
    end

    def successful_status
      double = Object.new
      def double.success?
        true
      end
      double
    end
  end

  class Manifest
    attr_reader :order, :segments

    def initialize(order:, segments:)
      @order = order.map(&:to_i)
      @segments = segments.map { |segment| segment.transform_keys(&:to_sym) }
    end

    def self.coerce(manifest, segments = nil)
      return manifest if manifest.is_a?(Manifest)

      manifest = manifest ? manifest.transform_keys { |key| key.to_sym } : {}
      list = segments || manifest[:segments] || []
      list = list.map { |segment| segment.respond_to?(:to_h) ? segment.to_h : segment }
      order = manifest[:order] || list.map { |segment| segment[:number] || segment["number"] }
      new(order: order, segments: list)
    end

    def self.from_volume(raw_volume)
      parsed = new(order: [], segments: []).send(:parse, raw_volume)
      new(order: parsed[:order], segments: parsed[:segments].values)
    end

    def to_h
      { order: order, segments: segments }
    end

    def verify!(raw_volume)
      data = parse(raw_volume)
      unless data[:order] == order
        raise ManifestError, "trailer order #{data[:order].inspect} does not match manifest #{order.inspect}"
      end

      segments.each do |expected|
        number = expected[:number].to_i
        actual = data[:segments][number]
        raise ManifestError, "missing segment #{number}" unless actual

        expected_digest = expected[:digest]
        if expected_digest && actual[:digest] != expected_digest
          raise DigestMismatch, "segment #{number} digest mismatch"
        end
      end
      true
    end

    private

    def parse(raw_volume)
      if raw_volume.start_with?("{")
        parsed = JSON.parse(raw_volume, symbolize_names: true)
        if parsed[:segments].is_a?(Array)
          return {
            order: parsed[:order] || parsed[:segments].map { |segment| segment[:number] },
            segments: parsed[:segments].to_h { |segment| [segment[:number].to_i, segment] }
          }
        end
      end

      digest_by_number = {}
      raw_volume.to_s.scan(/^SEGMENT (\d+) SHA256 ([0-9a-f]{64})\n/) do |number, digest|
        digest_by_number[number.to_i] = { number: number.to_i, digest: digest }
      end
      trailer = raw_volume.to_s[/^TRAILER (.+)$/, 1]
      order = trailer ? JSON.parse(trailer).fetch("order").map(&:to_i) : digest_by_number.keys.sort
      { order: order, segments: digest_by_number }
    rescue JSON::ParserError => error
      raise ManifestError, "cannot parse volume trailer: #{error.message}"
    end
  end
end
