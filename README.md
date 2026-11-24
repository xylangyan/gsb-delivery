[![pypi](https://img.shields.io/pypi/v/PyWavefront.svg)](https://pypi.org/project/PyWavefront/)
[![CircleCI](https://circleci.com/gh/pywavefront/PyWavefront.svg?style=svg)](https://circleci.com/gh/pywavefront/PyWavefront)

<div align="center">

[![preview](https://raw.githubusercontent.com/pywavefront/PyWavefront/master/extras/logo.png)](#readme)

</div>

# PyWavefront

PyWavefront reads Wavefront 3D object files (`something.obj`, `something.obj.gz`
and `something.mtl`) and generates interleaved vertex data for each material ready for rendering.

* Python 3.4+ is supported in 1.x versions
* Python 2.7 is supported in 0.x versions

A simple (optional) visualization module is also provided for
rendering the object(s). The interleaved data can also be used by
more modern renderers thought VBOs or VAOs.

Currently the most commonly used features in [the specification](https://en.wikipedia.org/wiki/Wavefront_.obj_file) has
been implemented:

* Positions
* Texture Coordinates
* Normals
* Vertex Color
* Material parsing
* Texture and texture parameters

We currently don't support parameter space vertices, line elements or smoothing groups.
Create an issue or pull request on github if needed features are missing.

The package is on [pypi](https://pypi.org/project/PyWavefront/)
or can be cloned on [github](https://github.com/pywavefront/PyWavefront).

```bash
pip install pywavefront
```

Also check out the [roadmap](https://github.com/pywavefront/PyWavefront/blob/master/ROADMAP.md) for future plans.

## Usage

Basic example loading an obj file:

```python
import pywavefront
scene = pywavefront.Wavefront('something.obj')
```

A more complex example

* `strict` (Default: `False`) will raise an exception if unsupported features are found in the obj or mtl file
* `encoding` (Default: `utf-8`) of the obj and mtl file(s)
* `create_materials` (Default: `False`) will create materials if mtl file is missing or obj file references non-existing materials
* `collect_faces` (Default: `False`) will collect triangle face data for every mesh. In case faces with more than three vertices are specified they will be triangulated. See the documentation of `ObjParser#consume_faces()` in [`obj.py`](https://github.com/pywavefront/PyWavefront/blob/master/pywavefront/obj.py).
* `parse` (Default: `True`) decides if parsing should start immediately.
* `cache` (Default: `False`) writes the parsed geometry to a binary file    for faster loading in the future

```python
import pywavefront
scene = pywavefront.Wavefront(
    'something.obj',
    strict=True,
    encoding="iso-8859-1",
    parse=False,
)
scene.parse()  # Explicit call to parse() needed when parse=False

# Iterate vertex data collected in each material
for name, material in scene.materials.items():
    # Contains the vertex format (string) such as "T2F_N3F_V3F"
    # T2F, C3F, N3F and V3F may appear in this string
    material.vertex_format
    # Contains the vertex list of floats in the format described above
    material.vertices
    # Material properties
    material.diffuse
    material.ambient
    material.texture
    # ..
```

## Binary Cache

When ``cache=True`` the interleaved vertex data is written
as floats to a ``.bin`` file after the file is loaded. A json
file is also generated describing the contents of the binary file.
The binary file will be loaded the next time we attempt to load
the obj file reducing the loading time significantly.

Tests have shown loading time reduction by 10 to 100 times
depending on the size and structure of the original obj file.

Loading ``myfile.obj`` will generate the following files in the
same directory.

```txt
myfile.obj.bin
myfile.obj.json
```

Json file example:

```json
{
  "created_at": "2018-07-16T14:28:43.451336",
  "version": "0.1",
  "materials": [
    "lost_empire.mtl"
  ],
  "vertex_buffers": [
    {
      "material": "Stone",
      "vertex_format": "T2F_N3F_V3F",
      "byte_offset": 0,
      "byte_length": 5637888
    },
    {
      "material": "Grass",
      "vertex_format": "T2F_N3F_V3F",
      "byte_offset": 5637888,
      "byte_length": 6494208
    }
  ]
}
```

These files will **not be recreated until you delete them**.
The bin file is also compressed with gzip to greatly reduce size.

## Incremental Patches

For inspection pipelines where a supplier delivers small supplemental
batches instead of a complete obj, the `pywavefront.incremental` module
keeps the per-object expanded caches, triangle provenance and material
revisions without ever re-expanding an untouched object.

The first delivery is scanned once with the recording parser. Its float
output is identical to a normal `Wavefront` load, but each triangle also
remembers the raw signed face tokens, the resolved source record
identities (`v:7`, `vt:3`, ...), its object and the material occurrence
that bound it.

```python
from pywavefront.incremental import record_scene, apply_patch, write_archive

scene = record_scene(
    'fixture.obj',
    revision='r0',
    mtl_revisions={'red.mtl': 'red-r1', 'blue.mtl': 'blue-r1'})
```

A patch is described by a json manifest: the base revision, the target
object, exact 1-based insert/delete ranges for the `v`/`vt`/`vn` pools and
the triangle ranges whose face text is replaced. A partial obj carries the
new records and replacement face lines.

```python
apply_patch(scene, 'manifest_blue_vt.json', 'supp_blue_vt.obj')
write_archive(scene, 'fixture.obj')
```

Index semantics follow the Wavefront rules instead of raw coordinates:

* A positive index is a record identity. Inserting records in front of it
  shifts its position but keeps it bound to the same record, so a
  neighbouring face referencing old entries is not rebuilt.
* A negative index is relative to the current end of pool and is
  re-resolved after every patch, so `-1` picks up the new last entry.
* A material-only patch (new mtl revision, no data changes) reuses the
  existing geometry buffers verbatim; a data patch re-expands only the
  triangles whose provenance actually changed.

Materials are keyed by source library plus name, so two mtl files that
both define `seal` stay distinct, and each group records the mtl revision
it was bound with.

`write_archive()` stores the canonical `.pywf.json` / `.pywf.bin` cache.
Every patch also snapshots the state it replaced, and older revisions
remain openable with the exact material and vertex combination archived at
that time:

```python
from pywavefront.incremental import load_archive

current = load_archive('fixture.obj')
historical = load_archive('fixture.obj', revision='r0')
```

## Visualization

[Pyglet](http://www.pyglet.org/) is required to use the visualization module.

```bash
pip install pyglet
```

Example:

```python
import pywavefront
from pywavefront import visualization

[create a window and set up your OpenGl context]
obj = pywavefront.Wavefront('something.obj')

[inside your drawing loop]
visualization.draw(obj)
```

## Logging

The default log level is `ERROR`. This is configurable including overriding the formatter.

```python
import logging
import pywavefront

pywavefront.configure_logging(
    logging.DEBUG,
    formatter=logging.Formatter('%(name)s-%(levelname)s: %(message)s')
)
```

### Examples

The  [examples](https://github.com/pywavefront/PyWavefront/tree/master/examples)
directory contains some basic examples using the `visualization` module and further
instructions on how to run them.

### Generating a Wavefront file with Blender

The following presumes you are using [Blender](http://www.blender.org/) to generate your mesh:

* Using Blender, create a mesh with a UV-mapped texture. The UV-mapping is important!
  If it is working properly, you will see the texture applied within Blender's 3d view.
* Export the mesh from Blender using the Wavefront format, including normals.
* Reference your `*.obj` file as in the pywavefront example above.

## Tests

All tests can be found in the `tests` directory. To run the tests:

```bash
# Install pywavefront in develop mode
python setup.py develop

# Install required packages for running tests
pip install -r test-requirements.txt

# Run all tests
pytest

# Optionally specific tests modules can be runned separately
pytest tests/test_parser.py
```

## Community

PyWavefront Discord server : https://discord.gg/h3Rh4QN

## Owners & Maintainers

* Einar Forselv ([@einarf](https://github.com/einarf)) - Main Contact
* Kurt Yoder ([@greenmoss](https://github.com/greenmoss/)) - Backup

## Contributors

In alphabetical order:

* [ComFreek](https://github.com/ComFreek)
* Daniel Coelho [1danielcoelho](https://github.com/1danielcoelho)
* [@dav92lee](https://github.com/dav92lee)
* Jerek Shoemaker ([intrepid94](https://github.com/intrepid94))
* [Marxlp](https://github.com/Marxlp)
* Mathieu Lamarre
* [Oliv4945](https://github.com/Oliv4945)
* Patrik Huber ([patrikhuber](https://github.com/patrikhuber))
* Sérgio Agostinho ([SergioRAgostinho](https://github.com/SergioRAgostinho))
* Zohar Jackson
* hkarrson ([hkarrson](https://github.com/hkarrson))

## Project History

PyWavefront was originally started by @greenmoss (Kurt Yoder) in 2013.
He was the sole maintainer of the project until February 2019 when
the PyWavefront Maintainers organization was created adding @einarf
(Einar Forselv) as an additional owner and maintainer of the project.

License
-------

PyWavefront is [BSD-licensed](https://github.com/pywavefront/PyWavefront/blob/master/LICENSE)
