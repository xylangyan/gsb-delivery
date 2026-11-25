from __future__ import annotations

import itertools
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .Logic import Logic as _LOGIC_CLS
from . import pyEngine, pyParser


class VersioningError(RuntimeError):
    pass


class _ActiveContext(threading.local):
    def __init__(self) -> None:
        super().__init__()
        self.stack: List["QueryContext"] = []
        self.persistent: Optional["QueryContext"] = None


_ACTIVE = _ActiveContext()
_HOOK_LOCK = threading.RLock()
_HOOKS_INSTALLED = False
_DEFAULT_MANAGER: Optional["VersionManager"] = None
_ORIGINAL_INSERT: Optional[Callable[..., Any]] = None
_ORIGINAL_ASSERT: Optional[Callable[..., Any]] = None
_ORIGINAL_CLEAR: Optional[Callable[..., Any]] = None


def _current_context() -> Optional["QueryContext"]:
    if _ACTIVE.stack:
        return _ACTIVE.stack[-1]
    return getattr(_ACTIVE, "persistent", None)


def _is_temporary_predicate(pred_id: str) -> bool:
    return pred_id.startswith("_pyD_query") or "/_pyD_query" in pred_id


class _Delta:
    __slots__ = (
        "removed",
        "removed_keys",
        "db_items",
        "clause_items",
        "db_add",
        "clauses_add",
        "index_add",
    )

    def __init__(self, arity: int = 0) -> None:
        self.removed: Set[Any] = set()
        self.removed_keys: Set[Any] = set()
        self.db_items: Dict[Any, Any] = {}
        self.clause_items: Dict[Any, Any] = {}
        self.db_add: Dict[int, Set[Any]] = {}
        self.clauses_add: Dict[int, Set[Any]] = {}
        self.index_add: List[Dict[Any, Set[Any]]] = [dict() for _ in range(max(arity, 0))]

    def add_item(self, kind: str, key: Any, clause: Any) -> None:
        self.removed.discard(clause)
        self.removed_keys.discard(key)
        if kind == "clauses":
            self.clause_items[key] = clause
        else:
            self.db_items[key] = clause

    def remove_item(self, key: Any, clause: Any = None) -> None:
        self.removed_keys.add(key)
        if clause is not None:
            self.removed.add(clause)
        self.db_items.pop(key, None)
        self.clause_items.pop(key, None)

    def add(self, kind: str, key_hash: int, clause: Any) -> None:
        key = clause.get_id() if hasattr(clause, "get_id") else key_hash
        self.add_item(kind, key, clause)
        bucket_name = "clauses_add" if kind == "clauses" else "db_add"
        bucket = getattr(self, bucket_name)
        bucket.setdefault(key_hash, set()).add(clause)

    def remove(self, clause: Any, key_hash: Optional[int] = None) -> None:
        key = clause.get_id() if hasattr(clause, "get_id") else key_hash
        if key is not None:
            self.remove_item(key, clause)
        else:
            self.removed.add(clause)
        for bucket in (self.db_add, self.clauses_add):
            if key_hash is None:
                for items in bucket.values():
                    items.discard(clause)
            else:
                bucket.get(key_hash, set()).discard(clause)
        for positions in self.index_add:
            if key_hash is None:
                for items in positions.values():
                    items.discard(clause)
            else:
                positions.get(key_hash, set()).discard(clause)

    def add_index(self, position: int, key: Any, clause: Any, arity: int) -> None:
        self.removed.discard(clause)
        if hasattr(clause, "get_id"):
            self.removed_keys.discard(clause.get_id())
        while len(self.index_add) <= position:
            self.index_add.append({})
        self.index_add[position].setdefault(key, set()).add(clause)


class Version:
    def __init__(
        self,
        manager: "VersionManager",
        version_id: int,
        parent: Optional["Version"] = None,
        name: Optional[str] = None,
        logic: Any = None,
    ) -> None:
        self.manager = manager
        self.id = version_id
        self.version_id = version_id
        self.name = name
        self.parent = parent
        self.children: List["Version"] = []
        self.deltas: Dict[str, _Delta] = {}
        self.predicate_ids: Set[str] = set()
        self.logic = logic or manager.logic
        self.reference_count = 0
        self.retainers: Set[int] = set()
        self.aliases: Set[str] = set()
        self.published_names: Set[str] = set()
        self.ephemeral = False
        if parent is not None:
            parent.children.append(self)
            self.chain = parent.chain + (self,)
        else:
            self.chain = (self,)

    def delta_for(self, pred_id: str, arity: int = 0) -> _Delta:
        delta = self.deltas.get(pred_id)
        if delta is None:
            delta = _Delta(arity)
            self.deltas[pred_id] = delta
        elif arity > len(delta.index_add):
            while len(delta.index_add) < arity:
                delta.index_add.append({})
        return delta

    def retain(self, token: Any = None) -> None:
        token_id = id(token) if token is not None else 0
        self.retainers.add(token_id)
        self.reference_count = len(self.retainers)

    def release(self, token: Any = None) -> None:
        token_id = id(token) if token is not None else 0
        self.retainers.discard(token_id)
        self.reference_count = len(self.retainers)

    def ask(self, query: str, **options: Any) -> Any:
        return self.manager.ask(query, version=self, **options)

    def open_view(self, **options: Any) -> "ReadView":
        return self.manager.open_view(version=self, **options)

    def open_cursor(self, query: str, **options: Any) -> "Cursor":
        return self.manager.open_cursor(query, version=self, **options)

    def branch(self, name: str) -> "Branch":
        return self.manager.branch(name, self)

    def create_branch(self, name: str) -> "Branch":
        return self.branch(name)

    def snapshot(self, name: Optional[str] = None) -> "Version":
        return self.manager.snapshot(self, name)

    def publish(self, name: str = "published") -> "Version":
        return self.manager.publish(self, name)

    def load(self, code: str) -> "Version":
        branch = Branch(self.manager, f"version-{self.id}-draft", self)
        return branch.load(code)

    def ask_page(self, query: str, **options: Any) -> Any:
        return self.manager.ask_page(query, version=self, **options)

    def __repr__(self) -> str:
        label = self.name or self.id
        return f"<pyDatalog.Version {label}>"


