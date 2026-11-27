# frozen_string_literal: true

require 'stringio'

# A minimal, deterministic PDF writer used to build the two tiny inspection
# volumes. It emits raw PDF object graphs so the test fixtures can exercise
# legacy and modern destination styles that high level libraries flatten away:
#
# * catalog level /Dests dictionaries (the old "table of contents dictionary"),
# * modern /Names name trees, including nested /Kids nodes,
# * link annotations targeting page objects, integer page numbers or names,
# * outline (bookmark) trees with parent/child nesting,
# * ordinary external URI actions that must survive untouched.
module VolumeCombine
  class TinyPDF
    # An indirect object reference (id 0 R).
    class Ref
      attr_reader :id

      def initialize(id)
        @id = id
      end

      def to_s
        "#{@id} 0 R"
      end
    end

    # A stream object: dictionary fragment plus raw (unencoded) stream bytes.
    class Stream
      attr_reader :dict, :data

      def initialize(dict, data)
        @dict = dict
        @data = data
      end
    end

    PAGE_SIZE = [0, 0, 612.0, 792.0].freeze

    attr_reader :next_id

    def initialize
      @bodies = {}
      @next_id = 1
    end

    # Reserve an object id and return its reference before the body exists.
    def alloc
      id = @next_id
      @next_id += 1
      Ref.new(id)
    end

    def add(body)
      ref = alloc
      set(ref.id, body)
      ref
    end

    def set(id, body)
      @bodies[id] = body
      id
    end

    # Build a page content stream which prints the given text lines using a
    # single Helvetica font. The first line is the machine readable SRC marker
    # the auditor greps for after the output is re-parsed.
    def content_stream(lines)
      ops = +'BT /F1 12 Tf 1.0 1.0 1.0 rg 50 760 Td 14 TL'
      lines.each do |line|
        ops << "\n#{literal(line.to_s)} Tj T*"
      end
      ops << "\nET"
      Stream.new({}, ops.b)
    end

    def page(marker, title, pages_ref, ref: nil, annots: nil, mediabox: PAGE_SIZE)
      contents = add(content_stream(["SRC=#{marker}", title]))
      dict = {
        Type: :Page,
        Parent: pages_ref,
        MediaBox: mediabox,
        Resources: { Font: { F1: font_ref } },
        Contents: contents
      }
      dict[:Annots] = annots unless annots.nil? || annots.empty?
      if ref
        set(ref.id, dict)
        ref
      else
        add(dict)
      end
    end

    def font_ref
      @font_ref ||= add({ Type: :Font, Subtype: :Type1, BaseFont: :Helvetica })
    end

    def link_annotation(rect, target)
      ann = { Subtype: :Link, Rect: rect, Border: [0, 0, 0] }
      case target
      in { uri: uri }
        ann[:A] = { S: :URI, URI: uri }
      in { dest: dest }
        # Half the internal links use an explicit action, half a direct /Dest.
        if target[:direct]
          ann[:Dest] = dest
        else
          ann[:A] = { S: :GoTo, D: dest }
        end
      end
      add(ann)
    end

    # Serialize the whole file. catalog_body is a Hash; page refs are collected
    # into a /Pages tree (optionally nested to prove catalog nesting is walked).
    def render(catalog_body, page_refs:, nested: false)
      pages_ref = catalog_body[:Pages]
      if nested && page_refs.length >= 4
        half = (page_refs.length / 2.0).ceil
        branch_a = add({ Type: :Pages, Parent: pages_ref, Kids: page_refs[0...half], Count: half })
        branch_b = add({ Type: :Pages, Parent: pages_ref, Kids: page_refs[half..], Count: page_refs.length - half })
        set(pages_ref.id, { Type: :Pages, Kids: [branch_a, branch_b], Count: page_refs.length })
        page_refs.each { |_r| }
      else
        set(pages_ref.id, { Type: :Pages, Kids: page_refs, Count: page_refs.length })
      end
      catalog = add(catalog_body.merge({ Type: :Catalog, Pages: pages_ref }))

      out = StringIO.new
      out.write "%PDF-1.4\n%\xFF\xFF\xFF\xFF\n".b
      offsets = {}
      @bodies.keys.sort.each do |id|
        offsets[id] = out.pos
        out.write "#{id} 0 obj\n".b
        out.write render_body(@bodies[id])
        out.write "\nendobj\n".b
      end
      xref_pos = out.pos
      count = @next_id
      out.write "xref\n0 #{count}\n".b
      out.write "0000000000 65535 f \n".b
      (1...count).each do |id|
        out.write format("%010d 00000 n \n", offsets[id] || 0)
      end
      out.write "trailer\n<< /Size #{count} /Root #{catalog} >>\n".b
      out.write "startxref\n#{xref_pos}\n%%EOF\n".b
      out.string.force_encoding(Encoding::ASCII_8BIT)
    end

    private

    def render_body(body)
      case body
      when Stream
        dict = body.dict.merge(Length: body.data.bytesize)
        "<<\n#{render_hash(dict)}\n>>\nstream\n#{body.data}\nendstream".b
      else
        render_value(body)
      end
    end

    def render_hash(hash)
      hash.map { |k, v| "#{render_value(k)} #{render_value(v)}" }.join("\n")
    end

    def render_value(value)
      case value
      when Ref then value.to_s
      when Hash then "<< #{render_hash(value)} >>"
      when Array then "[#{value.map { |v| render_value(v) }.join(' ')}]"
      when Symbol then render_name(value.to_s)
      when String then render_string(value)
      when Integer then value.to_s
      when Float then format('%f', value)
      when true then 'true'
      when false then 'false'
      when nil then 'null'
      else raise "unrenderable #{value.class}"
      end
    end

    def render_name(name)
      bytes = name.dup.force_encoding(Encoding::ASCII_8BIT).bytes
      encoded = bytes.map do |b|
        if b >= 0x21 && b <= 0x7e && !['#', ' ', '/', '(', ')', '[', ']', '<', '>', '{', '}', '%'].include?(b.chr)
          b.chr
        else
          "##{format('%02X', b)}"
        end
      end
      "/#{encoded.join}".b
    end

    def render_string(str)
      bytes = str.dup.force_encoding(Encoding::ASCII_8BIT).bytes
      if bytes.all? { |b| b >= 0x20 && b <= 0x7e }
        literal(str)
      else
        "<#{bytes.map { |b| format('%02X', b) }.join}>".b
      end
    end

    def literal(str)
      escaped = str.dup.force_encoding(Encoding::ASCII_8BIT).gsub('\\', '\\\\\\\\').gsub('(', '\\(').gsub(')', '\\)')
      "(#{escaped})".b
    end
  end
end
