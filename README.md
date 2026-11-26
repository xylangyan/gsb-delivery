# FileUtils

Namespace for several file utility methods for copying, moving, removing, etc.

## Installation

Add this line to your application's Gemfile:

```ruby
gem 'fileutils'
```

And then execute:

    $ bundle

Or install it yourself as:

    $ gem install fileutils

## Usage

Just call `FileUtils` methods. For example:

```ruby
FileUtils.mkdir("somedir")
# => ["somedir"]

FileUtils.cd("/usr/bin")
FileUtils.pwd
# => "/usr/bin"
```

You can find a full method list in the [documentation](https://docs.ruby-lang.org/en/master/FileUtils.html).

## Archive pre-planning

`FileUtils.plan_archive` is a read-only planner for the archive staging
workflow.  It reads a manifest (sign-off sheet plus working-volume state)
and reports the minimum supplier download within the space quota, the group
order to follow, and which preview attachments to keep as staging hard
links.  It never reads or writes actual files.

```ruby
plan = FileUtils.plan_archive(
  "quota" => 6,
  "attachments" => (1..6).to_h {|i| [i.to_s, 1] },
  "verification_size" => 3,
  "queues" => [
    {"name" => "preview", "kind" => "preview", "items" => (1..6).map(&:to_s)},
    {"name" => "a", "kind" => "formal",
     "packages" => [%w[3 4], %w[1 2], %w[5 6]]},
    {"name" => "b", "kind" => "formal",
     "packages" => [%w[6 1], %w[2 3], %w[4 5]]}
  ]
)

plan.minimum_download_bytes # => 10
plan.peak_occupancy         # => 6
puts plan                   # step-by-step operator plan
```

A manifest can also be supplied as JSON or YAML:

    $ bin/archive-plan manifest.json
    $ bin/archive-plan manifest.yml --json

The manifest schema is documented at `FileUtils::ArchivePlan`.  Explicit
queue preconditions are supported through a `preconditions` array, and names
for the same physical item must be declared with `aliases` (or
`same_as`); the planner deliberately does not infer identity from names or
sizes.

## Contributing

Bug reports and pull requests are welcome on GitHub at https://github.com/ruby/fileutils.

## License

The gem is available as open source under the terms of the [2-Clause BSD License](https://opensource.org/licenses/BSD-2-Clause).