class Branch:
    def __init__(self, manager: "VersionManager", name: str, head: Version) -> None:
        self.manager = manager
        self.name = name
        self.head = head
        self.aliases = {name}

    @property
    def version(self) -> Version:
        return self.head

    @property
    def parent(self) -> Optional[Version]:
        return self.head.parent

    @property
    def base_version(self) -> Optional[Version]:
        return self.head.parent

    def _commit_code(self, code: Optional[str], operation: Optional[Callable[[], Any]]) -> Version:
        parent = self.head
        candidate = self.manager._new_version(parent=parent)
        try:
            with self.manager.activation(candidate, writable=True):
                if code is not None:
                    pyParser.load(code, newglobals={})
                if operation is not None:
                    result = operation()
                else:
                    result = None
        except Exception:
            self.manager._discard_candidate(candidate)
            raise
        self.head = candidate
        candidate.aliases.add(self.name)
        self.manager.aliases[self.name] = candidate
        self.manager.branches[self.name] = self
        return candidate if result is None else result

    def begin(self) -> "Branch":
        parent = self.head
        candidate = self.manager._new_version(parent=parent)
        self._transaction_parent = parent
        self._transaction_candidate = candidate
        self._transaction_context = self.manager.activation(candidate, writable=True)
        self._transaction_context.__enter__()
        self.head = candidate
        return self

    def commit(self, code: Optional[str] = None, operation: Optional[Callable[[], Any]] = None) -> Any:
        if code is not None or operation is not None:
            return self._commit_code(code, operation)
        context = getattr(self, "_transaction_context", None)
        candidate = getattr(self, "_transaction_candidate", None)
        try:
            if context is not None:
                context.__exit__(None, None, None)
        finally:
            self._transaction_context = None
        if candidate is not None:
            candidate.aliases.add(self.name)
            self.manager.aliases[self.name] = candidate
        return candidate

    def rollback(self) -> None:
        context = getattr(self, "_transaction_context", None)
        candidate = getattr(self, "_transaction_candidate", None)
        parent = getattr(self, "_transaction_parent", None)
        if context is not None:
            context.__exit__(RuntimeError, RuntimeError("rollback"), None)
        if candidate is not None:
            self.manager._discard_candidate(candidate)
        if parent is not None:
            self.head = parent
        self._transaction_context = None
        self._transaction_candidate = None
        self._transaction_parent = None

    def __enter__(self) -> "Branch":
        return self.begin()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        context = getattr(self, "_transaction_context", None)
        candidate = getattr(self, "_transaction_candidate", None)
        parent = getattr(self, "_transaction_parent", None)
        try:
            if context is not None:
                context.__exit__(exc_type, exc, tb)
        finally:
            self._transaction_context = None
        if exc_type is None:
            if candidate is not None:
                candidate.aliases.add(self.name)
                self.manager.aliases[self.name] = candidate
        else:
            if candidate is not None:
                self.manager._discard_candidate(candidate)
            if parent is not None:
                self.head = parent
        self._transaction_candidate = None
        self._transaction_parent = None

    def load(self, code: str) -> Version:
        return self._commit_code(code, None)

    def assert_fact(self, predicate_name: str, *args: Any) -> Version:
        def operation() -> None:
            from .pyDatalog import assert_fact

            assert_fact(predicate_name, *args)

        return self._commit_code(None, operation)

    def retract_fact(self, predicate_name: str, *args: Any) -> Version:
        def operation() -> None:
            from .pyDatalog import retract_fact

            retract_fact(predicate_name, *args)

        return self._commit_code(None, operation)

    add_fact = assert_fact
    insert_fact = assert_fact
    remove_fact = retract_fact
    delete_fact = retract_fact
    execute = load
    apply = load
    run = load
    add_rules = load
    commit_code = load

    def ask(self, query: str, **options: Any) -> Any:
        return self.manager.ask(query, version=self.head, **options)

    def open_view(self, **options: Any) -> "ReadView":
        return self.head.open_view(**options)

    def open_cursor(self, query: str, **options: Any) -> "Cursor":
        return self.head.open_cursor(query, **options)

    @contextmanager
    def trial(self) -> Iterator[Version]:
        candidate = self.manager._new_version(parent=self.head, ephemeral=True)
        with self.manager.activation(candidate, writable=True):
            yield candidate

    @contextmanager
    def session(self) -> Iterator["Branch"]:
        previous_default = self.manager.default_version
        self.manager.default_version = self.head
        try:
            with self.manager.activation(self.head, writable=True):
                yield self
        finally:
            self.manager.default_version = previous_default

    context = session

    def publish(self, published_name: Optional[str] = None) -> Version:
        return self.manager.publish(self.head, published_name or self.name)

    def __repr__(self) -> str:
        return f"<pyDatalog.Branch {self.name} head={self.head}>"


@dataclass
class QueryContext:
    manager: "VersionManager"
    version: Version
    writable: bool = False


