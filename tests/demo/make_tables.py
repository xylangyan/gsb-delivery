"""Write the small demo tables used by the audit tests and examples."""

from __future__ import unicode_literals

import os


BASE_RULES = [
    ("0061", "[.1000.0020.0002]", "LATIN SMALL LETTER A"),
    ("0062", "[.1100.0020.0002]", "LATIN SMALL LETTER B"),
    ("0063", "[.1200.0020.0002]", "LATIN SMALL LETTER C"),
    ("0064", "[.1300.0020.0002]", "LATIN SMALL LETTER D"),
    ("0065", "[.1400.0020.0002]", "LATIN SMALL LETTER E"),
    ("0301", "[.0000.0021.0002]", "COMBINING ACUTE ACCENT"),
    ("0327", "[.0000.0022.0002]", "COMBINING CEDILLA"),
    ("0323", "[.0000.0023.0002]", "COMBINING DOT BELOW"),
]


def _table(rules, version="demo"):
    lines = ["@version {0}".format(version)]
    for codepoints, elements, label in rules:
        lines.append("{0} ; {1} # {2}".format(codepoints, elements, label))
    return "\n".join(lines) + "\n"


DEMO_OLD = _table(BASE_RULES)


DEMO_NEW = _table(
    [
        rule for rule in BASE_RULES
        if rule[0] not in ("0301", "0327")
    ] + [
        # A new contraction a+cedilla sorts before a.  Because the matcher
        # skips a single lower-class intervening mark, ``a + cedilla + acute``
        # changes from a+acute to a+cedilla even though no literal adjacency
        # was introduced by NFD itself.
        ("0061 0327", "[.0F00.0020.0002]", "a with cedilla"),
        # Secondary order on the two accents reverses.
        ("0301", "[.0000.0040.0002]", "COMBINING ACUTE ACCENT"),
        ("0327", "[.0000.0022.0002]", "COMBINING CEDILLA"),
    ]
)


def write_tables(directory):
    old_path = os.path.join(directory, "demo-old.txt")
    new_path = os.path.join(directory, "demo-new.txt")
    with open(old_path, "w", encoding="utf-8") as old_file:
        old_file.write(DEMO_OLD)
    with open(new_path, "w", encoding="utf-8") as new_file:
        new_file.write(DEMO_NEW)
    return old_path, new_path


def write_secondary_tables(directory):
    """Tables that differ only in the secondary order of two marks."""
    old = _table(BASE_RULES)
    new = _table([
        rule for rule in BASE_RULES if rule[0] != "0301"
    ] + [("0301", "[.0000.0040.0002]", "COMBINING ACUTE ACCENT")])
    old_path = os.path.join(directory, "secondary-old.txt")
    new_path = os.path.join(directory, "secondary-new.txt")
    with open(old_path, "w", encoding="utf-8") as old_file:
        old_file.write(old)
    with open(new_path, "w", encoding="utf-8") as new_file:
        new_file.write(new)
    return old_path, new_path


def write_push_tables(directory):
    """Only ``d`` moves: it is inserted between ``a`` and ``b``.

    Neighbouring cards keep identical keys but are pushed one rank later.
    """
    old = _table(BASE_RULES)
    new = _table([
        ("0064", "[.1050.0020.0002]", "LATIN SMALL LETTER D") if
        rule[0] == "0064" else rule
        for rule in BASE_RULES
    ])
    old_path = os.path.join(directory, "push-old.txt")
    new_path = os.path.join(directory, "push-new.txt")
    with open(old_path, "w", encoding="utf-8") as old_file:
        old_file.write(old)
    with open(new_path, "w", encoding="utf-8") as new_file:
        new_file.write(new)
    return old_path, new_path
