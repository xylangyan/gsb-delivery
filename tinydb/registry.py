"""
Pure helpers for the table-generation metadata stored alongside the tables.

The storage layout gains a single reserved top-level key while the document
format of every individual table stays unchanged::

    {
        '__tinydb_meta__': {
            'table_versions': {'north': 2, 'units': 1},
            'aliases': {
                'north': [{'version': 1, 'table': 'units'}],
                'south': [{'version': 1, 'table': 'units'}],
            },
        },
        'north': {...},
        'units': {...},
    }

``table_versions`` records the generation of every *live* table name. A name
disappears from it once the name is retired by a merge; its history then lives
on in ``aliases``. Reusing a name allocates a fresh (higher) generation, so a
late write addressed to the old generation can never land in the new table.

Databases written before this feature existed simply have no metadata. Every
physical table in such a database is interpreted as generation 1.
"""

from copy import deepcopy
from typing import Optional

from .references import DEFAULT_TABLE_VERSION

__all__ = (
    'META_KEY',
    'get_meta',
    'ensure_meta',
    'get_version',
    'bump_version',
    'retire_name',
    'forget_name',
    'add_alias',
    'alias_entry',
    'latest_alias_entry',
    'alias_lookup',
    'alias_doc_ids',
    'rewrite_aliases_pointing_at',
    'is_live',
    'register_table',
    'generations_for',
    'physical_table_names',
    'normalize_data',
)

#: Reserved top-level storage key holding the generation metadata.
META_KEY = '__tinydb_meta__'
_VERSIONS_KEY = 'table_versions'
_ALIASES_KEY = 'aliases'


def get_meta(data: Optional[dict]) -> dict:
    """
    Return the generation metadata or an empty dict without mutating ``data``.
    """

    if not data:
        return {}

    meta = data.get(META_KEY)
    if not isinstance(meta, dict):
        return {}

    return meta


def ensure_meta(data: dict) -> dict:
    """
    Make sure ``data`` contains a well-formed metadata entry and return it.

    Physical tables that exist but have no recorded generation (this is the
    case for databases written before generations existed) are backfilled as
    generation :data:`~tinydb.references.DEFAULT_TABLE_VERSION`.
    """

    meta = data.get(META_KEY)
    if not isinstance(meta, dict):
        meta = {}
        data[META_KEY] = meta

    if not isinstance(meta.get(_VERSIONS_KEY), dict):
        meta[_VERSIONS_KEY] = {}
    if not isinstance(meta.get(_ALIASES_KEY), dict):
        meta[_ALIASES_KEY] = {}

    for name in physical_table_names(data):
        meta[_VERSIONS_KEY].setdefault(
            name, DEFAULT_TABLE_VERSION
        )

    return meta


def get_version(data: Optional[dict], name: str) -> int:
    """
    Return the current generation of a live table name.

    Names absent from the metadata (including all legacy databases) are
    treated as generation 1.
    """

    meta = get_meta(data)
    versions = meta.get(_VERSIONS_KEY, {})
    version = versions.get(name)

    return version if isinstance(version, int) else 1


def bump_version(data: dict, name: str) -> int:
    """
    Allocate the next generation for ``name`` and record it as live.

    The new generation is one above every generation already known for the
    name, whether that generation is still live or only survives in the
    alias history after a merge.

    :returns: the newly allocated generation number.
    """

    meta = ensure_meta(data)
    versions = meta[_VERSIONS_KEY]

    known = [version for version, _ in generations_for(data, name)]
    new_version = (max(known) + 1) if known else DEFAULT_TABLE_VERSION
    versions[name] = new_version

    return new_version


def retire_name(data: dict, name: str) -> None:
    """
    Mark a live table name as retired. Its alias history is preserved.
    """

    meta = ensure_meta(data)
    meta[_VERSIONS_KEY].pop(name, None)


def forget_name(data: dict, name: str) -> None:
    """
    Remove every trace of a name: its live entry and its whole alias history.
    """

    meta = ensure_meta(data)
    meta[_VERSIONS_KEY].pop(name, None)
    meta[_ALIASES_KEY].pop(name, None)


def add_alias(
    data: dict,
    name: str,
    version: int,
    target: str,
    doc_ids: Optional[dict[str, list[int]]] = None,
) -> None:
    """
    Record that generation ``version`` of ``name`` now lives in ``target``.

    ``doc_ids`` maps every old document ID (as a string) to the list of IDs
    its documents received in ``target``. It allows references that only
    stored the old table name and document ID to find the moved documents.
    """

    meta = ensure_meta(data)
    aliases = meta[_ALIASES_KEY]
    entries = aliases.setdefault(name, [])

    payload = {'version': version, 'table': target}
    if doc_ids is not None:
        payload['doc_ids'] = doc_ids

    for entry in entries:
        if entry.get('version') == version:
            entry['table'] = target
            if doc_ids is not None:
                entry['doc_ids'] = doc_ids
            return

    entries.append(payload)


