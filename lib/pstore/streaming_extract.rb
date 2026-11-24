# frozen_string_literal: true

require "fileutils"

module PStoreExtract
  # Streaming subset extraction for Marshal based PStore files.
  #
  # The source ledger is parsed positionally with a small read buffer, so the
  # structure of the whole file can be examined without loading payload bodies.
  # Frames copied verbatim (strings, symbols, numbers) are streamed with
  # IO.copy_stream, so delivering a message of N bytes never requires a Ruby
  # string of N bytes to be held in memory.
  #
  # Understood value types are the ones used by ordinary PStore ledgers:
  # integers, true/false/nil, symbols, strings, arrays, plain hashes (with an
  # optional default value) and instance variable wrappers carrying encoding
  # information.
  class StreamingExtract
    class ParseError < StandardError
      attr_reader :position

      def initialize(message, position)
        @position = position
        super("#{message} (byte #{position})")
      end
    end

    # One parsed Marshal frame. Payload bodies are referenced by file
    # position, never stored on the node.
    class Node
      attr_accessor :id, :kind, :pos, :end_pos, :obj_slot, :sym_slot,
                    :children, :target, :body_pos, :body_len

      def initialize(kind, pos, **options)
        @kind = kind
        @pos = pos
        @end_pos = pos
        @children = []
        options.each { |name, value| public_send("#{name}=", value) }
      end
    end

    # One entry that could not be delivered.
    Fault = Struct.new(:type, :message, :position, :keys, keyword_init: true)

    Result = Struct.new(:output_path, :delivered_keys, :faults, keyword_init: true)

    MARKER_VERSION = "\x04\x08".b
    M_ARRAY = 0x5B
    M_HASH = 0x7B
    M_HASH_DEFAULT = 0x7D
    M_STRING = 0x22
    M_IVAR = 0x49
    M_SYMBOL = 0x3A
    M_SYMLINK = 0x3B
    M_LINK = 0x40
    M_FIXNUM = 0x69
    M_BIGNUM = 0x6C
    M_TRUE = 0x54
    M_FALSE = 0x46
    M_NIL = 0x30

    MAX_DEPTH = 1_000
    DEFAULT_CHUNK_SIZE = 1 << 16

    # Buffered, position aware reader. Payload bodies are skipped by moving
    # the position instead of being read into memory.
    class Reader
      attr_reader :pos

      def initialize(io, chunk_size = DEFAULT_CHUNK_SIZE)
        @io = io
        @chunk_size = chunk_size
        @buffer = "".b
        @buffer_start = 0
        @buffer_end = 0
        @pos = 0
      end

      def getbyte
        fill if @pos >= @buffer_end
        byte = @buffer.getbyte(@pos - @buffer_start)
        raise ParseError.new("unexpected end of stream", @pos) if byte.nil?
        @pos += 1
        byte
      end

      def read(bytes)
        data = "".b
        while data.bytesize < bytes
          fill if @pos >= @buffer_end
          offset = @pos - @buffer_start
          piece = @buffer.byteslice(offset, bytes - data.bytesize)
          data << piece
          @pos += piece.bytesize
        end
        data
      end

      def skip(bytes)
        @pos += bytes
        return if @pos < @buffer_end
        @io.seek(@pos)
        @buffer = "".b
        @buffer_start = @pos
        @buffer_end = @pos
      end

      private

      def fill
        @io.seek(@pos)
        @buffer = @io.read(@chunk_size) || "".b
        @buffer.force_encoding(Encoding::ASCII_8BIT)
        @buffer_start = @pos
        @buffer_end = @pos + @buffer.bytesize
        raise ParseError.new("unexpected end of stream", @pos) if @buffer.empty?
      end
    end

    def self.extract(source_path:, dest_path:, keys:, report: nil, chunk_size: DEFAULT_CHUNK_SIZE)
      worker = new(source_path, chunk_size)
      worker.run(dest_path, keys, report)
      worker.result
    end

    attr_reader :result

    def initialize(source_path, chunk_size = DEFAULT_CHUNK_SIZE)
      @source_path = source_path
      @chunk_size = chunk_size
      @nodes = []
      @obj_slot_owner = {}
      @sym_slot_owner = {}
      @next_obj_slot = 0
      @next_sym_slot = 0
      @next_node_id = 0
      @depth = 0
      @faults = []
      @symbol_names = {}
    end

    # Parse metadata, select entries, verify reachability and stream the new
    # ledger. The whole pipeline keeps payload bodies out of memory.
    def run(dest_path, wanted_keys, report)
      if File.size(@source_path).zero?
        # An empty PStore file is the same freshly created store as an empty
        # root Hash; every requested key is necessarily absent.
        wanted_keys.each do |key|
          @faults << Fault.new(
            type: :missing_key,
            message: "requested root key #{key.inspect} is not present in the source ledger",
            position: nil,
            keys: [key.inspect]
          )
        end
        FileUtils.mkdir_p(File.dirname(File.expand_path(dest_path)))
        File.binwrite(dest_path, MARKER_VERSION + "{\x00".b)
        @result = Result.new(output_path: dest_path, delivered_keys: [], faults: @faults)
        print_report(report) if report
        return @result
      end

      File.open(@source_path, "rb") do |source|
        @source = source
        @reader = Reader.new(source, @chunk_size)
        entries, root_info = parse_ledger
        parse_footer
        wanted = match_entries(entries, wanted_keys).sort_by { |entry| entry[:index] }.uniq { |e| e[:index] }
        retained, excluded_indexes = mark_retained(wanted, root_info)
        deliverable = wanted.reject { |entry| excluded_indexes.include?(entry[:index]) }
        emit_ledger(dest_path, root_info, deliverable)
        @result = Result.new(
          output_path: dest_path,
          delivered_keys: deliverable.map { |entry| entry[:label] },
          faults: @faults
        )
      end
      print_report(report) if report
      @result
    end

    private

    # ---- Parsing ---------------------------------------------------------

    def parse_ledger
      header = @reader.read(2)
      raise ParseError.new("not a Marshal stream", 0) unless header == MARKER_VERSION

      root_info = { truncated_at: nil }
      marker = @reader.getbyte
      ivar_node = nil
      if marker == M_IVAR
        ivar_node = register(Node.new(:ivar, @reader.pos - 1, obj_slot: reserve_obj_slot))
        marker = @reader.getbyte
      end
      unless [M_HASH, M_HASH_DEFAULT].include?(marker)
        raise ParseError.new("top level object is not a Hash", @reader.pos - 1)
      end
      has_default = marker == M_HASH_DEFAULT
      pair_count = read_count
      hash_node =
        if ivar_node
          register(Node.new(has_default ? :hash_default : :hash, @reader.pos - 1))
        else
          register(Node.new(has_default ? :hash_default : :hash, @reader.pos - 1,
                            obj_slot: reserve_obj_slot))
        end
      ivar_node.children << hash_node.id if ivar_node
      root_info[:hash_node] = hash_node
      root_info[:pair_count] = pair_count
      root_info[:default] = has_default
      root_info[:ivar_node] = ivar_node

      entries = parse_entries(root_info, hash_node)
      parse_root_tail(root_info, hash_node, ivar_node)
      [entries, root_info]
    end

    # Parse root pairs one at a time so a damaged entry does not erase the
    # positions confirmed for the entries before it.
    def parse_entries(root_info, hash_node)
      entries = []
      root_info[:pair_count].times do |index|
        start_pos = @reader.pos
        begin
          key_id = parse_value
          value_id = parse_value
        rescue ParseError => error
          remaining = root_info[:pair_count] - index
          @faults << Fault.new(
            type: :broken_structure,
            message: "structure damaged at byte #{error.position}; #{remaining} " \
                     "root entr#{remaining == 1 ? 'y' : 'ies'} from index #{index} " \
                     "could not be position-confirmed and are held back",
            position: error.position,
            keys: []
          )
          root_info[:truncated_at] = index
          return entries
        end
        hash_node.children << key_id << value_id
        entries << {
          index: index,
          key_id: key_id,
          value_id: value_id,
          start_pos: start_pos,
          end_pos: @reader.pos,
          label: describe_key(@nodes[key_id])
        }
      end
      entries
    end

    def parse_root_tail(root_info, hash_node, ivar_node)
      return if root_info[:truncated_at]
      begin
        hash_node.children << parse_value if root_info[:default]
        if ivar_node
          pair_count = read_count
          pair_count.times do
            ivar_node.children << parse_value
            ivar_node.children << parse_value
          end
        end
      rescue ParseError => error
        @faults << Fault.new(
          type: :broken_structure,
          message: "root frame tail damaged at byte #{error.position}",
          position: error.position,
          keys: []
        )
        root_info[:truncated_at] = root_info[:pair_count]
      end
    end

    def parse_footer
      return unless @reader.pos < @source.size
      @faults << Fault.new(
        type: :trailing_bytes,
        message: "trailing bytes after root object; data past byte #{@reader.pos} was not used",
        position: @reader.pos,
        keys: []
      )
    end

    def parse_value(registered: true)
      @depth += 1
      if @depth > MAX_DEPTH
        raise ParseError.new("nesting deeper than #{MAX_DEPTH} frames", @reader.pos)
      end
      pos = @reader.pos
      marker = @reader.getbyte
      node =
        case marker
        when M_ARRAY then parse_array(pos, registered)
        when M_HASH, M_HASH_DEFAULT then parse_hash(pos, marker == M_HASH_DEFAULT, registered)
        when M_STRING then parse_string(pos, registered)
        when M_IVAR then parse_ivar(pos)
        when M_SYMBOL then parse_symbol(pos)
        when M_SYMLINK then parse_symlink(pos)
        when M_LINK then parse_link(pos)
        when M_FIXNUM then parse_fixnum(pos)
        when M_BIGNUM then parse_bignum(pos, registered)
        when M_TRUE then leaf(:true, pos)
        when M_FALSE then leaf(:false, pos)
        when M_NIL then leaf(:nil, pos)
        else raise ParseError.new("unsupported Marshal marker 0x#{marker.to_s(16).upcase}", pos)
        end
      @depth -= 1
      node.id
    end

    def parse_array(pos, registered)
      slot = registered ? reserve_obj_slot : nil
      count = read_count
      node = register(Node.new(:array, pos, obj_slot: slot))
      count.times { node.children << parse_value }
      node.end_pos = @reader.pos
      node
    end

    def parse_hash(pos, has_default, registered)
      slot = registered ? reserve_obj_slot : nil
      pair_count = read_count
      node = register(Node.new(has_default ? :hash_default : :hash, pos, obj_slot: slot))
      pair_count.times do
        node.children << parse_value
        node.children << parse_value
      end
      node.children << parse_value if has_default
      node.end_pos = @reader.pos
      node
    end

    def parse_string(pos, registered)
      slot = registered ? reserve_obj_slot : nil
      length = read_count
      body_pos = @reader.pos
      @reader.skip(length)
      node = register(Node.new(:string, pos, obj_slot: slot,
                               body_pos: body_pos, body_len: length))
      node.end_pos = @reader.pos
      node
    end

    # The I wrapper owns the object slot; the wrapped frame is dumped without
    # registering its own slot.
    def parse_ivar(pos)
      node = register(Node.new(:ivar, pos, obj_slot: reserve_obj_slot))
      node.children << parse_value(registered: false)
      pair_count = read_count
      pair_count.times do
        node.children << parse_value
        node.children << parse_value
      end
      node.end_pos = @reader.pos
      node
    end

    def parse_symbol(pos)
      slot = @next_sym_slot
      @next_sym_slot += 1
      length = read_count
      body_pos = @reader.pos
      @reader.skip(length)
      node = register(Node.new(:symbol, pos, sym_slot: slot,
                               body_pos: body_pos, body_len: length))
      node.end_pos = @reader.pos
      @sym_slot_owner[slot] = node.id
      node
    end

    def parse_symlink(pos)
      target = read_index
      node = register(Node.new(:symlink, pos, target: target))
      node.end_pos = @reader.pos
      node
    end

    def parse_link(pos)
      target = read_index
      node = register(Node.new(:link, pos, target: target))
      node.end_pos = @reader.pos
      node
    end

    def parse_fixnum(pos)
      first = @reader.getbyte
      if first.between?(1, 4)
        @reader.skip(first)
      elsif first.between?(0xFC, 0xFF)
        @reader.skip(0x100 - first)
      end
      node = register(Node.new(:fixnum, pos))
      node.end_pos = @reader.pos
      node
    end

    def parse_bignum(pos, registered)
      slot = registered ? reserve_obj_slot : nil
      @reader.getbyte # sign '+' / '-'
      words = read_count
      @reader.skip(words * 2)
      node = register(Node.new(:bignum, pos, obj_slot: slot))
      node.end_pos = @reader.pos
      node
    end

    def register(node)
      node.id = @next_node_id
      @next_node_id += 1
      @nodes << node
      @obj_slot_owner[node.obj_slot] = node.id if node.obj_slot
      node
    end

    def leaf(kind, pos)
      node = register(Node.new(kind, pos))
      node.end_pos = @reader.pos
      node
    end

    def reserve_obj_slot
      slot = @next_obj_slot
      @next_obj_slot += 1
      slot
    end

    # Count/index encoding of this Marshal generation:
    # 0 -> byte 0; 1..122 -> one byte value+5; >=123 -> a 1..4 byte length
    # marker followed by the little endian value.
    def read_count
      decode_index_or_count
    end

    def read_index
      decode_index_or_count
    end

    def decode_index_or_count
      first = @reader.getbyte
      return 0 if first.zero?
      # 1..4 is the little endian length marker, 5..127 is the one byte form
      # (stored value + 5).
      return first - 5 if first > 4
      value = 0
      first.times { |shift| value |= @reader.getbyte << (shift * 8) }
      value
    end

    # ---- Selection -------------------------------------------------------

    def match_entries(entries, wanted_keys)
      wanted_keys.filter_map do |wanted|
        match = entries.find { |entry| key_eql?(@nodes[entry[:key_id]], wanted) }
        unless match
          @faults << Fault.new(
            type: :missing_key,
            message: "requested root key #{wanted.inspect} is not present in the source ledger",
            position: nil,
            keys: [wanted.inspect]
          )
        end
        match
      end
    end

    def key_eql?(node, wanted)
      case wanted
      when Symbol
        symbol_key?(node) && symbol_name(resolve_symbol(node)) == wanted.name
      when String
        string_key?(node) &&
          read_body(resolve_string(node)).bytes == wanted.bytes &&
          encoding_of(node) == wanted.encoding
      else
        raise ArgumentError, "root keys must be Symbols or Strings, got #{wanted.class}"
      end
    end

    def symbol_key?(node)
      node.kind == :symbol || node.kind == :symlink
    end

    def string_key?(node)
      node.kind == :string ||
        (node.kind == :ivar && @nodes[node.children.first].kind == :string)
    end

    def resolve_symbol(node)
      node.kind == :symlink ? @nodes[@sym_slot_owner[node.target]] : node
    end

    def resolve_string(node)
      node.kind == :ivar ? @nodes[node.children.first] : node
    end

    def describe_key(node)
      case node.kind
      when :symbol, :symlink then ":#{symbol_name(resolve_symbol(node))}"
      when :string, :ivar then string_inspect(node)
      else "key@#{node.pos}"
      end
    end

    def string_inspect(node)
      string_node = resolve_string(node)
      read_body(string_node).dup.force_encoding(encoding_of(node)).inspect
    end

    def symbol_name(symbol_node)
      @symbol_names[symbol_node.id] ||=
        read_body(symbol_node).force_encoding(Encoding::UTF_8)
    end

    def read_body(node)
      @source.seek(node.body_pos)
      data = @source.read(node.body_len) || "".b
      data.force_encoding(Encoding::ASCII_8BIT)
    end

    def encoding_of(string_or_ivar_node)
      node = string_or_ivar_node
      return Encoding::ASCII_8BIT unless node.kind == :ivar
      ivar_pairs(node).each do |name_id, value_id|
        next unless ivar_symbol_name(@nodes[name_id]) == "E"
        return @nodes[value_id].kind == :true ? Encoding::UTF_8 : Encoding::US_ASCII
      end
      ivar_pairs(node).each do |name_id, value_id|
        next unless ivar_symbol_name(@nodes[name_id]) == "encoding"
        value_id = @nodes[value_id].children.first if @nodes[value_id].kind == :ivar
        name_bytes = read_body(@nodes[value_id]).force_encoding(Encoding::UTF_8)
        return Encoding.find(name_bytes)
      end
      Encoding::ASCII_8BIT
    end

    def ivar_pairs(ivar_node)
      ivar_node.children[1..].each_slice(2)
    end

    def ivar_symbol_name(node)
      node = @nodes[@sym_slot_owner[node.target]] if node.kind == :symlink
      symbol_name(node)
    end

    # ---- Reachability ----------------------------------------------------

    def mark_retained(wanted, root_info)
      retained = {}
      queue = []
      retain = lambda do |id|
        unless retained[id]
          retained[id] = true
          queue << id
        end
      end
      retain.call(0) # root ivar frame (if any) is always emitted
      retain.call(root_info[:hash_node].id)
      wanted.each do |entry|
        retain.call(entry[:key_id])
        retain.call(entry[:value_id])
      end
      while (id = queue.shift)
        node = @nodes[id]
        if id == root_info[:hash_node].id
          # The root hash is rebuilt with only the delivered entries: traverse
          # its default value, but never the key/value children of entries
          # that are not delivered.
          retain.call(node.children.last) if node.kind == :hash_default
        else
          node.children.each { |child_id| retain.call(child_id) }
        end
        # Links keep an object or symbol alive even when its defining frame
        # lives inside an entry that is not delivered.  The definition is
        # expanded lazily at the link position during emission.
        case node.kind
        when :link
          next if node.target.zero? # slot 0 is the ledger root, always remapped
          owner_id = @obj_slot_owner[node.target]
          retain.call(owner_id) if owner_id
        when :symlink
          owner_id = @sym_slot_owner[node.target]
          retain.call(owner_id) if owner_id
        end
      end

      [retained, dangling_indexes(retained, wanted)]
    end

    # A link whose target slot has no readable defining frame means the bytes
    # are damaged; the affected approved entries are held back and reported
    # with their file positions.
    def dangling_indexes(retained, wanted)
      broken_links = retained.each_key.filter_map do |id|
        node = @nodes[id]
        case node.kind
        when :link
          next if node.target.zero?
          owner_id = @obj_slot_owner[node.target]
          node if owner_id.nil? || !retained[owner_id]
        when :symlink
          owner_id = @sym_slot_owner[node.target]
          node if owner_id.nil? || !retained[owner_id]
        end
      end
      return [] if broken_links.empty?

      affected = {}
      labels = []
      wanted.each do |entry|
        touched = broken_links.any? do |link_node|
          subgraph_contains?(entry[:value_id], link_node.id, retained)
        end
        if touched
          affected[entry[:index]] = true
          labels << entry[:label]
        end
      end
      broken_links.each do |link_node|
        kind = link_node.kind == :link ? "object" : "symbol"
        @faults << Fault.new(
          type: :dangling_reference,
          message: "#{kind} link at byte #{link_node.pos} references slot " \
                   "#{link_node.target} whose definition is unreadable or outside " \
                   "the approved entries",
          position: link_node.pos,
          keys: labels
        )
      end
      affected.keys
    end

    def subgraph_contains?(start_id, target_id, retained)
      seen = {}
      queue = [start_id]
      while (id = queue.shift)
        next if seen[id]
        seen[id] = true
        return true if id == target_id
        @nodes[id].children.each { |child_id| queue << child_id if retained[child_id] }
      end
      false
    end

    # ---- Emission --------------------------------------------------------

    def emit_ledger(dest_path, root_info, deliverable)
      FileUtils.mkdir_p(File.dirname(File.expand_path(dest_path)))
      @out_obj_slots = {}
      @next_out_obj_slot = 0
      @out_sym_slots = {}
      @next_out_sym_slot = 0
      File.open(dest_path, "wb") do |dest|
        @dest = dest
        @write_buffer = "".b
        dest.write(MARKER_VERSION)
        # The root is the first registered object in every dump and keeps
        # output slot 0, so every "@ 00" back reference stays valid.
        root_slot = root_info[:ivar_node]&.obj_slot || root_info[:hash_node].obj_slot
        @out_obj_slots[root_slot] = 0
        @next_out_obj_slot = 1
        emit_root(root_info, deliverable)
        flush_write_buffer
      end
    end

    def emit_root(root_info, deliverable)
      ivar_node = root_info[:ivar_node]
      keep_ivar = ivar_node && root_info[:truncated_at].nil?
      write_byte(M_IVAR) if keep_ivar
      hash_node = root_info[:hash_node]
      # A damaged tail can lose the default value: emit a plain Hash instead
      # of a default marker with no default following it.
      has_default = hash_node.kind == :hash_default && root_info[:truncated_at].nil?
      write_byte(has_default ? M_HASH_DEFAULT : M_HASH)
      write_count(deliverable.size)
      deliverable.each do |entry|
        emit_node(entry[:key_id])
        emit_node(entry[:value_id])
      end
      emit_node(hash_node.children.last) if has_default
      return unless keep_ivar
      pairs = ivar_node.children[1..]
      write_count(pairs.size / 2)
      pairs.each { |id| emit_node(id) }
    end

    def emit_node(id)
      node = @nodes[id]
      # Frames carrying an object slot are emitted as a definition the first
      # time they are reached and as a link on every later encounter, which
      # reproduces the exact sharing identity of the source ledger.
      if node.obj_slot
        existing = @out_obj_slots[node.obj_slot]
        return emit_object_link(existing) if existing
        allocate_object_slot(node)
      end

      case node.kind
      when :array
        write_byte(M_ARRAY)
        write_count(node.children.size)
        node.children.each { |child_id| emit_node(child_id) }
      when :hash, :hash_default
        write_byte(node.kind == :hash ? M_HASH : M_HASH_DEFAULT)
        children = node.children
        if node.kind == :hash_default
          write_count((children.size - 1) / 2)
          children[0...-1].each { |child_id| emit_node(child_id) }
          emit_node(children.last)
        else
          write_count(children.size / 2)
          children.each { |child_id| emit_node(child_id) }
        end
      when :ivar
        write_byte(M_IVAR)
        emit_node(node.children.first)
        pairs = node.children[1..]
        write_count(pairs.size / 2)
        pairs.each { |child_id| emit_node(child_id) }
      when :string, :bignum, :fixnum, :true, :false, :nil
        copy_verbatim(node)
      when :symbol
        existing = @out_sym_slots[node.sym_slot]
        if existing
          # Output ordering can move a source symbol definition behind one of
          # its references: a second encounter must become a symlink instead
          # of silently writing nothing.
          write_byte(M_SYMLINK)
          encode_index_or_count(existing).each { |byte| write_byte(byte) }
        else
          define_symbol(node)
        end
      when :symlink
        source_symbol = @nodes[@sym_slot_owner[node.target]]
        existing = @out_sym_slots[source_symbol.sym_slot]
        if existing
          write_byte(M_SYMLINK)
          encode_index_or_count(existing).each { |byte| write_byte(byte) }
        else
          # First output encounter happens through a source symlink: expand
          # the symbol definition in place instead of emitting a dangling link.
          emit_node(source_symbol.id)
        end
      when :link
        if node.target.zero?
          emit_object_link(0)
        else
          # First encounter happens through a back reference: expand the
          # target definition in place, then subsequent uses become links.
          emit_node(@obj_slot_owner.fetch(node.target))
        end
      else
        raise ParseError.new("cannot emit frame kind #{node.kind}", node.pos)
      end
    end

    def allocate_object_slot(node)
      @out_obj_slots[node.obj_slot] = @next_out_obj_slot
      @next_out_obj_slot += 1
    end

    def emit_object_link(out_slot)
      write_byte(M_LINK)
      encode_index_or_count(out_slot).each { |byte| write_byte(byte) }
    end

    # Symbols are defined lazily in output traversal order and renumbered in a
    # private symbol table, so a symbol first registered in a discarded entry
    # is still recognised wherever retained data references it.
    def define_symbol(symbol_node)
      source_slot = symbol_node.sym_slot
      existing = @out_sym_slots[source_slot]
      return existing if existing
      out_slot = @next_out_sym_slot
      @next_out_sym_slot += 1
      @out_sym_slots[source_slot] = out_slot
      copy_verbatim(symbol_node)
      out_slot
    end

    def copy_verbatim(node)
      flush_write_buffer
      length = node.end_pos - node.pos
      IO.copy_stream(@source, @dest, length, node.pos)
    end

    def write_byte(byte)
      @write_buffer << byte
      flush_write_buffer if @write_buffer.bytesize >= @chunk_size
    end

    def write_count(value)
      encode_index_or_count(value).each { |byte| write_byte(byte) }
    end

    def encode_index_or_count(value)
      return [0x00] if value.zero?
      return [value + 5] if value <= 122
      bytes = []
      remaining = value
      while remaining.positive?
        bytes << (remaining & 0xFF)
        remaining >>= 8
      end
      [bytes.size] + bytes
    end

    def flush_write_buffer
      return if @write_buffer.empty?
      @dest.write(@write_buffer)
      @write_buffer = "".b
    end

    # ---- Reporting -------------------------------------------------------

    def print_report(report)
      count = @result.delivered_keys.size
      report.puts "delivered #{count} root entr#{count == 1 ? 'y' : 'ies'}: " \
                  "#{@result.delivered_keys.join(', ')}"
      @faults.each do |fault|
        location = fault.position ? " at byte #{fault.position}" : ""
        scope = fault.keys.empty? ? "" : " [#{fault.keys.join(', ')}]"
        report.puts "#{fault.type}#{location}#{scope}: #{fault.message}"
      end
    end
  end
end
