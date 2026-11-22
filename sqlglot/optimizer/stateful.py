from __future__ import annotations

import typing as t

from sqlglot import expressions as exp
from sqlglot.errors import UnsupportedError
from sqlglot.helper import find_new_name

if t.TYPE_CHECKING:
    from sqlglot._typing import E

    # A single-evaluation boundary is named by its node (a ``Select``, ``CTE`` or
    # ``Subquery``) or by the alias of the CTE/subquery that wraps it.
    Boundaries = t.Iterable[exp.Expr | str] | None


# Functions whose value is not a pure function of their arguments. Each written call is a
# separate evaluation; several references to one projected alias must reuse one value.
STATEFUL_NAMES: frozenset[str] = frozenset({"SAMPLE"})

STATEFUL_TYPES: tuple[type[exp.Expr], ...] = (exp.Rand, exp.Randn, exp.Randstr, exp.Uuid)

# Meta key marking a layer this rewrite created as a single-evaluation boundary.
BOUNDARY = "stateful_boundary"


def is_stateful_call(expression: exp.Expr, names: t.Container[str] | None = None) -> bool:
    """Whether ``expression`` is itself a stateful call node."""
    if isinstance(expression, STATEFUL_TYPES):
        return True
    return isinstance(expression, exp.Func) and expression.name.upper() in (names or STATEFUL_NAMES)


def _scope_walk(expression: exp.Expr) -> t.Iterator[exp.Expr]:
    """Walk ``expression`` without descending into nested queries or CTE bodies."""
    stack: list[exp.Expr] = [expression]
    while stack:
        node = stack.pop()
        yield node
        for child in node.iter_expressions():
            # A nested query or CTE is a separate scope with its own evaluation points.
            if isinstance(child, (exp.Query, exp.CTE)):
                continue
            stack.append(child)


def is_stateful(expression: exp.Expr | None, names: t.Container[str] | None = None) -> bool:
    """Whether ``expression`` contains a stateful call in its own scope."""
    if expression is None:
        return False
    return any(is_stateful_call(node, names) for node in _scope_walk(expression))


def contains_stateful(expression: exp.Expr | None, names: t.Container[str] | None = None) -> bool:
    """Whether ``expression`` contains a stateful call anywhere, including nested queries.

    Use this when deciding whether an expression may be copied or inlined: duplicating a
    nested stateful call would evaluate it a second time.
    """
    if expression is None:
        return False
    return any(is_stateful_call(node, names) for node in expression.walk())


def stateful_calls(expression: exp.Expr, names: t.Container[str] | None = None) -> list[exp.Expr]:
    """Every stateful call node in ``expression``'s own scope."""
    return [node for node in _scope_walk(expression) if is_stateful_call(node, names)]


def stateful_aliases(
    select: exp.Select, names: t.Container[str] | None = None
) -> dict[str, exp.Expr]:
    """Projection aliases of ``select`` whose value is a stateful call in this scope."""
    result: dict[str, exp.Expr] = {}
    for projection in select.selects:
        if isinstance(projection, exp.Alias) and is_stateful(projection.this, names):
            result[projection.alias] = projection.this
    return result


def _query_of(holder: exp.Expr | None) -> exp.Select | None:
    if isinstance(holder, (exp.CTE, exp.Subquery)):
        query = holder.this.unnest()
        return query if isinstance(query, exp.Select) else None
    return None


def is_boundary(
    holder: exp.Expr | None, query: exp.Expr | None = None, boundaries: Boundaries = None
) -> bool:
    """Whether a query layer is guaranteed to evaluate its projections once per input row.

    ``holder`` is the CTE/subquery wrapping ``query``. A layer is a boundary when the caller
    declared it, or when it is a materialized CTE. A plain CTE or derived table is *not* a
    boundary merely because it exists: the optimizer or the warehouse may inline it and
    re-evaluate its projections once per consuming row.
    """
    if holder is None:
        return False

    if isinstance(holder, exp.CTE) and holder.args.get("materialized"):
        return True

    # A layer created by `insert_layer` is this rewrite's own single-evaluation boundary.
    if holder.meta_get(BOUNDARY):
        return True

    alias = holder.alias if isinstance(holder, (exp.CTE, exp.Subquery)) else ""
    for boundary in boundaries or ():
        if isinstance(boundary, str):
            if alias and boundary == alias:
                return True
        elif boundary is holder or boundary is query or boundary is getattr(holder, "this", None):
            return True

    return False