def alias_entry(
    data: Optional[dict], name: str, version: int
) -> Optional[dict]:
    """
    Return the raw alias entry for the given generation, if one exists.
    """

    meta = get_meta(data)
    for entry in meta.get(_ALIASES_KEY, {}).get(name, []):
        if entry.get('version') == version:
            return entry

    return None


def latest_alias_entry(data: Optional[dict], name: str) -> Optional[dict]:
    """
    Return the highest-generation alias entry recorded for ``name``.
    """

    meta = get_meta(data)
    entries = meta.get(_ALIASES_KEY, {}).get(name, [])
    if not entries:
        return None

    return max(entries, key=lambda entry: entry.get('version', 0))


def alias_doc_ids(
    data: Optional[dict], name: str, version: int
) -> dict[str, list[int]]:
    """
    Return the old-ID -> new-ID(s) map stored for a merged generation.
    """

    entry = alias_entry(data, name, version)
    if entry is None:
        return {}

    doc_ids = entry.get('doc_ids')
    return doc_ids if isinstance(doc_ids, dict) else {}


def rewrite_aliases_pointing_at(
    data: dict,
    source_name: str,
    target_name: str,
    source_id_map: dict[int, list[int]],
) -> None:
    """
    Re-point every alias that targets ``source_name`` at ``target_name``.

    When a table is itself merged away, aliases of older generations that
    pointed at it must follow the documents to their new home. Their document
    ID maps are composed with ``source_id_map`` so they keep resolving to the
    same physical documents.
    """

    meta = ensure_meta(data)
    string_id_map = {
        str(old_id): new_ids for old_id, new_ids in source_id_map.items()
    }

    for entries in meta[_ALIASES_KEY].values():
        for entry in entries:
            if entry.get('table') != source_name:
                continue

            entry['table'] = target_name

            mapped: dict[str, list[int]] = {}
            doc_ids = entry.get('doc_ids')
            if not isinstance(doc_ids, dict):
                continue

            for old_id, intermediate_ids in doc_ids.items():
                composed: list[int] = []
                for intermediate_id in intermediate_ids:
                    composed.extend(
                        string_id_map.get(str(intermediate_id), [])
                    )
                mapped[old_id] = composed

            entry['doc_ids'] = mapped


def alias_lookup(
    data: Optional[dict], name: str, version: int
) -> Optional[str]:
    """
    Return the table currently holding ``name``'s given generation, if known.
    """

    entry = alias_entry(data, name, version)
    if entry is None:
        return None

    target = entry.get('table')
    return target if isinstance(target, str) else None


def is_live(data: Optional[dict], name: str) -> bool:
    """
    Return whether ``name`` currently denotes a live (non-retired) table.
    """

    return name in get_meta(data).get(_VERSIONS_KEY, {})


def register_table(data: dict, name: str) -> int:
    """
    Record a table's generation the first time it is persisted.

    Tables that already have a generation (live or backfilled) keep it.
    Returns the recorded generation.
    """

    meta = ensure_meta(data)
    versions = meta[_VERSIONS_KEY]
    versions.setdefault(name, DEFAULT_TABLE_VERSION)

    return versions[name]


def generations_for(data: Optional[dict], name: str) -> list[tuple[int, str]]:
    """
    Return every generation known for ``name`` as ``(version, current_table)``
    pairs, sorted by generation. This includes both merged-away generations
    (from the alias history) and the currently live generation, if any.
    """

    meta = get_meta(data)
    found: dict[int, str] = {}

    for entry in meta.get(_ALIASES_KEY, {}).get(name, []):
        version = entry.get('version')
        target = entry.get('table')
        if isinstance(version, int) and isinstance(target, str):
            found[version] = target

    live_version = meta.get(_VERSIONS_KEY, {}).get(name)
    if isinstance(live_version, int):
        found[live_version] = name

    return sorted(found.items())


def physical_table_names(data: Optional[dict]) -> set[str]:
    """
    Return the names of the actual tables, excluding the metadata entry.
    """

    if not data:
        return set()

    return {key for key in data.keys() if key != META_KEY}


def normalize_data(data: Optional[dict]) -> Optional[dict]:
    """
    Return a deep copy of storage data with a well-formed metadata entry.

    Used when a table needs an isolated, stable view of the database (for
    example a read-only snapshot).
    """

    if data is None:
        return None

    normalized = deepcopy(data)
    ensure_meta(normalized)

    return normalized
