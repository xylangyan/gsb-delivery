"""Archive-aware sample location queries for pygcode.

The engine intentionally walks template calls recursively.  It never creates a
fully expanded G-code program and never pre-populates a grid: only observed
samples become entries in the first-hit index/window set.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .machine import Machine

Point = Tuple[int, int]

CS_NAME_TO_ID = {
    "G54": 1,
    "G55": 2,
    "G56": 3,
    "G57": 4,
    "G58": 5,
    "G59": 6,
    "G59.1": 7,
    "G59.2": 8,
    "G59.3": 9,
}
CS_ID_TO_NAME = {value: key for key, value in CS_NAME_TO_ID.items()}

DEFAULT_OFFSET = {"X": 0, "Y": 0, "Z": 0}
WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
LEADING_N_RE = re.compile(r"^\s*N\s*(\d+(?:\.\d+)?)\s*", re.IGNORECASE)


class SampleArchiveError(ValueError):
    """Raised when an archive or query cannot be interpreted."""


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise SampleArchiveError("boolean is not a coordinate")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise SampleArchiveError("invalid number %r" % value) from exc


def lattice_value(value: Any, tolerance: float = 1e-8) -> int:
    number = _number(value)
    rounded = int(round(number))
    if abs(number - rounded) > tolerance:
        raise SampleArchiveError("sample is not on the integer lattice: %r" % (value,))
    return rounded


def point_key(x: Any, y: Any) -> Point:
    return lattice_value(x), lattice_value(y)


def normalize_cs(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        cs_id = value
    else:
        text = str(value).strip().upper().replace(" ", "")
        if text in CS_NAME_TO_ID:
            cs_id = CS_NAME_TO_ID[text]
        elif text.isdigit():
            cs_id = int(text)
        else:
            raise SampleArchiveError("unknown coordinate system %r" % (value,))
    if cs_id not in CS_ID_TO_NAME:
        raise SampleArchiveError("unsupported coordinate system id %r" % cs_id)
    return cs_id


def cs_name(cs_id: int) -> str:
    return CS_ID_TO_NAME.get(cs_id, "CS%d" % cs_id)


def normalize_offset(value: Any) -> Dict[str, float]:
    if value is None:
        return dict(DEFAULT_OFFSET)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, dict)):
        axes = ("X", "Y", "Z")
        return {axis: _number(value[index]) if index < len(value) else 0.0
                for index, axis in enumerate(axes)}
    if isinstance(value, dict):
        result = dict(DEFAULT_OFFSET)
        for raw_key, raw_value in value.items():
            axis = str(raw_key).strip().upper()
            if axis not in DEFAULT_OFFSET:
                continue
            result[axis] = _number(raw_value)
        return result
    raise SampleArchiveError("invalid work offset %r" % (value,))


def normalize_offsets(raw_offsets: Optional[Dict[str, Any]]) -> Dict[int, Dict[str, float]]:
    offsets: Dict[int, Dict[str, float]] = {
        cs_id: dict(DEFAULT_OFFSET) for cs_id in CS_ID_TO_NAME
    }
    if not raw_offsets:
        return offsets
    for key, value in raw_offsets.items():
        offsets[normalize_cs(key)] = normalize_offset(value)
    return offsets


def _first_key(data: Dict[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _parse_line_number(text: str) -> Tuple[Optional[int], str]:
    match = LEADING_N_RE.match(text)
    if not match:
        return None, text
    return int(float(match.group(1))), text[match.end():]


def normalize_line(raw_line: Any, fallback_number: Optional[int] = None) -> Dict[str, Any]:
    if isinstance(raw_line, str):
        parsed_number, gcode = _parse_line_number(raw_line)
        line: Dict[str, Any] = {"gcode": gcode.strip()}
        if parsed_number is not None:
            line["line"] = parsed_number
        elif fallback_number is not None:
            line["line"] = fallback_number
        return line

    if not isinstance(raw_line, dict):
        raise SampleArchiveError("template line must be string or object: %r" % (raw_line,))

    line = deepcopy(raw_line)
    line_number = _first_key(line, ("line", "lineno", "line_number", "no", "n", "original_line"))
    if line_number is None:
        line_number = fallback_number
    if line_number is not None:
        line["line"] = int(_number(line_number))

    if "gcode" not in line:
        code = _first_key(line, ("gcode", "code", "text", "command"))
        call = _first_key(line, ("call", "template", "ref", "use"))
        if code is not None:
            line["gcode"] = str(code)
        elif call is not None:
            line["call"] = str(call)
            repeat = _first_key(line, ("repeat", "times", "count", "n"), 1)
            line["repeat"] = max(1, int(_number(repeat)))
        else:
            raise SampleArchiveError("line has neither gcode nor template call: %r" % raw_line)

    if "gcode" in line and isinstance(line["gcode"], str):
        parsed_number, gcode = _parse_line_number(line["gcode"])
        line["gcode"] = gcode.strip()
        if "line" not in line and parsed_number is not None:
            line["line"] = parsed_number

    if "call" in line:
        repeat = _first_key(line, ("repeat", "times", "count", "n"), 1)
        line["repeat"] = max(1, int(_number(repeat)))

    explicit_mark = _first_key(line, ("sample", "marked", "is_sample", "mark"), None)
    mark_id = _first_key(line, ("mark_id", "sample_id", "handoff_id", "id"))
    line["marked"] = bool(explicit_mark) or (mark_id is not None and explicit_mark is not False)
    if mark_id is not None:
        line["mark_id"] = str(mark_id)
    return line


def normalize_template(name: str, raw_template: Any) -> Dict[str, Any]:
    if isinstance(raw_template, str):
        raw_template = {"lines": raw_template.splitlines()}
    if isinstance(raw_template, Sequence) and not isinstance(raw_template, (str, bytes, dict)):
        raw_template = {"lines": list(raw_template)}
    if not isinstance(raw_template, dict):
        raise SampleArchiveError("template %r must be object/list/string" % name)

    raw_lines = _first_key(raw_template, ("lines", "body", "blocks", "gcode"), [])
    if isinstance(raw_lines, str):
        raw_lines = raw_lines.splitlines()
    if not isinstance(raw_lines, Sequence):
        raise SampleArchiveError("template %r lines must be a sequence" % name)

    lines = [
        normalize_line(raw_line, fallback_number=index)
        for index, raw_line in enumerate(raw_lines, start=1)
    ]
    template = {
        "name": str(name),
        "lines": lines,
    }
    count = _first_key(raw_template, ("count", "times", "copies", "repeat", "root_repeat"))
    if count is not None:
        template["count"] = max(1, int(_number(count)))
    return template


def normalize_handoff(raw_archive: Dict[str, Any],
                      templates: Dict[str, Dict[str, Any]]) -> Tuple[set, set]:
    marked_keys = set()
    marked_ids = set()

    handoff = _first_key(raw_archive, ("handoff", "samples", "marks", "sample_sheet"), [])
    if isinstance(handoff, dict):
        if all(isinstance(value, (list, tuple, set)) for value in handoff.values()):
            for template_name, line_numbers in handoff.items():
                for line_number in line_numbers:
                    marked_keys.add((str(template_name), int(_number(line_number))))
            handoff = []
        else:
            handoff = handoff.get("rows", handoff.get("entries", []))

    if not isinstance(handoff, Sequence):
        raise SampleArchiveError("handoff must be a sequence or template->lines object")

    all_line_numbers: Dict[int, List[str]] = {}
    for template_name, template in templates.items():
        for line in template["lines"]:
            if "line" in line:
                all_line_numbers.setdefault(int(line["line"]), []).append(template_name)
            if line.get("marked"):
                marked_keys.add((template_name, int(line["line"])))
                if line.get("mark_id"):
                    marked_ids.add(line["mark_id"])

    for row in handoff:
        if isinstance(row, (int, float, str)) and not isinstance(row, dict):
            text = str(row)
            if text.upper().startswith(("M", "S", "H")) and not text.replace(".", "").isdigit():
                marked_ids.add(text)
                continue
            line_number = int(_number(row))
            owners = all_line_numbers.get(line_number, [])
            if len(owners) != 1:
                raise SampleArchiveError(
                    "original line %r needs template because owners=%r" % (line_number, owners)
                )
            marked_keys.add((owners[0], line_number))
            continue

        if not isinstance(row, dict):
            raise SampleArchiveError("invalid handoff row %r" % (row,))

        mark_id = _first_key(row, ("mark_id", "sample_id", "handoff_id", "id"))
        if mark_id is not None:
            marked_ids.add(str(mark_id))

        template_name = _first_key(row, ("template", "template_name", "tpl", "name"))
        line_number = _first_key(row, ("line", "lineno", "line_number", "original_line", "no", "n"))
        if line_number is not None:
            line_number = int(_number(line_number))
            if template_name is None:
                owners = all_line_numbers.get(line_number, [])
                if len(owners) != 1:
                    raise SampleArchiveError(
                        "original line %r needs template because owners=%r"
                        % (line_number, owners)
                    )
                template_name = owners[0]
            marked_keys.add((str(template_name), line_number))

    if marked_ids:
        for template_name, template in templates.items():
            for line in template["lines"]:
                if line.get("mark_id") in marked_ids:
                    marked_keys.add((template_name, int(line["line"])))

    return marked_keys, marked_ids


def normalize_program(raw_archive: Dict[str, Any],
                      templates: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_program = _first_key(raw_archive, ("program", "roots", "entries", "calls", "archive"))
    program: List[Dict[str, Any]] = []

    if raw_program is None:
        for name, template in templates.items():
            if "count" in template:
                program.append({"call": name, "repeat": template["count"]})
    elif isinstance(raw_program, dict):
        for name, value in raw_program.items():
            repeat = 1
            if isinstance(value, int):
                repeat = max(1, value)
            elif isinstance(value, dict):
                repeat = max(1, int(_number(_first_key(
                    value, ("repeat", "times", "count", "n"), 1))))
            program.append({"call": str(name), "repeat": repeat})
    elif isinstance(raw_program, Sequence) and not isinstance(raw_program, (str, bytes)):
        for raw_call in raw_program:
            call = normalize_line(raw_call) if not isinstance(raw_call, dict) else deepcopy(raw_call)
            if "call" not in call:
                name = _first_key(call, ("template", "name", "ref", "use"))
                if name is None:
                    raise SampleArchiveError("program entry has no template: %r" % raw_call)
                call["call"] = str(name)
            call["repeat"] = max(1, int(_number(_first_key(
                call, ("repeat", "times", "count", "n"), 1))))
            program.append(call)
    else:
        raise SampleArchiveError("program must be list/object")

    if not program:
        raise SampleArchiveError("archive has no root template executions")
    for call in program:
        if call["call"] not in templates:
            raise SampleArchiveError("unknown template %r" % call["call"])
    return program


def load_archive(data: Any) -> Dict[str, Any]:
    if isinstance(data, (str, bytes, os.PathLike)):
        path = os.fspath(data)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    if not isinstance(data, dict):
        raise SampleArchiveError("archive JSON must be an object")

    raw_templates = _first_key(data, ("templates", "template_archive", "templates"), {})
    if not isinstance(raw_templates, dict):
        raise SampleArchiveError("templates must be an object keyed by template name")
    templates = {
        str(name): normalize_template(name, template)
        for name, template in raw_templates.items()
    }
    if not templates:
        raise SampleArchiveError("archive contains no templates")

    offsets = normalize_offsets(_first_key(data, ("offsets", "work_offsets", "coordinate_offsets")))
    marked_keys, marked_ids = normalize_handoff(data, templates)
    program = normalize_program(data, templates)

    window = _first_key(data, ("window", "machine_window", "bounds"))
    target = _first_key(data, ("position", "target", "query_position", "first_position"))

    return {
        "templates": templates,
        "program": program,
        "offsets": offsets,
        "marked_keys": marked_keys,
        "marked_ids": marked_ids,
        "window": window,
        "target": target,
        "source": _first_key(data, ("source", "path", "name"), None),
    }


def load_archive_file(path: str) -> Dict[str, Any]:
    return load_archive(path)


def parse_gcode_ops(gcode: str) -> Tuple[List[Tuple[str, Any]], Dict[str, float]]:
    text = re.sub(r"\([^)]*\)", " ", gcode or "")
    text = text.split(";", 1)[0]
    words = [(letter.upper(), _number(value)) for letter, value in WORD_RE.findall(text)]
    mode_ops: List[Tuple[str, Any]] = []
    coordinates: Dict[str, float] = {}
    for letter, value in words:
        if letter == "G":
            code = int(round(value)) if abs(value - round(value)) < 1e-9 else value
            mode_ops.append(("g", code))
        elif letter in set("XYZABCUVW"):
            coordinates[letter] = value
    return mode_ops, coordinates


class BuiltinMotion:
    """Minimal exact-enough machine state using actual machine coordinates."""

    def __init__(self, offsets: Dict[int, Dict[str, float]]):
        self.offsets = offsets
        self.abs_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.cs = 1
        self.absolute = True

    def apply_ops(self, mode_ops: Sequence[Tuple[str, Any]],
                  coordinates: Dict[str, float]) -> None:
        for _letter, code in mode_ops:
            if code == 90:
                self.absolute = True
            elif code == 91:
                self.absolute = False
            elif isinstance(code, int) and 54 <= code <= 59:
                self.cs = code - 53
            elif code == 59.1:
                self.cs = 7
            elif code == 59.2:
                self.cs = 8
            elif code == 59.3:
                self.cs = 9

        if not coordinates:
            return

        offset = self.offset()
        for axis, value in coordinates.items():
            if axis not in self.abs_pos:
                continue
            if self.absolute:
                self.abs_pos[axis] = value + offset.get(axis, 0.0)
            else:
                self.abs_pos[axis] += value

    def process(self, gcode: str) -> None:
        mode_ops, coordinates = parse_gcode_ops(gcode)
        self.apply_ops(mode_ops, coordinates)

    def offset(self) -> Dict[str, float]:
        return self.offsets.get(self.cs, dict(DEFAULT_OFFSET))

    def work_position(self) -> Dict[str, float]:
        offset = self.offset()
        return {axis: self.abs_pos[axis] - offset.get(axis, 0.0)
                for axis in ("X", "Y", "Z")}


class PygcodeMotion:
    """The unmodified upstream Machine used for short-archive replay checks."""

    def __init__(self, offsets: Dict[int, Dict[str, float]]):
        self.machine = Machine()
        for cs_id, offset in offsets.items():
            self.machine.state.coord_systems[cs_id].offset.update(
                X=offset["X"], Y=offset["Y"], Z=offset["Z"]
            )

    def process(self, gcode: str) -> None:
        self.machine.process_str(gcode)

    @property
    def cs(self) -> int:
        return self.machine.state.cur_coord_sys

    @property
    def abs_pos(self) -> Dict[str, float]:
        pos = self.machine.abs_pos
        return {"X": float(pos.X), "Y": float(pos.Y), "Z": float(pos.Z)}


def _is_marked(line: Dict[str, Any], template_name: str,
               marked_keys: set, marked_ids: set) -> bool:
    line_number = line.get("line")
    if line_number is not None and (template_name, int(line_number)) in marked_keys:
        return True
    mark_id = line.get("mark_id")
    return mark_id is not None and mark_id in marked_ids


def iter_sample_evidence(archive: Dict[str, Any], verify: bool = False) -> Iterator[Dict[str, Any]]:
    templates = archive["templates"]
    marked_keys = archive["marked_keys"]
    marked_ids = archive["marked_ids"]
    offsets = archive["offsets"]
    state = BuiltinMotion(offsets)
    checker = PygcodeMotion(offsets) if verify else None
    template_occurrences: Dict[str, int] = {}
    sample_order = 0

    def make_evidence(template_name: str, line: Dict[str, Any],
                      stack: List[Dict[str, Any]], kind: str = "gcode") -> Dict[str, Any]:
        nonlocal sample_order
        sample_order += 1
        abs_pos = state.abs_pos
        offset = state.offset()
        work_pos = state.work_position()
        machine_point = point_key(abs_pos["X"], abs_pos["Y"])
        evidence = {
            "order": sample_order,
            "kind": kind,
            "root_template": stack[0]["template"] if stack else template_name,
            "template": template_name,
            "stack": deepcopy(stack),
            "layer_repeats": [frame["repeat"] for frame in stack],
            "original_line": line.get("line"),
            "gcode": line.get("gcode", line.get("call", "")),
            "coordinate_system": cs_name(state.cs),
            "machine_position": {"X": machine_point[0], "Y": machine_point[1]},
            "machine_position_z": lattice_value(abs_pos["Z"]),
            "work_position": {
                "X": lattice_value(work_pos["X"]),
                "Y": lattice_value(work_pos["Y"]),
                "Z": lattice_value(work_pos["Z"]),
            },
            "work_offset": {
                "X": lattice_value(offset["X"]),
                "Y": lattice_value(offset["Y"]),
                "Z": lattice_value(offset["Z"]),
            },
        }

        if checker is not None:
            checker_abs = checker.abs_pos
            for axis in ("X", "Y", "Z"):
                if abs(checker_abs[axis] - abs_pos[axis]) > 1e-7:
                    raise SampleArchiveError(
                        "Machine replay mismatch at %s:%s: builtin=%r pygcode=%r"
                        % (template_name, line.get("line"), abs_pos, checker_abs)
                    )
            evidence["verified_with_pygcode_machine"] = True
        return evidence

    def visit_call(call: Dict[str, Any], parent_stack: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        child_name = call["call"]
        repeat = int(call.get("repeat", 1))
        for repeat_index in range(1, repeat + 1):
            template_occurrences[child_name] = template_occurrences.get(child_name, 0) + 1
            frame = {
                "template": child_name,
                "layer": len(parent_stack) + 1,
                "repeat": repeat_index,
                "template_occurrence": template_occurrences[child_name],
                "call_line": call.get("line"),
            }
            stack = parent_stack + [frame]
            template = templates[child_name]

            for line in template["lines"]:
                if "call" in line:
                    yield from visit_call(line, stack)
                    if _is_marked(line, child_name, marked_keys, marked_ids):
                        yield make_evidence(child_name, line, stack, kind="call_after")
                    continue

                gcode = line.get("gcode", "")
                state.process(gcode)
                if checker is not None:
                    checker.process(gcode)
                if _is_marked(line, child_name, marked_keys, marked_ids):
                    yield make_evidence(child_name, line, stack)

    for root_call in archive["program"]:
        root_call = deepcopy(root_call)
        root_call.setdefault("line", root_call.get("line"))
        yield from visit_call(root_call, [])


def iter_machine_points(archive: Dict[str, Any]) -> Iterator[Point]:
    """Yield marked machine XY points without building first-hit evidence."""
    templates = archive["templates"]
    marked_keys = archive["marked_keys"]
    marked_ids = archive["marked_ids"]
    state = BuiltinMotion(archive["offsets"])

    compiled_templates: Dict[str, List[tuple]] = {}
    for template_name, template in templates.items():
        operations: List[tuple] = []
        for line in template["lines"]:
            marked = _is_marked(line, template_name, marked_keys, marked_ids)
            if "call" in line:
                operations.append((
                    "call", line["call"], max(1, int(line.get("repeat", 1))), marked
                ))
            else:
                operations.append((
                    "move", *parse_gcode_ops(line.get("gcode", "")), marked
                ))
        compiled_templates[template_name] = operations

    def visit_call(child_name: str, repeat_count: int) -> Iterator[Point]:
        for _repeat_index in range(repeat_count):
            for operation in compiled_templates[child_name]:
                if operation[0] == "call":
                    _, next_name, next_repeat, marked = operation
                    yield from visit_call(next_name, next_repeat)
                    if marked:
                        yield point_key(state.abs_pos["X"], state.abs_pos["Y"])
                    continue

                _, mode_ops, coordinates, marked = operation
                state.apply_ops(mode_ops, coordinates)
                if marked:
                    yield point_key(state.abs_pos["X"], state.abs_pos["Y"])

    for root_call in archive["program"]:
        yield from visit_call(
            root_call["call"], max(1, int(root_call.get("repeat", 1)))
        )


def normalize_window(window: Any) -> Optional[Tuple[int, int, int, int]]:
    if window is None:
        return None
    if isinstance(window, dict):
        if "xmin" in window or "x_min" in window:
            xmin = _first_key(window, ("xmin", "x_min", "min_x", "left"))
            xmax = _first_key(window, ("xmax", "x_max", "max_x", "right"))
            ymin = _first_key(window, ("ymin", "y_min", "min_y", "bottom"))
            ymax = _first_key(window, ("ymax", "y_max", "max_y", "top"))
        elif "min" in window and "max" in window:
            min_point = window["min"]
            max_point = window["max"]
            xmin, ymin = min_point[0], min_point[1]
            xmax, ymax = max_point[0], max_point[1]
        else:
            raise SampleArchiveError("window object needs xmin/xmax/ymin/ymax")
    elif isinstance(window, Sequence) and not isinstance(window, (str, bytes)) and len(window) == 4:
        xmin, ymin, xmax, ymax = window
    else:
        raise SampleArchiveError("window must be [xmin, ymin, xmax, ymax] or object")

    xmin_i, xmax_i = lattice_value(xmin), lattice_value(xmax)
    ymin_i, ymax_i = lattice_value(ymin), lattice_value(ymax)
    if xmin_i > xmax_i:
        xmin_i, xmax_i = xmax_i, xmin_i
    if ymin_i > ymax_i:
        ymin_i, ymax_i = ymax_i, ymin_i
    return xmin_i, ymin_i, xmax_i, ymax_i


def point_in_window(point: Point, window: Optional[Tuple[int, int, int, int]]) -> bool:
    if window is None:
        return True
    x, y = point
    xmin, ymin, xmax, ymax = window
    return xmin <= x <= xmax and ymin <= y <= ymax


def target_machine_point(archive: Dict[str, Any],
                         position: Any = None,
                         work_position: Any = None,
                         coordinate_system: Any = None) -> Optional[Point]:
    if position is None and work_position is None:
        target = archive.get("target")
        if target is None:
            return None
        if isinstance(target, dict) and ("work" in target or "work_position" in target):
            work_position = _first_key(target, ("work", "work_position"))
            coordinate_system = _first_key(target, ("coordinate_system", "cs"), "G54")
        else:
            position = target

    if position is not None:
        if isinstance(position, dict):
            return point_key(_first_key(position, ("X", "x")), _first_key(position, ("Y", "y")))
        return point_key(position[0], position[1])

    if work_position is not None:
        if isinstance(work_position, dict):
            wx = _first_key(work_position, ("X", "x"))
            wy = _first_key(work_position, ("Y", "y"))
        else:
            wx, wy = work_position[0], work_position[1]
        cs_id = normalize_cs(coordinate_system or "G54")
        offset = archive["offsets"][cs_id]
        return point_key(lattice_value(wx) + offset["X"], lattice_value(wy) + offset["Y"])

    return None


def count_window(archive: Any, window: Any = None, verify: bool = False) -> int:
    archive = load_archive(archive)
    normalized_window = normalize_window(window if window is not None else archive.get("window"))
    seen = set()
    if verify:
        for evidence in iter_sample_evidence(archive, verify=True):
            point = (evidence["machine_position"]["X"], evidence["machine_position"]["Y"])
            if point_in_window(point, normalized_window):
                seen.add(point)
    else:
        for point in iter_machine_points(archive):
            if point_in_window(point, normalized_window):
                seen.add(point)
    return len(seen)


def first_at(archive: Any,
             position: Any = None,
             work_position: Any = None,
             coordinate_system: Any = None,
             verify: bool = False) -> Optional[Dict[str, Any]]:
    archive = load_archive(archive)
    target = target_machine_point(archive, position, work_position, coordinate_system)
    if target is None:
        raise SampleArchiveError("a target machine position is required")
    for evidence in iter_sample_evidence(archive, verify=verify):
        point = (evidence["machine_position"]["X"], evidence["machine_position"]["Y"])
        if point == target:
            return evidence
    return None


def audit(archive_or_path: Any,
          window: Any = None,
          position: Any = None,
          work_position: Any = None,
          coordinate_system: Any = None,
          verify: bool = False,
          include_points: bool = False) -> Dict[str, Any]:
    archive = load_archive(archive_or_path)
    normalized_window = normalize_window(window if window is not None else archive.get("window"))
    target = target_machine_point(archive, position, work_position, coordinate_system)

    window_points: Optional[Set[Point]] = None
    if include_points:
        window_points = {
            point for point in iter_machine_points(archive)
            if point_in_window(point, normalized_window)
        }
        distinct_count = len(window_points)
    else:
        distinct_count = count_window(archive, normalized_window, verify=verify)

    first_target = None
    if target is not None:
        first_target = first_at(archive, position=target, verify=verify)

    result = {
        "window": normalized_window,
        "target_machine_position": {"X": target[0], "Y": target[1]} if target else None,
        "window_distinct_plane_positions": distinct_count,
        "first_at_target": first_target,
        "verified_with_pygcode_machine": verify,
    }
    if include_points and window_points is not None:
        result["window_points"] = [{"X": x, "Y": y} for x, y in sorted(window_points)]
    return result


class SampleArchive:
    """Loaded template archive with machine-coordinate sample queries."""

    def __init__(self, source: Any):
        self.data = load_archive(source)

    def count_window(self, window: Any = None, verify: bool = False) -> int:
        return count_window(self.data, window=window, verify=verify)

    def first_at(self, position: Any = None, work_position: Any = None,
                 coordinate_system: Any = None, verify: bool = False) -> Optional[Dict[str, Any]]:
        return first_at(
            self.data,
            position=position,
            work_position=work_position,
            coordinate_system=coordinate_system,
            verify=verify,
        )

    def audit(self, window: Any = None, position: Any = None,
              work_position: Any = None, coordinate_system: Any = None,
              verify: bool = False, include_points: bool = False) -> Dict[str, Any]:
        return audit(
            self.data,
            window=window,
            position=position,
            work_position=work_position,
            coordinate_system=coordinate_system,
            verify=verify,
            include_points=include_points,
        )

    def verify_short(self) -> Dict[str, Any]:
        return verify_short_archive(self.data)

    def machine_points(self) -> Iterator[Point]:
        return iter_machine_points(self.data)


def replay_gcode_lines(lines: Iterable[str],
                       offsets: Any = None,
                       marked_lines: Optional[Iterable[int]] = None) -> Dict[str, Any]:
    """Replay an already-expanded short G-code program with upstream Machine."""
    normalized_offsets = normalize_offsets(offsets)
    checker = PygcodeMotion(normalized_offsets)
    marks = {int(line_number) for line_number in (marked_lines or [])}

    rows = []
    for line_number, gcode in enumerate(lines, start=1):
        checker.process(gcode)
        if marks and line_number not in marks:
            continue
        abs_pos = checker.abs_pos
        offset = normalized_offsets[checker.cs]
        rows.append({
            "original_line": line_number,
            "gcode": gcode,
            "coordinate_system": cs_name(checker.cs),
            "machine_position": {
                "X": lattice_value(abs_pos["X"]),
                "Y": lattice_value(abs_pos["Y"]),
            },
            "machine_position_z": lattice_value(abs_pos["Z"]),
            "work_position": {
                "X": lattice_value(abs_pos["X"] - offset["X"]),
                "Y": lattice_value(abs_pos["Y"] - offset["Y"]),
                "Z": lattice_value(abs_pos["Z"] - offset["Z"]),
            },
            "work_offset": {
                "X": lattice_value(offset["X"]),
                "Y": lattice_value(offset["Y"]),
                "Z": lattice_value(offset["Z"]),
            },
        })
    return {"verified_rows": len(rows), "rows": rows}


def replay_gcode_text(gcode: str,
                      offsets: Any = None,
                      marked_lines: Optional[Iterable[int]] = None) -> Dict[str, Any]:
    return replay_gcode_lines(gcode.splitlines(), offsets=offsets, marked_lines=marked_lines)


def replay_gcode_file(path: str,
                      offsets: Any = None,
                      marked_lines: Optional[Iterable[int]] = None) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return replay_gcode_lines(
            handle.read().splitlines(), offsets=offsets, marked_lines=marked_lines
        )


def verify_short_archive(archive_or_path: Any) -> Dict[str, Any]:
    archive = load_archive(archive_or_path)
    rows = []
    for evidence in iter_sample_evidence(archive, verify=True):
        rows.append({
            "order": evidence["order"],
            "template": evidence["template"],
            "original_line": evidence["original_line"],
            "machine_position": evidence["machine_position"],
            "coordinate_system": evidence["coordinate_system"],
            "work_position": evidence["work_position"],
        })
    return {"verified_rows": len(rows), "rows": rows}
