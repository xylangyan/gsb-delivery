# unlzw3

[![ci](https://github.com/scivision/unlzw3/actions/workflows/ci.yml/badge.svg)](https://github.com/scivision/unlzw3/actions/workflows/ci.yml)
[![PyPi Download stats](http://pepy.tech/badge/unlzw3)](http://pepy.tech/project/unlzw3)

Pure Python decompression module for .Z files compressed using Unix compress utility.
Unlike the faster but Linux-specific
[unlzw](https://pypi.org/project/unlzw/)
using Python CFFI, `unlzw3` is slower but works on any platform that runs Python including Windows.

This is a purely Python adaptation of Mark Adler's
['unlzw' C function](http://mathematica.stackexchange.com/questions/60531/how-can-i-read-compressed-z-file-automatically-by-mathematica/60879#60879)
on Stackoverflow.
Python can be much slower than using any compiled utility for the same purpose.

## Usage

`unlzw3.unlzw(data)` takes LZW .Z compressed data as any type which can be converted to a bytearray (generally a string).
It returns a UTF-8 decoded string containing the decompressed data.

```python
import unlzw3
from pathlib import Path

uncompressed_data = unlzw3.unlzw(Path('file.Z').read_bytes())

# or

uncompressed_data = unlzw3.unlzw(Path('file.Z'))
```

## Comparing retained fragments of two archives

Two legacy `.Z` volumes can hold the same decompressed text while differing in
code width, dictionary clear timing and code boundaries (one code on one side
may emit the same bytes that two codes emit on the other). The comparison API
never compares compressed codes: it compares the reconstructed bytes.

A per-code index is built with a single scan of each compressed stream. The
index stores one fixed-size record per *compressed code* (bit range, code
value, dictionary segment, decompressed start/length of the emitted word); the
decompressed body — often dozens of times larger than the archive — is never
stored or generated while indexing. One index is reused for every handoff sheet
that involves the same volume.

```python
from unlzw3 import build_index, make_selection, compare
from unlzw3 import ByteAnchor, CodeAnchor, RangeSpec

left_index = build_index(Path("left.Z").read_bytes())
right_index = build_index(Path("right.Z").read_bytes())

# Anchors may sit in the middle of an emitted word, independently per side.
left_sheet = [
    RangeSpec(ByteAnchor(137), length=900),
    RangeSpec(ByteAnchor(5000), length=1234),
]
right_sheet = [
    RangeSpec(CodeAnchor(code=109, word=2), length=900),
    RangeSpec(ByteAnchor(5000), length=1234),
]

result = compare(
    make_selection(left_index, left_sheet),
    make_selection(right_index, right_sheet),
)
print(result.equal)
```

On a mismatch the result gives the first differing offset in the concatenated
selection, the two bytes, and for each side: which handoff range it falls in,
the dictionary segment, the compressed code ordinal/value, the code's original
bit range (padding bits skipped on width changes are separate), the position
inside the emitted word, and the absolute decompressed offset. If one side ends
first, `short_by` says exactly how many bytes are missing. Archives the
reference reader would reject are reported by side through
`compare_archives(...)` (or `InvalidArchive` from `build_index`).

Indexes can be persisted without the original archive:

```python
from unlzw3 import save_index, load_index

save_index(left_index, "left.lzwidx")
restored = load_index("left.lzwidx")
```

The same functionality is available from the command line:

```sh
python -m unlzw3 index left.Z left.lzwidx
python -m unlzw3 compare left.lzwidx right.Z \
    --left-start byte:0 --left-length 1000 \
    --right-start byte:0 --right-length 1000
```

## Contributions

* reference C code: Mark Adler
* pure Python implemetation: [Brandon Owen](https://github.com/umeat/unlzw)
* modernization, test / CI and PyPi: Michael Hirsch
