# frozen_string_literal: true

module Feedjira
  module OfflineArchive
    # Locates the exact byte range of every direct child +entry+ of the
    # document element, without ever reserialising the document. Signed entries
    # must be carried byte for byte, and Nokogiri's +to_xml+ normalises quoting,
    # namespace declarations and whitespace, so it cannot be used on the output
    # path.
    class RawDocument
      Slice = Struct.new(:index, :start_byte, :end_byte, :xml, keyword_init: true) do
        def length
          end_byte - start_byte
        end

        def bytes
          xml.b
        end
      end

      attr_reader :xml

      def initialize(xml)
        @xml = xml.to_s.dup.force_encoding(Encoding::BINARY)
        @bytes = @xml
      end

      def slices
        @slices ||= scan
      end

      def slice(index)
        slices.fetch(index)
      end

      def slice_xml(index)
        slice(index).xml
      end

      private

      def scan
        result = []
        stack = []
        cursor = 0

        while cursor < @bytes.bytesize
          open = @bytes.index("<", cursor)
          break if open.nil?

          head = @bytes.byteslice(open, 9)
          if head.start_with?("<!--")
            cursor = skip_past(open + 4, "-->")
            next
          elsif head == "<![CDATA["
            cursor = skip_past(open + 9, "]]>")
            next
          end

          two = @bytes.byteslice(open, 2)
          if two == "<?"
            cursor = skip_past(open + 2, "?>")
            next
          elsif two == "<!"
            cursor = skip_declaration(open)
            next
          end

          close = tag_end(open)
          if close.nil?
            cursor = open + 1
            next
          end

          raw = @bytes.byteslice(open, close - open + 1)
          if raw.start_with?("</")
            close_element(stack, result, raw, close)
          else
            open_element(stack, result, raw, open, close)
          end

          cursor = close + 1
        end

        result
      end

      def close_element(stack, result, raw, close)
        name = tag_name(raw, 2)
        opened = stack.pop
        return unless opened && opened[:name] == name && direct_child_of_root?(name, stack)

        result << Slice.new(
          index: result.length,
          start_byte: opened[:start],
          end_byte: close + 1,
          xml: utf8(@bytes.byteslice(opened[:start], close + 1 - opened[:start]))
        )
      end

      def open_element(stack, result, raw, open, close)
        name = tag_name(raw, 1)
        self_closing = raw.rstrip.end_with?("/>")

        if self_closing
          if direct_child_of_root?(name, stack)
            result << Slice.new(index: result.length, start_byte: open, end_byte: close + 1, xml: utf8(raw))
          end
        else
          stack << { name: name, start: open }
        end
      end

      def direct_child_of_root?(name, stack)
        local(name) == "entry" && stack.length == 1 && local(stack.first[:name]) == "feed"
      end

      def skip_past(from, terminator)
        found = @bytes.index(terminator, from)
        found.nil? ? @bytes.bytesize : found + terminator.bytesize
      end

      # Skips <!DOCTYPE ...> including an internal subset in square brackets.
      def skip_declaration(open)
        cursor = open + 2
        in_subset = false
        quote = nil

        while cursor < @bytes.bytesize
          char = @bytes.getbyte(cursor)
          if quote
            quote = nil if char == quote
          elsif char == 0x22 || char == 0x27
            quote = char
          elsif char == 0x5B
            in_subset = true
          elsif char == 0x5D
            in_subset = false
          elsif char == 0x3E && !in_subset
            return cursor + 1
          end
          cursor += 1
        end

        @bytes.bytesize
      end

      def tag_end(open)
        cursor = open + 1
        quote = nil

        while cursor < @bytes.bytesize
          char = @bytes.getbyte(cursor)
          if quote
            quote = nil if char == quote
          elsif char == 0x22 || char == 0x27
            quote = char
          elsif char == 0x3E
            return cursor
          end
          cursor += 1
        end

        nil
      end

      def tag_name(raw, offset)
        body = raw.byteslice(offset..).to_s
        body = body.sub(%r{\s*/?>\z}, "")
        body.split(%r{[\s/]}, 2).first.to_s
      end

      def local(name)
        name.to_s.split(":").last
      end

      def utf8(string)
        string.dup.force_encoding(Encoding::UTF_8)
      end
    end
  end
end