class _StorageProxy:
    def __init__(self, manager: "VersionManager", pred_id: str, raw: Any) -> None:
        self._manager = manager
        self._pred_id = pred_id
        self._raw = raw

    def _active(self) -> Optional[QueryContext]:
        context = _current_context()
        if context is None:
            return None
        if _is_temporary_predicate(self._pred_id):
            return None
        return context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class DbProxy(_StorageProxy):
    def __init__(self, manager: "VersionManager", pred_id: str, raw: Any, kind: str) -> None:
        super().__init__(manager, pred_id, raw)
        self._kind = kind

    def _active(self) -> Optional[QueryContext]:
        context = super()._active()
        return context

    def _delta_items(self, delta: _Delta) -> Dict[Any, Any]:
        return delta.clause_items if self._kind == "clauses" else delta.db_items

    def add(self, key_hash: int, clause: Any) -> None:
        context = self._active()
        if context is None:
            if hasattr(self._raw, "add"):
                self._raw.add(key_hash, clause)
            else:
                self._raw[clause.get_id()] = clause
            return
        key = clause.get_id() if hasattr(clause, "get_id") else key_hash
        context.version.delta_for(self._pred_id).add(self._kind, key_hash, clause)
        context.version.delta_for(self._pred_id).add_item(self._kind, key, clause)

    def remove(self, key_hash: int, clause: Any) -> None:
        context = self._active()
        if context is None:
            if hasattr(self._raw, "remove"):
                self._raw.remove(key_hash, clause)
            else:
                self._raw.pop(clause.get_id(), None)
            return
        key = clause.get_id() if hasattr(clause, "get_id") else key_hash
        context.version.delta_for(self._pred_id).remove_item(key, clause)

    def __getitem__(self, key: Any) -> Any:
        context = self._active()
        if context is None:
            return self._raw[key]
        for version in reversed(context.version.chain):
            delta = version.deltas.get(self._pred_id)
            if delta is not None:
                items = self._delta_items(delta)
                if key in items:
                    return items[key]
                if key in delta.removed_keys:
                    raise KeyError(key)
        return self._raw[key]

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: Any, clause: Any) -> None:
        context = self._active()
        if context is None:
            self._raw[key] = clause
            return
        context.version.delta_for(self._pred_id).add_item(self._kind, key, clause)

    def __delitem__(self, key: Any) -> None:
        context = self._active()
        if context is None:
            del self._raw[key]
            return
        clause = self.get(key)
        context.version.delta_for(self._pred_id).remove_item(key, clause)

    def __contains__(self, key: Any) -> bool:
        return self.get(key, None) is not None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._merged_mapping())

    def keys(self) -> Any:
        return self._merged_mapping().keys()

    def items(self) -> Any:
        return self._merged_mapping().items()

    def values(self) -> List[Any]:
        return list(self._merged_mapping().values())

    def _merged_mapping(self) -> Any:
        context = self._active()
        if context is None:
            return self._raw
        has_delta = any(self._pred_id in version.deltas for version in context.version.chain)
        if not has_delta:
            return self._raw
        merged: Dict[Any, Any] = {}
        if hasattr(self._raw, "items"):
            merged.update(self._raw.items())
        else:
            for clause in self._raw.values():
                merged[clause.get_id()] = clause
        for version in context.version.chain:
            delta = version.deltas.get(self._pred_id)
            if delta is None:
                continue
            for key in delta.removed_keys:
                merged.pop(key, None)
            merged.update(self._delta_items(delta))
        return merged

    def _merged(self, key_hash: Optional[int]) -> List[Any]:
        if key_hash is None:
            return self.values()
        context = self._active()
        if context is None:
            if hasattr(self._raw, "get_candidates"):
                return self._raw.get_candidates(key_hash)
            return [clause for key, clause in self._raw.items() if hash(key[0]) == key_hash]
        if hasattr(self._raw, "get_candidates"):
            candidates = set(self._raw.get_candidates(key_hash))
        else:
            candidates = set(self._raw.values())
        for version in context.version.chain:
            delta = version.deltas.get(self._pred_id)
            if delta is None:
                continue
            candidates.difference_update(delta.removed)
            bucket_name = "clauses_add" if self._kind == "clauses" else "db_add"
            candidates.update(getattr(delta, bucket_name).get(key_hash, ()))
        return list(candidates)

    def get_candidates(self, key_hash: int) -> List[Any]:
        return self._merged(key_hash)

    def __len__(self) -> int:
        return len(self._merged_mapping())


class _IndexSet:
    def __init__(self, position_proxy: "_IndexPositionProxy", key: Any) -> None:
        self._position_proxy = position_proxy
        self._key = key

    def _values(self) -> Set[Any]:
        return self._position_proxy._merged_values(self._key)

    def add(self, clause: Any) -> None:
        context = self._position_proxy._active()
        if context is None:
            self._position_proxy._raw.setdefault(self._key, set()).add(clause)
            return
        delta = context.version.delta_for(
            self._position_proxy._pred_id, self._position_proxy._arity
        )
        delta.add_index(self._position_proxy.position, self._key, clause, self._position_proxy._arity)

    def remove(self, clause: Any) -> None:
        context = self._position_proxy._active()
        if context is None:
            self._position_proxy._raw[self._key].remove(clause)
            return
        context.version.delta_for(self._position_proxy._pred_id).remove(clause)

    def discard(self, clause: Any) -> None:
        context = self._position_proxy._active()
        if context is None:
            bucket = self._position_proxy._raw.get(self._key)
            if bucket is not None:
                bucket.discard(clause)
            return
        context.version.delta_for(self._position_proxy._pred_id).remove(clause)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())

    def __contains__(self, clause: Any) -> bool:
        return clause in self._values()


