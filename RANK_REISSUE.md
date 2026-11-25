Rank reissue index
==================

`bottleneck.reissue.RankReissueIndex` indexes a historical float64 reading
array and the `move_rank` values that were published from it. Each correction
slip is compared with that same published report, independently of other
slips.

```python
import bottleneck as bn

index = bn.RankReissueIndex(values, window=24, issued=published)
slip = index.slip({100: 3.5, 101: float("nan")}, name="slip-1")

slip.count(start=90, stop=130)      # samples whose rank must be reissued
slip.earliest(start=90, stop=130)   # first such sample, or None
slip.changed_positions              # every changed sample for this slip
slip.corrections_in(start=90, stop=130)
```

Queries use closed intervals by default; pass `inclusive=False` for open
bounds. A sample whose issued and corrected ranks are both NaN is treated as
unchanged, matching how missing measurements are inherited.

`CorrectionArchive` keeps several named slips over one shared index:

```python
archive = bn.CorrectionArchive(values, window=24, issued=published)
archive.add("slip-1", {100: 3.5})
archive.add("slip-2", {101: 4.0})
archive.count("slip-1", 0, 500)
```
