# Kiba ETL

[![Gem Version](https://badge.fury.io/rb/kiba.svg)](http://badge.fury.io/rb/kiba)
[![Build Status](https://github.com/thbar/kiba/actions/workflows/ci.yml/badge.svg)](https://github.com/thbar/kiba/actions)

Writing reliable, concise, well-tested & maintainable data-processing code is tricky.

Kiba lets you define and run such high-quality ETL ([Extract-Transform-Load](http://en.wikipedia.org/wiki/Extract,_transform,_load)) jobs using Ruby.

:warning: Kiba is considered "done". It works in its current form, and gets maintenance update (mostly ensuring its compatibility with [releases of Ruby](https://www.ruby-lang.org/en/downloads/branches/) and following their EOL lifecycle).

## Getting Started

Head over to the [Wiki](https://github.com/thbar/kiba/wiki) for up-to-date documentation.

**If you need help**, please [ask your question with tag kiba-etl on StackOverflow](http://stackoverflow.com/questions/ask?tags=kiba-etl) so that other can benefit from your contribution! I monitor this specific tag and will reply to you.

[Kiba Pro](https://www.kiba-etl.org/kiba-pro) customers get priority private email support for any unforeseen issues and simple matters such as installation troubles. Our consulting services will also be prioritized to Kiba Pro subscribers. If you need any coaching on ETL & data pipeline implementation, please [reach out via email](mailto:info@logeek.fr) so we can discuss how to help you out.

You can also check out the [author blog](https://thibautbarrere.com) and [StackOverflow answers](http://stackoverflow.com/questions/tagged/kiba-etl).

## Supported Ruby versions

Kiba currently supports Ruby 3.1+, JRuby 10+ and TruffleRuby. See [test matrix](https://github.com/thbar/kiba/actions).

## ETL consulting & commercial version

**Consulting services**: if your organization needs guidance on Kiba / ETL implementations, we provide consulting services. Contact at [https://www.logeek.fr](https://www.logeek.fr).

**Kiba Pro**: for vendor-backed ETL extensions, check out [Kiba Pro](https://www.kiba-etl.org/kiba-pro).

## Multi-route receiving

`Kiba::Fanout` is an opt-in destination for jobs that deliver one row to
several isolated receiver groups. Jobs that do not use it keep the normal
`Kiba.run` behavior and destination protocol unchanged.

```ruby
require "kiba"

work_order = Kiba::Fanout::WorkOrder.build do
  order_key "order_id"

  manifest do
    node "lines", bytes: 48, whole: true
    node "adjustments", bytes: 16, whole: true
  end

  group :picking do
    destination Picking
    destination PickingReview
    reference "adjustments"
    mutable "lines.*.picked"
    require_mutation "lines.*.picked"
    on_write writes: ["lines.*.picked"]
  end

  group :settlement do
    destination Settlement
    on_write reads: ["lines.*.amount"]
  end

  group :archive do
    destination Archive
    retain_first "lines"
    on_close reads: ["lines.*.amount"]
  end
end

Kiba.run(Kiba.parse do
  source Orders
  transform SplitLines
  destination Kiba::Fanout::Multiplexer, work_order, on_init: ->(mux) { @mux = mux }
end)
```

Rules implemented by the interface:

- Each group receives its own delivery-time view. `write` annotations never
  reach another group; `require_mutation` paths are shared by every receiver
  in one group.
- `reference` fields are delivery snapshots shared by original identity and
  content inside a group. The same list on two orders shares one snapshot;
  two equal-but-distinct lists never merge. Read-only snapshots cost no bytes.
- `retain` (or `retain_first`) keeps a delivery-time copy for a receiver that
  copies its paper page at `close`. Later rows cannot rewrite the retained
  page. `retain_first` bills only the first page the receiver kept.
- `mutable` detail nodes are copied once per group and per delivery. A
  transform that yields one row, mutates it and returns another delivers two
  independently isolated variants.
- `close_only_if_received` gates close-yielded marker rows to groups that
  already received the named order.
- `manifest` declares fixed billable bytes per logical detail node. The ledger
  counts one version per independently mutable or retained copy; field names,
  references to an existing copy and read-only originals are free. `whole`
  nodes are never split. `mux.minimum_copy_bytes` is the cumulative cost and
  `mux.budget[:decisions]` names the version that determined each charge.
- `require_from` is a declared cross-group visibility request. It is reported
  as a conflict with the original order key and nodes each side actually saw;
  compliant routes can still be exercised with `conflict_mode = :report`.
- Existing jobs without Fanout are unaffected. A runnable example is in
  `examples/fanout_receiving.rb`.

## License

Copyright (c) LoGeek SARL. Kiba is an Open Source project licensed under the terms of
the LGPLv3 license.  Please see <http://www.gnu.org/licenses/lgpl-3.0.html> for license text.

## Contributing & Legal

(agreement below borrowed from [Sidekiq Legal](https://github.com/mperham/sidekiq/blob/master/Contributing.md))

By submitting a Pull Request, you disavow any rights or claims to any changes submitted to the Kiba project and assign the copyright of those changes to LoGeek SARL.

If you cannot or do not want to reassign those rights (your employment contract for your employer may not allow this), you should not submit a PR. Open an issue and someone else can do the work.

This is a legal way of saying "If you submit a PR to us, that code becomes ours". 99.9% of the time that's what you intend anyways; we hope it doesn't scare you away from contributing.