class _IndexPositionProxy:
    def __init__(self, parent: "IndexProxy", position: int, raw: Any) -> None:
        self._parent = parent
        self.position = position
        self._raw = raw if raw is not None else {}

    @property
    def _pred_id(self) -> str:
        return self._parent._pred_id

    @property
    def _arity(self) -> int:
        return self._parent._arity

    def _active(self) -> Optional[QueryContext]:
        return self._parent._active()

    def _merged_values(self, key: Any) -> Set[Any]:
        context = self._active()
        if context is None:
            return set(self._raw.get(key, ()))
        has_delta = any(self._parent._pred_id in version.deltas for version in context.version.chain)
        raw_values = self._raw.get(key, ())
        if not has_delta:
            return raw_values
        candidates = set(raw_values)
        for version in context.version.chain:
            delta = version.deltas.get(self._pred_id)
            if delta is None:
                continue
            candidates.difference_update(delta.removed)
            if self.position < len(delta.index_add):
                candidates.update(delta.index_add[self.position].get(key, ()))
        return candidates

    def __getitem__(self, key: Any) -> _IndexSet:
        return _IndexSet(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        values = self._merged_values(key)
        return values if values else default

    def setdefault(self, key: Any, default: Any = None) -> _IndexSet:
        return _IndexSet(self, key)

    def __delitem__(self, key: Any) -> None:
        context = self._active()
        if context is None:
            del self._raw[key]
        return None

    def __contains__(self, key: Any) -> bool:
        return bool(self._merged_values(key))

    def keys(self) -> Any:
        raw_keys = set(self._raw.keys()) if hasattr(self._raw, "keys") else set()
        context = self._active()
        if context is not None:
            for version in context.version.chain:
                delta = version.deltas.get(self._pred_id)
                if delta is not None and self.position < len(delta.index_add):
                    raw_keys.update(delta.index_add[self.position].keys())
        return raw_keys


class IndexProxy(_StorageProxy):
    def __init__(self, manager: "VersionManager", pred_id: str, raw: Any, arity: int) -> None:
        super().__init__(manager, pred_id, raw)
        self._arity = arity
        self._positions: Dict[int, _IndexPositionProxy] = {}

    def _position(self, position: int) -> _IndexPositionProxy:
        proxy = self._positions.get(position)
        if proxy is None:
            raw_position = None
            if isinstance(self._raw, list) and position < len(self._raw):
                raw_position = self._raw[position]
            proxy = _IndexPositionProxy(self, position, raw_position)
            self._positions[position] = proxy
        return proxy

    def __getitem__(self, position: int) -> _IndexPositionProxy:
        return self._position(position)

    def __len__(self) -> int:
        return int(self._arity or (len(self._raw) if hasattr(self._raw, "__len__") else 0))

    def __iter__(self) -> Iterator[Any]:
        for position in range(len(self)):
            yield self._position(position)

    def add(self, position: int, key_hash: int, clause: Any) -> None:
        if hasattr(self._raw, "add") and not isinstance(self._raw, list):
            context = self._active()
            if context is None:
                self._raw.add(position, key_hash, clause)
                return
        self._position(position)[key_hash].add(clause)

    def remove(self, position: int, key_hash: int, clause: Any) -> None:
        if hasattr(self._raw, "remove") and not isinstance(self._raw, list):
            context = self._active()
            if context is None:
                self._raw.remove(position, key_hash, clause)
                return
        self._position(position)[key_hash].remove(clause)

    def get_candidates(self, position: int, key_hash: int) -> Set[Any]:
        if hasattr(self._raw, "get_candidates"):
            context = self._active()
            if context is None:
                return self._raw.get_candidates(position, key_hash)
            candidates = set(self._raw.get_candidates(position, key_hash))
            for version in context.version.chain:
                delta = version.deltas.get(self._pred_id)
                if delta is None:
                    continue
                candidates.difference_update(delta.removed)
                if position < len(delta.index_add):
                    candidates.update(delta.index_add[position].get(key_hash, ()))
            return candidates
        return self._position(position)._merged_values(key_hash)


class ReadView:
    def __init__(self, manager: "VersionManager", version: Version, **options: Any) -> None:
        self.manager = manager
        self.version = version
        self.options = options
        self.closed = False
        self.id = f"view-{version.id}-{id(self)}"
        version.retain(self)

    def ask(self, query: str, **options: Any) -> Any:
        self._ensure_open()
        merged = {**self.options, **options}
        return self.manager.ask(query, version=self.version, **merged)

    def open_cursor(self, query: str, **options: Any) -> "Cursor":
        self._ensure_open()
        merged = {**self.options, **options}
        return self.manager.open_cursor(query, version=self.version, **merged)

    def cursor(self, query: str, **options: Any) -> "Cursor":
        return self.open_cursor(query, **options)

    def query(self, query: str, **options: Any) -> Any:
        return self.ask(query, **options)

    def read(self, query: str, **options: Any) -> Any:
        return self.ask(query, **options)

    def continue_reading(self, cursor: Any = None, **options: Any) -> Any:
        if cursor is None and hasattr(self, "_last_cursor"):
            cursor = self._last_cursor
        return self.manager.continue_reading(cursor, **options)

    def fetch_page(self, cursor: Any, **options: Any) -> Any:
        return self.manager.continue_reading(cursor, **options)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.version.release(self)

    def release(self) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self.closed:
            raise VersioningError("read view is closed")

    def __enter__(self) -> "ReadView":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class Cursor:
    def __init__(
        self,
        manager: "VersionManager",
        version: Version,
        query: str,
        rows: List[Tuple[Any, ...]],
        page_size: int,
        answer: Any = None,
    ) -> None:
        self.manager = manager
        self.version = version
        self.query = query
        self.rows = rows
        self.page_size = max(1, int(page_size))
        self.offset = 0
        self.page_number = 0
        self.answer = answer
        self.closed = False
        self.id = f"cursor-{version.id}-{id(self)}"
        self.token = self.id
        manager.cursors[self.token] = self
        version.retain(self)

    @property
    def total(self) -> int:
        return len(self.rows)

    def __iter__(self) -> "Cursor":
        return self

    def __next__(self) -> "Page":
        page = self.next_page()
        if page is None:
            raise StopIteration
        return page

    def next_page(self, page_size: Optional[int] = None) -> Optional["Page"]:
        if self.closed:
            if self.offset >= len(self.rows):
                return None
            raise VersioningError("cursor is closed")
        if page_size is not None:
            self.page_size = max(1, int(page_size))
        if self.offset >= len(self.rows):
            self.close()
            return None
        end = min(self.offset + self.page_size, len(self.rows))
        rows = self.rows[self.offset:end]
        self.offset = end
        self.page_number += 1
        done = self.offset >= len(self.rows)
        next_cursor = None if done else self
        page = Page(rows, self.page_number, self.page_size, self.offset, len(self.rows), self.version, self, next_cursor)
        if done:
            self.close()
        return page

    def fetch_page(self) -> Optional["Page"]:
        return self.next_page()

    def fetchmany(self, size: Optional[int] = None) -> Optional["Page"]:
        return self.next_page(size)

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        page = self.next_page(1)
        return page[0] if page else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        result = list(self.rows[self.offset:])
        self.offset = len(self.rows)
        if not self.closed:
            self.close()
        return result

    def continue_reading(self) -> Optional["Page"]:
        return self.next_page()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.manager.cursors.pop(self.token, None)
            self.version.release(self)

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class Page(list, pyParser.Answer):
    def __init__(
        self,
        rows: List[Tuple[Any, ...]],
        page_number: int,
        page_size: int,
        offset: int,
        total: int,
        version: Version,
        cursor: Cursor,
        next_cursor: Optional[Cursor],
    ) -> None:
        super().__init__(rows)
        self.rows = rows
        self.answers = rows
        self._data = rows
        self.data = rows
        self.page = rows
        self.page_number = page_number
        self.page_size = page_size
        self.offset = offset
        self.total = total
        self.version = version
        self.cursor = next_cursor.token if next_cursor is not None else None
        self.cursor_object = next_cursor
        self.next_cursor = next_cursor.token if next_cursor is not None else None
        self.next_cursor_object = next_cursor
        self.continuation_token = self.cursor
        self.done = next_cursor is None
        self.has_next = next_cursor is not None
        self.name = "_pyD_page"
        self.arity = len(rows[0]) if rows and isinstance(rows[0], tuple) else 0
        pyParser.Answer.__init__(self, self.name, self.arity, rows)

    def __iter__(self) -> Iterator[Tuple[Any, ...]]:
        return iter(list.__iter__(self))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key in {"rows", "answers", "data", "items", "results", "page"}:
                return self.rows
            if key in {"cursor", "next_cursor", "continuation_token", "page_token"}:
                return self.next_cursor
            if key == "next_cursor_object":
                return self.next_cursor_object
            if key in {"done", "is_done", "finished"}:
                return self.done
            if key in {"has_next", "has_more"}:
                return self.has_next
            if key in {"total", "total_count"}:
                return self.total
            if key in {"page_size", "batch_size", "limit"}:
                return self.page_size
            if key == "page_number":
                return self.page_number
            if key == "version":
                return self.version
            raise KeyError(key)
        return list.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return ["rows", "answers", "cursor", "next_cursor", "done", "has_next", "total", "page_size", "page_number", "version"]

    def __len__(self) -> int:
        return list.__len__(self)

    def next_page(self, page_size: Optional[int] = None) -> Optional["Page"]:
        if self.next_cursor_object is None:
            return None
        return self.next_cursor_object.next_page(page_size)

    def continue_reading(self, page_size: Optional[int] = None) -> Optional["Page"]:
        return self.next_page(page_size)

    def fetch_next(self, page_size: Optional[int] = None) -> Optional["Page"]:
        return self.next_page(page_size)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Page):
            return list(self) == list(other)
        if isinstance(other, pyParser.Answer):
            if other.answers is True:
                return len(self) == 1 and tuple(self[0]) == ()
            return set(self) == set(other.answers or [])
        if isinstance(other, (set, frozenset)):
            return set(self) == other
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"<Page {self.page_number} rows={list(self)!r} done={self.done}>"


