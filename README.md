# GcodeParser

A simple gcode parser that can be used to parse a gcode file into python `GcodeLine` objects.

The structure of the python object is:

`G1 X10 Y-2.5 ; this is a comment`

```python
GcodeLine(
  command = ('G', 1),
  params = {'X': 10, 'Y': -2.5},
  comment = 'this is a comment',
  line_index = 0,
  type = Commands.MOVE # Commands.MOVE, Commands.COMMENT, Commands.OTHER, Commands.TOOLCHANGE
)
```

## Install

```
pip install gcodeparser
```

Alternatively:

```
pip install -e "git+https://github.com/AndyEveritt/GcodeParser.git@master#egg=gcodeparser"
```

## Usage

```python
from gcodeparser import parse_gcode_lines

# Recommended: iterate over lines in a file
with open('my_gcode.gcode', 'r') as f: # note that file should be open during iteration
    for line in parse_gcode_lines(f, include_comments=False):
        print(line)

# Alternative: open gcode file and parse lines into a list without iteration
with open('my_gcode.gcode', 'r') as f:
    lines = list(parse_gcode_lines(f, include_comments=False))

# Also, we can convert string to parsed lines
with open('my_gcode.gcode', 'r') as f:
    gcode = f.read()
lines = list(parse_gcode_lines(gcode, include_comments=False))
```

_output:_

```py
[GcodeLine(command=('G', 10), params={'P': 0, 'R': 0, 'S': 0}, comment='sets the standby temperature'),
 GcodeLine(command=('G', 29), params={'S': 1}, comment=''),
 GcodeLine(command=('T', 0), params={}, comment=''),
 GcodeLine(command=('G', 21), params={}, comment='set units to millimeters'),
 GcodeLine(command=('G', 90), params={}, comment='use absolute coordinates'),
 GcodeLine(command=('M', 83), params={}, comment='use relative distances for extrusion'),
 GcodeLine(command=('G', 1), params={'E': -0.6, 'F': 3600.0}, comment=''),
 GcodeLine(command=('G', 1), params={'Z': 0.45, 'F': 7800.0}, comment=''),
 GcodeLine(command=('G', 1), params={'Z': 2.35}, comment=''),
 GcodeLine(command=('G', 1), params={'X': 119.575, 'Y': 89.986}, comment=''),
 GcodeLine(command=('G', 1), params={'Z': 0.45}, comment=''),
 GcodeLine(command=('G', 1), params={'E': 0.6, 'F': 3600.0}, comment=''),
 GcodeLine(command=('G', 1), params={'F': 1800.0}, comment=''),
 GcodeLine(command=('G', 1), params={'X': 120.774, 'Y': 88.783, 'E': 0.17459}, comment=''),
 GcodeLine(command=('G', 1), params={'X': 121.692, 'Y': 88.145, 'E': 0.11492}, comment=''),
 GcodeLine(command=('G', 1), params={'X': 122.7, 'Y': 87.638, 'E': 0.11596}, comment=''),
 GcodeLine(command=('G', 1), params={'X': 123.742, 'Y': 87.285, 'E': 0.11317}, comment=''),
 ...
]
```

### Include Comments

`parse_gcode_lines()` takes a second argument called `include_comments` which defaults to `False`. If this is set to `True` then any line from the gcode file which only contains a comment will also be included in the output.

```python
from gcodeparser import parse_gcode_lines

gcode = """G1 X1 ; this comment is always included
; this comment will only be included if `include_comments=True`"""

lines = list(parse_gcode_lines(gcode, include_comments=True))
```

If `include_comments` is `True` then the comment line will be in the form of:

```python
GcodeLine(
  command = (';', None),
  params = {},
  comment = 'this comment will only be included if `include_comments=True`',
)
```

### Convert Command Tuple to String

The `GcodeLine` class has a property `command_str` which will return the command tuple as a string. ie `('G', 91)` -> `"G91"`.

### Changing back to Gcode String

The `GcodeLine` class has a property `gcode_str` which will return the equivalent gcode string.

> This was called `to_gcode()` in version 0.0.6 and before.

### Retrieving Params

To retrieve a param, use the method `get_param(param: str, return_type=None, default=None)` which
returns the value of the param if it exists, otherwise it will the `default` value.
If `return_type` is set, the return value will be type cast.

```python
line.get_param('X')
```

### Updating Params

To update a param, use the method `update_param(param: str, value: int | float)`

```python
line.update_param('X', 10)
```

If the param does not exist, it will return `None` else it will return the updated value.

### Deleting Params

To delete a param, use the method `delete_param(param: str)`

```python
line.delete_param('X')
```

### Converting to DataFrames

If for whatever reason you want to convert your list of `GcodeLine` objects into a pandas dataframe:

```python
from gcodeparser import parse_gcode_lines
import pandas as pd

with open('my_gcode.gcode', 'r') as f:
    lines = list(parse_gcode_lines(f))
df = pd.DataFrame(lines)
```

## Material reconciliation