def _raise(
    select: exp.Select,
    where: str,
    names: t.Container[str] | None,
    expression: exp.Expr | None = None,
) -> None:
    root = expression if expression is not None else select
    call = next((node for node in root.walk() if is_stateful_call(node, names)), None)
    expression_sql = call.sql() if call is not None else "stateful expression"
    raise UnsupportedError(
        f"Stateful expression {expression_sql} has no reliable single-evaluation boundary "
        f"in {where}; scope: {select.sql()}"
    )


def _stateful_sources(select: exp.Select, names: t.Container[str] | None) -> dict[str, exp.Expr]:
    """Map each source alias that exposes a stateful projection to its defining holder."""
    result: dict[str, exp.Expr] = {}

    with_ = select.args.get("with_")
    ctes_by_name = {cte.alias: cte for cte in with_.expressions} if with_ else {}

    from_ = select.args.get("from_")
    sources: list[exp.Expr] = [from_.this] if from_ else []
    sources.extend(join.this for join in select.args.get("joins") or ())

    for source in sources:
        if isinstance(source, (exp.CTE, exp.Subquery)):
            query = _query_of(source)
            if query is not None and stateful_aliases(query, names):
                result[source.alias] = source
        elif isinstance(source, exp.Table):
            cte = ctes_by_name.get(source.name)
            query = _query_of(cte)
            if query is not None and stateful_aliases(query, names):
                result[source.alias_or_name] = t.cast(exp.Expr, cte)

    return result


def validate_qualify(
    select: exp.Select,
    boundaries: Boundaries = None,
    names: t.Container[str] | None = None,
) -> None:
    """Reject QUALIFY usage of a stateful value that may be re-evaluated.

    A stateful projection written directly in a joined scope is evaluated once per joined
    row, so it cannot mean "sample once per original row": the value must instead come from
    a boundary below the join. A value read from a plain (inlinable) CTE or derived table is
    shared with the outer scope and likewise needs the caller to declare that layer a
    boundary, or the layer to be materialized.
    """
    qualify = select.args.get("qualify")
    if not isinstance(select, exp.Select) or not qualify:
        return

    if stateful_aliases(select, names) and select.args.get("joins"):
        _raise(select, "a joined scope", names)

    for alias, holder in _stateful_sources(select, names).items():
        if is_boundary(holder, _query_of(holder), boundaries):
            continue
        if any(column.table == alias for column in qualify.find_all(exp.Column)):
            _raise(select, f"source '{alias}'", names, holder)


def validate_shared(
    expression: exp.Expr,
    boundaries: Boundaries = None,
    names: t.Container[str] | None = None,
) -> None:
    """Reject a stateful value read across an unreliable layer without a declared boundary.

    A correlated subquery's own stateful call is local to it and is left untouched.
    """
    from sqlglot.optimizer.scope import traverse_scope

    for scope in traverse_scope(expression):
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        if not stateful_aliases(select, names):
            continue

        holder = select.parent
        if not isinstance(holder, (exp.CTE, exp.Subquery)):
            continue
        if is_boundary(holder, select, boundaries):
            continue
        if _read_outside(scope):
            _raise(select, "a source that may be inlined", names)


def _read_outside(scope: t.Any) -> bool:
    """Whether a consumed layer reads any column produced by ``scope``."""
    parent = scope.parent
    if parent is None:
        return False

    names = {name for name, source in parent.sources.items() if source is scope}
    if not names:
        return False
    return any(column.table in names for column in parent.columns)


def _window_stateful(
    qualify: exp.Expr,
    aliases: dict[str, exp.Expr] | None = None,
    names: t.Container[str] | None = None,
) -> bool:
    for window in qualify.find_all(exp.Window):
        if is_stateful(window, names):
            return True
        if aliases and any(column.name in aliases for column in window.find_all(exp.Column)):
            return True
    return False


def _filter_stateful(qualify: exp.Expr, names: t.Container[str] | None) -> bool:
    """Whether the filter writes a stateful call of its own (not just an alias reference)."""
    return bool(stateful_calls(qualify, names))


