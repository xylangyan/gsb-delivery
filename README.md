# ddsketch

This repo contains the Python implementation of the distributed quantile sketch
algorithm DDSketch [1]. DDSketch has relative-error guarantees for any quantile
q in [0, 1]. That is if the true value of the qth-quantile is `x` then DDSketch
returns a value `y` such that `|x-y| / x < e` where `e` is the relative error
parameter. (The default here is set to 0.01.)  DDSketch is also fully mergeable,
meaning that multiple sketches from distributed systems can be combined in a
central node.

Our default implementation, `DDSketch`, is guaranteed [1] to not grow too large
in size for any data that can be described by a distribution whose tails are
sub-exponential.

We also provide implementations (`LogCollapsingLowestDenseDDSketch` and
`LogCollapsingHighestDenseDDSketch`) where the q-quantile will be accurate up to
the specified relative error for q that is not too small (or large). Concretely,
the q-quantile will be accurate up to the specified relative error as long as it
belongs to one of the `m` bins kept by the sketch.  If the data is time in
seconds, the default of `m = 2048` covers 80 microseconds to 1 year.

## Installation

To install this package, run `pip install ddsketch`, or clone the repo and run
`python setup.py install`. This package depends on `numpy` and `protobuf`. (The
protobuf dependency can be removed if it's not applicable.)

## Usage
```
from ddsketch import DDSketch

sketch = DDSketch()
```
Add values to the sketch
```
import numpy as np

values = np.random.normal(size=500)
for v in values:
  sketch.add(v)
```
Find the quantiles of `values` to within the relative error.
```
quantiles = [sketch.get_quantile_value(q) for q in [0.5, 0.75, 0.9, 1]]
```
Merge another `DDSketch` into `sketch`.
```
another_sketch = DDSketch()
other_values = np.random.normal(size=500)
for v in other_values:
  another_sketch.add(v)
sketch.merge(another_sketch)
```
The quantiles of `values` concatenated with `other_values` are still accurate to within the relative error.

## Budgeted rollup planning

A delayed rollup can keep one positive/negative sketch pair per tenant and use
the planner to decide which tenant percentiles it can issue from one minute
under a shared bucket budget.

```python
from ddsketch import CandidateConfiguration, Capability, TenantContract
from ddsketch import WindowSketch, plan_rollup

contract = TenantContract(
    "tenant-a",
    percentiles=[0.5, 0.9, 0.99],
    error_bounds={0.5: 0.01, 0.9: 0.02, 0.99: 0.02},
    priorities={0.5: 1, 0.9: 5, 0.99: 10},
)
window = WindowSketch(
    window="2026-11-22T10:31",
    tenant="tenant-a",
    old=old_sketch,
    new=new_sketch,
    capability=Capability.from_sketch(old_sketch),
    incoming_range=(0, 20),  # counts that may still arrive, values unknown
)
plan = plan_rollup(
    [window],
    [contract],
    [
        CandidateConfiguration("tight", 64, "lowest", 0.01),
        CandidateConfiguration("roomy", 256, "lowest", 0.01),
    ],
    budget=512,
)
for item in plan.issuance_schedule():
    print(item["tenant"], item["percentile"], item["value"])
for item in plan.shortfalls:
    print(item.tenant, item.percentile, item.reason)
```

The plan maximizes the number of certified reports first, then the summed
business priority of certified reports, then minimizes capacity used. A report
is certified only when the actual merged bucket supports the contract:

* its bucket was not collapsed;
* the bucket value is within the tenant's relative-error bound;
* every admissible incoming-volume scenario keeps the requested rank in a
  bucket that satisfies the contract (including exact-zero reports when the
  tenant requires them).

`ReportDecision.evidence` records the total count, the rank used, the bucket
key and count, the bucket's source-key range, and the value bounds, so any
issued report can be recomputed from the merged sketch. Uncertified reports
appear in `RollupPlan.shortfalls` with their reason and, for incoming-volume
failures, a concrete `extra`/`placement`/`future_rank` witness. Each candidate
configuration carries its own counterexample in
`RollupPlan.shortfall_witnesses()`.

An old summary may already have lost an edge bucket. `Capability` records that
mapping and the damaged source range; if the requested rank later falls in
that damaged range, allocating more buckets to the new window cannot recover
the lost distribution, and the plan says so with `inherited_collapse=True`.
Ordinary `merge()` calls are unchanged; `merge_without_plan()` is a convenience
wrapper for callers without a contract or a budget.

### Rollup audit example

Two tenants of one minute illustrate the two distinct failures:

```python
# Tenant A: the old summary already folded its low edge.
a = WindowSketch(
    "10:31", "a", old_a,
    new=new_a,  # new counts push the lower ranks into the folded edge
    capability=Capability.from_sketch(old_a),
)
# Tenant B: the future rank stays far from either edge under both volumes.
b = WindowSketch(
    "10:31", "b", old_b,
    scenarios=[VolumeScenario("calm", 0, "none"),
               VolumeScenario("low-20", 20, "low"),
               VolumeScenario("high-20", 20, "high")],
)
plan = plan_rollup([b, a], [contract_a, contract_b], candidates, budget=4096)
```

The plan serves the maximum number of certified reports before business
priority. B's median is issuable because both volume extremes keep its rank
away from the edges. A's low and median reports land in the previously folded
range: each shortfall carries `inherited_collapse=True`, the damaged source
range, and the merged future rank. Allocating a larger candidate for A does not
change that verdict, and `plan.shortfall_witnesses()` shows each candidate's
own counterexample. For a known new summary, use
`VolumeScenario(..., sketch=new_sketch)` instead of an `incoming_range`; the
planner merges that sketch into the scenario before checking the rank.

## Development

To work on ddsketch a Python interpreter must be installed. It is recommended to use the provided development
container (requires [docker](https://www.docker.com/)) which includes all the required Python interpreters.

    docker-compose run dev

Or, if developing outside of docker then it is recommended to use a virtual environment:

    pip install virtualenv
    virtualenv --python=3 .venv
    source .venv/bin/activate


### Testing

To run the tests install `riot`:

    pip install riot

Replace the Python version with the interpreter(s) available.

    # Run tests with Python 3.9
    riot run -p3.9 test

### Release notes

New features, bug fixes, deprecations and other breaking changes must have
release notes included.

To generate a release note for the change:

    riot run reno new <short-description-of-change-no-spaces>

Edit the generated file to include notes on the changes made in the commit/PR
and add commit it.


### Formatting

Format code with

    riot run fmt


### Type-checking

Type checking is done with [mypy](http://mypy-lang.org/):

    riot run mypy


### Type-checking

Lint the code with [flake8](https://flake8.pycqa.org/en/latest/):

    riot run flake8


### Protobuf

The protobuf is stored in the go repository: https://github.com/DataDog/sketches-go/blob/master/ddsketch/pb/ddsketch.proto

Install the minimum required protoc and generate the Python code:

```sh
docker run -v $PWD:/code -it ubuntu:18.04 /bin/bash
apt update && apt install protobuf-compiler  # default is 3.0.0
protoc --proto_path=ddsketch/pb/ --python_out=ddsketch/pb/ ddsketch/pb/ddsketch.proto
```


### Releasing

1. Generate the release notes and use [`pandoc`](https://pandoc.org/) to format
them for Github:
```bash
    git checkout master && git pull
    riot run -s reno report --no-show-source | pandoc -f rst -t gfm --wrap=none
```
   Copy the output into a new release: https://github.com/DataDog/sketches-py/releases/new.

2. Enter a tag for the release (following [`semver`](https://semver.org)) (eg. `v1.1.3`, `v1.0.3`, `v1.2.0`).
3. Use the tag without the `v` as the title.
4. Save the release as a draft and pass the link to someone else to give a quick review.
5. If all looks good hit publish


## References
[1] Charles Masson and Jee E Rim and Homin K. Lee. DDSketch: A fast and fully-mergeable quantile sketch with relative-error guarantees. PVLDB, 12(12): 2195-2205, 2019. (The code referenced in the paper, including our implementation of the the Greenwald-Khanna (GK) algorithm, can be found at: https://github.com/DataDog/sketches-py/releases/tag/v0.1 )
