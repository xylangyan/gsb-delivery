Nori
====

[![CI](https://github.com/savonrb/nori/actions/workflows/ci.yml/badge.svg)](https://github.com/savonrb/nori/actions/workflows/ci.yml)
[![Gem Version](https://img.shields.io/gem/v/nori.svg)](https://rubygems.org/gems/nori)
[![Coverage Status](https://coveralls.io/repos/github/savonrb/nori/badge.svg?branch=main)](https://coveralls.io/github/savonrb/nori?branch=main)

Really simple XML parsing ripped from Crack, which ripped it from Merb.  

Nori supports pluggable parsers and ships with both REXML and Nokogiri implementations.  
It defaults to Nokogiri since v2.0.0, but you can change it to use REXML via:

``` ruby
Nori.new(:parser => :rexml)  # or :nokogiri
```

Make sure Nokogiri is in your LOAD_PATH when parsing XML, because Nori tries to load it when it's needed.

# Examples

```ruby
Nori.new.parse("<tag>This is the content</tag>")
# => {"tag"=>"This is the content"}

Nori.new.parse('<foo />')
#=> {"foo"=>nil}

Nori.new.parse('<foo bar />')
#=> {}

Nori.new.parse('<foo bar="baz"/>')
#=> {"foo"=>{"@bar"=>"baz"}}

Nori.new.parse('<foo bar="baz">Content</foo>')
#=> {"foo"=>"Content"}
```

## Nori::StringWithAttributes

You can access a string node's attributes via `attributes`.

```ruby
result = Nori.new.parse('<foo bar="baz">Content</foo>')
#=> {"foo"=>"Content"}

result["foo"].class
# => Nori::StringWithAttributes

result["foo"].attributes
# => {"bar"=>"baz"}
```

Because `StringWithAttributes` hides the attributes on an accessor, they are
lost on a plain `to_json` and callers have to type-check each value. The
[`serializable`](#serializable) profile returns a plain Hash instead and will
probably be the default in 3.0.

## advanced_typecasting

Nori can automatically convert string values to `TrueClass`, `FalseClass`, `Time`, `Date`, and `DateTime`:

```ruby
# "true" and "false" String values are converted to `TrueClass` and `FalseClass`.
Nori.new.parse("<value>true</value>")
# => {"value"=>true}

# String values matching xs:time, xs:date and xs:dateTime are converted to `Time`, `Date` and `DateTime` objects.
Nori.new.parse("<value>09:33:55.7Z</value>")
# => {"value"=>2022-09-29 09:33:55.7 UTC

# disable with advanced_typecasting: false
Nori.new(advanced_typecasting: false).parse("<value>true</value>")
# => {"value"=>"true"}

```

## strip_namespaces

Nori can strip the namespaces from your XML tags. This feature is disabled by default.

``` ruby
Nori.new.parse('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"></soap:Envelope>')
# => {"soap:Envelope"=>{"@xmlns:soap"=>"http://schemas.xmlsoap.org/soap/envelope/"}}

Nori.new(:strip_namespaces => true).parse('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"></soap:Envelope>')
# => {"Envelope"=>{"@xmlns:soap"=>"http://schemas.xmlsoap.org/soap/envelope/"}}
```

## convert_tags_to

Nori lets you specify a custom formula to convert XML tags to Hash keys using `convert_tags_to`.

``` ruby
Nori.new.parse('<userResponse><accountStatus>active</accountStatus></userResponse>')
# => {"userResponse"=>{"accountStatus"=>"active"}}

parser = Nori.new(:convert_tags_to => lambda { |tag| Nori::StringUtils.snakecase(tag).to_sym })
parser.parse('<userResponse><accountStatus>active</accountStatus></userResponse>')
# => {:user_response=>{:account_status=>"active"}}
```

## convert_dashes_to_underscores

By default, Nori will automatically convert dashes in tag names to underscores.

```ruby
Nori.new.parse('<any-tag>foo bar</any-tag>')
# => {"any_tag"=>"foo bar"}

# disable with convert_dashes_to_underscores
parser = Nori.new(:convert_dashes_to_underscores => false)
parser.parse('<any-tag>foo bar</any-tag>')
# => {"any-tag"=>"foo bar"}
```

## empty_tag_value

By default, an empty tag becomes `nil`. The `empty_tag_value` option replaces that value.

```ruby
Nori.new.parse('<foo/>')
# => {"foo"=>nil}

Nori.new(:empty_tag_value => "").parse('<foo/>')
# => {"foo"=>""}
```

The option does not apply to empty tags with attributes.
They become a hash of their prefixed attributes instead.

```ruby
Nori.new(:empty_tag_value => "").parse('<foo bar="baz"/>')
# => {"foo"=>{"@bar"=>"baz"}}
```

## consistent_empty_tags

With `consistent_empty_tags`, every empty tag (with or without attributes) becomes the `empty_tag_value`.

```ruby
Nori.new(:consistent_empty_tags => true).parse('<foo bar="baz"/>')
# => {"foo"=>nil}
```

A string `empty_tag_value` keeps the attributes accessible on the value,
and an explicit `xsi:nil="true"` always becomes `nil`.

```ruby
parser = Nori.new(:consistent_empty_tags => true, :empty_tag_value => "")

result = parser.parse('<foo bar="baz"/>')
# => {"foo"=>""}

result["foo"].attributes
# => {"bar"=>"baz"}

parser.parse('<foo xsi:nil="true" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
# => {"foo"=>nil}
```

## standards

`standards` is a profile that turns on Nori's spec-correct parsing as a group,
rather than one flag at a time. It is opt-in on the 2.x line and becomes the
default in Nori 3.0.

```ruby
Nori.new(:standards => true)
```

Members of the profile:

- **xml:space honoring** ([XML 1.0 §2.10](https://www.w3.org/TR/xml/#sec-white-space)).
  Whitespace-only text under `xml:space="preserve"` is kept instead of stripped.
  The nearest ancestor that sets `xml:space` wins, and `xml:space="default"`
  resets to the stripping behavior.

  ```ruby
  Nori.new(:standards => true).parse('<name xml:space="preserve">   </name>')
  # => {"name"=>"   "}
  ```

- **the XML string-value model for empty elements.** The profile implies
  `consistent_empty_tags: true` with `empty_tag_value: ""`. Both remain
  overridable, so passing either option explicitly wins over the profile.

  ```ruby
  Nori.new(:standards => true).parse('<foo bar="baz"/>')
  # => {"foo"=>""}
  ```

- **no schema-less typing.** Without a schema, character data is just text, so
  the profile implies `advanced_typecasting: false` (overridable) and ignores
  the bare `type=` and `nil=` attribute conventions that Nori inherited from
  Rails' `Hash.from_xml`. A `type="integer"` no longer casts, a `type="array"`
  no longer folds children into an Array, and both stay visible as ordinary
  attributes. Only the prefixed `xsi:nil="true"` still marks an element nil,
  because that convention comes from XML Schema Instance itself.

  ```ruby
  Nori.new(:standards => true).parse('<id type="integer">123</id>')
  # => {"id"=>"123"}    (with "type" accessible via #attributes)
  ```

## streaming

`Nori::Streaming::Session` accepts a document in pieces and hands back each
record as soon as its closing tag arrives. `Nori#parse` still waits for the
complete string; the streaming layer is opt-in and does not change it.

```ruby
session = Nori::Streaming::Session.new(
  :records => [{ :path => %w[daily warehouse record] }],
  :strip_namespaces => true
)

session.feed(chunk) do |record|
  record.id                   # one unique business id per original record
  record.value                # Nori hash for just this record
  record.byte_start           # absolute offset in the original transmission
  record.byte_end             # exclusive
  record.credential           # id, path, byte range, status
end

session.acknowledge(record.id) # late acknowledgements are fine and idempotent
session.discard_acknowledged!  # upstream may now delete those bytes
session.checkpoint!            # atomic snapshot of scanner, spool and ledger
```

A commit point is selected by the **raw** element name, including any
namespace prefix and before any of Nori's key normalization, plus an optional
directory hierarchy. `"record"`, `"p:record"`, `"daily/warehouse/record"`,
`["daily", "warehouse", "record"]` and
`[{ :name => "record", :path => %w[daily warehouse record] }]` all work.
A flat array of strings is a directory hierarchy; use an array of hashes or
nested arrays to declare several independent commit points.

The session never needs the bytes it has already released. A pause inside
text, a CDATA section, a start tag or a large attachment only retains the
half-read token, and a checkpoint keeps the exact absolute offset. Declarations
inherited from ancestors (namespace prefixes and `xml:space`) are replayed when
a record is cut out of its document, so the record parses as it would in the
complete document. A record is parsed with the options that were in effect when
it started; changing `session.options` affects only records that have not
started yet.

The receive ledger assigns a business id exactly once per original record, keyed
by the record's position in the transmission, and can look the record up by
that id (`session.ledger[id]`). A tag converter declared with two parameters
receives the current record's business id as its second argument:

```ruby
converter = lambda { |tag, business_id| "#{tag}:#{business_id}" }
Nori::Streaming::Session.new(:records => ["record"], :convert_tags_to => converter)
```

Zero or one required parameters keep the old one-argument behavior, so existing
converters are unaffected.

## serializable

`serializable` is a profile that makes Nori return plain, directly-serializable
data with no custom value classes. It is opt-in on the 2.x line and will
probably become the default in 3.0.

```ruby
Nori.new(:serializable => true)
```

Members of the profile:

- **text nodes with attributes become a Hash.** A tag with both text content
  and attributes maps to the XML JSON convention (`{"#text" => content}` merged
  with the `@`-prefixed attributes) instead of a `Nori::StringWithAttributes`.
  This is the same `@`-keyed shape element nodes already use, so the attributes
  survive `to_json`, `to_yaml`, and `Marshal`. A text node without attributes
  stays a plain `String`. Typecast values keep their attributes the same way
  (`{"#text" => true, "@source" => "ldap"}`) – attributes are never silently
  dropped under the profile. The `#text` key goes through `convert_tags_to`
  like every other key, so a key-converting formula can reshape it (or drop
  the `#` entirely).

  ```ruby
  Nori.new(:serializable => true).parse('<foo bar="baz">Content</foo>')
  # => {"foo"=>{"#text"=>"Content", "@bar"=>"baz"}}

  Nori.new(:serializable => true).parse('<foo>Content</foo>')
  # => {"foo"=>"Content"}
  ```

- **`type="file"` nodes are not decoded.** Without the profile, a node carrying
  `type="file"` is base64-decoded into a `Nori::StringIOFile` with its filename
  and content type. The profile skips that. The node folds into text and
  attributes like any other, leaving the base64 content as a plain `String` for
  the caller to decode. This `type=` decoding is a Rails/ActiveSupport
  `Hash.from_xml` convention Nori inherited through crack and merb, not part of
  any XML specification, so the plain-data profile leaves it behind.

  ```ruby
  Nori.new(:serializable => true).parse('<doc type="file" name="x.pdf">aGVsbG8=</doc>')
  # => {"doc"=>{"#text"=>"aGVsbG8=", "@type"=>"file", "@name"=>"x.pdf"}}
  ```
