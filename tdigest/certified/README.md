# Certified mergeable quantile summaries

`tdigest.certified` adds a deterministic quantile summary that can be merged
at *any* level of an aggregation tree while carrying a provable rank error.

## Guarantees

- **Integer units, stable identity.** Every window is built from integer
  samples with a stable `window_id`, a monotonic `version` and stable record
  ids. Re-delivered records (duplicate values or retried messages) are counted
  once.
- **Rank error against every original observation.** A window keeps the exact
  `(value, count)` histogram. A root compresses it into deterministic blocks
  of at most `B = floor(epsilon * n)` observations; each value's true rank is
  bracketed by an interval of width at most `B`, i.e. a relative rank error no
  greater than `epsilon`.
- **Tree-independent canonical bytes.** Histogram union is associative and
  compression is a pure function of the union. The same effective windows
  produce byte-identical `to_canonical_bytes()` for any grouping tree, any
  number of retries, and any mix of pruned/full intermediate nodes.
- **Certificates that merge upward.** `certificate()` embeds only lightweight
  window manifests plus hashes (never raw values or per-window summaries).
  `verify_certificate()` re-validates a lower-layer proof after an upward merge.
- **Controlled root memory.** A `prune()`-ed root holds `O(1/epsilon)` blocks
  plus one manifest per source; `memory_budget(source_count)` reports the
  budget from the configured precision and source count.
- **Explicit gaps, never silent downgrades.** A legacy `TDigest.to_dict`
  window, a replacement at a non-retractable boundary, an ambiguous retry
  overwrite or an unverifiable supersession records the offending window id
  in `gap_sources` and forbids certification at the requested precision.

## Typical flow

```python
from tdigest.certified import CertifiedDigest, validate_two_trees

# Leaves: stable record ids make duplicate deliveries harmless.
window = CertifiedDigest.from_window(
    "win-1", version=1, values=[...], epsilon=0.05,
    record_ids=[...])

# Merge at any level; retries and grouping do not change the result.
root = window.merge(other_window, another_window)
cert = root.certificate()          # raises CertificationError on any gap
low, high = root.rank_interval(42)  # proven rank interval over all samples
root.percentile(95)

# Two differently shaped trees must agree byte-for-byte and on the proof.
result = validate_two_trees(windows, partitions=4, retries={0: 2})
assert result                       # canonical_equal and certification_equal
```

## Alternatives, replacements and legacy windows

```python
from tdigest.certified import AlternativeSummary

# Same-precision bulk summary, byte-equivalent to the streaming construction.
alt = AlternativeSummary.from_window("win-1", 1, values, epsilon=0.05)

# Old versions may only be substituted at a retractable boundary.
replaced = node.replace_window("win-1", new_window_record)
node.prune().replace_window(...)     # raises CertificationError

# Old TDigest.to_dict windows stay readable but can never be certified.
legacy = CertifiedDigest.from_legacy_dict("old", 1, tdigest.to_dict())
legacy.percentile(50)                # approximate value still available
legacy.certificate()                 # CertificationError naming "old"
```
