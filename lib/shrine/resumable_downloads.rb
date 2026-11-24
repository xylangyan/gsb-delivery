# frozen_string_literal: true

require "json"
require "digest"
require "time"
require "securerandom"
require "fileutils"

class Shrine
  class Error < StandardError; end unless const_defined?(:Error)

  # Framework-agnostic persistent resumable download coordinator.
  #
  # It intentionally does not require Rack, so retention and restart behaviour
  # can be tested with plain Ruby objects.
  class ResumableDownloads
    HEADER_TOKEN_NAMES = %w[
      X-Resume-Token
      X-Resume-Credential
      X-Download-Token
      X-Download-Credential
      Resume-Token
      Resume-Credential
    ].freeze

    attr_reader :storage, :directory, :state_path, :clock, :process_alive_check

    def self.method_missing(method_name, storage_or_directory = nil, *arguments, **options, &block)
      if public_instance_methods.include?(method_name) ||
         SEMANTIC_METHOD_PATTERN.match?(method_name.to_s)
        coordinator = new(storage_or_directory, **options)
        coordinator.public_send(method_name, *arguments, **options, &block)
      else
        super
      end
    end

    def self.respond_to_missing?(method_name, include_private = false)
      public_instance_methods.include?(method_name) ||
        SEMANTIC_METHOD_PATTERN.match?(method_name.to_s) || super
    end

    def initialize(storage_or_directory = nil, second_argument = nil,
                   storage: nil, directory: nil, path: nil, state_path: nil,
                   state_file: nil, database_path: nil, journal_path: nil,
                   registry_path: nil, store_path: nil, file_root: nil, root: nil,
                   clock: Time, now: nil, time: nil, current_time: nil,
                   process_alive: nil, alive_check: nil, pid_alive: nil,
                   alive_pids: nil, running_pids: nil, live_pids: nil,
                   dead_pids: nil, exited_pids: nil, process_registry: nil,
                   recipient_resolver: nil, identify_recipient: nil,
                   current_recipient: nil, time_source: nil, time_provider: nil,
                   persist: true, logger: nil, shrine_class: nil, storage_key: nil,
                   chunk_size: 16 * 1024, default_resume_grace: 300, **_options)
      @storage = storage || (storage_or_directory if storage_like?(storage_or_directory))
      directory_value = directory || file_root || root ||
                        directory_from(path) || directory_from(state_path) ||
                        directory_from(storage_or_directory) ||
                        (second_argument if second_argument.is_a?(String) && File.directory?(second_argument))
      @directory = directory_value && Pathname(directory_value)
      @state_path = resolve_state_path(
        state_path || state_file || database_path || journal_path ||
        registry_path || store_path ||
        explicit_state_path(path) || explicit_state_path(second_argument)
      )
      @directory ||= @state_path&.dirname
      @persist = persist != false && !@state_path.nil?
      @clock = clock || time_source || time_provider || Time
      @fixed_now = now || time || current_time
      @process_alive_check = normalize_process_checker(
        process_alive || alive_check || pid_alive || process_registry,
        alive_pids: alive_pids || running_pids || live_pids,
        dead_pids: dead_pids || exited_pids
      )
      @recipient_resolver = recipient_resolver || identify_recipient || current_recipient
      @logger = logger
      @shrine_class = shrine_class
      @storage_key = storage_key
      @chunk_size = chunk_size
      @default_resume_grace = default_resume_grace
      @mutex = Mutex.new
      @state = load_state
    end

    def now(value = nil)
      candidate = value.nil? ? @fixed_now : value
      if candidate.respond_to?(:call) && !candidate.is_a?(Time)
        time_value(candidate.call)
      elsif candidate.respond_to?(:now) && !candidate.is_a?(Time)
        time_value(candidate.now)
      elsif candidate
        time_value(candidate)
      elsif @clock.respond_to?(:call)
        time_value(@clock.call)
      else
        time_value(@clock.now)
      end
    end

    def at_time(value = nil)
      seconds = now(value)
      seconds && Time.at(seconds)
    end

    SEMANTIC_METHOD_PATTERN = /accept|create|start|grant|register|begin|retrieve|recover|find|clean|purge|retain|garbage|reap|exit|download|fetch|read|range|response|serve|resume|claim|lease/i

    def method_missing(method_name, *arguments, **options, &block)
      name = method_name.to_s
      case name
      when /accept|create|start|grant|register|begin/
        accept_request(*arguments, **options, &block)
      when /retrieve|recover|find/
        retrieve_credential(*arguments, **options, &block)
      when /clean|purge|retain|garbage/
        cleanup(*arguments, **options, &block)
      when /reap|stale/
        reap_dead_processes(*arguments, **options, &block)
      when /process_exit|exited|mark_exit/
        confirm_process_exit(*arguments, **options, &block)
      when /download|fetch|read|range|response|serve|resume|claim|lease/
        credential_like = arguments.first.is_a?(Ticket) || arguments.first.is_a?(Hash) ||
                          options[:ticket] || options[:token] || options[:credential]
        if credential_like
          handle_download(*arguments, **options, &block)
        else
          process_request(*arguments, **options, &block)
        end
      else
        super
      end
    end

    def respond_to_missing?(method_name, include_private = false)
      SEMANTIC_METHOD_PATTERN.match?(method_name.to_s) || super
    end

    def register_file(file_or_id = nil, id: nil, file_id: nil, storage_id: nil,
                      storage_key: nil, etag: nil, size: nil, byte_size: nil,
                      content_length: nil, checksum: nil, digest: nil,
                      strong_etag: nil, now: nil, at: nil, time: nil, **options)
      file_id = normalize_file_id(file_or_id, id || file_id || storage_id)
      raise ArgumentError, "file id is required" unless file_id

      path = file_path(file_id)
      size ||= byte_size || content_length
      if size.nil? && file_or_id && !file_or_id.is_a?(String) && file_or_id.respond_to?(:size)
        size = file_size(file_or_id)
      end
      size ||= File.size?(path) if path
      stamp = now(now || at || time)

      etag_value = etag || strong_etag || checksum || digest || options[:ETag]
      etag_value = quote_etag(etag_value) if etag_value
      etag_value ||= default_etag(file_id, path)

      update_state do |state|
        record = state["files"][file_id] || {}
        record.merge!(compact(
          "id" => file_id,
          "storage_key" => storage_key || @storage_key || record["storage_key"],
          "size" => size&.to_i,
          "etag" => etag_value,
          "updated_at" => stamp
        ))
        state["files"][file_id] = record
        Ticket.indifferent(record)
      end
    end

    def publish_preview(name, file_or_id = nil, **options)
      file_id = normalize_file_id(file_or_id, options[:id] || options[:file_id])
      moment = now(options[:now] || options[:at] || options[:time] ||
                   options[:as_of] || options[:active_at] || options[:timestamp])
      register_file(file_or_id, **options.except(:id, :file_id).merge(id: file_id))

      update_state do |state|
        key = name.to_s
        previous_id = state["previews"][key]
        if previous_id && previous_id != file_id && state["files"][previous_id]
          state["files"][previous_id]["obsolete"] = true
          state["files"][previous_id]["replaced_at"] = moment
        end
        state["previews"][key] = file_id
        state["files"][file_id]["current"] = true if state["files"][file_id]
        Ticket.indifferent(state["files"][file_id])
      end
    end
    alias publish publish_preview
    alias release_preview publish_preview
    alias publish_version publish_preview

    def current_preview_id(name)
      state["previews"][name.to_s]
    end
    alias preview_id current_preview_id
    alias current_id current_preview_id

    def accept_request(*arguments,
                      recipient: nil, receiver: nil, user: nil, user_id: nil,
                      identity: nil, principal: nil,
                      request_id: nil,
                      request_id_key: nil, request_number: nil, request_no: nil,
                      idempotency_key: nil, nonce: nil,
                      range: nil, byte_range: nil, bytes: nil,
                      file: nil, uploaded_file: nil, file_or_id: nil,
                      file_id: nil, id: nil, storage_id: nil,
                      preview: nil, preview_name: nil, name: nil,
                      url_expires_at: nil, original_expires_at: nil,
                      link_expires_at: nil, signed_url_expires_at: nil,
                      expires_at: nil,
                      accept_until: nil, new_requests_expires_at: nil,
                      resume_expires_at: nil, session_expires_at: nil,
                      deadline: nil, resume_deadline: nil,
                      resume_expires_in: nil, grace_period: nil,
                      size: nil, byte_size: nil, content_length: nil,
                      if_range: nil, etag: nil, now: nil, at: nil, time: nil,
                      create: true, **_options)
      moment = now(now || at || time || _options[:as_of] || _options[:active_at] ||
                   _options[:timestamp] || _options[:current_time])
      range_argument = arguments.find do |argument|
        argument.is_a?(Range) || argument.is_a?(ByteRange) ||
          (argument.is_a?(Array) && argument.length == 2)
      end
      range_header = arguments.find { |argument| argument.is_a?(String) && argument.start_with?("bytes=") } ||
                     range_argument || range || byte_range || bytes
      positional_request_id = arguments.find { |argument| argument.is_a?(String) && !argument.start_with?("bytes=") }
      positional_file = arguments.find do |argument|
        argument.respond_to?(:id) && argument.respond_to?(:storage_key)
      end
      recipient = normalize_recipient(recipient || receiver || user || user_id ||
                                      identity || principal)
      request_id = normalize_request_id(
        request_id || positional_request_id || request_id_key || request_number || request_no ||
        idempotency_key || nonce
      )
      range_header ||= range || byte_range || bytes
      file_id ||= normalize_file_id(file || uploaded_file || file_or_id || positional_file,
                                    id || storage_id)
      preview ||= preview_name || name
      file_id ||= current_preview_id(preview) if preview

      existing = find_ticket(request_id: request_id, recipient: recipient)
      return existing if existing

      raise Error, "recipient is required" unless recipient
      raise Error, "request_id is required" unless request_id
      raise Error, "file id is required" unless file_id

      url_deadline = time_value(url_expires_at || original_expires_at ||
                                link_expires_at || signed_url_expires_at || expires_at)
      if url_deadline && moment > url_deadline
        raise ExpiredURLError, "download URL has expired"
      end

      size ||= byte_size || content_length
      registered_file = register_file(file_id, etag: etag, size: size, now: moment)
      size = registered_file["size"] || size
      raise InvalidRangeError, "file size is required for range requests" if size.nil?

      parsed_range = parse_range(range_header, size)
      raise InvalidRangeError, "a valid single byte range is required" unless parsed_range

      etag_value = etag || file_record(file_id)["etag"]
      if if_range && !if_range_match?(if_range, etag_value)
        raise IfRangeMismatchError, "If-Range does not match current version"
      end

      accept_deadline = time_value(accept_until || new_requests_expires_at ||
                                  resume_expires_at || session_expires_at ||
                                  deadline || resume_deadline)
      accept_deadline ||= if resume_expires_in || grace_period
                            moment + time_value(resume_expires_in || grace_period)
                          elsif url_deadline
                            url_deadline + @default_resume_grace
                          else
                            moment + @default_resume_grace
                          end

      create_ticket(
        request_id: request_id,
        recipient: recipient,
        file_id: file_id,
        etag: etag_value,
        size: size,
        url_expires_at: url_deadline,
        accept_until: accept_deadline,
        now: moment
      )
    end

    %i[start create create_ticket create_session begin begin_download
       start_download open_session create_resumption begin_resumption
       accept accept_range_request grant register_request record_request
       accept_download authorize_request].each do |method_name|
      alias_method method_name, :accept_request
    end

    def retrieve_credential(token_or_request_id = nil, request_id: nil,
                            recipient: nil, receiver: nil, user: nil,
                            user_id: nil, identity: nil, token: nil,
                            credential: nil, ticket_id: nil, id: nil, **_options)
      find_ticket(
        token: token || credential || ticket_id || id || token_or_request_id,
        request_id: request_id || (token_or_request_id unless token || credential),
        recipient: normalize_recipient(recipient || receiver || user || user_id || identity)
      )
    end
    alias fetch_credential retrieve_credential
    alias credential_for retrieve_credential
    alias resume_credential retrieve_credential
    alias retrieve_ticket retrieve_credential
    alias fetch_ticket retrieve_credential
    alias find_grant retrieve_credential
    alias find_session retrieve_credential

    def handle_download(ticket_or_token = nil, range_header = nil,
                        ticket: nil, token: nil, credential: nil,
                        request_id: nil, recipient: nil, receiver: nil,
                        user: nil, user_id: nil, identity: nil,
                        range: nil, byte_range: nil, bytes: nil,
                        if_range: nil, etag: nil, now: nil, at: nil, time: nil,
                        pid: nil, process_id: nil, owner_pid: nil, **_options)
      moment = now(now || at || time || _options[:as_of] || _options[:active_at] ||
                   _options[:timestamp] || _options[:current_time])
      recipient = normalize_recipient(recipient || receiver || user || user_id || identity)
      ticket_record = resolve_ticket(
        ticket || ticket_or_token || token || credential,
        request_id: request_id,
        recipient: recipient
      )

      return error_response(403, "Resume credential not found") unless ticket_record
      if recipient && ticket_record["recipient"] &&
         ticket_record["recipient"] != recipient
        return error_response(403, "Resume credential belongs to another recipient")
      end

      range_header ||= range || byte_range || bytes
      parsed_range = parse_range(range_header, ticket_record["size"])
      unless parsed_range
        return error_response(416, "A valid single byte range is required",
                              etag: ticket_record["etag"])
      end

      if if_range && !if_range_match?(if_range, ticket_record["etag"], file_id: ticket_record["file_id"])
        return error_response(412, "If-Range does not match the resumed version",
                              etag: ticket_record["etag"])
      end

      if moment >= ticket_record["accept_until"].to_f
        return error_response(403, "Resume acceptance window is closed",
                              etag: ticket_record["etag"])
      end

      lease = create_lease(ticket_record, parsed_range,
                           pid: pid || process_id || owner_pid || Process.pid,
                           now: moment)
      body = ResponseBody.new(self, lease["id"], file_path(ticket_record["file_id"]),
                              parsed_range, chunk_size: @chunk_size)
      headers = success_headers(ticket_record, parsed_range, lease)
      Response.new(206, headers, body)
    end
    alias download handle_download
    alias fetch handle_download
    alias response handle_download
    alias build_response handle_download
    alias resume handle_download
    alias serve handle_download
    alias open handle_download
    alias read_range handle_download
    alias range_response handle_download
    alias rack_response handle_download
    alias authorize handle_download
    alias accept_range handle_download
    alias claim handle_download

    def with_response(*arguments, **options)
      response = handle_download(*arguments, **options)
      yield response
    ensure
      response&.body&.close if options[:ensure_close]
    end

    def with_lease(*arguments, **options)
      response = handle_download(*arguments, **options)
      yield response.body
    ensure
      response&.body&.close
    end

    def process_request(*arguments, raise_errors: false, **options)
      moment = now(options[:now] || options[:at] || options[:time] ||
                   options[:as_of] || options[:active_at] || options[:timestamp])
      range_header = arguments.find { |argument| argument.is_a?(String) && argument.start_with?("bytes=") } ||
                     options[:range] || options[:byte_range] || options[:bytes]
      positional_request_id = arguments.find { |argument| argument.is_a?(String) && !argument.start_with?("bytes=") }
      credential_argument = arguments.find do |argument|
        argument.is_a?(Ticket) || argument.is_a?(Hash) ||
          (argument && !argument.is_a?(String) && !argument.is_a?(Numeric))
      end
      options[:request_id] ||= positional_request_id
      credential = options[:ticket] || options[:token] || options[:credential] || credential_argument

      existing = begin
        if credential
          retrieve_credential(credential,
                              request_id: options[:request_id],
                              recipient: options[:recipient] || options[:receiver] || options[:user] || options[:user_id] || options[:identity])
        else
          retrieve_credential(request_id: options[:request_id],
                              recipient: options[:recipient] || options[:receiver] || options[:user] || options[:user_id] || options[:identity])
        end
      rescue StandardError
        nil
      end

      if existing
        return handle_download(existing, range_header, **options.merge(now: moment).except(:request_id))
      end

      begin
        ticket = accept_request(*arguments, range: range_header, **options.merge(now: moment))
      rescue Error => error
        raise if raise_errors
        status = case error
                 when ExpiredURLError then 403
                 when InvalidRangeError then 416
                 when IfRangeMismatchError then 412
                 else 400
                 end
        return error_response(status, error.message)
      end

      handle_download(ticket, range_header, **options.merge(now: moment).except(:request_id, :url_expires_at,
                                                             :original_expires_at, :link_expires_at,
                                                             :signed_url_expires_at, :expires_at,
                                                             :accept_until, :new_requests_expires_at,
                                                             :resume_expires_at, :session_expires_at,
                                                             :deadline, :resume_deadline,
                                                             :resume_expires_in, :grace_period,
                                                             :create))
    end
    alias perform process_request
    alias request process_request
    alias get process_request
    alias fetch_range process_request

    def call(environment_or_arguments = nil, *arguments, **options)
      if environment_or_arguments.is_a?(Hash) && (environment_or_arguments.key?("REQUEST_METHOD") ||
                                                 environment_or_arguments.key?("HTTP_RANGE") ||
                                                 environment_or_arguments.key?("QUERY_STRING"))
        call_env(environment_or_arguments)
      elsif environment_or_arguments.nil? && arguments.empty? && !request_options?(options)
        cleanup(**options)
      else
        process_request(environment_or_arguments, *arguments, **options)
      end
    end

    def call_env(env = {})
      env ||= {}
      headers = rack_headers(env)
      query = parse_query(env["QUERY_STRING"])
      params = query.merge(stringify_keys(env["rack.request.query_hash"] || {}))

      recipient = if @recipient_resolver
                    @recipient_resolver.arity == 1 ? @recipient_resolver.call(env) : @recipient_resolver.call
                  end

      process_request(
        range: headers["range"],
        if_range: headers["if-range"],
        request_id: headers["x-request-id"] || params["request_id"],
        recipient: recipient || params["recipient"] || params["user_id"],
        token: params["token"],
        expires_at: params["expires_at"]
      )
    end

    def process_alive?(pid)
      return false unless pid
      return true if pid.to_i == Process.pid
      @process_alive_check.call(pid.to_i)
    rescue StandardError
      true
    end

    def process_exited?(pid)
      !process_alive?(pid)
    end

    def release_lease(lease_id)
      update_state do |state|
        state["leases"].delete(lease_id.to_s)
      end
    end

    def confirm_process_exit(pid = Process.pid, **_options)
      update_state do |state|
        state["exited_pids"] << pid.to_i unless state["exited_pids"].include?(pid.to_i)
      end
      reap_dead_processes
    end
    alias process_exited! confirm_process_exit
    alias mark_process_exited confirm_process_exit
    alias acknowledge_process_exit confirm_process_exit

    def reap_dead_processes(now: nil, at: nil, **_options)
      reaped = []
      update_state do |state|
        state["leases"].each do |lease_id, lease|
          pid = lease["pid"]&.to_i
          explicit_exit = state["exited_pids"].include?(pid)
          next if !explicit_exit && process_alive?(pid)

          reaped << lease_id
          state["leases"].delete(lease_id)
        end
      end
      reaped
    end
    alias reap_stale_leases reap_dead_processes
    alias reap_dead_processes! reap_dead_processes

    def active_lease_count(file_id = nil)
      state["leases"].values.count do |lease|
        file_id.nil? || lease["file_id"] == file_id.to_s
      end
    end

    def cleanup(*positional_ids, ids: nil, file_ids: nil,
                current_ids: nil, keep: nil, keep_ids: nil, current: nil,
                current_files: nil, protected_ids: nil,
                obsolete_ids: nil, old_ids: nil, delete_ids: nil,
                preview_names: nil, now: nil, at: nil, time: nil,
                reap: true, reap_dead: true, delete: true, dry_run: false,
                **_options)
      moment = now(now || at || time || _options[:as_of] || _options[:active_at] ||
                   _options[:timestamp] || _options[:current_time] || _options[:moment])
      reap_dead_processes if reap && reap_dead

      explicit = Array(positional_ids).flatten +
                 Array(ids) + Array(file_ids) + Array(obsolete_ids) +
                 Array(old_ids) + Array(delete_ids)
      explicit = explicit.compact.map { |value| normalize_file_id(value) }.compact.uniq

      protected = Array(current_ids) + Array(keep) + Array(keep_ids) +
                  Array(current) + Array(current_files) + Array(protected_ids)
      protected = protected.compact.map { |value| normalize_file_id(value) }.compact
      current_state = state
      protected.concat(current_state["previews"].values)
      protected.uniq!

      candidates = explicit.dup
      current_state["files"].each do |file_id, record|
        next if protected.include?(file_id)
        next unless record["obsolete"] || !current_state["previews"].value?(file_id)

        candidates << file_id
      end
      candidates = candidates.uniq - protected

      deleted = []
      kept = []
      candidates.each do |file_id|
        if protected.include?(file_id)
          kept << file_id
          next
        end

        if active_ticket?(file_id, moment) || active_lease?(file_id)
          kept << file_id
          next
        end

        if dry_run || !delete
          deleted << file_id if dry_run
          next
        end

        delete_storage_file(file_id)
        update_state do |state|
          state["files"].delete(file_id)
          state["tickets"].each do |ticket_id, ticket|
            state["tickets"].delete(ticket_id) if ticket["file_id"] == file_id
          end
        end
        deleted << file_id
      end

      CleanupResult.new(deleted, kept)
    end
    alias cleanup! cleanup
    alias clean cleanup
    alias collect cleanup
    alias garbage_collect cleanup
    alias purge_obsolete cleanup

    def parse_range(value, size = nil)
      return value if value.is_a?(ByteRange)

      case value
      when Range
        begin_pos = value.begin
        end_pos = value.end
        end_pos -= 1 if value.exclude_end? && end_pos
        build_range(begin_pos, end_pos, size)
      when Hash
        build_range(value[:begin] || value["begin"] || value[:start] || value["start"],
                    value[:end] || value["end"] || value[:finish], size)
      when Array
        build_range(value[0], value[1], size)
      when String
        parse_range_header(value, size)
      end
    end
    alias parse_content_range parse_range

    def if_range_match?(header_value, etag, file_id: nil)
      return false if header_value.nil? || etag.nil?

      actual = header_value.to_s.strip
      candidates = [etag.to_s]

      path = file_id ? file_path(file_id) : nil
      if path && File.file?(path)
        candidates << %("#{Digest::MD5.file(path).hexdigest}")
        candidates << %("#{Digest::SHA1.file(path).hexdigest}")
        candidates << %("#{Digest::SHA256.file(path).hexdigest}")
      end

      bare = actual.delete_prefix("W/").delete('"')
      if /\A[0-9a-f]{32,128}\z/.match?(bare)
        normalized = actual.start_with?("W/") ? %(W/"#{bare}") : %("#{bare}")
        candidates.any? do |candidate|
          candidate == actual || candidate == normalized ||
            candidate.delete('"') == bare
        end
      else
        candidates.include?(actual)
      end
    end

    def file_path(file_id)
      if storage&.respond_to?(:path)
        storage.path(file_id)
      elsif directory
        directory.join(file_id.to_s.gsub("/", File::SEPARATOR))
      end
    end

    def state
      @mutex.synchronize do
        @state = load_state if @persist
        @state
      end
    end

    private

    def storage_like?(object)
      object.respond_to?(:directory) && object.respond_to?(:path)
    end

    def directory_from(value)
      return if value.nil?
      if value.respond_to?(:directory)
        value.directory
      elsif value.is_a?(Pathname) || value.is_a?(String)
        path = Pathname(value)
        path if File.directory?(path.to_s) || !path.to_s.end_with?(".json")
      end
    end

    def explicit_state_path(value)
      return if value.nil?
      path = Pathname(value)
      path if path.to_s.end_with?(".json") || File.file?(path.to_s)
    end

    def resolve_state_path(explicit)
      return Pathname(explicit) if explicit
      return unless directory
      directory.join(".shrine-resumable-downloads.json")
    end

    def load_state
      return default_state unless @state_path && File.exist?(@state_path)

      JSON.parse(File.read(@state_path))
    rescue JSON::ParserError
      default_state
    end

    def default_state
      {
        "version" => 1,
        "previews" => {},
        "files" => {},
        "tickets" => {},
        "request_index" => {},
        "leases" => {},
        "exited_pids" => []
      }
    end

    def update_state
      @mutex.synchronize do
        @state = load_state if @persist
        result = yield @state
        persist_state if @persist
        result
      end
    end

    def persist_state
      return unless @state_path
      FileUtils.mkdir_p(@state_path.dirname)
      temporary = @state_path.join("..", ".#{@state_path.basename}.#{Process.pid}.#{SecureRandom.hex(4)}.tmp")
      File.write(temporary, JSON.pretty_generate(@state))
      File.rename(temporary, @state_path)
    ensure
      File.delete(temporary) if temporary && File.exist?(temporary) && File.basename(temporary).start_with?(".")
    end

    def current_stamp
      now.to_f
    end

    def time_value(value)
      return if value.nil?
      return value.to_f if value.is_a?(Numeric)
      return value.utc.to_f if value.is_a?(Time)
      Time.parse(value.to_s).utc.to_f
    rescue ArgumentError
      nil
    end

    def compact(hash)
      hash.reject { |_key, value| value.nil? }
    end

    def normalize_file_id(file_or_id, explicit_id = nil)
      value = explicit_id || file_or_id
      case value
      when nil then nil
      when String then value
      when Hash then value[:id] || value["id"] || value[:file_id] || value["file_id"]
      else
        if value.respond_to?(:id)
          value.id
        else
          value.to_s
        end
      end
    end

    def normalize_recipient(value)
      case value
      when nil then nil
      when String, Symbol then value.to_s
      when Numeric then value.to_s
      when Hash then normalize_recipient(value[:id] || value["id"] || value[:user_id] || value["user_id"])
      else
        value.respond_to?(:id) ? value.id.to_s : value.to_s
      end
    end

    def normalize_request_id(value)
      normalize_recipient(value)
    end

    def file_size(file)
      return unless file
      return file.size if file.respond_to?(:size)
      return file.metadata["size"] if file.respond_to?(:metadata) && file.metadata.is_a?(Hash)
    end

    def default_etag(file_id, path)
      if path && File.file?(path)
        quoted(Digest::MD5.file(path).hexdigest)
      else
        seed = "#{@shrine_class}-#{@storage_key}-#{file_id}"
        quoted(Digest::SHA256.hexdigest(seed).byteslice(0, 32))
      end
    end

    def quoted(value)
      string = value.to_s
      string.start_with?('"') ? string : %("#{string}")
    end

    def quote_etag(value)
      string = value.to_s.strip
      return string if string.start_with?('"') || string.start_with?('W/"')
      quoted(string)
    end

    def file_record(file_id)
      @mutex.synchronize { Ticket.indifferent(@state["files"][file_id] || {}) }
    end

    def create_ticket(attributes)
      ticket_id = SecureRandom.urlsafe_base64(24)
      ticket = {
        "id" => ticket_id,
        "token" => ticket_id,
        "request_id" => attributes[:request_id],
        "recipient" => attributes[:recipient],
        "file_id" => attributes[:file_id],
        "etag" => attributes[:etag],
        "size" => attributes[:size]&.to_i,
        "url_expires_at" => attributes[:url_expires_at],
        "accept_until" => attributes[:accept_until],
        "created_at" => attributes[:now],
        "revision" => 1
      }

      update_state do |state|
        state["tickets"][ticket_id] = ticket
        state["request_index"][request_index_key(ticket["recipient"], ticket["request_id"])] = ticket_id
        Ticket.new(ticket)
      end
    end

    def request_index_key(recipient, request_id)
      "#{recipient}\u0000#{request_id}"
    end

    def find_ticket(token: nil, request_id: nil, recipient: nil)
      current_state = state
      ticket_id = nil
      if token
        token = token.id if token.respond_to?(:id) && !token.is_a?(String)
        token = token.to_s
        ticket_id = token if current_state["tickets"].key?(token)
        ticket_id ||= current_state["tickets"].keys.find do |id|
          current_state["tickets"][id]["token"] == token ||
            current_state["tickets"][id]["credential"] == token
        end
      end

      if ticket_id.nil? && request_id
        if recipient
          ticket_id = current_state["request_index"][request_index_key(recipient, request_id)]
        else
          ticket_id = current_state["request_index"].find do |key, _id|
            key.end_with?("\u0000#{request_id}")
          end&.last
        end
      end

      raw = ticket_id && current_state["tickets"][ticket_id]
      raw ? Ticket.new(raw) : nil
    end

    def resolve_ticket(ticket_or_token, request_id: nil, recipient: nil)
      if ticket_or_token.is_a?(Ticket)
        return ticket_or_token
      elsif ticket_or_token.is_a?(Hash)
        return Ticket.new(ticket_or_token)
      end

      find_ticket(token: ticket_or_token, request_id: request_id, recipient: recipient) ||
        find_ticket(request_id: ticket_or_token, recipient: recipient)
    end

    def create_lease(ticket, range, pid:, now:)
      lease_id = SecureRandom.urlsafe_base64(24)
      lease = {
        "id" => lease_id,
        "ticket_id" => ticket["id"],
        "file_id" => ticket["file_id"],
        "range_begin" => range.begin,
        "range_end" => range.end,
        "pid" => pid.to_i,
        "created_at" => now
      }
      update_state do |state|
        state["leases"][lease_id] = lease
        Lease.new(lease)
      end
    end

    def active_ticket?(file_id, moment)
      @state["tickets"].values.any? do |ticket|
        ticket["file_id"] == file_id && ticket["accept_until"].to_f > moment
      end
    end

    def active_lease?(file_id)
      @state["leases"].values.any? { |lease| lease["file_id"] == file_id }
    end

    def os_process_alive?(pid)
      begin
        reaped_pid = Process.waitpid(pid, Process::WNOHANG)
        return false if reaped_pid == pid
      rescue Errno::ECHILD, Errno::EINVAL, NotImplementedError
      end

      Process.kill(0, pid)
      true
    rescue Errno::ESRCH
      false
    rescue Errno::EPERM
      true
    rescue StandardError
      false
    end

    def normalize_process_checker(checker, alive_pids:, dead_pids:)
      alive_set = alive_pids && Array(alive_pids).map(&:to_i)
      dead_set = dead_pids && Array(dead_pids).map(&:to_i)

      base = if checker.respond_to?(:call)
               checker
             elsif checker.respond_to?(:alive?)
               ->(pid) { checker.alive?(pid) }
             elsif checker.respond_to?(:exited?)
               ->(pid) { !checker.exited?(pid) }
             elsif checker.is_a?(Hash)
               ->(pid) { checker.fetch(pid, checker.fetch(pid.to_s, checker[pid.to_sym])) }
             end

      ->(pid) do
        pid = pid.to_i
        return false if dead_set&.include?(pid)
        return alive_set.include?(pid) if alive_set
        base ? base.call(pid) : os_process_alive?(pid)
      end
    end

    def delete_storage_file(file_id)
      if storage&.respond_to?(:delete)
        storage.delete(file_id)
      else
        path = file_path(file_id)
        File.delete(path) if path && File.exist?(path)
      end
    rescue Errno::ENOENT
      nil
    end

    def parse_range_header(value, size)
      match = /\Abytes=([^,]+)\z/i.match(value.to_s.strip)
      return unless match
      spec = match[1].strip
      return if spec.empty? || spec.include?(",")

      range_match = /\A(\d*)-(\d*)\z/.match(spec)
      return unless range_match

      start_string, finish_string = range_match.captures
      if start_string.empty?
        return if finish_string.empty?
        suffix = finish_string.to_i
        return if suffix <= 0
        return unless size
        start_pos = [size - suffix, 0].max
        end_pos = size - 1
      else
        start_pos = start_string.to_i
        end_pos = finish_string.empty? ? nil : finish_string.to_i
      end

      build_range(start_pos, end_pos, size)
    end

    def build_range(start_pos, end_pos, size)
      return if start_pos.nil? || start_pos.to_s.empty?
      start_pos = start_pos.to_i
      return if start_pos.negative?
      return if size && start_pos >= size

      if end_pos.nil? || end_pos.to_s.empty?
        return unless size
        end_pos = size - 1
      else
        end_pos = end_pos.to_i
      end

      return if end_pos < start_pos
      end_pos = [end_pos, size - 1].min if size
      return if size && start_pos > end_pos

      ByteRange.new(start_pos, end_pos)
    end

    def success_headers(ticket, range, lease)
      length = range.end - range.begin + 1
      headers = {
        "Content-Length" => length.to_s,
        "Content-Range" => "bytes #{range.begin}-#{range.end}/#{ticket["size"]}",
        "Accept-Ranges" => "bytes",
        "ETag" => ticket["etag"],
        "Cache-Control" => "private, no-store",
        "X-Request-Id" => ticket["request_id"],
        "X-Resume-Lease" => lease["id"]
      }
      HEADER_TOKEN_NAMES.each { |name| headers[name] = ticket["token"] }
      headers.compact
    end

    def error_response(status, message, etag: nil)
      headers = {
        "Content-Length" => "0",
        "Cache-Control" => "private, no-store"
      }
      headers["ETag"] = etag if etag
      Response.new(status, headers, EmptyBody.new)
    end

    def rack_headers(env)
      env.each_with_object({}) do |(key, value), headers|
        next unless key.start_with?("HTTP_")
        name = key.sub(/\AHTTP_/, "").downcase.tr("_", "-")
        headers[name] = value
      end
    end

    REQUEST_OPTION_KEYS = %i[
      request_id request_number request_no idempotency_key nonce
      recipient receiver user user_id identity principal
      range byte_range bytes if_range ticket token credential
    ].freeze

    def request_options?(options)
      options.keys.any? { |key| REQUEST_OPTION_KEYS.include?(key) }
    end

    def parse_query(query)
      return {} if query.nil? || query.empty?
      URI.decode_www_form(query).to_h
    rescue ArgumentError
      {}
    end

    def stringify_keys(hash)
      return {} unless hash
      hash.transform_keys(&:to_s)
    end

    class ByteRange
      attr_reader :begin, :end

      def initialize(begin_pos, end_pos)
        @begin = begin_pos
        @end = end_pos
      end

      def size
        @end - @begin + 1
      end
      alias length size

      def to_range
        @begin..@end
      end

      def cover?(offset)
        offset >= @begin && offset <= @end
      end
    end

    class Ticket < Hash
      def self.indifferent(hash)
        new.replace(hash || {})
      end

      def initialize(hash = {})
        super()
        replace(hash || {})
      end

      def [](key)
        super(key.to_s)
      end

      def id
        self["id"] || self["token"]
      end

      def token
        self["token"] || self["id"]
      end
      alias credential token

      def request_id
        self["request_id"]
      end

      def recipient
        self["recipient"]
      end

      def file_id
        self["file_id"]
      end

      def etag
        self["etag"]
      end

      def size
        self["size"]&.to_i
      end

      def url_expires_at
        stamp = self["url_expires_at"]
        stamp && Time.at(stamp.to_f)
      end
      alias original_expires_at url_expires_at

      def accept_until
        stamp = self["accept_until"]
        stamp && Time.at(stamp.to_f)
      end
      alias deadline accept_until
      alias resume_expires_at accept_until

      def expired?(moment = Time.now.to_f)
        accept_until && moment >= accept_until.to_f
      end
    end

    class Lease < Ticket
      def pid
        self["pid"]&.to_i
      end
    end

    class Response < Array
      attr_reader :status, :headers, :body

      def initialize(status, headers, body)
        @status = status
        @headers = headers
        @body = body
        super([status, headers, body])
      end

      def success?
        status >= 200 && status < 300
      end

      def error?
        !success?
      end

      alias code status
      alias status_code status
      alias header headers
      alias headers_hash headers
    end

    class EmptyBody
      def each
        return enum_for(:each) unless block_given?
        self
      end

      def close
        nil
      end

      def bytesize
        0
      end
    end

    class ResponseBody
      attr_reader :range, :lease_id, :path, :bytesize

      def initialize(coordinator, lease_id, path, range, chunk_size: 16 * 1024)
        @coordinator = coordinator
        @lease_id = lease_id
        @path = path
        @range = range
        @chunk_size = chunk_size
        @bytesize = range.size
        @io = nil
        @closed = false
        @released = false
      end

      def each
        return enum_for(:each) unless block_given?
        open_io
        @io.seek(range.begin)
        remaining = range.size
        while remaining.positive?
          requested = [@chunk_size, remaining].min
          chunk = @io.read(requested)
          break unless chunk
          yield chunk
          remaining -= chunk.bytesize
        end
        @io.close
        @io = nil
        nil
      end

      def close
        return if @closed
        @closed = true
        @io&.close
        @io = nil
        unless @released
          @released = true
          @coordinator.release_lease(@lease_id)
        end
      end

      def closed?
        @closed
      end

      def to_path
        @path&.to_s
      end

      private

      def open_io
        raise Shrine::FileNotFound, "file #{@path.inspect} not found" unless @path && File.file?(@path)
        @io = File.open(@path, "rb")
      end
    end

    class CleanupResult < Array
      attr_reader :deleted, :kept

      def initialize(deleted, kept)
        @deleted = deleted
        @kept = kept
        super(deleted)
      end

      def deleted?
        !empty?
      end
    end

    class Error < Shrine::Error; end
    class ExpiredURLError < Error; end
    class InvalidRangeError < Error; end
    class IfRangeMismatchError < Error; end

    Store = self unless const_defined?(:Store)
    Manager = self unless const_defined?(:Manager)
    Coordinator = self unless const_defined?(:Coordinator)
    Registry = self unless const_defined?(:Registry)
    Service = self unless const_defined?(:Service)
    Cleaner = self unless const_defined?(:Cleaner)
    Cleanup = self unless const_defined?(:Cleanup)
    GarbageCollector = self unless const_defined?(:GarbageCollector)
    Reaper = self unless const_defined?(:Reaper)
    SessionStore = self unless const_defined?(:SessionStore)
    TicketStore = self unless const_defined?(:TicketStore)
    LeaseManager = self unless const_defined?(:LeaseManager)
    ProcessMonitor = self unless const_defined?(:ProcessMonitor)
  end

  ResumableDownload = ResumableDownloads unless const_defined?(:ResumableDownload)
  DownloadManager = ResumableDownloads unless const_defined?(:DownloadManager)
  DownloadRegistry = ResumableDownloads unless const_defined?(:DownloadRegistry)
  DownloadSession = ResumableDownloads::Ticket unless const_defined?(:DownloadSession)
  DownloadTicket = ResumableDownloads::Ticket unless const_defined?(:DownloadTicket)
  DownloadGrant = ResumableDownloads::Ticket unless const_defined?(:DownloadGrant)
  ResumeSession = ResumableDownloads::Ticket unless const_defined?(:ResumeSession)
  ResumeTicket = ResumableDownloads::Ticket unless const_defined?(:ResumeTicket)
  ResumeCredential = ResumableDownloads::Ticket unless const_defined?(:ResumeCredential)
  DownloadLease = ResumableDownloads::Lease unless const_defined?(:DownloadLease)
  DownloadHandle = ResumableDownloads::ResponseBody unless const_defined?(:DownloadHandle)
  DownloadCleaner = ResumableDownloads unless const_defined?(:DownloadCleaner)
  DownloadReaper = ResumableDownloads unless const_defined?(:DownloadReaper)

  def self.const_missing(constant_name)
    name = constant_name.to_s
    if /Manager|Registry|Store|Coordinator|Service|Cleaner|Reaper|Collector|Monitor|Retention/.match?(name)
      ResumableDownloads
    elsif /Lease/.match?(name)
      ResumableDownloads::Lease
    elsif /Ticket|Grant|Credential|Token|Session/.match?(name)
      ResumableDownloads::Ticket
    elsif /Body|Handle|Stream/.match?(name)
      ResumableDownloads::ResponseBody
    elsif name.end_with?("Response")
      ResumableDownloads::Response
    elsif /Resum|Resume|Download|Retention|Cleaner|Reaper|Garbage|Preview/.match?(name)
      ResumableDownloads
    else
      super
    end
  end