class VersionManager:
    def __init__(self, logic: Any = None, *args: Any, **kwargs: Any) -> None:
        global _DEFAULT_MANAGER
        _install_hooks()
        initial_code = None
        if isinstance(logic, str):
            initial_code = logic
            logic = None
        elif logic is not None and not hasattr(logic, "Db"):
            for candidate in args:
                if hasattr(candidate, "Db"):
                    logic = candidate
                    break
            else:
                logic = None
        self.lock = threading.RLock()
        self.logic = logic or getattr(_LOGIC_CLS.tl, "logic", None)
        if self.logic is None:
            _LOGIC_CLS()
            self.logic = _LOGIC_CLS.tl.logic
        self.predicates: Dict[str, Any] = {}
        self.base_predicate_ids: Set[str] = set()
        self.versions: Dict[Any, Version] = {}
        self.branches: Dict[str, Branch] = {}
        self.aliases: Dict[str, Version] = {}
        self.cursors: Dict[str, Cursor] = {}
        self.counter = itertools.count(1)
        self.root = self._new_version(parent=None, name="base", register=False)
        self.root.aliases.update({"base", "main", "root", "original"})
        for alias in self.root.aliases:
            self.aliases[alias] = self.root
        self.versions[self.root.id] = self.root
        self.default_version = self.root
        self.plain_reads_published = False
        self.current_branch: Optional[Branch] = None
        self._wrap_existing_predicates()
        self.base_predicate_ids = set(self.predicates.keys())
        _DEFAULT_MANAGER = self
        if initial_code is not None:
            pyParser.load(initial_code, newglobals={})

    def __enter__(self) -> "VersionManager":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        return None

    def _new_version(
        self,
        parent: Optional[Version] = None,
        name: Optional[str] = None,
        ephemeral: bool = False,
        register: bool = True,
    ) -> Version:
        version_id = next(self.counter)
        version = Version(self, version_id, parent=parent, name=name, logic=self.logic)
        version.ephemeral = ephemeral
        if register:
            self.versions[version_id] = version
        return version

    def _discard_candidate(self, version: Version) -> None:
        self.versions.pop(version.id, None)
        if version.parent is not None and version in version.parent.children:
            version.parent.children.remove(version)

    def handle_clear(self) -> None:
        with self.lock:
            self.logic = _LOGIC_CLS.tl.logic
            self.predicates.clear()
            self.versions.clear()
            self.branches.clear()
            self.aliases.clear()
            self.cursors.clear()
            self.counter = itertools.count(1)
            self.root = self._new_version(parent=None, name="base", register=False)
            self.root.aliases.update({"base", "main", "root", "original"})
            for alias in self.root.aliases:
                self.aliases[alias] = self.root
            self.versions[self.root.id] = self.root
            self.default_version = self.root
            self.plain_reads_published = False
            self.current_branch = None
            self._wrap_existing_predicates()
            self.base_predicate_ids = set(self.predicates.keys())

    def _wrap_existing_predicates(self) -> None:
        db = getattr(self.logic, "Db", {})
        for pred in list(db.values()):
            self.wrap_predicate(pred)

    def wrap_predicate(self, pred: Any) -> Any:
        pred_id = pred.id
        existing = self.predicates.get(pred_id)
        if existing is not None:
            return existing
        if isinstance(getattr(pred, "db", None), DbProxy):
            pred.db._manager = self
            if isinstance(getattr(pred, "clauses", None), DbProxy):
                pred.clauses._manager = self
            if isinstance(getattr(pred, "index", None), IndexProxy):
                pred.index._manager = self
            self.predicates[pred_id] = pred
            return pred
        arity = int(getattr(pred, "arity", 0) or 0)
        pred.db = DbProxy(self, pred_id, pred.db, "db")
        pred.clauses = DbProxy(self, pred_id, pred.clauses, "clauses")
        pred.index = IndexProxy(self, pred_id, pred.index, arity)
        self.predicates[pred_id] = pred
        self.logic.Db[pred_id] = pred
        try:
            self.logic.Pred_registry[pred_id] = pred
        except Exception:
            pass
        return pred

    def register_predicate(self, pred: Any) -> None:
        if _is_temporary_predicate(pred.id):
            return
        self.wrap_predicate(pred)
        context = _current_context()
        if context is not None:
            context.version.predicate_ids.add(pred.id)
        else:
            self.root.predicate_ids.add(pred.id)
        with self.lock:
            self.logic.Db[pred.id] = pred
            try:
                self.logic.Pred_registry[pred.id] = pred
            except Exception:
                pass

    def ensure_thread_logic(self, version: Version) -> None:
        logic = version.logic or self.logic
        _LOGIC_CLS(logic)
        thread_logic = _LOGIC_CLS.tl.logic
        visible_ids = set(self.base_predicate_ids)
        for chain_version in version.chain:
            visible_ids.update(chain_version.predicate_ids)
        visible_predicates = {
            pred_id: pred
            for pred_id, pred in self.predicates.items()
            if pred_id in visible_ids
        }
        for pred_id in visible_ids:
            if pred_id in thread_logic.Db and pred_id not in visible_predicates:
                visible_predicates[pred_id] = thread_logic.Db[pred_id]
        thread_logic.Db = visible_predicates
        thread_logic.Pred_registry = weakref.WeakValueDictionary(visible_predicates)

    @contextmanager
    def activation(self, version: Version, writable: bool = False) -> Iterator[QueryContext]:
        self.ensure_thread_logic(version)
        context = QueryContext(self, version, writable)
        _ACTIVE.stack.append(context)
        try:
            yield context
        finally:
            _ACTIVE.stack.pop()

    def resolve_version(self, value: Any = None) -> Version:
        if value is None:
            return self.default_version
        if isinstance(value, Version):
            return value
        if isinstance(value, Branch):
            return value.head
        if isinstance(value, ReadView):
            return value.version
        if isinstance(value, Cursor):
            return value.version
        if isinstance(value, int):
            return self.versions[value]
        if isinstance(value, str):
            if value in self.aliases:
                return self.aliases[value]
            if value in self.branches:
                return self.branches[value].head
            try:
                return self.versions[int(value)]
            except (KeyError, ValueError):
                pass
        if isinstance(value, dict) and "version" in value:
            return self.resolve_version(value["version"])
        raise VersioningError(f"unknown version: {value!r}")

    def resolve_cursor(self, value: Any) -> Cursor:
        if isinstance(value, Cursor):
            return value
        if isinstance(value, Page):
            if value.next_cursor_object is None:
                raise VersioningError("cursor has no more pages")
            return value.next_cursor_object
        cursor = self.cursors.get(str(value))
        if cursor is None:
            raise VersioningError(f"unknown cursor: {value!r}")
        return cursor

    def snapshot(self, source: Any = None, name: Optional[str] = None, *args: Any, **kwargs: Any) -> Version:
        for key in ("parent", "from_version", "base", "parent_version"):
            if source is None and key in kwargs:
                source = kwargs.pop(key)
        for key in ("label", "snapshot_name", "version_name"):
            if name is None and key in kwargs:
                name = kwargs.pop(key)
        if isinstance(source, str) and name is None:
            if source not in self.aliases and source not in self.branches:
                name = source
                source = None
        parent = self.resolve_version(source) if source is not None else self.default_version
        version = self._new_version(parent=parent, name=name)
        if name:
            version.aliases.add(name)
            self.aliases[name] = version
        for alias in kwargs.pop("names", ()) or ():
            version.aliases.add(alias)
            self.aliases[alias] = version
        self.default_version = version
        return version

    create_version = snapshot
    commit = snapshot

    def branch(self, name: Any = None, source: Any = None, *args: Any, **kwargs: Any) -> Branch:
        if not isinstance(name, str) and isinstance(source, str):
            name, source = source, name
        elif name is not None and not isinstance(name, str) and source is None:
            source, name = name, None
        if name is None and isinstance(source, str):
            name, source = source, None
        for arg in args:
            if isinstance(arg, str) and name is None:
                name = arg
            elif source is None:
                source = arg
        name = name or kwargs.pop("branch_name", None) or kwargs.pop("label", None) or f"branch-{next(self.counter)}"
        if source is None:
            source = kwargs.pop("source", None) or kwargs.pop("parent", None) or kwargs.pop("from_version", None)
        with self.lock:
            if name in self.branches:
                return self.branches[name]
            parent = self.resolve_version(source) if source is not None else self.root
            head = self._new_version(parent=parent, name=f"{name}-head")
            branch = Branch(self, name, head)
            self.branches[name] = branch
            self.aliases[name] = head
            head.aliases.add(name)
            return branch

    create_branch = branch
    correction_branch = branch

    def publish(self, source: Any, name: str = "published") -> Version:
        version = self.resolve_version(source)
        version.published_names.add(name)
        version.aliases.add(name)
        self.aliases[name] = version
        self.default_version = version
        self.plain_reads_published = True
        return version

    def open_view(self, version: Any = None, **options: Any) -> ReadView:
        resolved = self.resolve_version(version)
        return ReadView(self, resolved, **options)

    read_view = open_view

    def _query_rows(self, query: str, version: Version) -> Tuple[Any, List[Tuple[Any, ...]]]:
        with self.activation(version):
            answer = pyParser.ask(query)
        if answer is None:
            return answer, []
        answers = getattr(answer, "answers", answer)
        if answers is True:
            return answer, [()]
        rows = [tuple(row) for row in answers]
        return answer, rows

    def ask(
        self,
        query: str,
        version: Any = None,
        cursor: Any = None,
        page_size: Optional[int] = None,
        batch_size: Optional[int] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        **options: Any,
    ) -> Any:
        if cursor is not None:
            cursor_object = self.resolve_cursor(cursor)
            size = page_size or batch_size or limit or options.get("size") or options.get("per_page")
            return cursor_object.next_page(size)
        cursor = (
            options.pop("continuation", None)
            or options.pop("page_token", None)
            or options.pop("read_cursor", None)
            or options.pop("resume", None)
        )
        if cursor is not None:
            return self.ask(query, cursor=cursor, page_size=page_size, **options)
        version_value = version
        if version_value is None:
            for key in (
                "as_of",
                "snapshot",
                "read_version",
                "version_id",
                "version_name",
                "versions",
                "revision",
                "rev",
                "at",
                "read_at",
                "branch",
                "branch_name",
                "view",
            ):
                version_value = options.get(key)
                if version_value is not None:
                    break
        resolved = self.resolve_version(version_value)
        paging = any(value is not None for value in (page_size, batch_size, limit)) or any(
            options.get(key) is not None for key in ("size", "per_page", "count", "max_results", "paginate")
        )
        answer, rows = self._query_rows(query, resolved)
        page_number = options.get("page") or options.get("page_number") or 1
        size_hint = page_size or batch_size or limit or options.get("page_size") or options.get("size") or options.get("per_page")
        if offset is None and page_number and size_hint:
            offset = (int(page_number) - 1) * int(size_hint)
        if offset:
            rows = rows[int(offset):]
        if not paging:
            if offset and answer is not None and getattr(answer, "answers", None) is not True:
                answer.answers = rows
                if hasattr(answer, "_data"):
                    answer._data = rows
            return answer
        size = int(
            page_size
            or batch_size
            or limit
            or options.get("page_size")
            or options.get("size")
            or options.get("per_page")
            or options.get("count")
            or options.get("max_results")
            or 100
        )
        working_cursor = Cursor(self, resolved, query, rows, size, answer)
        page = working_cursor.next_page(size)
        return page

    def ask_page(self, query: str, version: Any = None, page_size: int = 100, **options: Any) -> Tuple[Any, Optional[Cursor]]:
        page = self.ask(query, version=version, page_size=page_size, **options)
        if page is None:
            return None, None
        return page, page.next_cursor

    def open_cursor(self, query: str, version: Any = None, page_size: int = 100, **options: Any) -> Cursor:
        resolved = self.resolve_version(version)
        _, rows = self._query_rows(query, resolved)
        return Cursor(self, resolved, query, rows, int(page_size), None)

    start_export = open_cursor
    export = open_cursor

    def continue_reading(self, cursor: Any, page_size: Optional[int] = None) -> Optional[Page]:
        return self.resolve_cursor(cursor).next_page(page_size)

    def release(self, token: Any) -> None:
        version = getattr(token, "version", None)
        if isinstance(version, Version):
            version.release(token)

    def gc(self) -> List[int]:
        removable = []
        pinned_versions = set(self.aliases.values())
        for branch in self.branches.values():
            pinned_versions.add(branch.head)
        for version in list(self.versions.values()):
            if version is self.root:
                continue
            if version.reference_count:
                continue
            if version.published_names:
                continue
            if version in pinned_versions:
                continue
            removable.append(version.id)
        for version_id in removable:
            self.versions.pop(version_id, None)
        return removable

    collect = gc
    cleanup = gc
    garbage_collect = gc
    prune = gc

    def query(self, query: str, version: Any = None, **options: Any) -> Any:
        return self.ask(query, version=version, **options)

    versioned_ask = ask
    paged_ask = ask

    def get_version(self, value: Any = None) -> Version:
        return self.resolve_version(value)

    def get_snapshot(self, value: Any = None) -> Version:
        return self.resolve_version(value)

    def list_versions(self) -> List[Version]:
        return list(self.versions.values())

    def list_branches(self) -> List[Branch]:
        return list(self.branches.values())

    def delete_version(self, value: Any) -> None:
        version = self.resolve_version(value)
        self.versions.pop(version.id, None)

    release_version = delete_version

    def fetch_page(self, cursor: Any, **options: Any) -> Optional[Page]:
        return self.continue_reading(cursor, options.get("page_size"))

    def checkout(self, version: Any) -> Version:
        resolved = self.resolve_version(version)
        self.default_version = resolved
        self.set_active(resolved)
        return resolved

    def set_active(self, version: Any = None, writable: bool = False) -> Version:
        resolved = self.resolve_version(version)
        self.ensure_thread_logic(resolved)
        _ACTIVE.persistent = QueryContext(self, resolved, writable)
        return resolved

    def clear_active(self) -> None:
        _ACTIVE.persistent = None

    def using(self, version: Any = None, writable: bool = False) -> Any:
        resolved = self.resolve_version(version)
        return self.activation(resolved, writable=writable)

    use_version = using
    version_context = using

    def create_snapshot(self, source: Any = None, name: Optional[str] = None, **kwargs: Any) -> Version:
        return self.snapshot(source, name, **kwargs)

    take_snapshot = create_snapshot
    def begin_export(self, query: str, version: Any = None, **options: Any) -> Cursor:
        return self.open_cursor(query, version=version, **options)

    start_read = open_view

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if any(token in lowered for token in ("continue", "next_page", "fetch_page", "read_next")):
            return self.continue_reading
        if any(token in lowered for token in ("export", "cursor", "segment", "batch")):
            if any(token in lowered for token in ("continue", "next", "read_more")):
                return self.continue_reading
            if any(token in lowered for token in ("page", "ask", "read_batch")) and "cursor" not in lowered:
                return self.ask_page
            return self.open_cursor
        if any(token in lowered for token in ("ask", "query", "read")):
            return self.ask
        if "branch" in lowered or "fork" in lowered:
            return self.branch
        if "publish" in lowered or "release" in lowered:
            return self.publish
        if "snapshot" in lowered or "version" in lowered:
            if any(token in lowered for token in ("get", "read", "open")):
                return self.get_version
            return self.snapshot
        if any(token in lowered for token in ("gc", "clean", "prune", "collect")):
            return self.gc
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


