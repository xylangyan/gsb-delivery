"""Work-order manifest loading.

A work order is a JSON document describing two SQL scripts (the previous
shift's delivered order and the current one), the physical metadata of
every external table, and the read-only sealed temporary generations the
receiving end is allowed to reuse.

Keeping the data contract explicit means the verifier never touches a
real database: every finite alternative, byte cost and fee comes from
the manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlparse.reconcile.model import (
    AllowedSeal,
    ExternalVersion,
    RowMeta,
)


@dataclass
class WorkOrder:
    previous_sql: str
    current_sql: str
    externals: dict[str, ExternalVersion]
    allowed_seals: list[AllowedSeal]


def load_work_order(path: str | Path) -> WorkOrder:
    path = Path(path)
    payload = json.loads(path.read_text(encoding='utf-8'))
    base_dir = path.parent
    return WorkOrder(
        previous_sql=_resolve_sql(payload, 'previous', base_dir),
        current_sql=_resolve_sql(payload, 'current', base_dir),
        externals={
            name: _external(name, spec)
            for name, spec in payload.get('externals', {}).items()
        },
        allowed_seals=[
            AllowedSeal(name=item['name'], generation=item['generation'])
            for item in payload.get('allowed_seals', [])
        ],
    )


def _resolve_sql(payload: dict, prefix: str, base_dir: Path) -> str:
    inline = payload.get(f'{prefix}_sql')
    if inline is not None:
        return inline
    file_name = payload.get(f'{prefix}_file')
    if file_name is None:
        raise ValueError(
            f'work order must define {prefix}_sql or {prefix}_file'
        )
    return (base_dir / file_name).read_text(encoding='utf-8')


def _external(name: str, spec: dict) -> ExternalVersion:
    columns = list(spec['columns'])
    rows = [tuple(row) for row in spec.get('rows', [])]
    row_meta = [
        RowMeta(line_no=item.get('line_no', index + 1), pk=item.get('pk'))
        for index, item in enumerate(spec.get('row_meta', []))
    ]
    if len(rows) != len(row_meta):
        raise ValueError(
            f'external table {name!r}: rows and row_meta must have equal length'
        )
    variants = {
        label: [tuple(row) for row in data]
        for label, data in spec.get('variants', {}).items()
    }
    variant_costs = {
        label: (int(cost[0]), float(cost[1]))
        for label, cost in spec.get('variant_costs', {}).items()
    }
    previous_rows = spec.get('previous_rows')
    return ExternalVersion(
        table=name,
        instance=spec['instance'],
        columns=columns,
        bytes=int(spec['bytes']),
        fee=float(spec['fee']),
        rows=rows,
        row_meta=row_meta,
        variants=variants,
        variant_costs=variant_costs,
        previous_instance=spec.get('previous_instance'),
        previous_rows=(
            [tuple(row) for row in previous_rows]
            if previous_rows is not None
            else None
        ),
    )
