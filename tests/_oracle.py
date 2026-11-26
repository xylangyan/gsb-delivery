# -*- coding: utf-8 -*-
"""Shared brute-force oracle for reconciliation tests."""

from isbnlib._core import check_digit10, check_digit13, to_isbn13

from isbnrecon.counting import PREFIX_978
from isbnrecon.results import ABSENT


def body_to_key13(body13):
    body_str = str(body13).zfill(12)
    return body_str + check_digit13(body_str)


def brute_support(view, body13):
    check13 = check_digit13(str(body13).zfill(12))
    for bundle in view.bundle_set:
        if (bundle.kind == 'isbn13' and bundle.width == 12
                and bundle.lo <= body13 <= bundle.hi
                and check13 in bundle.check_chars):
            return True
    if PREFIX_978 <= body13 < PREFIX_978 + 10 ** 9:
        body10 = body13 - PREFIX_978
        check10 = check_digit10(str(body10).zfill(9))
        for bundle in view.bundle_set:
            if (bundle.kind == 'isbn10' and bundle.width == 9
                    and bundle.lo <= body10 <= bundle.hi
                    and check10 in bundle.check_chars):
                return True
    return False


def brute_result(view, body13):
    if not brute_support(view, body13):
        return ABSENT
    return view.table.segment(body_to_key13(body13))


def brute_grid(views, region):
    lo, hi = region
    grid = {}
    for body13 in range(lo, hi + 1):
        label = tuple(brute_result(view, body13) for view in views)
        if all(item is ABSENT for item in label):
            continue
        key = body_to_key13(body13)
        if label not in grid:
            grid[label] = [0, key]
        grid[label][0] += 1
        if key < grid[label][1]:
            grid[label][1] = key
    return grid


def assert_grid_matches(utest, got, expected):
    utest.assertEqual(set(got), set(expected))
    for label, (count, min_key) in expected.items():
        cell = got[label]
        utest.assertEqual(cell.count, count, label)
        utest.assertEqual(cell.min_isbn13, min_key, label)


def brute_migrations(before_views, after_views, region):
    lo, hi = region
    migrations = {}
    for body13 in range(lo, hi + 1):
        before = tuple(brute_result(view, body13) for view in before_views)
        after = tuple(brute_result(view, body13) for view in after_views)
        if before == after:
            continue
        if all(item is ABSENT for item in before) and all(
                item is ABSENT for item in after):
            continue
        key = body_to_key13(body13)
        cell = migrations.setdefault((before, after), [0, key])
        cell[0] += 1
        cell[1] = min(cell[1], key)
    return migrations
