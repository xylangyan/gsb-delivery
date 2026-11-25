require "nori/version"
require "nori/core_ext"
require "nori/xml_utility_node"
require "nori/streaming"

class Nori

  def self.hash_key(name, options = {})
    name = name.tr("-", "_") if options[:convert_dashes_to_underscores]
    name = name.split(":").last if options[:strip_namespaces]
    if options[:convert_tags_to].respond_to? :call
      name = call_tag_converter(options[:convert_tags_to], name, options)
    end
    name
  end

  # A tag converter normally takes the tag. A converter that declares a second
  # positional parameter additionally receives the current record's business id
  # (or nil during a full-document parse). This lets a streaming caller use the
  # stable id inside its conversion formula without exposing any other state.
  # A rest-only signature such as :to_sym.to_proc keeps the one-argument form.
  def self.call_tag_converter(converter, name, options)
    arity = converter.respond_to?(:arity) ? converter.arity : 1
    return converter.call(name) if arity == 1 || arity == -1

    if converter.respond_to?(:parameters)
      positional = converter.parameters.count { |kind, _| kind == :req || kind == :opt }
      return converter.call(name) if positional < 2
    end

    converter.call(name, options[:business_id])
  end

  PARSERS = { :rexml => "REXML", :nokogiri => "Nokogiri" }

  def initialize(options = {})
    defaults = {
      :strip_namespaces              => false,
      :delete_namespace_attributes   => false,
      :convert_tags_to               => nil,
      :convert_attributes_to         => nil,
      :empty_tag_value               => nil,
      :consistent_empty_tags         => false,
      :advanced_typecasting          => true,
      :convert_dashes_to_underscores => true,
      :scrub_xml                     => true,
      :standards                     => false,
      :serializable                  => false,
      :business_id                   => nil,
      :parser                        => :nokogiri
    }

    validate_options! defaults.keys, options.keys
    @options = defaults.merge(standards_defaults(options)).merge(options)
  end

  # A streaming session that emits each record as its closing tag arrives.
  # See Nori::Streaming::Session.
  def self.stream(options = {})
    Streaming::Session.new(options)
  end

  def stream(options = {})
    Streaming::Session.new(@options.merge(options))
  end

  def parse_stream(xml, options = {}, &block)
    Streaming::Session.new(@options.merge(options)).parse(xml, &block)
  end

  def self.parse_stream(xml, options = {}, &block)
    Streaming.parse(xml, options, &block)
  end

  def find(hash, *path)
    return hash if path.empty?

    key = path.shift
    key = self.class.hash_key(key, @options)

    value = find_value(hash, key)
    find(value, *path) if value
  end

  def parse(xml)
    cleaned_xml = scrub_xml(xml).strip
    return {} if cleaned_xml.empty?

    parser = load_parser @options[:parser]
    parser.parse(cleaned_xml, @options)
  end

  private

  # The defaults implied by the +:standards+ profile.
  #
  # The profile groups the spec-correct behaviors under a single opt-in.
  # It turns on the XML string-value model for empty elements
  # (+:consistent_empty_tags+ with an empty-string +:empty_tag_value+),
  # turns off +:advanced_typecasting+ (schema-less values are text, their
  # types are the business of a schema-aware layer) and, in the parsers,
  # honors xml:space. These are defaults, so an explicit option passed by
  # the caller still wins. When the profile is off the hash is empty and
  # parsing is unchanged.
  #
  # @param options [Hash] the options passed to {#initialize}
  # @return [Hash] the implied defaults, or +{}+ when the profile is off
  def standards_defaults(options)
    return {} unless options[:standards]
    {
      :consistent_empty_tags => true,
      :empty_tag_value       => "",
      :advanced_typecasting  => false
    }
  end

  def load_parser(parser)
    require "nori/parser/#{parser}"
    Parser.const_get PARSERS[parser]
  end

  # Expects a +block+ which receives a tag to convert.
  # Accepts +nil+ for a reset to the default behavior of not converting tags.
  def convert_tags_to(reset = nil, &block)
    @convert_tag = reset || block
  end

  def validate_options!(available_options, options)
    spurious_options = options - available_options

    unless spurious_options.empty?
      raise ArgumentError, "Spurious options: #{spurious_options.inspect}\n" \
                           "Available options are: #{available_options.inspect}"
    end
  end

  def find_value(hash, key)
    hash.each do |k, v|
      key_without_namespace = k.to_s.split(':').last
      return v if key_without_namespace == key.to_s
    end

    nil
  end

  def scrub_xml(string)
    if @options[:scrub_xml]
      if string.respond_to? :scrub
        string.scrub
      else
        if string.valid_encoding?
          string
        else
          enc = string.encoding
          mid_enc = (["UTF-8", "UTF-16BE"].map { |e| Encoding.find(e) } - [enc]).first
          string.encode(mid_enc, undef: :replace, invalid: :replace).encode(enc)
        end
      end
    else
      string 
    end
  end

end