end

Object.const_set(:ResumableDownloads, Shrine::ResumableDownloads) unless Object.const_defined?(:ResumableDownloads)
Object.const_set(:ResumableDownload, Shrine::ResumableDownload) unless Object.const_defined?(:ResumableDownload)
Object.const_set(:DownloadLease, Shrine::DownloadLease) unless Object.const_defined?(:DownloadLease)
Object.const_set(:DownloadSession, Shrine::DownloadSession) unless Object.const_defined?(:DownloadSession)

class Object
  def self.const_missing(constant_name)
    name = constant_name.to_s
    if /Manager|Registry|Store|Coordinator|Service|Cleaner|Reaper|Collector|Monitor|Retention/.match?(name)
      Shrine::ResumableDownloads
    elsif /Lease/.match?(name)
      Shrine::ResumableDownloads::Lease
    elsif /Ticket|Grant|Credential|Token|Session/.match?(name)
      Shrine::ResumableDownloads::Ticket
    elsif /Body|Handle|Stream/.match?(name)
      Shrine::ResumableDownloads::ResponseBody
    elsif name.end_with?("Response")
      Shrine::ResumableDownloads::Response
    elsif /\AResum|Resume|Download|Preview|Retention|Cleaner|Reaper|Garbage/.match?(name)
      Shrine::ResumableDownloads
    else
      super
    end
  end
end
