# WKB Exchange

`RGeo::WKRep::WKBExchange` plans byte-preserving delivery of the atomic members
of a WKB or EWKB archive to downstream readers with different contracts. It is
useful when one side only accepts standard WKB 1.2 and another only accepts
EWKB, and when a nested `GeometryCollection` must be split according to a
business manifest rather than re-encoded as one geometry.

The normal `WKBParser` and `WKBGenerator` are unaffected.

## Model

- **Archive**: parses the source without constructing RGeo geometry objects. It
  retains every node's exact bytes, byte order, dialect, explicit or inherited
  SRID, and Z/M layout.
- **Atom**: a Point, LineString, or Polygon leaf. `GeometryCollection` and
  `Multi*` wrappers are traversed; leaves are routed in original preorder.
- **Reader**: one destination contract: one dialect, one or more accepted
  dimension configurations, and an SRID convention.
- **Packet**: a standalone WKB `GeometryCollection` for one reader. Every packet
  has one fixed dialect and dimension configuration. It may carry one SRID at
  its start, or (for EWKB) allow children to retain distinct explicit SRIDs.
- **Manifest**: which leaves go to which readers, plus the rewrite alternatives
  the business list permits.
- **Cost model**: split cost, per-packet cost, per-byte cost, and per-atom
  rewrite costs.

## Example

```ruby
archive = RGeo::WKRep::WKBExchange::Archive.parse(bytes, srid: 4326)

manifest = RGeo::WKRep::WKBExchange::Manifest.new
manifest.route("/0", to: :legacy)
manifest.route("/1", to: :modern)

# The legacy reader has no M, but the business permits this member's measure to
# be delivered as its Z coordinate.
manifest.permit("/1", reader: :legacy, dimensions: :xyz, axes: { z: :m })

readers = {
  legacy: {
    dialect: :wkb12,
    dimensions: [:xyz, :xym],
    srid: 4326
  },
  modern: {
    dialect: :ewkb,
    dimensions: [:xy, :xyz, :xym, :xyzm],
    srid: 4326,
    srid_prelude: :ewkb
  }
}

cost_model = RGeo::WKRep::WKBExchange::CostModel.new(
  split_cost: 10,
  rewrite_cost: 2
)

plan = RGeo::WKRep::WKBExchange::Planner.new(
  archive: archive,
  manifest: manifest,
  readers: readers,
  cost_model: cost_model
).plan

plan.feasible?  # => true
plan.cost       # => minimum total cost
plan.packets    # => one or more Packet objects
plan.verify!    # => true, re-reading every packet independently
```

Or in one call:

```ruby
plan = RGeo::WKRep::WKBExchange.plan(
  bytes, srid: 4326,
  manifest: manifest,
  readers: readers,
  cost_model: cost_model
)
```

## Paths and ordering

Paths are arrays of zero-based child indexes from the archive root. The string
form `"/0/2"` is also accepted. `Archive#atoms` lists the routable leaves, and
`Archive#tree_description` prints the whole structure.

Packets preserve the original collection order for each reader. A packet cannot
non-contiguously gather atoms: selecting atoms `/0` and `/2` for the same
reader may require two packets or including `/1` where the manifest permits it.

## SRID rules

1. A member's explicit local SRID is part of the archive and is never replaced
   by an outer/inherited SRID.
2. An inherited SRID comes from the nearest enclosing explicit SRID or from the
   `srid:` passed to `Archive.parse`.
3. `Manifest#srid` is an authored business fact. It may be global or scoped to
   one reader (`reader:`), and wins over both inherited and local values.
4. A packet may state one SRID at its start. EWKB readers may instead retain
   distinct explicit child SRIDs. Standard WKB readers cannot express child
   SRIDs; such a group must split into separate packets.

If a selected member cannot be expressed, the returned plan is infeasible.
`plan.infeasibilities` contains `UnsupportedMember` objects with:

- `path`: the original collection path, e.g. `"/1"`;
- `reader`: the destination reader;
- `missing`: the missing capability, e.g. `"Z coordinates"` or `"SRID"`;
- `message`: a precise description.

An outer SRID never masks a member's local SRID in these reports.

## Preservation and rewriting

Each leaf is forwarded byte-for-byte when its dialect, dimensions, SRID, and
axes already match the packet. Otherwise it is rewritten only if the manifest
permits an alternative representation via `Manifest#permit`.

Mixed byte orders are preserved whenever legal. A rewritten atom keeps its
source byte order.

## Cost minimization

For each reader, the planner runs a dynamic program over contiguous segments of
its ordered atom stream. It chooses packet boundaries and each packet's fixed
dialect, dimension, and SRID configuration jointly. Among minimum-cost plans it
prefers the one retaining the most original atom bytes.

`Plan#verify!` re-reads every packet with an independent decoder and checks the
ordered shape, dimensions, and SRID of each member.
