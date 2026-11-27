# frozen_string_literal: true

require 'strscan'

module VolumeCombine
  # A deliberately small raw PDF reader.
  #
  # CombinePDF unifies every parsed string and renames all name tree entries as
  # soon as a file is loaded, which destroys the original destination spellings
  # this tool must reason about. This reader performs a narrow, read-only scan
  # of the raw bytes and records the original structures:
  #
  # * ordered page object ids (nested /Pages trees are walked),
  # * catalog /Dests dictionaries and modern /Names name trees,
  # * link annotations in page order with their exact target kind and view,
  # * outline nodes with parent/child nesting,
  # * external URI actions.
  #
  # References are represented as [:ref, id] until the model layer resolves
  # them against the ordered page list.
  class RawReader
    Ref = Struct.new(:id) do
      def to_a
        [:ref, id]
      end
    end

    Link = Struct.new(:page_index, :order, :rect, :kind, :target, :view, :via_action, keyword_init: true)
    NameDest = Struct.new(:name, :page_index, :view, keyword_init: true)
    OutlineNode = Struct.new(:id, :title, :target_page, :view, :children, keyword_init: true)

    attr_reader :page_ids, :links, :name_dests, :outline_roots

    def self.read(path)
      new(IO.binread(path)).tap(&:parse)
    end

    def initialize(bytes)
      @scanner = StringScanner.new(bytes.dup.force_encoding(Encoding::ASCII_8BIT))
      @objects = {}
      @root_id = nil
      @page_ids = []
      @links = []
      @name_dests = []
      @outline_roots = []
    end

    def parse
      collect_objects
      walk_pages(catalog[:Pages])
      read_name_destinations
      read_annotations
      read_outlines
      self
    end

    private

    def collect_objects
      data = @scanner.string
      offset = 0
      while (index = data.index(/(\d+)\s+0\s+obj/, offset))
        @scanner.pos = index
        id = @scanner.scan(/\d+/).to_i
        @scanner.scan(/\s+0\s+obj/)
        value = parse_value
        @objects[id] = value
        offset = @scanner.pos
        break if id.zero? && value.nil?
      end
      trailer = data.index(/trailer/, offset)
      if trailer && (m = data.match(/\/Root\s+(\d+)\s+0\s+R/, trailer))
        @root_id = m[1].to_i
      elsif (m = data.match(/\/Root\s+(\d+)\s+0\s+R/))
        @root_id = m[1].to_i
      end
    end

    NAME_REGEX = /\/[^\x00\x09\x0a\x0c\x0d\x20\x28\x29\x3c\x3e\x5b\x5d\x7b\x7d\x2f\x25]*/

    def parse_value
      skip_ws
      if @scanner.scan(/<</)
        dict = {}
        loop do
          skip_ws
          break if @scanner.scan(/>>/)
          raw_name = @scanner.scan(NAME_REGEX)
          raise 'name expected' unless raw_name
          dict[parse_name(raw_name)] = parse_value
        end
        maybe_stream(dict)
      elsif @scanner.scan(/\[/)
        array = []
        loop do
          skip_ws
          break if @scanner.scan(/\]/)
          array << parse_value
        end
        resolve_inline_refs(array)
      elsif (name = @scanner.scan(NAME_REGEX))
        parse_name(name)
      elsif @scanner.scan(/\(/)
        parse_literal_string
      elsif (hex = @scanner.scan(/<[0-9A-Fa-f\s]*>/))
        [hex[1..-2].gsub(/\s/, '')].pack('H*').force_encoding(Encoding::ASCII_8BIT)
      elsif (num = @scanner.scan(/-?\d+\.\d+/))
        num.to_f
      elsif (num = @scanner.scan(/-?\d+/))
        position = @scanner.pos
        if @scanner.scan(/\s+0\s+R/)
          Ref.new(num.to_i)
        else
          @scanner.pos = position
          num.to_i
        end
      elsif @scanner.scan(/true/)
        true
      elsif @scanner.scan(/false/)
        false
      elsif @scanner.scan(/null/)
        nil
      else
        # Unknown token: advance one byte to keep the loop alive.
        @scanner.pos += 1
        nil
      end
    end

    def maybe_stream(dict)
      skip_ws
      if @scanner.scan(/stream\r?\n/)
        length = dict[:Length].is_a?(Integer) ? dict[:Length] : 0
        start = @scanner.pos
        if length.positive?
          @scanner.pos = start + length
        else
          @scanner.scan_until(/endstream/)
          @scanner.pos -= 'endstream'.bytesize
        end
        @scanner.scan(/\r?\n?endstream/)
        dict[:__stream__] = @scanner.string.byteslice(start, @scanner.pos - start - 'endstream'.bytesize)
      end
      dict
    end

    def resolve_inline_refs(array)
      # "id 0 R" was tokenized as [id, 0, :R-less]; refs are handled in
      # parse_value, so nothing extra is required here.
      array
    end

    def parse_literal_string
      str = +''
      depth = 1
      while depth.positive? && !@scanner.eos?
        chunk = @scanner.scan_until(/[()]/)
        raise 'unterminated string' unless chunk
        str << chunk
        backslashes = 0
        backslashes += 1 while str[-2 - backslashes] == '\\'
        case chunk[-1]
        when '(' then depth += 1 unless backslashes.odd?
        when ')' then depth -= 1 unless backslashes.odd?
        end
      end
      str[0..-2].force_encoding(Encoding::ASCII_8BIT)
    end

    def parse_name(raw)
      decode_name(raw[1..]).to_sym
    end

    def skip_ws
      @scanner.scan(/[\x00\x09\x0a\x0c\x0d\x20]+/)
    end

    def deref(value)
      value.is_a?(Ref) ? @objects[value.id] : value
    end

    def catalog
      @catalog ||= deref(Ref.new(@root_id))
    end

    def walk_pages(node_value)
      node = deref(node_value)
      case node[:Type]
      when :Pages
        node[:Kids].each { |kid| walk_pages(kid) }
      when :Page
        @page_ids << id_of(node_value)
      end
    end

    def id_of(value)
      value.is_a?(Ref) ? value.id : @objects.key(value)
    end

    def read_name_destinations
      legacy = catalog[:Dests]
      if legacy
        deref(legacy).each do |name, dest|
          next if name == :__stream__
          add_name_dest(name.to_s, dest)
        end
      end
      names = catalog[:Names]
      return unless names
      walk_name_tree(deref(deref(names)[:Dests]))
    end

    def walk_name_tree(node_value)
      node = deref(node_value)
      return unless node.is_a?(Hash)
      if node[:Names]
        node[:Names].each_slice(2) do |name, dest|
          add_name_dest(name.dup, deref(dest))
        end
      end
      node[:Kids]&.each { |kid| walk_name_tree(kid) }
    end

    def add_name_dest(name, dest)
      dest = deref(dest)
      dest = dest[:indirect_without_dictionary] if dest.is_a?(Hash) && dest.key?(:indirect_without_dictionary)
      @name_dests << NameDest.new(name: name, page_index: page_index_of(dest[0]), view: dest[1..])
    end

    def page_index_of(page_value)
      id = id_of(page_value)
      @page_ids.index(id)
    end

    def read_annotations
      @page_ids.each_with_index do |page_id, page_index|
        page = @objects[page_id]
        annots = page[:Annots]
        next unless annots
        deref(annots).each_with_index do |annot_ref, order|
          annot = deref(annot_ref)
          next unless annot[:Subtype] == :Link
          @links << build_link(page_index, order, annot)
        end
      end
    end

    def build_link(page_index, order, annot)
      rect = annot[:Rect]
      if annot[:A]
        action = deref(annot[:A])
        if action[:S] == :URI
          Link.new(page_index: page_index, order: order, rect: rect, kind: :uri,
                   target: action[:URI], view: nil, via_action: true)
        else
          kind, target, view = describe_destination(action[:D])
          Link.new(page_index: page_index, order: order, rect: rect, kind: kind,
                   target: target, view: view, via_action: true)
        end
      elsif annot[:Dest]
        kind, target, view = describe_destination(annot[:Dest])
        Link.new(page_index: page_index, order: order, rect: rect, kind: kind,
                 target: target, view: view, via_action: false)
      end
    end

    def describe_destination(dest)
      dest = deref(dest)
      if dest.is_a?(String)
        return [:name, dest.dup.force_encoding(Encoding::ASCII_8BIT), nil]
      elsif dest.is_a?(Symbol)
        return [:name, decode_name(dest.to_s).force_encoding(Encoding::ASCII_8BIT), nil]
      end
      case dest[0]
      when Ref
        [:page_object, page_index_of(dest[0]), dest[1..]]
      when Integer
        [:integer_page, dest[0], dest[1..]]
      when String
        [:name, dest[0].dup, nil]
      else
        raise "unsupported destination #{dest.inspect}"
      end
    end

    def decode_name(raw)
      raw.gsub(/#([0-9a-fA-F]{2})/) { [Regexp.last_match(1).hex].pack('C') }
         .force_encoding(Encoding::ASCII_8BIT)
    end

    def read_outlines
      outlines = catalog[:Outlines]
      return unless outlines
      root = deref(outlines)
      first = root[:First]
      @outline_roots = walk_outline_chain(first)
    end

    def walk_outline_chain(node_value)
      nodes = []
      current = node_value
      while current
        node = deref(current)
        children = node[:First] ? walk_outline_chain(node[:First]) : []
        if node[:Dest]
          kind, target, view = describe_destination(node[:Dest])
          nodes << OutlineNode.new(id: outline_id, title: node[:Title].dup, target_page: target, view: view, children: children)
        else
          nodes << OutlineNode.new(id: outline_id, title: node[:Title].dup, target_page: nil, view: nil, children: children)
        end
        current = node[:Next]
      end
      nodes
    end

    def outline_id
      @outline_seq ||= 0
      format('n%03d', @outline_seq += 1)
    end
  end
end