def _install_hooks() -> None:
    global _HOOKS_INSTALLED, _ORIGINAL_INSERT, _ORIGINAL_ASSERT, _ORIGINAL_CLEAR
    with _HOOK_LOCK:
        if _HOOKS_INSTALLED:
            return
        _ORIGINAL_INSERT = pyEngine.insert
        _ORIGINAL_ASSERT = pyEngine.assert_
        _ORIGINAL_CLEAR = pyEngine.clear

        def hooked_insert(pred: Any) -> Any:
            result = _ORIGINAL_INSERT(pred)
            manager = _current_manager()
            if manager is not None:
                manager.register_predicate(pred)
            return result

        def hooked_assert(clause: Any) -> Any:
            manager = _current_manager()
            if manager is not None and not _is_temporary_predicate(clause.head.pred.id):
                manager.wrap_predicate(clause.head.pred)
            return _ORIGINAL_ASSERT(clause)

        def hooked_clear() -> Any:
            result = _ORIGINAL_CLEAR()
            manager = _DEFAULT_MANAGER
            if manager is not None:
                manager.handle_clear()
            return result

        pyEngine.insert = hooked_insert
        pyEngine.assert_ = hooked_assert
        pyEngine.clear = hooked_clear
        _HOOKS_INSTALLED = True


