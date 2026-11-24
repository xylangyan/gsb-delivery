# rgeo-geojson

[![Gem Version](https://badge.fury.io/rb/rgeo-geojson.svg)](http://badge.fury.io/rb/rgeo-geojson)
[![CI](https://github.com/rgeo/rgeo-geojson/workflows/CI/badge.svg)](https://github.com/rgeo/rgeo-geojson/actions?query=workflow%3ACI+branch%3Amaster+event%3Apush)

`rgeo-geojson` is an extension to [RGeo](https://github.com/rgeo/rgeo)
that provides GeoJSON encoding and decoding.

## Summary

RGeo is a key component for writing location-aware applications in the Ruby
programming language. At its core is an implementation of the industry
standard OGC Simple Features Specification, which provides data
representations of geometric objects such as points, lines, and polygons,
along with a set of geometric analysis operations. See the README for the
"rgeo" gem for more information.

`RGeo::GeoJSON` is an optional RGeo module that provides GeoJSON encoding
and decoding. [GeoJSON](http://geojson.org) is a JSON format used for
geographic data structures.

Example:

```ruby
require 'rgeo/geo_json'

str1 = '{"type":"Point","coordinates":[1,2]}'
geom = RGeo::GeoJSON.decode(str1)
geom.as_text              # => "POINT (1.0 2.0)"

str2 = '{"type":"Feature","geometry":{"type":"Point","coordinates":[2.5,4.0]},"properties":{"color":"red"}}'
feature = RGeo::GeoJSON.decode(str2)
feature['color']          # => 'red'
feature.geometry.as_text  # => "POINT (2.5 4.0)"

hash = RGeo::GeoJSON.encode(feature)
hash.to_json == str2      # => true
```

## Dateline survey areas

Supplier survey areas are ordinary GeoJSON Polygons whose longitudes may run
past 180° or below -180° to cross the date line. `RGeo::GeoJSON::Dateline`
reads those registrations, enumerates every legal reading, and publishes only
the fragments whose owning original area is the same in every reading.

```ruby
require "rgeo/geo_json"

result = RGeo::GeoJSON::Dateline::Publisher.publish(
  "fixtures/coastal_registration.json",
  output_dir: "output"
)
result["report"]["status"]           # => "ok"
result["report"]["signed_areas"]     # => ["A", "B"]
RGeo::GeoJSON.decode(result["geojson"])  # => re-readable FeatureCollection
```

The command line wrapper does the same:

```sh
bundle exec ruby -Ilib bin/publish-coverage REGISTRATION.json -o output
```

### Registration document

A `FeatureCollection` of Polygon features. Each feature carries an `area_id`
(also read from `id`) in its properties. Rings keep the supplier's vertex
order; the first ring is the outer ring and the rest are holes. Coordinates
must be exact integer microdegrees, which is checked rather than silently
rounded.

Optional `properties.edge_ids` names each original edge, ring by ring. When
absent, edge ids default to `"<area_id>/r<ring>/e<index>"`.

A sibling `handover` object carries only what a registration cannot express:

```json
{
  "display_decimals": 6,
  "boundary_rule": "closed",
  "half_circle_edges": [
    {"edge_id": "A/r0/e0", "directions": ["positive"]}
  ],
  "shared_edges": [
    {"edges": [
      {"area_id": "A", "edge_id": "A/r0/e3", "direction": "forward"},
      {"area_id": "B", "edge_id": "B/r0/e1", "direction": "reverse"}
    ]}
  ],
  "stations": [
    {"id": "S1", "lon_micro": 185000000, "lat_micro": 5000000,
     "areas": ["A"], "status": "confirmed"}
  ],
  "independent_areas": ["B"]
}
```

- `display_decimals` is the only place rounding happens; all internal work is
  exact integer or rational arithmetic.
- `boundary_rule` is `closed` (a station exactly on an edge belongs to the
  area, the default) or `open` (it does not). The same rule applies to hole
  rings.
- `half_circle_edges` restricts edges whose two endpoints are exactly half a
  circle apart. Without a restriction both directions are legal.
- `shared_edges` declares original edges that are the same physical survey
  line. `direction` is `forward` (the registration's vertex order) or
  `reverse`, and the two expansions must place the line at the same point.
- `stations` with `status: "confirmed"` filter readings: a reading that gives
  a different owner set is discarded. `status: "questioned"` stations are
  reported but not used to filter.
- `independent_areas` stay publishable when another registration group
  conflicts.

### Output

`coverage_fragments.geojson` is a `FeatureCollection` whose coordinates all
lie in `[-180, 180]`. A registration that crosses the date line yields one
fragment on each side; both keep the original `area_id` and reference the same
original edges. Fragments from one original area count as that one area;
territory shared with a different original area is listed as two entries in
`resolution_report.json` under `overlaps`.

`resolution_report.json` carries:

- `station_ownership` for stable stations, including the original edge and
  along-edge parameter when the station lies on an edge;
- `ambiguous_stations` plus two complete `interpretations` when ownership
  genuinely depends on the reading;
- `junction_records`: for each dateline crossing or edge end, the `edge_id`,
  the exact along-edge parameter `t`, the original unwrapped endpoints, the
  exact interpolated point, and the folded display coordinate;
- `blocking_evidence` when no reading satisfies the registration, naming the
  first survey lines or station ownerships that must be re-evidenced.

If the number of readings exceeds the safeguard (`200_000`), the result is
`"undecided"` and names the areas with multiple readings instead of guessing.



`RGeo::GeoJSON` requires:

* Ruby 2.1.0 or later
* rgeo 1.0.0 or later

Include in your bundle:

```ruby
gem 'rgeo-geojson'
```

Install `rgeo-geojson` as a gem:

```sh
gem install rgeo-geojson
```

See the README for the `rgeo` gem, a required dependency, for further installation information.

### Development and support

RDoc Documentation is available at http://rdoc.info/gems/rgeo-geojson

Source code is hosted on Github at https://github.com/rgeo/rgeo-geojson

Contributions are welcome. Fork the project on Github.

Report bugs on Github issues at http://github.com/rgeo/rgeo-geojson/issues

### Acknowledgments

RGeo was created by Daniel Azuma (http://www.daniel-azuma.com).

Development is/was supported by [Pirq](http://www.pirq.com) and
[Neighborland](https://neighborland.com).

### License

Copyright (c) Daniel Azuma, Tee Parham

https://github.com/rgeo/rgeo-geojson/blob/master/LICENSE.txt