`gcodeparser.reconcile` accounts for material in a print stream that is stored
as snippets plus repeat counts, where snippets may invoke smaller snippets.  It
does not flatten the stream: commands are expanded lazily and addressed by their
position (*ordinal*) in the executed stream, and long runs of identical repeats
are handled arithmetically.

### Model

- Each tool keeps its own extruder coordinate. `T` changes the active tool but
  leaves every coordinate untouched.
- `M82` / `M83` select, from that command onwards, whether an `E` word is an
  absolute coordinate or an increment. The mode is modal and global.
- `G92 E` resets the coordinate of the active tool only.
- Negative displacement opens a retraction debt on the spool currently attached
  to the active tool. Positive displacement first repays that spool's open
  retractions in first-opened-first-repaid order; the remainder is new material.
- Debt belongs to the spool, not the tool. A handover may connect a tool to a
  different spool between operations; debt on the old spool remains there while
  the tool's own coordinate continues unchanged.
- A single command can both finish an older retraction and start new material.
  It is split at that boundary, and both parts carry the same credential.
- Quantities are `Decimal`s converted from the exact text in the source command,
  so `E0.050` keeps three decimal places in reports.

### Basic usage

```python
from gcodeparser.reconcile import Handover, MaterialLedger, PrintProgram, Repeat, Snippet

snippets = {
    "bead": Snippet(["M83", "G1 E0.250", "G1 E-0.100"], name="bead"),
    "pass": Snippet([Repeat("bead", 4), "G1 E0.400"], name="pass"),
}
program = PrintProgram(Repeat("pass", 1000), snippets=snippets)
ledger = MaterialLedger(program, initial_spools={0: "A"}, mode="relative")

ledger.usage()                  # {'A': Decimal('...')}
ledger.usage(40, 120)           # per-spool new material in that window
ledger.first_new_usage(40, 120) # credential of the first new-material command
ledger.window(40, 120).to_dict()
```

A credential (`CommandId`) contains:

- `ordinal`: position in the expanded stream, which distinguishes repeated
  executions of the same source line;
- `source`: original snippet, source line and command index (two commands on one
  physical line share a line index but have different command indices);
- `iteration`: the repeat indices leading to this execution;
- `text`: the command text.

### Handovers

```python
body = [
    "M83\nG1 E-4.0\nG92 E0\nT1\nG1 E3.0\nT0",
    Handover(0, "B"),
    "G1 E2.0",
    Handover(0, "A"),
    "G1 E2.0",
]
ledger = MaterialLedger(
    Snippet(body, name="root"),
    initial_spools={0: "A", 1: "A"},
    mode="relative",
)
ledger.usage()               # {'A': Decimal('1.0'), 'B': Decimal('2.0')}
ledger.repayment("A:0")      # (Decimal('4.0'), credential at T1's G1 E3.0)
```

`timed_handovers` is available when the hand-over order is recorded separately
from the G-code, keyed by the ordinal of the command it precedes.

### Retraction queries

```python
ledger.retractions("A")      # all retractions opened on spool A
ledger.outstanding("A")      # unrepaid debt at the end of the stream
ledger.pending("A")          # open retractions in repayment order
ledger.repaid("A:0", 100, 200)
ledger.first_repayment("A:0", 100, 200)
ledger.retraction_report("A:0", 100, 200)
```

A window includes both endpoints by default; pass `inclusive_end=False` to make
the end exclusive. Debt opened before the window is still accounted for, so a
window that starts mid-debt attributes only the repayments that happen inside it.

### JSON program specification

`python -m gcodeparser.reconcile program.json --queries queries.json` accepts:

```json
{
  "initial": {
    "tool": 0,
    "mode": "relative",
    "spools": {"0": "A", "1": "A"},
    "e": {"0": "0"},
    "timed_handovers": {}
  },
  "snippets": {
    "bead": ["M83", "G1 E0.250"],
    "pass": [
      {"repeat": {"snippet": "bead", "count": 4}},
      {"handover": {"tool": 0, "spool": "B"}},
      "G1 E0.400"
    ]
  },
  "root": {"snippet": "pass", "count": 1000}
}
```

Queries are a list of objects:

```json
[
  {"type": "usage", "start": 0, "end": 1000},
  {"type": "first_new", "start": 0, "end": 1000},
  {"type": "retraction", "retraction": "A:0", "start": 0, "end": 1000},
  {"type": "credential", "start": 42}
]
```

The command prints a JSON list with one result per query.

### Long repeat streams

The stream is expanded lazily and addressed by ordinal; nothing is flattened up
front.  In addition, a fast path recognises runs of repeat iterations that repeat
the same material release pattern with exactly the same mode, tool coordinates,
spool connections and retraction debt at both ends.  Those runs are evaluated
arithmetically, so a repeat count in the billions does not require visiting every
command (deeply nested neutral repeats are included).

Blocks where each iteration opens and fully repays its own retraction are also
compressed, with retraction identities reconstructed lazily per cycle.  Blocks
that carry open retraction debt across iterations fall back to the exact command
walk.  That walk is still lazy and window-bounded, but its cost is proportional
to the commands inside the requested window rather than to the whole stream.
