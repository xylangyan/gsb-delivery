# Offline Atom archive

`Feedjira::OfflineArchive` builds a small set of plain Atom pages from
already-verified Atom originals, so another team can open them with Feedjira
while offline.

```ruby
catalog = JSON.parse(File.read("catalog.json"))
verification = JSON.parse(File.read("verification.json"))   # optional
resources = JSON.parse(File.read("resources.json"))         # optional

result = Feedjira::OfflineArchive::Archive.new(
  catalog,
  base_dir: "path/to/sources",
  verification: verification,
  resources: resources
).build

result.pages        # written Atom pages (also kept in memory)
result.notices      # signatures the reviewer has to see
result.conflicts    # requested page groupings that could not be satisfied
result.retractions  # retractions that were actually applied
result.verification # re-parse check of the emitted pages
```

## Guarantees

- **Entries are byte-exact.** Each excerpt is sliced out of the original file
  by byte range; the page writer never re-serialises it. A signed entry
  therefore keeps the exact bytes the verifier signed.
- **Only direct children of `<feed>` are entries.** Nested elements that happen
  to be named `entry` are never picked up.
- **One source is one file.** A reprint that reuses another source's link is
  still a distinct source.
- **Pages are parsimonious.** Entries are packed into as few pages as the
  constraints allow.
- **Impossible groups are reported, never guessed at.** A requested same-page
  group that cannot be built comes back in `result.conflicts` with the original
  entry bytes and the clashing constraint. The rest of the issue is still
  written.
- **Signatures that cannot be satisfied by an excerpt are reported.** If a
  signature covers content that was not selected, or requires the whole original
  page, the entry is surfaced in `result.notices` with the covered positions
  instead of being filed.

## Catalog format

```jsonc
{
  "issues": [
    {
      "id": "issue-1",                       // required
      "cutoff": "2026-11-21T00:00:00Z",      // required, archived edition boundary
      "page_limit": 4,                       // maximum entries per page
      "sources": [
        {
          "name": "alpha",                   // identity of the publication
          "file": "alpha-1120.atom",         // relative to base_dir
          "uri": "https://alpha.example/2026/11/20.atom"  // base for relative refs
        }
      ],
      "entries": [
        {
          "source": "alpha",
          "id": "tag:alpha,2026:e1",
          "version": "2026-11-20T09:00:00Z", // pin a version (optional)
          "confirmed": true,                  // only confirmed entries are filed
          "include": true,                    // set false to hold back
          "resources": {}                     // per-entry resource overrides
        }
      ],
      "must_group": [
        ["alpha:tag:alpha,2026:e1", "alpha:tag:alpha,2026:e2"]
      ],
      "retractions": [
        {
          "notice_source": "digest",          // who published the notice
          "original_source": "alpha",         // effective source that retracts
          "target_source": "alpha",
          "target_id": "tag:alpha,2026:e3",
          "target_version": "2026-11-20T10:00:00Z",
          "published_at": "2026-11-20T15:00:00Z",
          "effective": true,
          "forwarded": true,
          "reason": "factual error",
          "object": "tag:digest,2026:notice"
        }
      ]
    }
  ]
}
```

A source may appear more than once (for example the original publishing the same
Atom ID again the next day). The archived copy is the newest version at or
before the issue `cutoff`; a version published after the cutoff is not part of
the archived edition.

## Retraction rules

- A retraction removes only the entry it names, never a sibling that shares a
  link.
- A retraction forwarded by an aggregator still retracts the original; the
  original publication source is recorded as `effective_source` and the
  aggregator as `published_by`.
- A retraction pinned to `target_version` removes only that version. A newer
  version of the same Atom ID, published later, is left alone.
- An unpinned retraction removes the newest version that existed when the notice
  was published.

## Page composition

- Every entry in a `must_group` lands on one page.
- A page holds at most `page_limit` entries.
- Two entries that inherit the same prefix for different namespace URIs cannot
  share a page, unless the prefix is rebound inside one of the entries — then
  the carried bytes keep their own meaning and only the other binding is
  declared on the page.
- A page carries one `xml:base`. Entries whose relative references need
  different bases, or an entry that needs no base at all, are split.

## Signatures

The caller supplies signature verification. The planner reads, per entry:

```jsonc
{
  "verified": true,
  "scope": {
    "whole_page": false,                    // signature covers the whole original
    "line_ranges": [[1, 4]],                // line ranges the signature covers
    "unselected_paths": ["entry/content/p[2]"]
  }
}
```

`verified: false`, a whole-page scope, or any covered range outside the entry,
produces a notice instead of a filed entry. A verified signature whose scope is
inside the entry is filed normally.

## Resources

`resources` maps an original reference (relative, or the absolute URL it
resolves to through the inherited `xml:base` chain) to a local file:

```json
{
  "audio/a1.mp3": "attachments/audio/a1.mp3",
  "https://alpha.example/2026/11/20/audio/a1.mp3": "attachments/audio/a1.mp3"
}
```

Entry bytes are never rewritten, so a relative enclosure keeps its original
reference. Each page result exposes `attachments`, the resolved reference/local
file pairs, so an offline reader can follow them without the network.
