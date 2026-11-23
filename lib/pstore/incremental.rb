# frozen_string_literal: true
#
# Incremental, keyed amendments for PStore.
#
# The PStore file itself stays an ordinary Marshal snapshot, so legacy readers
# can always open it with PStore.new(path).transaction(true).  Amendments are
# appended to a side journal as self-delimiting, checksummed frames.  Readers
# apply the complete frames to the loaded snapshot; a torn trailing frame is
# ignored.  A read therefore observes either the complete old graph or the
# complete new graph, never a mixture of roots from two generations.
#
# A correction is one of:
#   * :update  modify an existing (possibly shared) object.  The object is
#              replaced by identity everywhere it is reachable, so every root
#              that shared it observes the same new object.
#   * :replace replace the reference at one path below one root.  The other
#              root keeps its previous state; other shared references and
#              cycles in the remaining graph are preserved.
#   * :put / :delete replace or remove one root.
#
# A frame contains only the new value and the target path/key.  Unchanged
# roots are not serialized into the journal.

require "digest"

class PStore
  class AmendmentError < Error; end
  class StaleAmendmentError < AmendmentError; end

  module Incremental
    FORMAT = 1
    MAGIC = "PSTLGR01".b
    JOURNAL_SUFFIX = ".pstore-journal"
    BASE_SUFFIX = ".pstore-base"
    SNAPSHOT_SUFFIX = ".pstore-snapshot"
    CHECKSUM_BYTES = 8

    @records = {}.compare_by_identity
    @records_mutex = Mutex.new

    class << self
      # Declare an additional record type accepted by amendments.
      # Struct and Data subclasses are accepted automatically.
      def record(klass = nil, &block)
        klass ||= block
        raise ArgumentError, "record type required" unless klass.is_a?(Class)
        @records_mutex.synchronize { @records[klass] = true }
        klass
      end
      alias declared_record record
      alias trust_record record

      def registered_record?(klass)
        @records_mutex.synchronize { @records.key?(klass) }
      end

      def record_type?(klass)
        registered_record?(klass) ||
          (defined?(Struct) && klass <= Struct) ||
          (defined?(Data) && klass <= Data)
      end

      def journal_path(path)
        "#{path}#{JOURNAL_SUFFIX}"
      end

      def base_path(path)
        "#{path}#{BASE_SUFFIX}"
      end

      def snapshot_path(path)
        "#{path}#{SNAPSHOT_SUFFIX}"
      end

      # Verify that +value+ is one of the supported amendment payloads:
      # nil/true/false, numbers, strings, symbols, time-like values, arrays,
      # hashes, and declared/Struct/Data records.  Cycles and shared
      # references are accepted.
      def trusted_value?(value)
        untrusted_reason(value).nil?
      end

      def assert_trusted!(value)
        reason = untrusted_reason(value)
        return value if reason.nil?
        raise AmendmentError, "unsupported amendment value: #{reason}"
      end

      # Return nil when trusted, otherwise a short path explaining the first
      # rejected value.
      def untrusted_reason(value, path = [], seen = {})
        case value
        when NilClass, TrueClass, FalseClass, Integer, Float, String, Symbol,
             Rational, Complex, Time, Regexp, Range
          nil
        when Array
          return nil if seen[value.object_id]
          seen[value.object_id] = true
          value.each_with_index do |item, index|
            reason = untrusted_reason(item, path + [index], seen)
            return reason if reason
          end
          nil
        when Hash
          return nil if seen[value.object_id]
          seen[value.object_id] = true
          value.each do |k, v|
            reason = untrusted_reason(k, path + [:key], seen)
            return reason if reason
            reason = untrusted_reason(v, path + [k], seen)
            return reason if reason
          end
          nil
        else
          unless record_type?(value.class)
            return "#{value.class} at #{path.inspect}"
          end
          return nil if seen[value.object_id]
          seen[value.object_id] = true
          record_members(value).each do |member, member_value|
            reason = untrusted_reason(member_value, path + [member], seen)
            return reason if reason
          end
          nil
        end
      end

      def record_members(value)
        if value.is_a?(Struct)
          value.each_pair.to_a
        elsif defined?(Data) && value.is_a?(Data)
          value.members.map { |member| [member, value.public_send(member)] }
        else
          value.instance_variables.map do |name|
            [name, value.instance_variable_get(name)]
          end
        end
      end

      # Base descriptor written after a full snapshot is published:
      #   version\nsha256-of-base-file\n
      def base_info(path)
        file = base_path(path)
        return { version: 0, digest: nil } unless File.exist?(file)
        version_line, digest_line = File.binread(file).split("\n", 3)
        version = Integer(version_line, 10)
        digest = digest_line && digest_line.strip
        return { version: 0, digest: nil } if digest.nil? || digest.empty?
        { version: version, digest: digest }
      rescue ArgumentError, Errno::ENOENT
        { version: 0, digest: nil }
      end

      # Write the descriptor before the base rename.  If the process stops
      # after this write but before the rename, the old base digest will not
      # match and all journal frames are replayed, which still produces the
      # complete new graph.
      def write_base_info(path, version, digest)
        target = base_path(path)
        temp = "#{target}.tmp.#{Process.pid}.#{rand 1_000_000}"
        File.open(temp, "wb", 0o600) do |io|
          io.write("#{version}\n#{digest}\n")
          io.flush
          begin
            io.fsync
          rescue StandardError
            nil
          end
        end
        File.rename(temp, target)
        version
      ensure
        File.unlink(temp) if temp && File.exist?(temp)
      end

      def digest(data)
        Digest::SHA256.hexdigest(data)
      end

      # Merge a complete in-memory graph (already containing every committed
      # frame plus the caller's changes) into the ordinary PStore file using a
      # crash-safe order:
      #   1. write the base descriptor for the *new* snapshot first
      #   2. atomically replace the PStore file
      #   3. delete the journal
      #
      # If the process stops after step 1 but before step 2, the old base does
      # not match the descriptor, so every frame is replayed and the complete
      # new graph is reconstructed.  If it stops after step 2 but before step
      # 3, the descriptor matches and the already-merged frames are skipped.
      def merge!(pstore, file, table)
        frames = JournalStore.new(journal_path(pstore.path)).read_frames
        version = frames.map { |frame| frame[:version].to_i }.max.to_i
        version = [version, base_info(pstore.path)[:version].to_i].max
        data = Marshal.dump(table)
        write_base_info(pstore.path, version, digest(data))
        atomic_replace(pstore, file, data)
        JournalStore.new(journal_path(pstore.path)).clear!
        table
      end

      def atomic_replace(pstore, file, data)
        temp = "#{pstore.path}.tmp.#{Process.pid}.#{rand 1_000_000}"
        mode = file.stat.mode & 0o7777
        begin
          File.open(temp, File::WRONLY | File::CREAT | File::TRUNC | File::BINARY,
                    0o600, encoding: Encoding::ASCII_8BIT) do |io|
            io.write(data)
            io.flush
            begin
              io.fsync
            rescue StandardError
              nil
            end
            io.chmod(mode)
          end
          File.rename(temp, pstore.path)
        ensure
          File.unlink(temp) if File.exist?(temp)
        end
      end

      # Load the base snapshot, then apply every committed journal frame.
      # Returns [table, checksum, original_size, frames, base_digest].
      def read_state(pstore, file, read_only = true)
        data = file.read || "".b
        if data.empty?
          table = {}
          checksum = pstore.send(:empty_marshal_checksum)
        else
          table = pstore.send(:load, data)
          unless table.is_a?(Hash)
            raise Error, "PStore file seems to be corrupted."
          end
          checksum = Digest::SHA256.digest(data)
        end
        digest = digest(data)
        info = base_info(pstore.path)
        frames = JournalStore.new(journal_path(pstore.path)).read_frames
        apply_frames!(table, frames, base_digest: digest, base_info: info)
        [table, checksum, data.bytesize, frames, digest]
      rescue EOFError
        raise unless file.size == 0
        table = {}
        digest = digest("".b)
        info = base_info(pstore.path)
        frames = JournalStore.new(journal_path(pstore.path)).read_frames
        apply_frames!(table, frames, base_digest: digest, base_info: info)
        [table, pstore.send(:empty_marshal_checksum), 0, frames, digest]
      end

      # Apply frames in order.  If the base descriptor matches the base we
      # just loaded, frames at or below its version were already merged and
      # are skipped.  Otherwise the base is older than the descriptor (for
      # example an interrupted merge) and every frame is replayed.
      def apply_frames!(table, frames, base_digest: nil, base_info: nil)
        skip_through =
          if base_info && base_info[:digest] && base_digest &&
             base_info[:digest] == base_digest
            base_info[:version] || 0
          else
            0
          end

        frames.each do |frame|
          validate_frame!(frame)
          version = frame[:version] || 0
          next if version <= skip_through
          apply_frame!(table, frame)
        end
        table
      end

      def validate_frame!(frame)
        raise AmendmentError, "unsupported journal frame" unless frame.is_a?(Hash)
        raise AmendmentError, "unsupported journal format" unless frame[:format] == FORMAT
        raise AmendmentError, "missing frame token" if frame[:token].nil?
        frame
      end

      def apply_frame!(table, frame)
        case frame[:kind]
        when :put
          table[frame[:key]] = Marshal.load(frame[:value])
        when :delete
          table.delete(frame[:key])
        when :replace
          root = table.fetch(frame[:key]) do
            raise AmendmentError, "unknown root #{frame[:key].inspect}"
          end
          value = Marshal.load(frame[:value])
          if frame[:expected]
            actual = frame[:path].reduce(root) { |node, segment| Graph.child(node, segment) }
            unless Graph.fingerprint(actual) == frame[:expected]
              raise StaleAmendmentError, "stale replace frame for #{frame[:key].inspect}"
            end
          end
          table[frame[:key]] = Graph.replace(root, frame[:path], value)
        when :update
          root = table.fetch(frame[:key]) do
            raise AmendmentError, "unknown root #{frame[:key].inspect}"
          end
          target = frame[:path].reduce(root) { |node, segment| Graph.child(node, segment) }
          value = Marshal.load(frame[:value])
          if frame[:expected]
            unless Graph.fingerprint(target) == frame[:expected]
              raise StaleAmendmentError, "stale update frame for #{frame[:key].inspect}"
            end
          end
          replace_target!(table, target, value)
        else
          raise AmendmentError, "unknown frame kind #{frame[:kind].inspect}"
        end
        table
      end

      # Replace +target+ by identity with +value+ in every root and every
      # nested container.  A single memo preserves remaining sharing/cycles.
      def replace_target!(table, target, value)
        memo = {}
        key_changes = {}
        table.each_key do |key|
          next if key.equal?(target)
          fresh = Graph.replace_identity(key, target, value, memo)
          key_changes[key] = fresh unless fresh.equal?(key)
        end
        key_changes.each do |old, fresh|
          table[fresh] = table.delete(old)
        end
        table.each_pair do |key, root|
          table[key] = Graph.replace_identity(root, target, value, memo)
        end
        table
      end
    end

    # A stable, path-based reference to an object in the ledger graph.
    #
    # Handles identify an object by root key and path.  +fingerprint+ guards
    # against applying a correction to a node that has since changed.
    class Handle
      attr_reader :key, :path, :class_name, :fingerprint

      def initialize(key, path, object)
        @key = key
        @path = path.dup.freeze
        @class_name = object.class.name
        @fingerprint = Graph.fingerprint(object)
      end

      def resolve(table)
        node = table.fetch(@key) { raise AmendmentError, "unknown root #{@key.inspect}" }
        @path.each { |segment| node = Graph.child(node, segment) }
        node
      end

      def verify!(table)
        node = resolve(table)
        unless Graph.fingerprint(node) == @fingerprint
          raise StaleAmendmentError, "object at #{@key.inspect}#{@path.inspect} has changed"
        end
        node
      end

      def to_a
        [@key, @path, @fingerprint]
      end

      def inspect
        "#<#{self.class} #{@key.inspect}#{@path.inspect} #{@class_name}>"
      end
    end

    # A correction request.  Construct with Amendment.put/delete/replace/update
    # or pass the equivalent keywords to PStore#amend.
    class Amendment
      attr_reader :kind, :key, :path, :handle, :value, :expected, :token

      def initialize(kind, key: nil, path: nil, handle: nil, value: nil,
                     expected: nil, token: nil)
        @kind = kind
        @key = key
        @path = path && path.dup.freeze
        @handle = handle
        @value = value
        @expected = expected
        @token = token
      end

      def self.put(key, value, token: nil)
        new(:put, key: key, value: value, token: token)
      end

      def self.delete(key, token: nil)
        new(:delete, key: key, token: token)
      end

      def self.replace(key, path, value, token: nil, expected: nil)
        new(:replace, key: key, path: path, value: value,
            expected: expected, token: token)
      end

      def self.update(handle, value, token: nil)
        raise ArgumentError, "handle required" unless handle.is_a?(Handle)
        new(:update, handle: handle, value: value, token: token)
      end

      def inspect
        "#<#{self.class} #{@kind} key=#{@key.inspect} path=#{@path.inspect} handle=#{@handle.inspect}>"
      end
    end

    # Result of a submission.  +applied+ is false when an equivalent token was
    # already committed (safe retry after an interrupted submission).
    Receipt = Struct.new(:token, :version, :applied, :bytes_written,
                         :objects_written, :references_written, :kind,
                         keyword_init: true) do
      def applied?
        !!applied
      end
    end

    module Graph
      module_function

      def child(node, segment)
        case node
        when Array
          unless segment.is_a?(Integer) && segment >= 0 && segment < node.length
            raise AmendmentError, "array index out of range: #{segment.inspect}"
          end
          node[segment]
        when Hash
          raise AmendmentError, "missing hash key: #{segment.inspect}" unless node.key?(segment)
          node[segment]
        when Struct
          node[member_name(node, segment)]
        else
          if defined?(Data) && node.is_a?(Data)
            node.public_send(member_name(node, segment))
          else
            ivar = ivar_name(node, segment)
            raise AmendmentError, "missing member #{segment.inspect} on #{node.class}" unless ivar
            node.instance_variable_get(ivar)
          end
        end
      end

      def member_name(node, segment)
        members = node.members
        if segment.is_a?(Integer)
          members.fetch(segment)
        elsif members.include?(segment)
          segment
        elsif segment.is_a?(String) && members.include?(segment.to_sym)
          segment.to_sym
        else
          raise AmendmentError, "unknown member #{segment.inspect} on #{node.class}"
        end
      end

      def ivar_name(node, segment)
        candidates =
          if segment.is_a?(Symbol)
            segment.to_s.start_with?("@") ? [segment, :"@#{segment}"] : [:"@#{segment}"]
          elsif segment.is_a?(String)
            segment.start_with?("@") ? [segment.to_sym] : [:"@#{segment}"]
          else
            []
          end
        candidates.find { |name| node.instance_variable_defined?(name) }
      end

      # Copy-on-write replacement of one path.
      #
      # Only containers that lie on the path are copied.  All other references
      # are rewritten to point at the copied ancestors, which keeps cycles and
      # other sharing intact: a self-reference inside a copied container is
      # redirected to the copy rather than to the old object.
      def replace(root, path, value)
        memo = {}
        copy_path(root, path, value, memo)
      end

      def copy_path(node, path, value, memo)
        return value if path.empty?
        return memo[node.object_id] if memo.key?(node.object_id)

        segment = path.first
        rest = path.drop(1)
        copy = allocate_copy(node)
        memo[node.object_id] = copy

        case node
        when Array
          node.each { |item| copy << retarget(item, memo) }
          copy[segment] = copy_path(child(node, segment), rest, value, memo)
          copy
        when Hash
          node.each do |k, v|
            copy[retarget(k, memo)] = retarget(v, memo)
          end
          copy[segment] = copy_path(child(node, segment), rest, value, memo)
          copy
        when Struct
          node.each_pair do |member, member_value|
            copy[member] = retarget(member_value, memo)
          end
          member = member_name(node, segment)
          copy[member] = copy_path(child(node, segment), rest, value, memo)
          copy
        else
          if defined?(Data) && node.is_a?(Data)
            values = {}
            node.members.each do |member|
              values[member] = retarget(node.public_send(member), memo)
            end
            member = member_name(node, segment)
            values[member] = copy_path(child(node, segment), rest, value, memo)
            copy.send(:initialize, **values)
            copy
          else
            node.instance_variables.each do |name|
              copy.instance_variable_set(name, retarget(node.instance_variable_get(name), memo))
            end
            ivar = ivar_name(node, segment)
            raise AmendmentError, "missing member #{segment.inspect} on #{node.class}" unless ivar
            copy.instance_variable_set(ivar, copy_path(child(node, segment), rest, value, memo))
            copy
          end
        end
      end

      def allocate_copy(node)
        case node
        when Array
          node.class == Array ? [] : node.class.allocate
        when Hash
          node.class == Hash ? {} : node.class.allocate
        when Struct, Data
          node.class.allocate
        else
          shallow_dup(node)
        end
      end

      # If +object+ is one of the containers already copied on this path,
      # return its copy; otherwise return it unchanged.
      def retarget(object, memo)
        memo.fetch(object.object_id, object)
      end

      def shallow_dup(object)
        copy = object.dup
        object.instance_variables.each do |name|
          copy.instance_variable_set(name, object.instance_variable_get(name))
        end
        copy
      end

      # Replace +target+ by identity with +value+ everywhere below +root+.
      # One memo is used across all roots by the caller, which preserves
      # cross-root sharing and cycles in the resulting graph.
      def replace_identity(root, target, value, memo = {})
        return value if root.equal?(target)
        return memo[root.object_id] if memo.key?(root.object_id)

        case root
        when Array
          copy = root.class == Array ? [] : root.class.allocate
          memo[root.object_id] = copy
          root.each { |item| copy << replace_identity(item, target, value, memo) }
          copy
        when Hash
          copy = root.class == Hash ? {} : root.class.allocate
          memo[root.object_id] = copy
          root.each do |k, v|
            copy[replace_identity(k, target, value, memo)] =
              replace_identity(v, target, value, memo)
          end
          copy
        when Struct
          copy = root.dup
          memo[root.object_id] = copy
          patches = []
          root.each_pair do |member, member_value|
            fresh = replace_identity(member_value, target, value, memo)
            patches << [member, fresh] unless fresh.equal?(member_value)
          end
          patches.each { |member, fresh| copy[member] = fresh }
          copy
        else
          if defined?(Data) && root.is_a?(Data)
            values = {}
            patches = []
            root.members.each do |member|
              fresh = replace_identity(root.public_send(member), target, value, memo)
              values[member] = fresh
              patches << [member, fresh] unless fresh.equal?(root.public_send(member))
            end
            copy = root.with(**values)
            memo[root.object_id] = copy
            patches.each do |member, fresh|
              copy.instance_variable_set(:"@#{member}", fresh) unless copy.public_send(member).equal?(fresh)
            end
            copy
          else
            copy = shallow_dup(root)
            memo[root.object_id] = copy
            root.instance_variables.each do |name|
              copy.instance_variable_set(
                name, replace_identity(root.instance_variable_get(name), target, value, memo)
              )
            end
            copy
          end
        end
      end

      def fingerprint(object)
        Digest::SHA256.hexdigest(Marshal.dump(object))
      rescue TypeError
        "#{object.class.name}:#{object.inspect}"
      end

      def each_object(root)
        seen = {}
        queue = [root]
        until queue.empty?
          object = queue.pop
          next if seen[object.object_id]
          seen[object.object_id] = true
          yield object
          case object
          when Array
            queue.concat(object)
          when Hash
            object.each { |k, v| queue << k << v }
          when Struct
            object.each_pair { |_member, member_value| queue << member_value }
          else
            if defined?(Data) && object.is_a?(Data)
              object.members.each { |member| queue << object.public_send(member) }
            else
              object.instance_variables.each do |name|
                queue << object.instance_variable_get(name)
              end
            end
          end
        end
      end

      def object_count(root)
        count = 0
        each_object(root) { count += 1 }
        count
      end
    end

    module Journal
      module_function

      def encode(frame)
        body = Marshal.dump(frame)
        checksum = Digest::SHA256.digest(body).byteslice(0, CHECKSUM_BYTES)
        [body.bytesize].pack("N") + body + checksum
      end

      # Return [frames, valid_offset].  A trailing partial frame (or any frame
      # with a bad checksum) is not returned; valid_offset points just past
      # the last trustworthy frame.
      def scan(data)
        return [[], 0] if data.nil? || data.empty?
        return [[], 0] unless data.start_with?(MAGIC)

        offset = MAGIC.bytesize
        frames = []
        while offset + 4 <= data.bytesize
          length = data.byteslice(offset, 4).unpack1("N")
          body_start = offset + 4
          body_end = body_start + length
          checksum_end = body_end + CHECKSUM_BYTES
          break if checksum_end > data.bytesize

          body = data.byteslice(body_start, length)
          checksum = data.byteslice(body_end, CHECKSUM_BYTES)
          break unless checksum == Digest::SHA256.digest(body).byteslice(0, CHECKSUM_BYTES)

          frames << Marshal.load(body)
          offset = checksum_end
        end
        [frames, offset]
      end
    end

    # Journal file wrapper.  All operations use flock on the journal itself.
    class JournalStore
      attr_reader :path

      def initialize(path)
        @path = path
      end

      def exist?
        File.exist?(@path)
      end

      def read_frames
        return [] unless exist?

        File.open(@path, "rb") do |io|
          io.flock(File::LOCK_SH)
          data = io.read || "".b
          Journal.scan(data).first
        end
      end

      # Append one frame, repairing a torn tail first.  Returns bytes written.
      def append(frame)
        record = Journal.encode(frame)
        File.open(@path, File::RDWR | File::CREAT | File::BINARY, 0o600,
                  encoding: Encoding::ASCII_8BIT) do |io|
          io.flock(File::LOCK_EX)
          data = io.read || "".b
          _frames, valid = Journal.scan(data)
          if data.bytesize < MAGIC.bytesize
            io.truncate(0)
            io.rewind
            io.write(MAGIC)
          elsif valid < data.bytesize
            # Drop only the uncommitted torn tail; keep every valid frame.
            io.truncate(valid)
          end
          io.seek(0, IO::SEEK_END)
          io.write(record)
          io.flush
          begin
            io.fsync
          rescue StandardError
            nil
          end
          record.bytesize
        end
      end

      def clear!
        File.unlink(@path) if exist?
        true
      rescue Errno::ENOENT
        true
      end
    end

    # Ledger facade bound to one PStore instance.
    class Ledger
      UNSET = Object.new.freeze

      attr_reader :pstore, :journal_store

      def initialize(pstore)
        @pstore = pstore
        @journal_store = JournalStore.new(Incremental.journal_path(pstore.path))
        @mutex = Mutex.new
      end

      def path
        @pstore.path
      end

      def journal_path
        @journal_store.path
      end

      def snapshot_path
        Incremental.snapshot_path(@pstore.path)
      end

      def version
        @journal_store.read_frames.length
      end

      # Load the complete graph (base snapshot plus every committed frame).
      def table
        @mutex.synchronize { read_table }
      end
      alias graph table

      # Return a handle to root[key] at +path+.
      def handle(key, path = [])
        tbl = table
        root = tbl.fetch(key) { raise AmendmentError, "unknown root #{key.inspect}" }
        node = path.reduce(root) { |current, segment| Graph.child(current, segment) }
        Handle.new(key, path, node)
      end
      alias handle_for handle

      # Replace the whole root +key+.
      def put(key, value, token: nil)
        submit(Amendment.put(key, value, token: token))
      end

      # Delete a root.
      def delete(key, token: nil)
        submit(Amendment.delete(key, token: token))
      end

      # Replace one reference below +key+.
      def replace(key, path, value, token: nil, expected: nil)
        submit(Amendment.replace(key, path, value, token: token, expected: expected))
      end

      # Update a shared object identified by +handle+.
      def update(handle, value, token: nil)
        submit(Amendment.update(handle, value, token: token))
      end

      # Submit an Amendment, Handle/value pair, hash request or positional key
      # plus value.  See the accept_* helpers for the accepted shapes.
      def amend(*args, token: nil, **options)
        amendment = normalize(args, options, token)
        submit(amendment)
      end
      alias correct amend

      # Compact all committed frames into the ordinary PStore file.  This is
      # the explicit merge operation: it may read and rewrite the whole file
      # once.  Afterwards legacy PStore readers see the merged graph directly.
      def compact!
        @mutex.synchronize do
          file = @pstore.send(:open_and_lock_file, @pstore.path, false)
          raise AmendmentError, "store disappeared" if file.nil?
          begin
            table, = Incremental.read_state(@pstore, file, false)
            Incremental.merge!(@pstore, file, table)
          ensure
            file.close
          end
          true
        end
      end
      alias compact compact!
      alias merge! compact!
      alias merge compact!

      # Write a standalone ordinary PStore snapshot containing the complete
      # graph.  Legacy readers can open it normally.
      def deliver(destination = snapshot_path)
        tbl = table
        destination = File.path(destination)
        temp = "#{destination}.tmp.#{Process.pid}.#{rand 1_000_000}"
        clean_sidecars(destination)
        begin
          writer = PStore.new(temp)
          writer.transaction do |out|
            tbl.each { |key, value| out[key] = value }
          end
          File.rename(temp, destination)
        ensure
          File.unlink(temp) if File.exist?(temp)
          clean_sidecars(destination)
        end
        destination
      end
      alias snapshot deliver
      alias deliver_snapshot deliver

      private

      def clean_sidecars(destination)
        [Incremental.journal_path(destination),
         Incremental.base_path(destination)].each do |path|
          File.unlink(path) if File.exist?(path)
        rescue Errno::ENOENT
          nil
        end
      end

      def normalize(args, options, token)
        if args.length == 1 && args[0].is_a?(Amendment)
          return args[0] if options.empty?
          raise ArgumentError, "unexpected options for Amendment"
        end

        if args.length == 1 && args[0].is_a?(Handle)
          raise ArgumentError, "value required for handle" unless options.key?(:value)
          return Amendment.update(args[0], options[:value], token: token)
        end

        if args.length == 2 && args[0].is_a?(Handle)
          return Amendment.update(args[0], args[1], token: token)
        end

        if args.length == 1 && args[0].is_a?(Hash)
          return request_from_hash(args[0].merge(options), token)
        end

        if args.empty?
          return request_from_hash(options, token) if options.key?(:kind) || options.key?(:op)
          if options.key?(:handle)
            return Amendment.update(options[:handle], options.fetch(:value), token: token)
          end
          if options.key?(:key)
            return request_from_hash(options, token)
          end
          raise ArgumentError, "missing amendment request"
        end

        key = args[0]
        if args.length == 2
          if options.key?(:shared) && options[:shared]
            handle = handle(key, [])
            return Amendment.update(handle, args[1], token: token)
          end
          return Amendment.put(key, args[1], token: token)
        end

        if args.length == 3
          if options.key?(:shared) && options[:shared]
            handle = handle(key, args[1])
            return Amendment.update(handle, args[2], token: token)
          end
          return Amendment.replace(key, args[1], args[2], token: token,
                                   expected: options[:expected])
        end

        raise ArgumentError, "unsupported amendment arguments"
      end

      def request_from_hash(hash, token)
        token ||= hash[:token]
        kind = (hash[:kind] || hash[:op] || hash[:action])
        if kind.nil?
          if hash.key?(:handle) || hash[:shared]
            kind = :update
          elsif hash.key?(:path)
            kind = :replace
          else
            kind = :put
          end
        end

        case kind.to_sym
        when :put, :set, :upsert
          Amendment.put(hash.fetch(:key), hash.fetch(:value), token: token)
        when :delete, :remove
          Amendment.delete(hash.fetch(:key), token: token)
        when :replace, :patch
          Amendment.replace(hash.fetch(:key), hash.fetch(:path, []), hash.fetch(:value),
                            token: token, expected: hash[:expected])
        when :update, :shared, :amend
          handle = hash[:handle]
          unless handle.is_a?(Handle)
            handle = Handle.new(hash.fetch(:key), hash.fetch(:path, []), handle)
          end
          Amendment.update(handle, hash.fetch(:value), token: token)
        else
          raise AmendmentError, "unknown amendment kind #{kind.inspect}"
        end
      end

      def read_table
        file = @pstore.send(:open_and_lock_file, @pstore.path, true)
        if file
          begin
            table, = Incremental.read_state(@pstore, file, true)
          ensure
            file.close
          end
        else
          table = {}
          frames = @journal_store.read_frames
          Incremental.apply_frames!(table, frames)
        end
        table
      end

      def submit(amendment)
        @mutex.synchronize do
          Incremental.assert_trusted!(amendment.value) unless amendment.kind == :delete
          file = @pstore.send(:open_and_lock_file, @pstore.path, false)
          raise AmendmentError, "store disappeared" if file.nil?
          begin
            table, _checksum, _size, frames, _digest =
              Incremental.read_state(@pstore, file, false)

            request = request_fingerprint(amendment)
            if (existing = find_existing(frames, amendment, request))
              return already_applied(existing)
            end
            if (existing = find_reapplied(frames, amendment))
              return already_applied(existing)
            end

            frame = build_frame(table, amendment, next_version(frames))
            bytes = @journal_store.append(frame)
            Receipt.new(token: frame[:token], version: frame[:version],
                        applied: true, bytes_written: bytes,
                        objects_written: frame[:objects],
                        references_written: frame[:references],
                        kind: frame[:kind])
          ensure
            file.close
          end
        end
      end

      # A retry made with a freshly resolved handle carries the new value's
      # fingerprint rather than the pre-change fingerprint.  If the most
      # recent frame for the same target already wrote exactly this value,
      # treat the submission as an idempotent retry.
      def find_reapplied(frames, amendment)
        return nil unless [:update, :replace].include?(amendment.kind)

        key, path, value =
          case amendment.kind
          when :update
            [amendment.handle.key, amendment.handle.path, amendment.value]
          when :replace
            [amendment.key, amendment.path || [], amendment.value]
          end
        candidate = Marshal.dump(value)
        last = frames.reverse.find do |frame|
          frame[:key] == key && (frame[:path] || []) == path
        end
        return nil unless last
        return nil unless last[:kind] == amendment.kind && last[:value] == candidate
        last
      end

      def already_applied(frame)
        Receipt.new(token: frame[:token], version: frame[:version],
                    applied: false, bytes_written: 0,
                    objects_written: 0, references_written: 0,
                    kind: frame[:kind])
      end

      def next_version(frames)
        base = Incremental.base_info(@pstore.path)[:version].to_i
        highest = frames.map { |frame| frame[:version].to_i }.max.to_i
        [base, highest].max + 1
      end

      # Content-derived identity of an amendment, computed without looking at
      # the current graph.  This lets an identical retry be recognized before
      # the handle/expected fingerprint check (which would fail once the
      # first submission already changed the target).
      def request_fingerprint(amendment)
        parts = [amendment.kind]
        case amendment.kind
        when :put
          parts << amendment.key << Marshal.dump(amendment.value)
        when :delete
          parts << amendment.key
        when :replace
          parts << amendment.key << (amendment.path || []) << Marshal.dump(amendment.value)
        when :update
          handle = amendment.handle
          parts << handle.key << handle.path << handle.fingerprint << Marshal.dump(amendment.value)
        end
        Digest::SHA256.hexdigest(Marshal.dump(parts))
      end

      def find_existing(frames, amendment, request)
        if amendment.token
          existing = frames.find { |item| item[:token] == amendment.token }
          if existing && existing[:request] != request
            raise AmendmentError, "token was already used for a different amendment"
          end
          return existing
        end
        frames.find { |item| item[:request] == request }
      end

      def build_frame(table, amendment, version)
        case amendment.kind
        when :put
          key = amendment.key
          raise AmendmentError, "missing root key" if key.nil?
          value = amendment.value
          value_dump = Marshal.dump(value)
          {
            format: FORMAT,
            version: version,
            kind: :put,
            key: key,
            value: value_dump,
            token: frame_token(amendment),
            request: request_fingerprint(amendment),
            expected: nil,
            objects: Graph.object_count(value),
            references: 1
          }
        when :delete
          key = amendment.key
          raise AmendmentError, "missing root key" if key.nil?
          {
            format: FORMAT,
            version: version,
            kind: :delete,
            key: key,
            token: frame_token(amendment),
            request: request_fingerprint(amendment),
            expected: nil,
            objects: 0,
            references: 1
          }
        when :replace
          key = amendment.key
          raise AmendmentError, "missing root key" if key.nil?
          path = (amendment.path || []).dup
          root = table.fetch(key) { raise AmendmentError, "unknown root #{key.inspect}" }
          if amendment.expected
            actual = amendment.expected.resolve(table)
            unless Graph.fingerprint(actual) == amendment.expected.fingerprint
              raise StaleAmendmentError, "reference at #{key.inspect}#{path.inspect} has changed"
            end
          end
          path.reduce(root) { |node, segment| Graph.child(node, segment) }
          value = amendment.value
          value_dump = Marshal.dump(value)
          {
            format: FORMAT,
            version: version,
            kind: :replace,
            key: key,
            path: path,
            value: value_dump,
            token: frame_token(amendment),
            request: request_fingerprint(amendment),
            expected: amendment.expected && amendment.expected.fingerprint,
            objects: Graph.object_count(value),
            references: path.length + 1
          }
        when :update
          handle = amendment.handle
          raise AmendmentError, "missing handle" unless handle.is_a?(Handle)
          handle.verify!(table)
          value = amendment.value
          value_dump = Marshal.dump(value)
          {
            format: FORMAT,
            version: version,
            kind: :update,
            key: handle.key,
            path: handle.path,
            value: value_dump,
            token: frame_token(amendment),
            request: request_fingerprint(amendment),
            expected: handle.fingerprint,
            objects: Graph.object_count(value),
            references: handle.path.length + 1
          }
        else
          raise AmendmentError, "unknown amendment kind #{amendment.kind.inspect}"
        end
      end

      # Idempotency token.  An explicit token is returned verbatim; otherwise
      # the request fingerprint doubles as the token.
      def frame_token(amendment)
        amendment.token || request_fingerprint(amendment)
      end

    end
  end

  # True when committed incremental amendments exist beside this store.
  def journal?
    File.exist?(Incremental.journal_path(@filename)) &&
      Incremental::JournalStore.new(Incremental.journal_path(@filename)).read_frames.any?
  end

  # Returns the incremental ledger facade for this store.
  def ledger
    @ledger ||= Incremental::Ledger.new(self)
  end
  alias incremental ledger

  # Convenience wrappers around the ledger.
  def handle(key, path = [])
    ledger.handle(key, path)
  end
  alias handle_for handle

  def amend(*args, token: nil, **options)
    ledger.amend(*args, token: token, **options)
  end
  alias submit_amendment amend
  alias correct amend
  alias amend_key amend

  def amend_put(key, value, token: nil)
    ledger.put(key, value, token: token)
  end
  alias put_amendment amend_put

  def amend_delete(key, token: nil)
    ledger.delete(key, token: token)
  end
  alias delete_amendment amend_delete

  def amend_replace(key, path, value, token: nil, expected: nil)
    ledger.replace(key, path, value, token: token, expected: expected)
  end
  alias replace_amendment amend_replace

  def amend_shared(handle, value, token: nil)
    ledger.update(handle, value, token: token)
  end
  alias update_shared amend_shared

  # Explicitly merge the journal into the ordinary PStore file.
  def compact!
    ledger.compact!
  end
  alias compact compact!
  alias merge! compact!
  alias merge compact!

  # Write a standalone ordinary PStore snapshot of the merged graph.
  def deliver(destination = Incremental.snapshot_path(path))
    ledger.deliver(destination)
  end
  alias snapshot deliver
  alias deliver_snapshot deliver

  def ledger_version
    ledger.version
  end
end

PStore::Amendment = PStore::Incremental::Amendment unless PStore.const_defined?(:Amendment, false)
PStore::Handle = PStore::Incremental::Handle unless PStore.const_defined?(:Handle, false)
PStore::Ledger = PStore::Incremental::Ledger unless PStore.const_defined?(:Ledger, false)
