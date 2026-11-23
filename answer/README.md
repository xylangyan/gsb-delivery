# Rubyzip byte-range relabel — writable revision and per-entry verdicts

## What was requested

Relabel a few entries (display name and comment only), keep every compressed
payload byte-for-byte, and stay inside the receiver's per-entry allowed byte
ranges. New names may use the receiver's permitted encodings, and listed
metadata extra fields may be deleted. The revised file must read identically
through both public entry points (`Zip::File` and `Zip::InputStream`), keep the
original payload CRCs, and end with a valid directory.

## What I could and could not use

The task's own `question.json` (which holds the concrete archive paths, the
absolute offsets, and the allowed ranges) is outside this session's readable
sandbox: the file exists and is mode `-rw-r--r--`, but every read returns
`EPERM` from the seatbelt profile. `attempt-1/` is likewise denied. The
workspace contains only the stock rubyzip 3.8.0 snapshot, so **no edit could be
applied to the real sample bytes.** What follows is therefore the writable
mechanism plus faithful reproductions of the A/B cases, each with real output
and real reader checks. Point `relabel.rb` at the real archive and a JSON plan
and it applies the same logic to it.

## The result in one line

A rename or comment change is writable **only when the growth in each header
can be paid for out of that same header's adjustable padding**, because moving
a local header by even one byte breaks the streaming reader while the directory
reader still works.

## Why the obvious approaches fail

**Rubyzip's writer (`Zip::File#commit`, `put_next_entry`) is unusable for the
patch itself.** It reorders and renormalises extra fields. On
`zip64-sample.zip`, `Entry#write_local_entry` emits `Ux` before `UT`, while the
original has `UT` before `Ux`; on `rubycode.zip` it also rewrites the CRC-bearing
local fields. Any writer that recomputes headers moves bytes outside the allowed
ranges even when no logical field changed. Rubyzip is used here only to parse and
to verify.

**"Move A's local header three bytes earlier into padding" cannot satisfy both
readers.** I built a two-entry archive, inserted a 4-byte gap before the second
entry, shifted the central directory and EOCD accordingly, and read it both ways:

```
Zip::File:        p.txt=PPPPPPPPPP, a.txt=AAAAAAAAAA
Zip::InputStream: p.txt=PPPPPPPPPP
```

`Zip::File` trusts the central directory, so it reads both. `Zip::InputStream`
seeks to `local_header_offset + header_size + compressed_size` after each entry;
the gap makes it land off the next signature and it stops silently. That is the
prompt's "A cannot meet both requirements" trap, and it is mechanical, not a
matter of policy.

**The offset-width trap is real but avoidable by never moving the header.** A
4-byte central offset field that would need the 8-byte Zip64 value grows the
record by 8 bytes (the Zip64 extra record is `id + len + 8` = 12 bytes versus the
4-byte field). The design below never rewrites a local-header offset, so the
width never changes; `apply` asserts this as a post-condition.

## The mechanism

Each header is rewritten as one length-neutral splice:

* the 30-byte local static header / 46-byte central static header is copied
  verbatim; only the name length, extra length and comment length fields change;
* the name (and comment, centrally) is replaced;
* the same number of bytes is removed from padding **in the same header**,
  authorised by the plan as `fund_local` / `fund_central`.

Consequences worth stating plainly:

* **Payloads are never touched.** CRC, compressed size and uncompressed size are
  copied, not computed. No recompression happens.
* **No absolute offset changes.** The directory boundary, every entry's
  local-header offset, and every untouched byte are identical. `apply` verifies
  output length and the full offset list, then walks the streaming chain.
* **Funding never crosses headers.** The local extra field and the central extra
  field are separate byte runs in separate places; bytes freed in one cannot pay
  for growth in the other.
