from click.testing import CliRunner
from csv_diff import cli, load_csv
import csv
import pytest
from .test_csv_diff import ONE, ONE_TSV, TWO, TWO_TSV, THREE, FIVE
import io
import json
from textwrap import dedent


@pytest.fixture
def tsv_files(tmpdir):
    one = tmpdir / "one.tsv"
    one.write(ONE_TSV)
    two = tmpdir / "two.tsv"
    two.write(TWO_TSV)
    return str(one), str(two)


@pytest.fixture
def json_files(tmpdir):
    one = tmpdir / "one.json"
    one.write(
        json.dumps(
            [
                {"id": 1, "name": "Cleo", "nested": {"foo": 3}, "extra": 1},
                {"id": 2, "name": "Pancakes", "nested": {"foo": 3}},
            ]
        )
    )
    two = tmpdir / "two.json"
    two.write(
        json.dumps(
            [
                {"id": 1, "name": "Cleo", "nested": {"foo": 3, "bar": 5}, "extra": 1},
                {"id": 2, "name": "Pancakes!", "nested": {"foo": 3}, "extra": 1},
            ]
        )
    )
    return str(one), str(two)


def test_human_cli(tmpdir):
    one = tmpdir / "one.csv"
    one.write(ONE)
    two = tmpdir / "two.csv"
    two.write(TWO)
    result = CliRunner().invoke(cli.cli, [str(one), str(two), "--key", "id"])
    assert 0 == result.exit_code
    assert (
        dedent(
            """
    1 row changed

      id: 1
        age: "4" => "5"
    """
        ).strip()
        == result.output.strip()
    )


def test_human_cli_alternative_names(tmpdir):
    one = tmpdir / "one.csv"
    one.write(ONE)
    five = tmpdir / "five.csv"
    five.write(FIVE)
    result = CliRunner().invoke(
        cli.cli,
        [str(one), str(five), "--key", "id", "--singular", "tree", "--plural", "trees"],
    )
    assert 0 == result.exit_code, result.output
    assert (
        dedent(
            """
    1 tree changed, 2 trees added

    1 tree changed

      id: 1
        age: "4" => "5"

    2 trees added

      id: 3
      name: Bailey
      age: 1

      id: 4
      name: Carl
      age: 7
    """
        ).strip()
        == result.output.strip()
    )


def test_human_cli_json(tmpdir):
    one = tmpdir / "one.csv"
    one.write(ONE)
    two = tmpdir / "two.csv"
    two.write(TWO)
    result = CliRunner().invoke(cli.cli, [str(one), str(two), "--key", "id", "--json"])
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [{"key": "1", "changes": {"age": ["4", "5"]}}],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_tsv_files(tsv_files):
    one, two = tsv_files
    result = CliRunner().invoke(
        cli.cli, [one, two, "--key", "id", "--json", "--format", "tsv"]
    )
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [{"key": "1", "changes": {"age": ["4", "5"]}}],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_json_files(json_files):
    one, two = json_files
    result = CliRunner().invoke(
        cli.cli,
        [one, two, "--key", "id", "--json", "--format", "json"],
        catch_exceptions=False,
    )
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [
            {"key": 1, "changes": {"nested": ['{"foo": 3}', '{"foo": 3, "bar": 5}']}},
            {
                "key": 2,
                "changes": {"name": ["Pancakes", "Pancakes!"], "extra": [None, 1]},
            },
        ],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_sniff_format(tsv_files):
    one, two = tsv_files
    result = CliRunner().invoke(cli.cli, [one, two, "--key", "id", "--json"])
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [{"key": "1", "changes": {"age": ["4", "5"]}}],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_format_overrides_sniff(tsv_files):
    one, two = tsv_files
    result = CliRunner().invoke(
        cli.cli, [one, two, "--key", "id", "--json", "--format", "csv"]
    )
    assert 1 == result.exit_code


def test_column_containing_dot(tmpdir):
    # https://github.com/simonw/csv-diff/issues/7
    one = tmpdir / "one.csv"
    two = tmpdir / "two.csv"
    one.write(
        dedent(
            """
    id,foo.bar,foo.baz
    1,Dog,Cat
    """
        ).strip()
    )
    two.write(
        dedent(
            """
    id,foo.bar,foo.baz
    1,Dog,Beaver
    """
        ).strip()
    )
    result = CliRunner().invoke(
        cli.cli, [str(one), str(two), "--key", "id", "--json"], catch_exceptions=False
    )
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [{"key": "1", "changes": {"foo.baz": ["Cat", "Beaver"]}}],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_semicolon_delimited(tmpdir):
    # https://github.com/simonw/csv-diff/issues/6
    one = tmpdir / "one.csv"
    two = tmpdir / "two.csv"
    one.write(
        dedent(
            """
    id;name
    1;Mark
    """
        ).strip()
    )
    two.write(
        dedent(
            """
    id;name
    1;Brian
    """
        ).strip()
    )
    result = CliRunner().invoke(
        cli.cli, [str(one), str(two), "--key", "id", "--json"], catch_exceptions=False
    )
    assert 0 == result.exit_code
    assert {
        "added": [],
        "removed": [],
        "changed": [{"key": "1", "changes": {"name": ["Mark", "Brian"]}}],
        "columns_added": [],
        "columns_removed": [],
    } == json.loads(result.output.strip())


