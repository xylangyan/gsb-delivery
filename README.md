# RSS

Really Simple Syndication (RSS) is a family of formats that describe _feeds_, specially constructed XML documents that allow an interested person to subscribe and receive updates from a particular web service. This portion of the standard library provides tooling to read and create these feeds.

The standard library supports RSS 0.91, 1.0, 2.0, and Atom, a related format. Here are some links to the standards documents for these formats:

* RSS
  * [0.9.1](http://www.rssboard.org/rss-0-9-1-netscape)
  * [1.0](http://web.resource.org/rss/1.0/)
  * [2.0](http://www.rssboard.org/rss-specification)
* [Atom](https://tools.ietf.org/html/rfc4287)

## Installation

Add this line to your application's Gemfile:

```ruby
gem 'rss'
```

And then execute:

    $ bundle

Or install it yourself as:

    $ gem install rss

## Usage

### Consuming RSS

If you'd like to read someone's RSS feed with your Ruby code, you've come to the right place. It's really easy to do this:

```ruby
require 'rss'

feed = RSS::Parser.parse('https://www.ruby-lang.org/en/feeds/news.rss')
puts "Title: #{feed.channel.title}"
feed.items.each do |item|
  puts "Item: #{item.title}"
end
```

As you can see, the workhorse is RSS::Parser#parse, which takes the source of the feed and a parameter that performs validation on the feed. We get back an object that has all of the data from our feed, accessible through methods. This example shows getting the title out of the channel element, and looping through the list of items.

### Producing RSS

Producing our own RSS feeds is easy as well. Let's make a very basic feed:

```ruby
  require "rss"

  rss = RSS::Maker.make("atom") do |maker|
    maker.channel.author = "matz"
    maker.channel.updated = Time.now.to_s
    maker.channel.about = "https://www.ruby-lang.org/en/feeds/news.rss"
    maker.channel.title = "Example Feed"

    maker.items.new_item do |item|
      item.link = "https://www.ruby-lang.org/en/news/2010/12/25/ruby-1-9-2-p136-is-released/"
      item.title = "Ruby 1.9.2-p136 is released"
      item.updated = Time.now.to_s
    end
  end

  puts rss
```

As you can see, this is a very Builder-like DSL. This code will spit out an Atom feed with one item. If we needed a second item, we'd make another block with maker.items.new_item and build a second one.

### Producing an iTunes podcast feed

The iTunes podcast namespace is supported for RSS 2.0 feeds. Set iTunes channel and item elements with the corresponding `itunes_` accessors:

```ruby
require "rss"

rss = RSS::Maker.make("2.0") do |maker|
  maker.channel.title = "Example Podcast"
  maker.channel.link = "https://example.com/podcast"
  maker.channel.description = "An example podcast feed"
  maker.channel.itunes_author = "Example Author"
  maker.channel.itunes_summary = "A podcast created with RSS::Maker"
  maker.channel.itunes_image = "https://example.com/podcast.png"
  maker.channel.itunes_explicit = false
  maker.channel.itunes_type = "serial"
  maker.channel.itunes_owner.itunes_name = "Example Author"
  maker.channel.itunes_owner.itunes_email = "author@example.com"
  maker.channel.itunes_categories.new_category.text = "Technology"

  maker.items.new_item do |item|
    item.title = "Episode 1"
    item.link = "https://example.com/podcast/1"
    item.itunes_title = "Getting started"
    item.itunes_author = "Example Author"
    item.itunes_summary = "The first episode"
    item.itunes_duration = "12:34"
    item.itunes_season = 1
    item.itunes_episode = 1
    item.itunes_episodeType = "full"

    item.enclosure.url = "https://example.com/podcast/1.mp3"
    item.enclosure.length = 12_345_678
    item.enclosure.type = "audio/mpeg"
  end
end

puts rss
```

## Blending RSS 1.0 feeds

`RSS::Blend` reorders items from several RSS 1.0 feeds into one feed. It reads
the original order from each feed's `rdf:Seq` occurrences rather than from the
order of `item` elements, and never merges occurrences merely because they
share a URL.

```ruby
require "rss"

blend = RSS::Blend.new(reference: "morning")
blend.add_source("morning", "morning.rdf", slots: %w[x y a b])
blend.add_source("evening", "evening.rdf", slots: %w[a b])

blend.add_hard("a", "x")
blend.add_hard("b", "x")
blend.add_preference("x", "y", 7, source: "morning")
blend.add_preference("y", "a", 4, source: "morning")
blend.add_preference("y", "b", 5, source: "morning")

result = blend.solve
result.order  # => ["y", "a", "b", "x"]
result.score  # => 9

puts result.to_rss
puts result.report
result.verify_readback!
```

Hard rules are mandatory and are checked for cycles before anything is
emitted (`RSS::Blend::HardCycleError`). Preferences are soft; Blend finds the
maximum-weight set of preferences that can all hold together. `result.report`
lists every dropped preference and the conflicting path that explains it.
When several maximum-weight orders exist, `reference:` (a source name or an
explicit slot array) supplies the original Seq order used to break the tie.

A `slots:` array has one entry per `rdf:Seq` position. `nil` entries become
per-occurrence slots named `source:position`. You can also pass a Hash keyed by
position (`{1 => "x", 2 => "y"}`) or by resource (`{"url" => "x"}`; use an
array such as `{"url" => ["x1", "x2"]}` when the same URL occurs twice).

If a source's Seq has a position whose `item` body was not returned, that slot
is kept in place and marked pending; Blend does not substitute another slot
with the same URL. Pending slots are reported in `result.pending_slots` and in
`result.report`.

```ruby
result.source_order_checks.each do |check|
  puts check.to_s unless check.satisfied?
end
```

`source_order_checks` compares every pair of distinct slots in each source's
Seq and explains why an unsatisfied pair was dropped. `result.diagnostics`
returns the same information as a Hash for machine-readable reports.

For a daily run, `sample/blend_daily.rb` reads a JSON manifest that lists the
feeds, their occurrence mappings, hard rules, and preferences:

```console
$ ruby sample/blend_daily.rb manifest.json \
    --rss blended.rdf --report report.txt \
    --json-report diagnostics.json --verify
```

## Development

After checking out the repo, run `rake test` to run the tests.

To install this gem onto your local machine, run `rake install`. To release a new version, update the version number in `lib/rss/version.rb`, and then run `rake release`, which will create a git tag for the version, push git commits and tags, and push the `.gem` file to [rubygems.org](https://rubygems.org).

## Contributing

Bug reports and pull requests are welcome on GitHub at https://github.com/ruby/rss.

## License

The gem is available as open source under the terms of the [BSD-2-Clause](LICENSE.txt).