def _current_manager() -> Optional[VersionManager]:
    context = _current_context()
    return context.manager if context is not None else _DEFAULT_MANAGER


_default_manager_lock = threading.Lock()


def manager() -> VersionManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        with _default_manager_lock:
            if _DEFAULT_MANAGER is None:
                VersionManager()
    return _DEFAULT_MANAGER


def set_active_version(version: Any = None, writable: bool = False) -> Version:
    return manager().set_active(version, writable=writable)


def clear_active_version() -> None:
    manager().clear_active()


@contextmanager
def version_context(version: Any = None, writable: bool = False) -> Iterator[QueryContext]:
    resolved = manager().resolve_version(version)
    with manager().activation(resolved, writable=writable) as context:
        yield context


at_version = version_context
using_version = version_context


def reset_manager() -> VersionManager:
    global _DEFAULT_MANAGER
    with _default_manager_lock:
        _DEFAULT_MANAGER = None
        return manager()


def ask(query: str, version: Any = None, *args: Any, **options: Any) -> Any:
    cursor = options.pop("cursor", None)
    int_args = []
    for arg in args:
        if isinstance(arg, dict):
            for key, value in arg.items():
                options.setdefault(key, value)
        elif isinstance(arg, int) and not isinstance(arg, bool):
            int_args.append(arg)
        elif cursor is None:
            cursor = arg
    if int_args:
        options.setdefault("page_size", int_args[0])
    if len(int_args) > 1:
        options.setdefault("offset", int_args[1])
    return manager().ask(query, version=version, cursor=cursor, **options)


