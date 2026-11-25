from pygcode.sample_audit import audit, first_at, verify_short_archive


def absolute_offset_archive():
    return {
        "offsets": {"G54": [100, 200, 0], "G55": [-5, 7, 0]},
        "templates": {
            "Probe": {
                "lines": [
                    {"line": 10, "gcode": "G90"},
                    {"line": 11, "gcode": "G54"},
                    {"line": 12, "gcode": "G0 X0 Y0", "sample": True},
                    {"line": 13, "gcode": "X2", "sample": True},
                    {"line": 14, "gcode": "X2", "sample": True},
                    {"line": 15, "gcode": "G55"},
                    {"line": 16, "gcode": "G0 X0 Y0", "sample": True},
                    {"line": 17, "gcode": "X2", "sample": True},
                ]
            }
        },
        "program": [{"template": "Probe"}],
    }


def nested_relative_archive():
    return {
        "templates": {
            "YLayer": {
                "lines": [
                    {"line": 30, "gcode": "G91"},
                    {"line": 31, "gcode": "Y1", "sample": True},
                ]
            },
            "XLayer": {
                "lines": [
                    {"line": 20, "gcode": "G91"},
                    {"line": 21, "gcode": "X1", "sample": True},
                    {"line": 22, "call": "YLayer", "repeat": 2},
                ]
            },
        },
        "program": [{"template": "XLayer", "repeat": 2}],
    }


def overlapping_templates_archive():
    return {
        "templates": {
            "StepTwo": {
                "lines": [
                    {"line": 40, "gcode": "G90 G0 X0 Y0", "sample": True},
                    {"line": 41, "gcode": "X2", "sample": True},
                    {"line": 42, "gcode": "X4", "sample": True},
                ]
            },
            "StepOne": {
                "lines": [
                    {"line": 50, "gcode": "G90 G0 X1 Y0", "sample": True},
                    {"line": 51, "gcode": "X2", "sample": True},
                    {"line": 52, "gcode": "X3", "sample": True},
                    {"line": 53, "gcode": "X4", "sample": True},
                ]
            },
        },
        "program": [{"template": "StepTwo"}, {"template": "StepOne"}],
    }


def test_absolute_repeats_are_same_and_g54_g55_are_distinct():
    archive = absolute_offset_archive()
    result = audit(archive, window=[-10, 0, 200, 300], verify=True)

    assert result["window_distinct_plane_positions"] == 4
    first = result["first_at_target"]
    assert first is None

    hit = first_at(archive, position=[102, 200], verify=True)
    assert hit["original_line"] == 13
    assert hit["layer_repeats"] == [1]
    assert hit["work_position"] == {"X": 2, "Y": 0, "Z": 0}
    assert hit["work_offset"] == {"X": 100, "Y": 200, "Z": 0}

    g55_hit = first_at(archive, position=[-3, 7], verify=True)
    assert g55_hit["original_line"] == 17
    assert g55_hit["coordinate_system"] == "G55"
    assert g55_hit["work_offset"] == {"X": -5, "Y": 7, "Z": 0}


def test_nested_relative_layers_retain_omitted_y():
    archive = nested_relative_archive()
    result = audit(archive, verify=True, include_points=True)

    assert [point for point in result["window_points"]] == [
        {"X": 1, "Y": 0},
        {"X": 1, "Y": 1},
        {"X": 1, "Y": 2},
        {"X": 2, "Y": 2},
        {"X": 2, "Y": 3},
        {"X": 2, "Y": 4},
    ]

    x_hit = first_at(archive, position=[2, 2])
    assert x_hit["original_line"] == 21
    assert x_hit["template"] == "XLayer"
    assert x_hit["layer_repeats"] == [2]

    y_hit = first_at(archive, position=[2, 3])
    assert y_hit["original_line"] == 31
    assert y_hit["template"] == "YLayer"
    assert y_hit["root_template"] == "XLayer"
    assert y_hit["layer_repeats"] == [2, 1]
    assert [frame["template_occurrence"] for frame in y_hit["stack"]] == [2, 3]


def test_overlapping_templates_count_machine_position_once_but_first_is_execution_order():
    archive = overlapping_templates_archive()
    result = audit(archive, window=[0, 0, 4, 0], verify=True)

    assert result["window_distinct_plane_positions"] == 5
    hit = first_at(archive, position=[2, 0], verify=True)
    assert hit["template"] == "StepTwo"
    assert hit["original_line"] == 41

    later = first_at(archive, position=[3, 0], verify=True)
    assert later["template"] == "StepOne"
    assert later["original_line"] == 52


def test_work_coordinate_target_uses_registered_offset():
    archive = absolute_offset_archive()
    hit = first_at(
        archive,
        work_position=[2, 0],
        coordinate_system="G54",
        verify=True,
    )
    assert hit["machine_position"] == {"X": 102, "Y": 200}
    assert hit["original_line"] == 13


def test_short_archive_replay_returns_every_marked_row():
    result = verify_short_archive(absolute_offset_archive())
    assert result["verified_rows"] == 5
    assert [row["original_line"] for row in result["rows"]] == [12, 13, 14, 16, 17]
    assert all("machine_position" in row for row in result["rows"])
