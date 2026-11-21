# -*- coding: UTF-8 -*-
#
# Tests for ExcelCompiler.restrict_to(): declare the ranges a consumer may
# use, trim everything else, export a text artifact, and evaluate it in a
# process without the original workbook.

import os

import pytest
from openpyxl import Workbook

from pycel import ExcelCompiler, RestrictedAccessError


# the three detail blocks the publisher is willing to share, plus the summary
ALLOWED_OUTPUTS = (
    'Report!B2:B4',   # Monday
    'Report!C2:C4',   # Tuesday (formulas: cross-sheet rate + fixed coefficient)
    'Report!D2:D4',   # Wednesday (contains a known blank)
    'Report!B6',      # monthly report summary
)
# declared inputs: the day selector and the modifiable rate.  The fixed
# coefficient (Rates!B2) is needed to compute Tuesday, so it is carried
# into the artifact, but it is not a directly selectable detail.
ALLOWED_INPUTS = ('Config!B1', 'Rates!B1')

CHECK_DIGITS = (99001, 99002, 99003)  # column E: not approved for export


def build_report_workbook(filename):
    wb = Workbook()
    report = wb.active
    report.title = 'Report'
    config = wb.create_sheet('Config')
    rates = wb.create_sheet('Rates')

    # selector, defaults to Monday
    config['B1'] = 'Monday'

    rates['B1'] = 1.5  # modifiable rate (declared input)
    rates['B2'] = 2.0  # fixed coefficient (carried, not declared)

    report['B1'] = 'Monday'
    report['C1'] = 'Tuesday'
    report['D1'] = 'Wednesday'

    # Monday: plain values
    report['B2'] = 10
    report['B3'] = 20
    report['B4'] = 30

    # Tuesday: formulas using the cross-sheet rate and fixed coefficient
    for row in (2, 3, 4):
        report['C{}'.format(row)] = '=B{0}*Rates!$B$1+Rates!$B$2'.format(row)

    # Wednesday: normal values with a known blank (D3 is defined empty)
    report['D2'] = 5
    report['D4'] = 7

    # check digits, not approved to leave the workbook
    for row, value in zip((2, 3, 4), CHECK_DIGITS):
        report['E{}'.format(row)] = value

    # a column unrelated to the monthly report
    report['G2'] = 'unrelated'
    report['G3'] = 12345

    # the monthly report summary, driven by the selector
    report['B6'] = ('=IF(Config!$B$1="Tuesday",SUM(C2:C4),'
                    'IF(Config!$B$1="Wednesday",SUM(D2:D4),SUM(B2:B4)))')

    wb.save(filename)
    return filename


@pytest.fixture()
def published_artifact(tmp_path):
    """Compile, restrict, trim and export; then remove the workbook"""
    xlsx = str(tmp_path / 'report.xlsx')
    build_report_workbook(xlsx)
    compiler = ExcelCompiler(xlsx)

    # the default configuration only touches Monday
    assert compiler.evaluate('Report!B6') == 60

    compiler.restrict_to(ALLOWED_OUTPUTS, ALLOWED_INPUTS)
    artifact = str(tmp_path / 'report.yml')
    compiler.to_file(artifact)

    # move the original workbook away: the consumer runs without it
    consumer_dir = tmp_path / 'consumer'
    consumer_dir.mkdir()
    moved = str(consumer_dir / 'report.yml')
    os.replace(artifact, moved)
    os.unlink(xlsx)
    return moved


def test_restricted_publish_flow(published_artifact):
    compiled = ExcelCompiler.from_file(published_artifact)

    # default configuration: only Monday is accessed
    assert compiled.evaluate('Report!B6') == 60

    # switch to Tuesday: formulas and their inputs compute after cold start
    compiled.set_value('Config!B1', 'Tuesday')
    assert compiled.evaluate('Report!C2:C4') == (17, 32, 47)
    assert compiled.evaluate('Report!B6') == 96

    # updating the declared rate is seen by the Tuesday formulas
    compiled.set_value('Rates!B1', 2.0)
    assert compiled.evaluate('Report!C2:C4') == (22, 42, 62)
    assert compiled.evaluate('Report!B6') == 126

    # switch to Wednesday: the known blank enters the aggregation with
    # the workbook's blank semantics (SUM ignores it)
    compiled.set_value('Config!B1', 'Wednesday')
    assert compiled.evaluate('Report!B6') == 12

    # a permitted empty cell reads as empty, it is not an access error
    assert compiled.evaluate('Report!D3') is None
    assert compiled.evaluate('Report!D2:D4') == (5, None, 7)