def needs_layer(
    select: exp.Select,
    names: t.Container[str] | None = None,
    referenced: t.Collection[str] = (),
) -> bool:
    """Whether QUALIFY must read stateful values through an inner single-evaluation layer.

    This is true when a window partitions/orders by a stateful value or contains a stateful
    call, when the filter mentions a stateful call, or when the filter reads a stateful value
    from a stateful source.
    """
    qualify = select.args.get("qualify")
    if not isinstance(select, exp.Select) or not qualify:
        return False

    aliases = stateful_aliases(select, names)
    if _window_stateful(qualify, aliases, names) or _filter_stateful(qualify, names):
        return True

    return any(column.table in referenced for column in qualify.find_all(exp.Column))


def insert_layer(select: exp.Select, names: t.Container[str] | None = None) -> None:
    """Push the query body below QUALIFY so stateful values are computed exactly once.

    The qualifier then reads plain columns, so a window cannot re-trigger the stateful call
    and a stateful alias referenced in several places is computed once. Calls written directly
    in the filter get their own column, keeping two written ``sample()`` calls independent.
    """
    qualify = select.args.get("qualify")
    if not isinstance(select, exp.Select) or not qualify:
        return

    filter_calls = stateful_calls(qualify, names)

    taken = set(select.named_selects)
    inner_alias = find_new_name(taken, "_b")
    taken.add(inner_alias)

    body = select.copy()
    body.set("qualify", None)

    helper_by_call: dict[int, str] = {}
    for call in filter_calls:
        helper = find_new_name(taken, "_s")
        taken.add(helper)
        body.select(exp.alias_(call.copy(), helper), copy=False)
        helper_by_call[id(call)] = helper

    for call in filter_calls:
        call.replace(exp.column(helper_by_call[id(call)]))

    # The filter now reads columns of the single body source, so table qualifiers must go;
    # keeping them would reference a source that no longer exists in the outer scope.
    for column in list(qualify.find_all(exp.Column)):
        column.replace(exp.column(column.name))

    exposed = set(_output_names(body, taken))
    for column in list(qualify.find_all(exp.Column)):
        name = column.name
        if name and name not in exposed:
            body.select(exp.alias_(column.copy(), name), copy=False)
            exposed.add(name)

    body_names = _output_names(body, taken)
    select.set(
        "expressions",
        [exp.alias_(exp.column(name, table=inner_alias), name) for name in body_names],
    )
    subquery = body.subquery(inner_alias, copy=False)
    # The layer we just created is the single-evaluation boundary for these values, so it must
    # not be inlined back into the consumer.
    subquery.meta[BOUNDARY] = True
    select.set("from_", exp.From(this=subquery))
    for key in _MOVED_KEYS:
        select.set(key, None)


_MOVED_KEYS = (
    "with_",
    "joins",
    "laterals",
    "pivots",
    "where",
    "group",
    "having",
    "order",
    "limit",
    "offset",
    "distinct",
    "sample",
    "hint",
    "kind",
    "into",
)


def _output_names(select: exp.Select, taken: set[str]) -> list[str]:
    names: list[str] = []
    for projection in select.selects:
        name = projection.alias_or_name
        if not name:
            name = find_new_name(taken, "_c")
            taken.add(name)
            projection.replace(exp.alias_(projection.copy(), name))
        names.append(name)
    return names


def prepare(expression: E, boundaries: Boundaries = None) -> E:
    """Prepare a query for QUALIFY elimination without re-evaluating stateful values.

    For each SELECT with a QUALIFY clause, validates that any shared stateful value reaches a
    reliable boundary, and inserts a single-evaluation layer where the filter would otherwise
    consume the value twice.

    ``boundaries`` names the layers the caller guarantees are evaluated once per input row, by
    node identity or by CTE/subquery alias. Materialized CTEs are always boundaries.

    Raises ``UnsupportedError`` naming the expression and scope when no legal boundary exists.
    Deterministic queries are left unchanged.
    """
    # Innermost SELECTs first: a parent then reads the layered child's plain columns.
    for select in reversed([node for node in expression.walk() if isinstance(node, exp.Select)]):
        if not select.args.get("qualify"):
            continue
        validate_qualify(select, boundaries)

        stateful_sources = set(_stateful_sources(select, None))
        if needs_layer(select, None, stateful_sources):
            insert_layer(select)

    validate_shared(expression, boundaries)
    return expression
