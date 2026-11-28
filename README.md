## Geobuf

Geobuf is a compact binary geospatial format for _lossless_ compression of GeoJSON.

**Note well**: this project has been transferred by Mapbox to the new pygeobuf organization.

Advantages over using GeoJSON directly (in this [revised version](https://github.com/mapbox/geobuf/issues/27)):

- **Very compact**: typically makes GeoJSON 6-8 times smaller.
- Smaller even when comparing gzipped sizes: 2-2.5x compression for GeoJSON.
- Easy **incremental parsing** &mdash; you can get features out as you read them,
without the need to build in-memory representation of the whole data.
- **Partial reads** &mdash; you can read only the parts you actually need, skipping the rest.
- Trivial **concatenation**: you can concatenate many Geobuf files together and they will form a valid combined Geobuf file.
- Potentially **faster encoding/decoding** compared to native JSON implementations (i.e. in Web browsers).
- Can still accommodate any GeoJSON, including extensions with arbitrary properties.

Think of this as an attempt to design a simple, modern Shapefile successor
that works seamlessly with GeoJSON.

Unlike [Mapbox Vector Tiles](https://github.com/mapbox/vector-tile-spec/), it aims for _lossless_ compression
of datasets &mdash; without tiling, projecting coordinates, flattening geometries or stripping properties.

#### pygeobuf

This repository is the first encoding/decoding implementation
of this new major version of [Geobuf](https://github.com/mapbox/geobuf) (in Python).
It serves as a prototyping playground, with faster implementations in JS and C++ coming in future.

#### Sample compression sizes

|                     | normal    | gzipped
| ------------------- | --------- | --------
| us-zips.json 	      | 101.85 MB | 26.67 MB
| us-zips.pbf         | 12.24 MB  | 10.48 MB
| us-zips.topo.json   | 15.02 MB  | 3.19 MB
| us-zips.topo.pbf    | 4.85 MB   | 2.72 MB
| idaho.json          | 10.92 MB  | 2.57 MB
| idaho.pbf           | 1.37 MB   | 1.17 MB
| idaho.topo.json     | 1.9 MB    | 612 KB
| idaho.topo.pbf      | 567 KB    | 479 KB

### Usage

Installation:

`pip install geobuf`

Command line:

```bash
geobuf encode < example.json > example.pbf
geobuf decode < example.pbf > example.pbf.json
```

As a module:

```python
import geobuf

pbf = geobuf.encode(my_json) # GeoJSON -> Geobuf string
my_json = geobuf.decode(pbf) # Geobuf string -> GeoJSON
```

The `encode` function accepts a dict-like object, for example the result of `json.loads(json_str)`.

Both `encode.py` and `geobuf.encode` accept two optional arguments:

- **precision** &mdash; max number of digits after the decimal point in coordinates, `6` by default.
- **dimensions** &mdash; number of dimensions in coordinates, `2` by default.

### Splitting a parcel collection into size-limited packets

For field devices that only accept one bounded packet at a time, `geobuf.plan_packets`
splits an ordered `FeatureCollection` into contiguous runs of parcels. Each packet is a
standard Geobuf `FeatureCollection` decodable on its own by the existing decoder, and
every packet uses a single coordinate precision.

```python
from geobuf import plan_packets, verify_boundaries, NoPacketArrangementError

try:
    plan = plan_packets(collection, precisions=[1, 2], max_bytes=512)
except NoPacketArrangementError as error:
    review = error.reasons  # shared boundaries, failed rings, packet limits
else:
    for packet in plan['packets']:
        send(packet['data'])  # plain Geobuf bytes
    verify_boundaries(plan['packets'], plan['boundaries'])
```

Behavior:

- Parcels keep their original order; packets are adjacent runs and every feature id and
  property travels with its own parcel, so any packet opens without reading another one.
- Parcels linked by a shared boundary are constrained to the same precision. A precision
  that collapses a ring or makes the read-back boundary diverge is rejected; the original
  coordinates are never moved, only rounded at the chosen precision.
- Each shared boundary gets a stable id (`B1`, ...) stored on both neighboring parcels, so
  a boundary split across two packets can still be checked by the same id.
- The arrangement with the smallest total *actual* encoded bytes wins. Ties break by fewer
  packets, then earlier packet end positions, then smaller per-packet precision.
- `NoPacketArrangementError.reasons` lists the shared boundaries, rings that fail at each
  precision, and the byte limit plus the smallest encodable size of each parcel.

The original `geobuf.encode` / `geobuf.decode` entry points are unchanged and keep the
default precision of 6.

### Tests

```bash
py.test -v
```

The tests run through all `.json` files in the `fixtures` directory,
comparing each original GeoJSON with an encoded/decoded one.

### Generating from geobuf.proto

To (re-)generate the `geobuf_pb2.py` file, you can run the following
commands:

```bash
protoc geobuf.proto --python_out=geobuf
```
