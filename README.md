# Open3

Open3 gives you access to stdin, stdout, and stderr when running other
programs.

## Installation

Add this line to your application's Gemfile:

```ruby
gem 'open3'
```

And then execute:

    $ bundle

Or install it yourself as:

    $ gem install open3

## Usage

Open3 grants you access to stdin, stdout, stderr and a thread to wait for the child process when running another program.
You can specify various attributes, redirections, current directory, etc., of the program in the same way as for Process.spawn.

- Open3.popen3 : pipes for stdin, stdout, stderr
- Open3.popen2 : pipes for stdin, stdout
- Open3.popen2e : pipes for stdin, merged stdout and stderr
- Open3.capture3 : give a string for stdin; get strings for stdout, stderr
- Open3.capture2 : give a string for stdin; get a string for stdout
- Open3.capture2e : give a string for stdin; get a string for merged stdout and stderr
- Open3.pipeline_rw : pipes for first stdin and last stdout of a pipeline
- Open3.pipeline_r : pipe for last stdout of a pipeline
- Open3.pipeline_w : pipe for first stdin of a pipeline
- Open3.pipeline_start : run a pipeline without waiting
- Open3.pipeline : run a pipeline and wait for its completion

## Archive Handoff Entry

`ArchiveHandoff::Runner` is the restartable entry for the fetch, normalize, and
package commands. All recovery state, package bytes, manifests, receipts, and
stderr chunks are persisted under the state directory, so a new OS process can
construct another runner with the same directory and call `resume`.

```ruby
runner = ArchiveHandoff::Runner.new(
  "var/archive-handoff",
  submit: ->(submission) { warehouse.submit_package(submission) },
  query: ->(key) { warehouse.find_attempt(key) },
  settle: ->(key) { warehouse.settle_attempt(key) },
  commands: {
    fetch: ["ruby", "commands/fetch_volume.rb"],
    normalize: ["ruby", "commands/normalize_volume.rb"],
    package: ["ruby", "commands/package_segment.rb"]
  }
)

runner.resume("OCC-20261126-OLD")
runner.snapshot("OCC-20261126-OLD")
runner.stderr_text("package")
runner.stop!
```

The runner verifies the complete trailer manifest before acknowledging volume
receipt or submitting segments. An unseen query is treated as possibly
in-flight; the same `(occurrence_id, segment, attempt)` is then settled. A
committed settlement adopts that attempt and its receipt/digest. An
`unpostable` settlement fences only that attempt, after which the segment is
repackaged and submitted with a new attempt number. Lost settlement responses
remain recoverable by querying or re-settling the same attempt.

Packages whose bytes already match the previous actual digest are reused. When the
previous segment changes attempts/digests, the next package is rebuilt with a
new issue number. `snapshot(occurrence_id)` reports the adopted submission,
receipt, package version/issue, and actual digest for every segment. Late
receipts from older attempts are ignored and never move state backwards or
affect another occurrence, including a correction volume.

A CLI is available at `exe/archive-handoff`:

```sh
ruby exe/archive-handoff resume \
  --state var/archive-handoff \
  --occurrence OCC-20261126-OLD \
  --config config/archive_handoff.rb
```

The config file is Ruby code whose last expression is a hash containing
`:warehouse` or `:gateway`, and optionally `:commands`.

## Development

After checking out the repo, run `bin/setup` to install dependencies. Then, run `rake test` to run the tests. You can also run `bin/console` for an interactive prompt that will allow you to experiment.

To install this gem onto your local machine, run `bundle exec rake install`. To release a new version, update the version number in `version.rb`, and then run `bundle exec rake release`, which will create a git tag for the version, push git commits and tags, and push the `.gem` file to [rubygems.org](https://rubygems.org).

## Contributing

Bug reports and pull requests are welcome on GitHub at https://github.com/ruby/open3.