def test_diff_with_extras(tmpdir):
    one = tmpdir / "one.json"
    two = tmpdir / "two.json"
    one.write(
        json.dumps(
            [
                {"id": 1, "name": "Cleo", "type": "dog"},
                {"id": 2, "name": "Suna", "type": "chicken"},
            ]
        )
    )
    two.write(
        json.dumps(
            [
                {"id": 2, "name": "Suna", "type": "pretty chicken"},
                {"id": 3, "name": "Artie", "type": "bunny"},
            ]
        )
    )
    result = CliRunner().invoke(
        cli.cli,
        [
            str(one),
            str(two),
            "--key",
            "id",
            "--format",
            "json",
            "--extra",
            "search",
            "https://www.google.com/search?q={name}",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    expected = dedent(
        """
    1 row changed, 1 row added, 1 row removed

    1 row changed

      id: 2
        type: "chicken" => "pretty chicken"
      extras:
        search: https://www.google.com/search?q=Suna

    1 row added

      id: 3
      name: Artie
      type: bunny
      extras:
        search: https://www.google.com/search?q=Artie

    1 row removed

      id: 1
      name: Cleo
      type: dog
      extras:
        search: https://www.google.com/search?q=Cleo
    """
    ).strip()
    assert result.output.strip() == expected


def test_reconcile_cli_json(tmpdir):
    previous = tmpdir / "previous.csv"
    current = tmpdir / "current.csv"
    archive = tmpdir / "archive.csv"
    previous.write("package,location,qty\nA,A仓,7\nB0,B仓,2\n")
    current.write("package,location,qty\nA,A仓,4\nB,B仓,5\n")
    archive.write(
        "occurrence_id,type,from_location,to_location,inputs,outputs,teams\n"
        "S1,split,A仓,,A:7,A:4|A-3:3,A\n"
        "T1,transfer,A仓,B仓,A-3:3,A-3:3,A|B\n"
        "M1,merge,,B仓,A-3:3|B0:2,B:5,B\n"
    )
    result = CliRunner().invoke(
        cli.cli,
        [
            str(previous),
            str(current),
            "--occurrences",
            str(archive),
            "--reconcile",
            "--segment-limit",
            "A=1",
            "--segment-limit",
            "B=1",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["status"] == "ok"
    assert plan["final_matches_target"] is True
    assert [segment["team"] for segment in plan["segments"]] == ["A", "B", "B"]


def test_reconcile_cli_resume_and_text(tmpdir):
    previous = tmpdir / "previous.csv"
    current = tmpdir / "current.csv"
    archive = tmpdir / "archive.csv"
    previous.write("package,location,qty\nA,A仓,7\nB0,B仓,2\n")
    current.write("package,location,qty\nA,A仓,4\nB,B仓,5\n")
    archive.write(
        "occurrence_id,type,from_location,to_location,inputs,outputs,teams\n"
        "S1,split,A仓,,A:7,A:4|A-3:3,A\n"
        "T1,transfer,A仓,B仓,A-3:3,A-3:3,A|B\n"
        "M1,merge,,B仓,A-3:3|B0:2,B:5,B\n"
    )

    first = CliRunner().invoke(
        cli.cli,
        [
            str(previous),
            str(current),
            "--occurrences",
            str(archive),
            "--reconcile",
            "--segment-limit",
            "B=1",
            "--execute-team",
            "B",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert first.exit_code == 0, first.output
    executed = json.loads(first.output)
    assert executed["status"] == "complete"
    assert executed["complete"] is True
    assert executed["checkpoint"]["next_segment"] == 4

    text = CliRunner().invoke(
        cli.cli,
        [
            str(previous),
            str(current),
            "--occurrences",
            str(archive),
            "--reconcile",
        ],
        catch_exceptions=False,
    )
    assert text.exit_code == 0, text.output
    assert "对账修补说明" in text.output
    assert "验收清单" in text.output


def test_plain_diff_still_works_when_occurrences_absent(tmpdir):
    one = tmpdir / "one.csv"
    two = tmpdir / "two.csv"
    one.write(ONE)
    two.write(TWO)
    result = CliRunner().invoke(cli.cli, [str(one), str(two), "--key", "id"])
    assert result.exit_code == 0
    assert "1 row changed" in result.output
