"""Static lineage analysis of the supported SQLite work-order subset.

The analyser turns a parsed work order into immutable *generations* of
temporary tables, external *reads* and delivered *reports*.  It only
understands the first-version subset promised by the work order:

* ``CREATE [TEMP] TABLE`` (plain or ``AS SELECT``)
* ``INSERT INTO ... SELECT``
* single-level ordinary ``WITH`` queries
* simple joins (``ON`` / ``USING``) and column projections
* ``SAVEPOINT`` / ``ROLLBACK TO SAVEPOINT`` / ``RELEASE SAVEPOINT``

Anything outside the subset raises :class:`ReconcileError`.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlparse
from sqlparse.reconcile.model import (
    ColumnRef,
    ExternalVersion,
    ReadRef,
    Report,
    TableGeneration,
)
from sqlparse.sql import (
    Comment,
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    TokenList,
    Where,
)
from sqlparse.tokens import DML, Keyword, Name, Token, Wildcard

_STOP_CLAUSES = frozenset(
    {
        'WHERE',
        'GROUP BY',
        'HAVING',
        'ORDER BY',
        'LIMIT',
        'UNION',
        'EXCEPT',
        'INTERSECT',
    }
)


class ReconcileError(Exception):
    """Raised when a work order uses SQL outside the supported subset."""


@dataclass
class _Binding:
    alias: str
    kind: str  # 'external' | 'temp' | 'cte'
    real_name: str
    generation: str
    columns: list[ColumnRef]


class LineageAnalyser:
    """Build generations, reads and reports for one work-order script."""

    def __init__(
        self,
        source: str,
        externals: dict[str, ExternalVersion],
        read_variant: str = 'base',
    ) -> None:
        self._source = source
        self._externals = externals
        self._variant = read_variant
        self.statements = sqlparse.parse(source)

        # name -> current visible generation id
        self._visible: dict[str, str] = {}
        # generation id -> TableGeneration
        self.generations: dict[str, TableGeneration] = {}
        # name -> monotonic generation counter
        self._counters: dict[str, int] = {}
        # savepoint name -> snapshot of the visible-generation map
        self._savepoints: dict[str, dict[str, str]] = {}
        self._savepoint_stack: list[str] = []
        # read key -> ReadRef, shared across statements and reports
        self._reads: dict[tuple[str, str, str], ReadRef] = {}
        self._read_seq = 0
        self.reports: list[Report] = []
        self._cursor = 0

    # -- public entry point ------------------------------------------------

    def run(self) -> None:
        for index, stmt in enumerate(self.statements):
            if not self._significant(stmt):
                continue
            kind = self._statement_kind(stmt)
            if kind == 'create_temp':
                self._handle_create(stmt, index)
            elif kind == 'insert':
                self._handle_insert(stmt, index)
            elif kind == 'select':
                self._handle_select(stmt, index)
            elif kind == 'savepoint':
                self._handle_savepoint(stmt)
            elif kind == 'rollback':
                self._handle_rollback(stmt)
            elif kind == 'release':
                self._handle_release(stmt)
            elif kind == 'drop':
                self._handle_drop(stmt)
            else:
                text = str(stmt).strip()[:40]
                raise ReconcileError(
                    f'unsupported statement at line '
                    f'{self._line_of(stmt)}: {text}'
                )
            self._advance_cursor(stmt)

    # -- statement classification -----------------------------------------

    def _statement_kind(self, stmt) -> str:
        words = self._significant_words(stmt)
        if not words:
            raise ReconcileError('empty or unparseable statement')
        first = words[0]
        if first == 'CREATE':
            return 'create_temp'
        if first == 'INSERT' or (first == 'WITH' and 'INSERT' in words):
            return 'insert'
        if first in ('SELECT', 'WITH'):
            return 'select'
        if first == 'SAVEPOINT':
            return 'savepoint'
        if first == 'ROLLBACK':
            return 'rollback'
        if first == 'RELEASE':
            return 'release'
        if first == 'DROP':
            return 'drop'
        raise ReconcileError(f'unsupported leading keyword {first!r}')

    # -- savepoints --------------------------------------------------------

    def _handle_savepoint(self, stmt) -> None:
        name = self._name_after(stmt, 'SAVEPOINT')
        self._savepoints[name] = dict(self._visible)
        self._savepoint_stack.append(name)

    def _handle_rollback(self, stmt) -> None:
        name = self._name_after(stmt, 'SAVEPOINT')
        if name not in self._savepoints:
            raise ReconcileError(f'rollback to unknown savepoint {name!r}')
        # Restore the snapshot; savepoints opened afterwards are discarded.
        self._visible = dict(self._savepoints[name])
        while self._savepoint_stack and self._savepoint_stack[-1] != name:
            inner = self._savepoint_stack.pop()
            self._savepoints.pop(inner, None)

    def _handle_release(self, stmt) -> None:
        name = self._name_after(stmt, 'SAVEPOINT')
        while self._savepoint_stack:
            current = self._savepoint_stack.pop()
            self._savepoints.pop(current, None)
            if current == name:
                return
        raise ReconcileError(f'release of unknown savepoint {name!r}')

    def _handle_drop(self, stmt) -> None:
        target = self._first_identifier(stmt)
        self._visible.pop(target, None)

    # -- create / insert ---------------------------------------------------

    def _handle_create(self, stmt, index: int) -> None:
        name = self._first_identifier(stmt)
        if self._has_select(stmt):
            cte_map, reads, columns, parents = self._analyse_select(stmt, index)
            del cte_map
        else:
            columns = self._declared_columns(stmt)
            reads, parents = [], []
        self._new_generation(name, index, stmt, columns, reads, parents)

    def _declared_columns(self, stmt) -> list[ColumnRef]:
        for token in stmt.tokens:
            if not isinstance(token, Parenthesis):
                continue
            columns: list[ColumnRef] = []
            for child in token.tokens:
                if not isinstance(child, Identifier):
                    continue
                leaf = next(
                    (
                        leaf
                        for leaf in child.flatten()
                        if leaf.ttype in Name
                    ),
                    None,
                )
                if leaf is not None and not any(
                    col.display == leaf.value for col in columns
                ):
                    columns.append(ColumnRef(leaf.value, ()))
            return columns
        return []

    def _handle_insert(self, stmt, index: int) -> None:
        target = self._insert_target(stmt)
        if not self._has_select(stmt):
            raise ReconcileError(
                f'only INSERT ... SELECT is supported '
                f'(line {self._line_of(stmt)})'
            )
        _, reads, columns, parents = self._analyse_select(stmt, index)
        prior = self._visible.get(target)
        if prior is not None:
            prior_gen = self.generations[prior]
            parents.append(prior)
            reads = self._merge_reads(prior_gen.reads, reads)
            # An append keeps the prior column contract; the select list
            # lines up positionally with it.
            columns = prior_gen.columns
        self._new_generation(target, index, stmt, columns, reads, parents)

    def _new_generation(self, name, index, stmt, columns, reads, parents):
        self._counters[name] = self._counters.get(name, 0) + 1
        generation = f'{name}#{self._counters[name]}'
        record = TableGeneration(
            name=name,
            generation=generation,
            statement_index=index,
            line_no=self._line_of(stmt),
            sealed=False,
            columns=list(columns),
            reads=list(reads),
            parents=list(dict.fromkeys(parents)),
        )
        self.generations[generation] = record
        self._visible[name] = generation
        return generation

    # -- select / report ---------------------------------------------------

    def _handle_select(self, stmt, index: int) -> None:
        cte_map, reads, columns, _ = self._analyse_select(stmt, index)
        bindings, _ = self._build_bindings(stmt, cte_map)
        temp_refs = frozenset(
            binding.generation
            for binding in bindings
            if binding.kind == 'temp'
        )
        self.reports.append(
            Report(
                name=f'report@{index}',
                statement_index=index,
                line_no=self._line_of(stmt),
                reads=list(reads),
                columns=list(columns),
                cte_names=frozenset(cte_map),
                temp_refs=temp_refs,
            )
        )

    def _analyse_select(self, fragment, index: int):
        """Analyse one SELECT fragment (statement or CTE body).

        Returns the CTE binding map, the external reads (deduplicated via
        the shared read registry), the projected columns and the
        temporary-table parent generations.
        """
        cte_map = self._build_cte_map(fragment, index)
        bindings, using_cols = self._build_bindings(fragment, cte_map)
        reads: list[ReadRef] = []
        parents: list[str] = []
        # Reads and parents contributed by CTE bodies.
        for cte in cte_map.values():
            for col in cte.columns:
                for generation, _ in col.sources:
                    if generation.startswith('ext:'):
                        read = self._read_for_generation(generation)
                        if read is not None:
                            reads.append(read)
                    elif '#' in generation and not generation.startswith('cte#'):
                        parents.append(generation)
        for binding in bindings:
            if binding.kind == 'external':
                reads.append(self._read_for_binding(binding))
            elif binding.kind == 'temp':
                parents.append(binding.generation)
        reads = self._dedupe_reads(reads)
        columns = self._project_columns(fragment, bindings, using_cols)
        parents = list(dict.fromkeys(parents))
        return cte_map, reads, columns, parents

    def _build_cte_map(self, fragment, index: int) -> dict[str, _Binding]:
        cte_map: dict[str, _Binding] = {}
        if not isinstance(fragment, TokenList):
            return cte_map
        in_cte_region = False
        for token in fragment.tokens:
            if self._is_keyword(token, 'WITH'):
                in_cte_region = True
                continue
            if token.ttype is DML:
                # The main statement starts; the CTE region ends.
                break
            if not in_cte_region or token.is_whitespace:
                continue
            cte_ids: list[Identifier] = []
            if isinstance(token, IdentifierList):
                cte_ids = [
                    sub
                    for sub in token.get_identifiers()
                    if isinstance(sub, Identifier)
                    and self._parenthesis_of(sub) is not None
                ]
            elif (
                isinstance(token, Identifier)
                and self._parenthesis_of(token) is not None
            ):
                cte_ids = [token]
            for cte_id in cte_ids:
                cte_name = cte_id.get_real_name()
                body = self._parenthesis_of(cte_id)
                _, reads, columns, _ = self._analyse_select(body, index)
                for read in reads:
                    self._register_read(read)
                cte_map[cte_name] = _Binding(
                    alias=cte_name,
                    kind='cte',
                    real_name=cte_name,
                    generation=f'cte#{index}:{cte_name}',
                    columns=columns,
                )
        return cte_map

    def _build_bindings(self, fragment, cte_map):
        bindings: list[_Binding] = []
        using_cols: set[str] = set()
        from_seen = False
        expect_table = False
        pending_using = False
        for token in self._significant(fragment):
            if self._is_keyword(token, 'FROM'):
                from_seen = True
                expect_table = True
                continue
            if not from_seen:
                continue
            if token.ttype in Keyword:
                word = token.normalized.upper()
                if word in _STOP_CLAUSES:
                    break
                if word == 'USING':
                    pending_using = True
                    expect_table = False
                    continue
                if 'JOIN' in word:
                    expect_table = True
                    continue
                if expect_table:
                    # sqlparse classifies some unquoted table names as
                    # keywords (e.g. RAW); in table position it is a name.
                    bindings.append(self._make_binding(token, cte_map))
                    expect_table = False
                continue
            if pending_using and isinstance(token, Parenthesis):
                using_cols.update(self._parenthesis_names(token))
                pending_using = False
                continue
            if isinstance(token, IdentifierList):
                for sub in token.get_identifiers():
                    if isinstance(sub, Identifier):
                        bindings.append(self._make_binding(sub, cte_map))
                expect_table = False
            elif isinstance(token, Identifier):
                bindings.append(self._make_binding(token, cte_map))
                expect_table = False
        aliases = [binding.alias for binding in bindings]
        if len(set(aliases)) != len(aliases):
            raise ReconcileError('duplicate table alias in FROM clause')
        return bindings, using_cols

    def _make_binding(self, identifier, cte_map) -> _Binding:
        if (
            isinstance(identifier, Identifier)
            and self._parenthesis_of(identifier) is not None
        ):
            raise ReconcileError(
                'subqueries in FROM are outside the supported work-order subset'
            )
        if isinstance(identifier, Identifier):
            alias = identifier.get_alias() or identifier.get_real_name()
            real = identifier.get_real_name()
        else:
            # A bare table name tokenised as a keyword by sqlparse.
            alias = real = identifier.value
        if real in cte_map:
            cte = cte_map[real]
            return _Binding(alias, 'cte', real, cte.generation, cte.columns)
        if real in self._visible:
            generation = self._visible[real]
            return _Binding(
                alias,
                'temp',
                real,
                generation,
                list(self.generations[generation].columns),
            )
        if real in self._externals:
            version = self._externals[real]
            generation = f'ext:{real}@{version.instance}'
            return _Binding(
                alias,
                'external',
                real,
                generation,
                [
                    ColumnRef(col, ((generation, col),))
                    for col in version.columns
                ],
            )
        raise ReconcileError(f'unknown table {real!r} in FROM clause')

    def _project_columns(self, fragment, bindings, using_cols):
        projected: list[ColumnRef] = []
        in_select = False
        emitted_using: set[str] = set()
        for token in self._significant(fragment):
            if self._is_keyword(token, 'SELECT'):
                in_select = True
                continue
            if not in_select:
                continue
            if self._is_keyword(token, 'FROM'):
                break
            if token.ttype is Wildcard:
                self._expand_wildcard(
                    None, bindings, using_cols, emitted_using, projected
                )
                continue
            if isinstance(token, Identifier) and self._is_star(token):
                qualifier = self._qualifier(token)
                self._expand_wildcard(
                    qualifier, bindings, using_cols, emitted_using, projected
                )
                continue
            if isinstance(token, IdentifierList):
                for sub in token.get_identifiers():
                    if sub.ttype is Wildcard:
                        self._expand_wildcard(
                            None, bindings, using_cols, emitted_using, projected
                        )
                    else:
                        projected.append(
                            self._project_one(sub, bindings, using_cols)
                        )
                continue
            if isinstance(token, Identifier):
                projected.append(
                    self._project_one(token, bindings, using_cols)
                )
        return projected

    def _expand_wildcard(
        self, qualifier, bindings, using_cols, emitted_using, projected
    ):
        for binding in bindings:
            if qualifier is not None and binding.alias != qualifier:
                continue
            for col in binding.columns:
                if col.display in using_cols:
                    if col.display in emitted_using:
                        continue
                    projected.append(
                        ColumnRef(
                            col.display,
                            self._merged_using_sources(
                                bindings, col.display
                            ),
                        )
                    )
                    emitted_using.add(col.display)
                else:
                    projected.append(
                        ColumnRef(
                            col.display,
                            self._binding_sources(binding, col.display),
                        )
                    )

    def _project_one(self, identifier, bindings, using_cols) -> ColumnRef:
        display = identifier.get_alias() or self._display_name(identifier)
        qualifier = self._qualifier(identifier)
        source_names = self._source_column_names(identifier)
        if not source_names:
            # Literal-only projection carries no table source.
            return ColumnRef(display, ())
        if len(source_names) == 1 and source_names[0] in using_cols:
            # A USING merge shows one value but holds both sources,
            # whether or not the column is written with a qualifier.
            column = source_names[0]
            return ColumnRef(
                display, self._merged_using_sources(bindings, column)
            )
        sources: list[tuple[str, str]] = []
        for column in source_names:
            owner = self._owner(bindings, qualifier, column)
            for source in self._binding_sources(owner, column):
                if source not in sources:
                    sources.append(source)
        return ColumnRef(display, tuple(sources))

    @staticmethod
    def _source_column_names(identifier: Identifier) -> list[str]:
        names: list[str] = []
        alias_seen = False
        saw_dot = False
        for token in identifier.tokens:
            if token.ttype in Keyword and token.normalized.upper() == 'AS':
                alias_seen = True
                continue
            if isinstance(token, Identifier):
                # Nested identifier is the output alias; ignore it.
                alias_seen = True
                continue
            if token.ttype is Name:
                if alias_seen:
                    continue
                if saw_dot and names:
                    names[-1] = token.value
                else:
                    names.append(token.value)
            elif token.ttype is Token.Punctuation and token.value == '.':
                saw_dot = True
        return names

    def _merged_using_sources(self, bindings, column):
        sources: list[tuple[str, str]] = []
        for binding in bindings:
            if any(col.display == column for col in binding.columns):
                for source in self._binding_sources(binding, column):
                    if source not in sources:
                        sources.append(source)
        return tuple(sources)

    @staticmethod
    def _binding_sources(binding: _Binding, column: str):
        for col in binding.columns:
            if col.display == column:
                return col.sources
        raise ReconcileError(
            f'column {column!r} not found in table {binding.real_name!r}'
        )

    def _owner(self, bindings, qualifier, column):
        candidates = [
            binding
            for binding in bindings
            if (qualifier is None or binding.alias == qualifier)
            and any(col.display == column for col in binding.columns)
        ]
        if not candidates:
            prefix = f'{qualifier}.' if qualifier else ''
            raise ReconcileError(f'cannot resolve column {prefix}{column}')
        if len(candidates) > 1:
            raise ReconcileError(
                f'ambiguous column {column!r}; qualify it with a table alias'
            )
        return candidates[0]

    # -- reads -------------------------------------------------------------

    def _read_for_binding(self, binding: _Binding) -> ReadRef:
        table = binding.real_name
        version = self._externals[table]
        return self._register_read(
            ReadRef(
                table=table,
                instance=version.instance,
                variant=self._variant,
                byte_cost=version.bytes,
                fee=version.fee,
                key=(table, version.instance),
                seq=0,
            )
        )

    def _read_for_generation(self, generation: str) -> ReadRef | None:
        # generation looks like 'ext:table@instance'
        if not generation.startswith('ext:'):
            return None
        body = generation[len('ext:'):]
        table, instance = body.rsplit('@', 1)
        version = self._externals.get(table)
        if version is None or version.instance != instance:
            return None
        return self._register_read(
            ReadRef(
                table=table,
                instance=instance,
                variant=self._variant,
                byte_cost=version.bytes,
                fee=version.fee,
                key=(table, instance),
                seq=0,
            )
        )

    def _register_read(self, read: ReadRef) -> ReadRef:
        existing = self._reads.get(read.key)
        if existing is not None:
            return existing
        self._read_seq += 1
        registered = ReadRef(
            table=read.table,
            instance=read.instance,
            variant=read.variant,
            byte_cost=read.byte_cost,
            fee=read.fee,
            key=read.key,
            seq=self._read_seq,
        )
        self._reads[read.key] = registered
        return registered

    @staticmethod
    def _dedupe_reads(reads):
        merged: dict[tuple, ReadRef] = {}
        for read in reads:
            merged.setdefault(read.key, read)
        return sorted(merged.values(), key=lambda item: item.seq)

    @staticmethod
    def _merge_reads(left, right):
        merged: dict[tuple, ReadRef] = {read.key: read for read in left}
        for read in right:
            merged.setdefault(read.key, read)
        return sorted(merged.values(), key=lambda item: item.seq)

    # -- small token helpers ----------------------------------------------

    @staticmethod
    def _significant(fragment):
        if not isinstance(fragment, TokenList):
            return ()
        return [
            token
            for token in fragment.tokens
            if not token.is_whitespace
            and token.ttype not in Token.Comment
            and not isinstance(token, (Where, Comment))
        ]

    def _significant_words(self, stmt):
        words = []
        for token in self._significant(stmt):
            if token.ttype in Keyword or token.ttype is DML:
                words.append(token.normalized.upper())
            else:
                words.append(token.normalized.upper().split()[0])
                break
        return words

    @staticmethod
    def _is_keyword(token, word):
        return token.ttype in Keyword and token.normalized.upper() == word

    @staticmethod
    def _has_select(stmt):
        return any(
            token.ttype is DML and token.normalized.upper() == 'SELECT'
            for token in stmt.flatten()
        )

    @staticmethod
    def _first_identifier(stmt) -> str:
        for token in stmt.tokens:
            if isinstance(token, Identifier):
                return token.get_real_name()
        raise ReconcileError('missing table identifier')

    @staticmethod
    def _name_after(stmt, keyword) -> str:
        tokens = [
            token
            for token in stmt.tokens
            if not token.is_whitespace
        ]
        for pos, token in enumerate(tokens):
            if token.normalized.upper() == keyword:
                for following in tokens[pos + 1:]:
                    if isinstance(following, Identifier):
                        return following.get_real_name()
                    if following.ttype in Name:
                        return following.value
        raise ReconcileError(f'no name following {keyword}')

    def _insert_target(self, stmt) -> str:
        for token in stmt.tokens:
            if isinstance(token, Function):
                return token.get_real_name()
            if isinstance(token, Identifier):
                return token.get_real_name()
        raise ReconcileError('INSERT without target table')

    @staticmethod
    def _parenthesis_names(parenthesis: Parenthesis):
        names = []
        for token in parenthesis.tokens:
            if isinstance(token, Identifier):
                names.append(token.get_real_name())
            elif token.ttype in Name:
                names.append(token.value)
        return names

    @staticmethod
    def _is_star(identifier: Identifier) -> bool:
        return any(token.ttype is Wildcard for token in identifier.tokens)

    @staticmethod
    def _parenthesis_of(identifier: Identifier) -> Parenthesis | None:
        for token in identifier.tokens:
            if isinstance(token, Parenthesis):
                return token
        return None

    @staticmethod
    def _qualifier(identifier: Identifier) -> str | None:
        has_dot = any(
            token.ttype is Token.Punctuation and token.value == '.'
            for token in identifier.tokens
        )
        if not has_dot:
            return None
        for token in identifier.tokens:
            if token.ttype is Name:
                return token.value
        return None

    @staticmethod
    def _display_name(identifier: Identifier) -> str:
        names = [
            leaf.value
            for leaf in identifier.flatten()
            if leaf.ttype in Name
        ]
        return names[-1] if names else identifier.normalized.strip()

    def _line_of(self, stmt) -> int:
        offset = self._source.find(str(stmt), self._cursor)
        if offset < 0:
            offset = self._cursor
        # Skip the statement's own leading whitespace and comments so the
        # reported line points at the first real token (CREATE/SELECT).
        first = next(
            (
                token
                for token in stmt.tokens
                if not token.is_whitespace and not isinstance(token, Comment)
            ),
            None,
        )
        if first is not None:
            inner = str(stmt).find(first.value)
            if inner >= 0:
                offset += inner
        return self._source.count('\n', 0, offset) + 1

    def _advance_cursor(self, stmt) -> None:
        offset = self._source.find(str(stmt), self._cursor)
        if offset >= 0:
            self._cursor = offset + len(str(stmt))
