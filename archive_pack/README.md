# Archive handover repacker (`archive_pack`)

Lossless re-packing of selected samples from legacy SEG-Y surveys into
SEG-Y Revision 1 reels that an archive can read directly, built on Segpy.

## What it guarantees

- Every handed-over sample is delivered exactly once; sample values are
  copied byte-for-byte within the source data sample format.
- Each output reel honours the trace cap, and every trace in a reel shares
  sample count, sample interval, data sample format and true start time.
- One physical source trace contributes at most one segment to any reel, so
  a reel never mixes two time bases of the same trace.
- Cuts are only made at the boundaries listed on the handover sheet.
- The number of reels is minimized; extra cuts are allowed when sharing
  reels already needed by other traces.
- All ten `time_scalar`-governed trace-header fields are encoded exactly,
  using `Fraction` arithmetic and the Rev 1 scalar codes only. Nothing is
  rounded; if no legal scalar encodes every field, the range is rejected.
- Each output reel is Rev 1 with the fixed-length trace flag set, and the
  binary reel header, every trace header and the actual samples agree.
- `pack_manifest.json` maps every output reel number and trace back to the
  source reel, physical trace index and source sample range.

## Handover sheet

See `examples/handover_abcd.json`. `permitted_cut_after` uses one-based
sample point numbers ("cut after point 2" separates samples at zero-based
indices 1 and 2). Sample ranges use half-open zero-based indices
(`start_sample` inclusive, `stop_sample` exclusive).

## Time fields and the scalar

Rev 1 stores these ten fields in milliseconds through one shared
`time_scalar`: `uphole_time_at_source`, `uphole_time_at_group`,
`source_static_correction`, `group_static_correction`, `total_static`,
`lag_time_a`, `lag_time_b`, `delay_recording_time`, `mute_start_time`,
`mute_end_time`.

A segment's start time is the true time of its first source sample:

```
start_ms = source_delay_ms + first_sample_offset * sample_interval_us / 1000
```

The packer tries scalar one first, then divisors 10/100/1000/10000, then
multipliers. For example a true `1/2 ms` start is stored as `5` with scalar
`-10`. A `4000 ms` mute end cannot share a fine divisor (it would overflow
the 16-bit field), so segmentations forcing that combination are discarded
rather than rounded.

## Reference outcomes

Running `python demo_archive_pack.py` reproduces the brief:

- A, B, C (6 samples, -1 ms start, 500 us interval; at most 3 samples per
  segment and 3 traces per reel) pack into 3 reels, all segments 2 samples.
- Adding D (same geometry, mute end exactly 4000 ms) gives 5 reels: A in two
  3-sample segments, D in three 2-sample segments.
- A trace whose start time has no exact encoding (e.g. `1/3 ms`) raises
  `UndeliverableRange` naming the source range and un-writable field.

## Modules

- `archive_pack/time_codes.py` - exact scalar encoding/decoding.
- `archive_pack/model.py` - handover items, source traces, segments, plans.
- `archive_pack/planner.py` - legal segment enumeration and minimum-reel
  search (cross-checked against brute force in the tests).
- `archive_pack/dataset.py` - Segpy `Dataset` adapter and source repository.
- `archive_pack/source_reader.py` - build sources from legacy SEG-Y reels.
- `archive_pack/writer.py` - write Rev 1 reels and the provenance manifest.
- `archive_pack/verify.py` - ordinary-reader read-back and coverage checks.

## Tests

```
.venv/bin/pytest test_archive_pack -q
```