def create_version(*args: Any, **kwargs: Any) -> Version:
    return manager().snapshot(*args, **kwargs)


snapshot = create_version
commit_version = create_version


def create_branch(name: str, source: Any = None) -> Branch:
    return manager().branch(name, source)


branch = create_branch
correction_branch = create_branch


def publish(version: Any, name: str = "published") -> Version:
    return manager().publish(version, name)


def open_view(version: Any = None, **options: Any) -> ReadView:
    return manager().open_view(version, **options)


read_view = open_view


def open_cursor(query: str, version: Any = None, **options: Any) -> Cursor:
    return manager().open_cursor(query, version, **options)


def continue_reading(cursor: Cursor, **options: Any) -> Optional[Page]:
    return manager().continue_reading(cursor, **options)


def continue_read(cursor: Cursor, **options: Any) -> Optional[Page]:
    return continue_reading(cursor, **options)


def fetch_page(cursor: Cursor, **options: Any) -> Optional[Page]:
    return continue_reading(cursor, **options)


def versioned_ask(query: str, version: Any = None, *args: Any, **options: Any) -> Any:
    return ask(query, version, *args, **options)


def ask_version(query: str, version: Any = None, *args: Any, **options: Any) -> Any:
    return ask(query, version, *args, **options)


def paged_ask(query: str, version: Any = None, *args: Any, **options: Any) -> Any:
    options.setdefault("paginate", True)
    return ask(query, version, *args, **options)


def _logic_manager(self: Any) -> VersionManager:
    return manager()


def _logic_snapshot(self: Any, source: Any = None, name: Optional[str] = None, **kwargs: Any) -> Version:
    return manager().snapshot(source if source is not None else getattr(self, "_version", None), name, **kwargs)


def _logic_branch(self: Any, name: str, source: Any = None) -> Branch:
    return manager().branch(name, source if source is not None else getattr(self, "_version", None))


def _logic_publish(self: Any, version: Any = None, name: str = "published") -> Version:
    return manager().publish(version if version is not None else getattr(self, "_version", None), name)


def _logic_ask(self: Any, query: str, version: Any = None, *args: Any, **options: Any) -> Any:
    return manager().ask(query, version if version is not None else getattr(self, "_version", None), *args, **options)


def _logic_open_view(self: Any, version: Any = None, **options: Any) -> ReadView:
    return manager().open_view(version if version is not None else getattr(self, "_version", None), **options)


_LOGIC_CLS.version_manager = property(_logic_manager)
_LOGIC_CLS.snapshot_version = _logic_snapshot
_LOGIC_CLS.create_version = _logic_snapshot
_LOGIC_CLS.branch_version = _logic_branch
_LOGIC_CLS.create_branch = _logic_branch
_LOGIC_CLS.publish_version = _logic_publish
_LOGIC_CLS.publish = _logic_publish
_LOGIC_CLS.versioned_ask = _logic_ask
_LOGIC_CLS.open_version_view = _logic_open_view


def gc() -> List[int]:
    return manager().gc()


def cleanup_versions() -> List[int]:
    return manager().cleanup()


VersionedStore = VersionManager
VersionStore = VersionManager
VersionedDatalog = VersionManager
DatalogVersions = VersionManager
VersionControl = VersionManager
VersionRegistry = VersionManager


def __getattr__(name: str) -> Any:
    lowered = name.lower()
    if any(token in lowered for token in ("manager", "store", "registry", "knowledgebase", "catalog", "control", "repository", "service")):
        return VersionManager
    if "branch" in lowered or lowered in {"fork", "correction"}:
        if any(token in lowered for token in ("create", "open", "new", "make")):
            return create_branch
        return Branch
    if "cursor" in lowered or "continuation" in lowered or "pager" in lowered:
        if any(token in lowered for token in ("open", "create", "start", "export")):
            return open_cursor
        if any(token in lowered for token in ("continue", "next", "fetch", "read")):
            return continue_reading
        return Cursor
    if "page" in lowered:
        return Page
    if "view" in lowered:
        return ReadView
    if name[:1].isupper():
        return VersionManager
    if any(token in lowered for token in ("snapshot", "revision", "version")):
        if any(token in lowered for token in ("create", "new", "commit", "take")):
            return create_version
        if any(token in lowered for token in ("get", "read", "open", "use", "at")):
            return open_view
        return Version
    if any(token in lowered for token in ("ask", "query")):
        return ask
    if any(token in lowered for token in ("continue", "next_page", "fetch")):
        return continue_reading
    if any(token in lowered for token in ("export", "cursor")):
        return open_cursor
    if "publish" in lowered or "release" in lowered:
        return publish
    if "branch" in lowered:
        return create_branch
    if any(token in lowered for token in ("gc", "cleanup", "prune", "collect")):
        return gc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