* **Funding must round-trip.** Trimming inside an *unknown/private* field is
  byte-stable. Draining a *structured* field is not: I trimmed 2 bytes from the
  Zip64 sample's `UT` field and produced a raw-byte-valid header, but Rubyzip
  re-expands `UT` on read, so the streamed next offset landed at 309 instead of
  307. `apply` now refuses that with `streaming reader would break`, which is why
  the funding allow-list must name only fields that are genuinely expendable
  (the receiver's "可删掉的元数据项") and not merely present.

## Per-entry verdicts

### A — new name is three bytes longer, with padding in its own headers

Reproduction: `a.txt` and `b.txt`, each with an 8-byte private pad in both
headers; A renamed to `a-xx.txt`.

```
ok   a.txt: local name grew 3, trimmed 3 bytes of local padding; header stays at 0
ok   a.txt: central record trimmed 3 bytes of padding; local-header offset stays 0
ok   "a.txt": "a.txt" -> "a-xx.txt"; comment "" -> ""

Zip::File:        a-xx.txt=<orig payload>, b.txt=<orig payload>
Zip::InputStream: a-xx.txt=<orig payload>, b.txt=<orig payload>
size 261 -> 261, directory 121 -> 121, CRCs unchanged
```

Verdict: **writable.** The growth is exactly covered, no offset moves, both
readers agree. Note the local pad is trimmed by 3, leaving a shorter but valid
record (`content_length` 8 -> 5); the test asserts the record survives with the
same id.

### A-strict — only B has spare bytes

Same archive, but A has no padding and B's headers are outside the allowed
ranges:

```
stop a.txt: local header needs 3 bytes but the authorised padding can supply only 0 (no matching extra field)
stop a.txt: central record needs 3 bytes but the authorised padding can supply only 0 (no matching extra field)
```

Verdict: **not writable in the allowed range.** The occupied space is A's own
local header (30 bytes static + 5-byte name) and central record (46 bytes static
+ 5-byte name); neither contains removable metadata. The only free space is in B,
which the receiver did not authorise.

### B — only the entry nothing else references

`b.txt` renamed to `b-xx.txt` with comment `display name only`, paid for from
B's own pads:

```
ok   b.txt: local name grew 3, trimmed 3 bytes of local padding; header stays at 53
ok   b.txt: central record trimmed 20 bytes of padding; local-header offset stays 53
ok   "b.txt": "b.txt" -> "b-xx.txt"; comment "" -> "display name only"

Zip::File:        a.txt=<orig>, b-xx.txt=<orig>
Zip::InputStream: a.txt=<orig>, b-xx.txt=<orig>
```

Verdict: **writable.** A is untouched, including its offset, CRC and bytes.

### Shared local header — one header indexed by two directory records

When two central records point at one local header, renaming is only safe if the
local header does not move; the planner still refuses when it cannot prove that,
naming the references:

```
stop <name>: name is used by 2 directory records that share one local header at <offset>;
             renaming the local header would also invalidate every other record pointing at it
```

Verdict: **refuse unless every referencing record is edited consistently**;
independent entries elsewhere in the archive remain editable.

## Files

* `answer/zip_relabel.rb` — parser, planner and length-neutral splice engine.
  `survey(bytes)` produces the per-entry offset/extra-field report;
  `apply(bytes, edits, allowed:)` returns `[revised_bytes, plan]` or raises
  `ZipRelabel::Refused` with reasons.
* `answer/relabel.rb` — CLI: `ruby relabel.rb ARCHIVE PLAN.json --report
  [--apply OUT.zip]`.
* `answer/fixture_zip.rb` — independent minimal writer for the test fixtures.
* `answer/demo.rb` — runs the A, A-strict and B reproductions and prints both
  readers' views.
* `answer/zip_relabel_test.rb`, `answer/relabel_cli_test.rb` — 14 tests, all
  passing, covering the two reader paths, CRC preservation, UTF-8/CP437 flag
  handling, partial pad trimming, the shared-header refusal, and the
  streaming-chain guard.

## What to send back for the real samples

1. The four absolute values per entry: local-header offset, local-header length,
   central-record offset, central-record length.
2. Which extra-field IDs in each header are expendable, and their exact byte
   lengths (the `X` in the `survey` output).
3. The permitted encodings per entry.

With those, `survey` reproduces the same table for the real archive and `apply`
either produces a same-size revision that both readers accept, or returns the
exact byte shortfall and names the other entries that can still be processed
independently.
