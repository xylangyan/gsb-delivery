# Streaming PStore Extraction

`PStoreExtract::StreamingExtract` produces a new PStore file containing only a
chosen set of root entries, while keeping the exact Ruby object semantics of
the source ledger.

The source ledger is a normal PStore file: one `Marshal.dump` of a root Hash.
Only the value types used by ordinary ledgers are understood: integers,
`true`/`false`/`nil`, symbols, strings (with their encoding), arrays and plain
hashes, including hashes with a default value.

## Usage

From Ruby:

```ruby
require "pstore_extract"

result = PStoreExtract::StreamingExtract.extract(
  source_path: "old_ledger.pstore",
  dest_path:   "audit_ledger.pstore",
  keys:        [:submission, :meta]
)
result.delivered_keys # root keys actually written, in source order
result.faults         # entries that could not be fully delivered
```

From the command line:

```sh
bin/pstore-extract old_ledger.pstore audit_ledger.pstore --keys :submission,:meta
bin/pstore-extract old_ledger.pstore audit_ledger.pstore --manifest keys.txt
```

A manifest lists one root key per line. A line starting with `:` is a Symbol;
any other non-empty, non-`#` line is a UTF-8 String key.

The destination is an ordinary PStore file and opens in a plain read-only
transaction:

```ruby
PStore.new("audit_ledger.pstore").transaction(true) { |store| store.keys }
```

## Guarantees

- Root entries are emitted in the source ledger's order, regardless of the
  order in which keys are requested.
- Values and string encodings are preserved; the emitted bytes match a fresh
  `Marshal.dump` of the delivered subset.
- Object sharing and object identity are preserved. Two references to one
  source object stay one object, while equal but separately created strings
  stay separate. This covers an object first written inside an excluded entry
  and only referenced through a delivered one.
- Self references and references back to the ledger root are preserved. A
  back reference to the root resolves to the new subset root.
- Symbols are re-registered on demand, so a symbol first defined in a
  discarded entry is still recognised wherever delivered data uses it.
- Large string payloads are streamed positionally with `IO.copy_stream`; the
  structural scan reads only a small buffer, so delivering a message of N
  bytes never needs a Ruby string of N bytes in memory.

## Fault handling

`result.faults` describes anything that could not be delivered.

- `missing_key`: a requested root key is absent; other entries are still
  delivered.
- `dangling_reference`: a delivered value contains an object link whose target
  cannot be read or lives only inside an excluded entry. The fault carries the
  byte position and the affected root keys; those entries are held back while
  unaffected entries are still delivered.
- `broken_structure`: the byte stream is damaged. Entries whose position and
  full structure were confirmed before the damage are still delivered; entries
  from the damaged point on are held back and their index range is reported.
- `trailing_bytes`: readable bytes exist after the root object; they are not
  used and the located entries are delivered normally.

The CLI exits non-zero when a `dangling_reference` or `broken_structure`
fault is reported, and zero when only recoverable `missing_key`/
`trailing_bytes` conditions occurred.