def test_undeclared_addresses_raise(published_artifact):
    compiled = ExcelCompiler.from_file(published_artifact)

    # a fully undeclared cell: the error names the requested address
    with pytest.raises(RestrictedAccessError) as exc_info:
        compiled.evaluate('Report!E2')
    assert 'Report!E2' in str(exc_info.value)

    # the carried but undeclared coefficient is not a selectable detail
    with pytest.raises(RestrictedAccessError) as exc_info:
        compiled.evaluate('Rates!B2')
    assert 'Rates!B2' in str(exc_info.value)

    # an unrelated trimmed column is not readable either
    with pytest.raises(RestrictedAccessError):
        compiled.evaluate('Report!G3')

    # an address only partially overlapping the permitted ranges: the
    # error names the requested address and the undeclared portion, and
    # the undeclared column is not read out
    with pytest.raises(RestrictedAccessError) as exc_info:
        compiled.evaluate('Report!B2:E4')
    message = str(exc_info.value)
    assert 'Report!B2:E4' in message
    assert 'Report!E2:E4' in message

    # setting an undeclared address is rejected as well
    with pytest.raises(RestrictedAccessError) as exc_info:
        compiled.set_value('Report!E2', 0)
    assert 'Report!E2' in str(exc_info.value)

    with pytest.raises(RestrictedAccessError):
        compiled.set_value('Rates!B2', 3.0)


def test_artifact_does_not_contain_undeclared_content(published_artifact):
    with open(published_artifact) as f:
        text = f.read()

    # the check digits column was not approved for export
    for value in CHECK_DIGITS:
        assert str(value) not in text
    for row in (2, 3, 4):
        assert 'Report!E{}'.format(row) not in text

    # the unrelated column was trimmed as well
    assert 'unrelated' not in text
    assert '12345' not in text

    # the declaration itself is persisted in the artifact
    assert 'allowed_ranges' in text
    assert 'Report!B2:B4' in text

    compiled = ExcelCompiler.from_file(published_artifact)
    assert not any(addr.startswith('Report!E') for addr in compiled.cell_map)
    assert not any(addr.startswith('Report!G') for addr in compiled.cell_map)


def test_restricted_pickle_round_trip(tmp_path):
    xlsx = str(tmp_path / 'report.xlsx')
    build_report_workbook(xlsx)
    compiler = ExcelCompiler(xlsx)
    compiler.restrict_to(ALLOWED_OUTPUTS, ALLOWED_INPUTS)
    compiler.to_file(file_types=('pkl',))

    compiled = ExcelCompiler.from_file(xlsx + '.pkl')
    assert compiled.evaluate('Report!B6') == 60
    with pytest.raises(RestrictedAccessError):
        compiled.evaluate('Report!E2')


def test_restricted_json_round_trip(tmp_path):
    xlsx = str(tmp_path / 'report.xlsx')
    build_report_workbook(xlsx)
    compiler = ExcelCompiler(xlsx)
    compiler.restrict_to(ALLOWED_OUTPUTS, ALLOWED_INPUTS)
    compiler.to_file(file_types=('json',))

    compiled = ExcelCompiler.from_file(xlsx + '.json')
    assert compiled.evaluate('Report!B6') == 60
    compiled.set_value('Config!B1', 'Tuesday')
    assert compiled.evaluate('Report!B6') == 96
    with pytest.raises(RestrictedAccessError):
        compiled.evaluate('Report!E2')


def test_unrestricted_compiler_is_unchanged(tmp_path):
    xlsx = str(tmp_path / 'report.xlsx')
    build_report_workbook(xlsx)
    compiler = ExcelCompiler(xlsx)

    # without a declaration everything remains accessible
    assert compiler.evaluate('Report!E2') == CHECK_DIGITS[0]
    assert compiler.evaluate('Report!B2:E4')[0] == (10, 17, 5, CHECK_DIGITS[0])
    compiler.set_value('Rates!B2', 3.0)
    assert compiler.evaluate('Rates!B2') == 3.0


def test_restrict_to_requires_connected_inputs(tmp_path):
    xlsx = str(tmp_path / 'report.xlsx')
    build_report_workbook(xlsx)
    compiler = ExcelCompiler(xlsx)

    # an input that no declared output depends on is rejected
    compiler.evaluate('Report!G3')
    with pytest.raises(ValueError, match='no outputs are dependant on it'):
        compiler.restrict_to(ALLOWED_OUTPUTS, ('Config!B1', 'Report!G3'))
