"""IDNA host helpers shared by extraction and site-key migration.

IDNA 2008 canonicalization keeps the Unicode host ``straße.de`` distinct from
``strasse.de`` while still treating the Unicode spelling and its ACE (A-label)
form as one host. IDNA 2003, used by legacy archives, folded both onto
``strasse.de``.
"""

from __future__ import annotations

import idna

_DOT_EQUIVALENTS = ("。", "．", "｡")


def canonical_host(host: str) -> str:
    """Return the host in canonical IDNA 2008 A-label form.

    Unicode dot variants are normalized to ``.``, a trailing root label is
    dropped, and every label is converted with UTS #46 processing. The Unicode
    and ACE forms of the same host therefore share one result::

        >>> canonical_host("straße.de") == canonical_host("xn--strae-oqa.de")
        True
        >>> canonical_host("例え.テスト")
        'xn--r8jz45g.xn--zckzah'

    Raises:
        UnicodeError: If a label cannot be represented as an IDNA 2008 A-label.
    """
    labels = split_labels(host)
    if not labels:
        return ""
    return ".".join(
        idna.encode(label, uts46=True, std3_rules=False).decode("ascii")
        for label in labels
    )


def legacy_host(host: str) -> str:
    """Return the host as a legacy IDNA 2003 A-label.

    A-labels are first decoded with IDNA 2008 so that both the Unicode and ACE
    spellings of a host flow through the same IDNA 2003 mapping. This is what
    reproduces the historical ``straße.de`` / ``strasse.de`` collision::

        >>> legacy_host("straße.de")
        'strasse.de'
        >>> legacy_host("xn--strae-oqa.de")
        'strasse.de'

    Labels that IDNA 2003 cannot encode are returned lowercased unchanged so
    that the original evidence is never lost.
    """
    encoded = []
    for label in split_labels(host):
        lowered = label.lower()
        if lowered.startswith("xn--"):
            try:
                lowered = idna.decode(lowered, uts46=True)
            except (UnicodeError, IndexError):
                pass
        try:
            encoded.append(lowered.encode("idna").decode("ascii"))
        except UnicodeError:
            encoded.append(lowered)
    return ".".join(encoded)


def split_labels(host: str) -> list[str]:
    """Split a host into labels after normalizing dot variants."""
    normalized = host.strip()
    for dot in _DOT_EQUIVALENTS:
        normalized = normalized.replace(dot, ".")
    normalized = normalized.rstrip(".")
    if not normalized:
        return []
    return normalized.split(".")
