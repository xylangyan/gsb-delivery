require "strscan"

class Nori
  module Streaming
    # A byte-oriented XML tokenizer that never needs the whole document in
    # memory. It reports the raw text of every start tag, end tag, text run and
    # CDATA section it has consumed, and it can stop at a closing tag that
    # matches a caller-supplied {Boundaries} object.
    #
    # The scanner tracks a directory stack of raw element names. Names include
    # their namespace prefix exactly as written in the source, so the name and
    # the hierarchy a caller specifies are compared before any of Nori's key
    # normalization (dashes, prefixes, symbols) gets a chance to collapse two
    # distinct XML names together.
    class Scanner
      StartTag = Struct.new(:name, :raw, :attributes, :self_closing, :namespaces,
                            :xml_space, :offset, :end_offset, :path,
                            :in_scope_namespaces, :in_scope_xml_space)
      EndTag = Struct.new(:name, :raw, :offset, :end_offset, :path, :self_closing)
      Text = Struct.new(:raw, :content, :cdata, :offset, :end_offset)
      Declaration = Struct.new(:raw, :offset, :end_offset, :kind)

      WHITESPACE = /[ \t\r\n]*/
      NAME = /[^\s\/>\t\r\n]+/
      ATTRIBUTE = /\s*([^\s=\/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/
      XML_SPACE_ATTRIBUTE = /\Axml:space\z/
      XMLNS_ATTRIBUTE = /\Axmlns(?::([^\s=]+))?\z/

      attr_reader :position, :stack, :line, :column, :pending_offset

      def initialize(boundaries, start_offset = 0)
        @boundaries = boundaries
        @position = start_offset
        @stack = []
        @line = 1
        @column = 1
        @pending = String.new(encoding: Encoding::BINARY)
        @pending_offset = start_offset
        @state = :text
        @ns_stack = [{}]
        @space_stack = ["default"]
      end

      # True while the scanner is inside a text run, a CDATA section, a tag,
      # a comment or a prolog construct. Callers use this to say exactly where
      # the retained bytes end.
      def in_text?
        @state != :text || !@pending.empty?
      end

      def pending_bytes
        @pending.bytesize
      end

      # Feed bytes. Complete tokens are returned as events. The last,
      # incomplete token is retained in the scanner for the next feed.
      def feed(bytes, final = false)
        @pending << bytes.b
        events = []
        offset = @pending_offset

        loop do
          break if @pending.empty?
          previous_state = @state

          case @state
          when :text
            consumed = scan_text(events, offset, final)
          when :cdata
            consumed = scan_cdata(events, offset, final)
          when :comment
            consumed = scan_comment(events, offset, final)
          when :declaration
            consumed = scan_declaration(events, offset, final)
          end

          if consumed.nil? || consumed == 0
            next if @state != previous_state
            break
          end
          @pending = @pending.byteslice(consumed, @pending.bytesize - consumed) || String.new(encoding: Encoding::BINARY)
          @pending_offset += consumed
          offset = @pending_offset
        end

        if final && @state == :cdata
          raise ParseError.new("Unterminated CDATA section", @pending_offset)
        elsif final && !@pending.empty?
          raise ParseError.new("Incomplete markup at the end of the transmission", @pending_offset)
        end

        events
      end

      def flush
        feed(+"", true)
      end

      # Serializes the scanner's incomplete-token state. Complete events are
      # already held by the session, so only the tail that has not become a
      # token yet needs to survive a pause.
      def snapshot
        {
          :position => @position,
          :pending_offset => @pending_offset,
          :pending => @pending,
          :state => @state,
          :stack => @stack.dup,
          :ns_stack => @ns_stack.map(&:dup),
          :space_stack => @space_stack.dup,
          :line => @line,
          :column => @column
        }
      end

      def restore(state)
        @position = state[:position]
        @pending_offset = state[:pending_offset]
        @pending = state[:pending].dup.force_encoding(Encoding::BINARY)
        @state = state[:state].to_sym
        @stack = state[:stack].dup
        @ns_stack = state[:ns_stack].map(&:dup)
        @space_stack = state[:space_stack].dup
        @line = state[:line]
        @column = state[:column]
        self
      end

      private

      def scan_text(events, offset, final)
        # A leading "<" may still grow into any markup construct. Wait for
        # enough bytes to tell which one before committing to a state.
        if @pending.start_with?("<") && @pending.bytesize < 9 && ambiguous_markup_prefix?(@pending)
          return 0
        end

        if @pending.start_with?("<![CDATA[")
          @state = :cdata
          return 0
        elsif @pending.start_with?("<!--")
          @state = :comment
          return 0
        elsif @pending.start_with?("<?") || @pending.start_with?("<!")
          @state = :declaration
          return 0
        elsif @pending.start_with?("</")
          return scan_end_tag(events, offset, final)
        elsif @pending.start_with?("<")
          return scan_start_tag(events, offset, final)
        end

        index = @pending.index("<")
        if index.nil?
          text = @pending
          return 0 if text.empty?
          emit_text(events, text, false, offset)
          consume(text)
          return text.bytesize
        end

        return 0 if index == 0
        text = @pending.byteslice(0, index)
        emit_text(events, text, false, offset)
        consume(text)
        text.bytesize
      end

      def scan_end_tag(events, offset, final)
        match = @pending.match(/\A<\/([^\s>]+)([^>]*)>/m)
        return 0 unless match
        name = match[1]
        raw = match[0]
        if @stack.last != name
          raise ParseError.new("Unexpected end tag </#{name}>; open element is #{@stack.last || "(none)"}", offset)
        end
        path = @stack.dup
        @stack.pop
        @ns_stack.pop
        @space_stack.pop
        events << EndTag.new(name, raw, offset, offset + raw.bytesize, path, false)
        consume(raw)
        raw.bytesize
      end

      def scan_start_tag(events, offset, final)
        match = @pending.match(/\A<([^\s\/>]+)((?:[^>"']|"[^"]*"|'[^']*')*)>/m)
        return 0 unless match
        name = match[1]
        body = match[2]
        raw = match[0]
        self_closing = body.rstrip.end_with?("/")
        attributes = parse_attributes(body)
        namespaces = parse_namespaces(attributes)
        xml_space = attributes["xml:space"]
        in_scope_namespaces = @ns_stack.last.merge(namespaces)
        in_scope_xml_space = xml_space || @space_stack.last || "default"
        path = @stack.dup << name
        events << StartTag.new(name, raw, attributes, self_closing, namespaces, xml_space,
                               offset, offset + raw.bytesize, path,
                               in_scope_namespaces, in_scope_xml_space)
        consume(raw)
        @stack.push(name)
        @ns_stack.push(in_scope_namespaces)
        @space_stack.push(in_scope_xml_space)
        if self_closing
          @stack.pop
          @ns_stack.pop
          @space_stack.pop
          events << EndTag.new(name, "", offset + raw.bytesize, offset + raw.bytesize,
                               path, true)
        end
        raw.bytesize
      end

      def scan_cdata(events, offset, final)
        terminator = @pending.index("]]>")
        if terminator.nil?
          if final
            raise ParseError.new("Unterminated CDATA section", offset)
          end
          # Keep only the bytes that could be the start of the terminator.
          keep = cdata_terminator_prefix_length(@pending)
          consumable = @pending.bytesize - keep
          return 0 if consumable <= 0
          raw = @pending.byteslice(0, consumable)
          events << Text.new(raw, raw, true, offset, offset + raw.bytesize)
          consume(raw)
          return raw.bytesize
        end
        raw = @pending.byteslice(0, terminator + 3)
        content = raw.byteslice(9, raw.bytesize - 12) || String.new
        events << Text.new(raw, content, true, offset, offset + raw.bytesize)
        consume(raw)
        @state = :text
        raw.bytesize
      end

      def scan_comment(events, offset, final)
        terminator = @pending.index("-->")
        return 0 unless terminator
        raw = @pending.byteslice(0, terminator + 3)
        events << Declaration.new(raw, offset, offset + raw.bytesize, :comment)
        consume(raw)
        @state = :text
        raw.bytesize
      end

      def scan_declaration(events, offset, final)
        terminator = declaration_terminator
        return 0 unless terminator
        raw = @pending.byteslice(0, terminator)
        events << Declaration.new(raw, offset, offset + raw.bytesize, declaration_kind(raw))
        consume(raw)
        @state = :text
        raw.bytesize
      end

      def declaration_kind(raw)
        if raw.start_with?("<?xml")
          :xml_declaration
        elsif raw.start_with?("<!DOCTYPE") || raw.start_with?("<!doctype")
          :doctype
        elsif raw.start_with?("<?")
          :instruction
        elsif raw.start_with?("<!--")
          :comment
        else
          :declaration
        end
      end

      # Finds the end of the markup declaration currently at the head of the
      # buffer. A processing instruction ends at "?>". A DOCTYPE can contain an
      # internal subset, whose ">" characters are nested inside "[ ... ]", so a
      # naive first-">" search would split the declaration.
      def declaration_terminator
        if @pending.start_with?("<?")
          index = @pending.index("?>")
          index && index + 2
        elsif doctype?
          doctype_terminator
        else
          index = @pending.index(">")
          index && index + 1
        end
      end

      def doctype?
        @pending.start_with?("<!DOCTYPE") || @pending.start_with?("<!doctype")
      end

      def doctype_terminator
        in_subset = false
        quote = nil
        index = 0
        while index < @pending.bytesize
          char = @pending.byteslice(index, 1)
          if quote
            quote = nil if char == quote
          elsif char == '"' || char == "'"
            quote = char
          elsif char == "["
            in_subset = true
          elsif char == "]"
            in_subset = false
          elsif char == ">" && !in_subset
            return index + 1
          end
          index += 1
        end
        nil
      end

      # True while +text+ is a proper prefix of a markup opener.
      def ambiguous_markup_prefix?(text)
        ["<![CDATA[", "<!--", "<!DOCTYPE", "<!doctype", "</", "<?"].any? do |opener|
          opener.start_with?(text)
        end
      end

      def emit_text(events, text, cdata, offset)
        events << Text.new(text, text, cdata, offset, offset + text.bytesize)
      end

      def cdata_terminator_prefix_length(text)
        return 2 if text.end_with?("]]")
        return 1 if text.end_with?("]")
        0
      end

      def parse_attributes(body)
        attributes = {}
        scanner = StringScanner.new(body)
        until scanner.eos?
          break unless scanner.scan(ATTRIBUTE)
          value = scanner[2] || scanner[3] || ""
          attributes[scanner[1]] = value
        end
        attributes
      end

      def parse_namespaces(attributes)
        result = {}
        attributes.each do |key, value|
          match = key.match(XMLNS_ATTRIBUTE)
          next unless match
          result[match[1] || ""] = value
        end
        result
      end

      def consume(text)
        newlines = text.count("\n")
        if newlines > 0
          @line += newlines
          @column = text.length - text.rindex("\n")
        else
          @column += text.length
        end
        @position += text.bytesize
      end
    end
  end
end
