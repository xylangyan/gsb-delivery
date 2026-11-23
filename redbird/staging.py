"""Staging review queue on top of Red Bird repositories

This module implements a two-layer staging area used to inspect
rows before they are ingested:

- a *draft* layer: a :class:`~redbird.repos.csv.CSVFileRepo` holding
  unreviewed CSV draft rows
- a *verified* layer: a :class:`~redbird.repos.sqlalchemy.SQLRepo`
  (SQLite) holding reviewed records. Any record in this layer --
  including revoked ones -- shadows the draft carrying the same
  business key.

Rows are served to reviewers round-robin by account: an account
consumes its turn only when a row is actually yielded for it. Inside
one account, rows are served from high priority to low priority;
accounts rotate in registration order.

Pages are navigated with cursors that are persisted in a small
SQLite control database, so review sessions survive process
restarts within an agreed retention period. Registering a batch of
drafts builds a reusable index (account, priority, business key and
file offset of each row) so that assembling the first page or
turning a page never requires rescanning the whole CSV file to
determine the next account.

Both layers stay ordinary Red Bird repositories: the existing
access pattern of explicit backend filtering
(``repo.filter_by(...)``) keeps working unchanged.

Examples
--------

.. code-block:: python

    from redbird.repos import CSVFileRepo, SQLRepo
    from redbird.staging import StagingQueue

    drafts = CSVFileRepo(filename="drafts.csv", model=Draft)
    verified = SQLRepo(model=Verified, table="verified",
                       conn_string="sqlite:///verified.db",
                       if_missing="create", id_field="key")

    queue = StagingQueue(drafts, verified, control="staging_control.db")
    queue.register_batch([Draft(key="A1", account="A", priority=10)])

    page = queue.next_page(2)            # first page, fresh session
    page = queue.next_page(2, cursor=page.cursor)  # turn the page
    queue.explain("A1")                  # why a key is (not) listed
"""

import io
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterator, List, Optional, Union

from pydantic import BaseModel


class CursorError(Exception):
    """Base class for staging cursor errors"""


class CursorNotFoundError(CursorError):
    """The cursor token is unknown (never issued or already pruned)"""


class CursorExpiredError(CursorError):
    """The cursor is older than the agreed retention period"""


class CursorState(BaseModel):
    """Position of a review session in the account rotation"""

    position: int = 0
    progress: Dict[str, int] = {}
    created_at: float
    updated_at: float


@dataclass
class Page:
    """One page of a review session

    Attributes
    ----------
    items : list
        Draft rows of this page (instances of the draft repo model)
    cursor : str
        Token to pass to :meth:`StagingQueue.next_page` to continue
        this session
    exhausted : bool
        True if a full account rotation yielded nothing more
    """

    items: List[Any]
    cursor: str
    exhausted: bool


@dataclass
class Explanation:
    """Why a business key is (not) part of the checklist

    Attributes
    ----------
    key : str
        The business key inspected
    visible : bool
        Whether the key currently appears in the checklist
    reason : str
        One of ``'visible'``, ``'shadowed'`` or ``'missing'``
    detail : str
        Human readable explanation
    """

    key: str
    visible: bool
    reason: str
    detail: str


class StagingQueue:
    """Round-robin review queue over a CSV draft and a verified layer

    Parameters
    ----------
    drafts : CSVFileRepo
        Repository of the CSV draft layer. Its model must carry the
        account, business key and priority fields. The repository
        should have no ``id_field`` so appends stay cheap.
    verified : SQLRepo
        Repository of the verified (SQLite) layer. Any record found
        here -- verified or revoked -- shadows the draft with the
        same business key.
    control : path-like
        Path of the SQLite control database holding the account
        registry, the reusable draft index and the cursors.
    account_field : str
        Item field naming the account (户) of a draft row.
    key_field : str
        Item field of the business key, present in both layers.
    priority_field : str
        Item field of the draft priority. Higher values are served
        first; ties keep the registration order.
    status_field : str, optional
        Field of the verified layer describing the record status.
        Only used for explanations.
    revoked_value : any
        Value of ``status_field`` marking a revoked record.
    cursor_ttl : datetime.timedelta
        Agreed retention period of cursors. Activity on a cursor
        renews it; expired cursors are rejected and may be pruned.

    Notes
    -----
    The draft index is built when batches are registered and is
    reused by every page assembly, so neither the first page nor
    page turns rescan the CSV file to determine the next account.
    If the CSV file is rewritten behind the queue's back (detected
    via a key mismatch), the index is rebuilt once automatically.
    """

    def __init__(self, drafts, verified, control, *,
                 account_field: str = "account",
                 key_field: str = "key",
                 priority_field: str = "priority",
                 status_field: Optional[str] = "status",
                 revoked_value: Any = "revoked",
                 cursor_ttl: timedelta = timedelta(days=1)):
        self.drafts = drafts
        self.verified = verified
        self.control_path = str(control)
        self.account_field = account_field
        self.key_field = key_field
        self.priority_field = priority_field
        self.status_field = status_field
        self.revoked_value = revoked_value
        self.cursor_ttl = cursor_ttl
        self._init_control()

    # Control database
    # ----------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.control_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_control(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_index (
                    seq INTEGER PRIMARY KEY,
                    account TEXT NOT NULL,
                    priority REAL NOT NULL,
                    key TEXT NOT NULL,
                    offset INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_draft_account
                    ON draft_index (account, priority DESC, seq);
                CREATE INDEX IF NOT EXISTS ix_draft_key
                    ON draft_index (key);
                CREATE TABLE IF NOT EXISTS cursors (
                    token TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    # Registration
    # ------------

    def register_account(self, account: str):
        "Register an account, keeping registration order"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seq FROM accounts WHERE account = ?", (account,)
            ).fetchone()
            if row is None:
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM accounts"
                ).fetchone()["next"]
                conn.execute(
                    "INSERT INTO accounts (account, seq) VALUES (?, ?)",
                    (account, seq),
                )

    def registered_accounts(self) -> List[str]:
        "Accounts in registration order"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account FROM accounts ORDER BY seq"
            ).fetchall()
        return [row["account"] for row in rows]

    def register_batch(self, items):
        """Register a batch of draft rows and index them

        Appends the rows to the CSV draft repository and records a
        reusable index entry (account, priority, business key, file
        offset) per row in the control database. Unknown accounts
        are registered on first sight.
        """
        filename = self.drafts.filename
        if not filename.exists() or filename.stat().st_size == 0:
            self.drafts.create(if_exists="ignore")

        items = [self.drafts.to_item(item) for item in items]
        accounts = [self.drafts.get_field_value(item, self.account_field) for item in items]
        for account in accounts:
            self.register_account(account)

        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM draft_index"
            ).fetchone()["next"]
            for item, account in zip(items, accounts):
                with open(filename, "r", newline="") as file:
                    file.seek(0, 2)
                    offset = file.tell()
                self.drafts.insert(item)
                conn.execute(
                    "INSERT INTO draft_index (seq, account, priority, key, offset)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        seq,
                        account,
                        self.drafts.get_field_value(item, self.priority_field),
                        self.drafts.get_field_value(item, self.key_field),
                        offset,
                    ),
                )
                seq += 1

    # Reading layers
    # --------------

    def _shadow_keys(self) -> set:
        "Business keys shadowed by the verified layer (verified or revoked)"
        keys = set()
        for item in self.verified.filter_by().all():
            keys.add(self.verified.get_field_value(item, self.key_field))
        return keys

    def _account_entries(self, account: str, shadow: set) -> List[sqlite3.Row]:
        "Visible index entries of an account, high priority first"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, key, offset FROM draft_index"
                " WHERE account = ? ORDER BY priority DESC, seq",
                (account,),
            ).fetchall()
        return [row for row in rows if row["key"] not in shadow]

    def _parse_line(self, line: str):
        reader = self.drafts.get_reader(io.StringIO(line))
        data = next(reader, None)
        if data is None:
            raise IOError("Draft CSV row could not be parsed")
        return self.drafts.data_to_item(data)

    def _read_entry(self, entry: sqlite3.Row):
        "Read the draft row an index entry points to"
        try:
            return self._read_at(entry["offset"], entry["key"])
        except IOError:
            # The CSV file was rewritten outside register_batch:
            # rebuild the index once and retry via the stable seq
            self._rebuild_index()
            with self._connect() as conn:
                fresh = conn.execute(
                    "SELECT seq, key, offset FROM draft_index WHERE seq = ?",
                    (entry["seq"],),
                ).fetchone()
            if fresh is None:
                raise IOError("Draft row vanished from the CSV file")
            return self._read_at(fresh["offset"], fresh["key"])

    def _read_at(self, offset: int, key):
        with open(self.drafts.filename, "r", newline="") as file:
            file.seek(offset)
            line = file.readline()
        item = self._parse_line(line)
        if self.drafts.get_field_value(item, self.key_field) != key:
            raise IOError("Draft index is stale")
        return item

    def _rebuild_index(self):
        "Rebuild the draft index from the CSV file (one full scan)"
        entries = []
        with open(self.drafts.filename, "r", newline="") as file:
            file.readline()  # header
            while True:
                offset = file.tell()
                line = file.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                item = self._parse_line(line)
                entries.append((
                    self.drafts.get_field_value(item, self.account_field),
                    self.drafts.get_field_value(item, self.priority_field),
                    self.drafts.get_field_value(item, self.key_field),
                    offset,
                ))
        with self._connect() as conn:
            conn.execute("DELETE FROM draft_index")
            conn.executemany(
                "INSERT INTO draft_index (seq, account, priority, key, offset)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (seq, account, priority, key, offset)
                    for seq, (account, priority, key, offset) in enumerate(entries)
                ],
            )

    # Cursors
    # -------

    def start_session(self) -> str:
        "Start a fresh review session and return its cursor token"
        now = time.time()
        token = uuid.uuid4().hex
        state = CursorState(created_at=now, updated_at=now)
        self._save_cursor(token, state)
        return token

    def _save_cursor(self, token: str, state: CursorState):
        expires_at = time.time() + self.cursor_ttl.total_seconds()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cursors (token, state, expires_at)"
                " VALUES (?, ?, ?)",
                (token, state.json(), expires_at),
            )

    def _load_cursor(self, token: str) -> CursorState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, expires_at FROM cursors WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            raise CursorNotFoundError(f"Unknown cursor: {token}")
        if time.time() > row["expires_at"]:
            raise CursorExpiredError(
                f"Cursor {token} exceeded the retention period"
            )
        return CursorState(**json.loads(row["state"]))

    def prune_cursors(self) -> int:
        "Delete cursors past the retention period, return their count"
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM cursors WHERE expires_at < ?", (time.time(),)
            )
            return cur.rowcount

    # Paging
    # ------

    def next_page(self, size: int, cursor: Optional[str] = None) -> Page:
        """Assemble the next page of a review session

        Accounts rotate in registration order and an account consumes
        its turn only when a row is actually yielded for it. Within an
        account, visible drafts are served from high to low priority.
        Drafts shadowed by the verified layer are skipped. Without a
        cursor a fresh session is started; pass ``page.cursor`` to
        continue a session, possibly from a restarted process.
        """
        if cursor is None:
            token = uuid.uuid4().hex
            now = time.time()
            state = CursorState(created_at=now, updated_at=now)
        else:
            token = cursor
            state = self._load_cursor(token)

        accounts = self.registered_accounts()
        items = []
        exhausted = True
        if accounts and size > 0:
            shadow = self._shadow_keys()
            progress = dict(state.progress)
            position = state.position % len(accounts)
            skips = 0
            while len(items) < size and skips < len(accounts):
                account = accounts[position]
                entries = self._account_entries(account, shadow)
                used = progress.get(account, 0)
                if used < len(entries):
                    items.append(self._read_entry(entries[used]))
                    progress[account] = used + 1
                    skips = 0
                else:
                    # Nothing (left) for this account: the turn is
                    # passed without consuming the account's chance
                    skips += 1
                position = (position + 1) % len(accounts)
            exhausted = skips >= len(accounts)
            state.position = position
            state.progress = progress

        state.updated_at = time.time()
        self._save_cursor(token, state)
        return Page(items=items, cursor=token, exhausted=exhausted)

    def iter_checklist(self, page_size: int = 100) -> Iterator:
        "Yield all currently visible draft rows in checklist order"
        cursor = None
        while True:
            page = self.next_page(page_size, cursor=cursor)
            yield from page.items
            if page.exhausted:
                return
            cursor = page.cursor

    # Explanation
    # -----------

    def explain(self, key) -> Explanation:
        "Explain why a business key is (not) part of the checklist"
        record = self.verified.filter_by(**{self.key_field: key}).first()
        if record is not None:
            status = None
            if self.status_field is not None:
                try:
                    status = self.verified.get_field_value(record, self.status_field)
                except (AttributeError, KeyError):
                    status = None
            if status == self.revoked_value:
                detail = (
                    f"Draft '{key}' is shadowed: the verified layer holds "
                    f"a revoked record for this business key."
                )
            else:
                detail = (
                    f"Draft '{key}' is shadowed: the verified layer already "
                    f"holds a record for this business key."
                )
            return Explanation(key=key, visible=False, reason="shadowed", detail=detail)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT account FROM draft_index WHERE key = ? ORDER BY seq LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            return Explanation(
                key=key, visible=False, reason="missing",
                detail=f"No draft row carries business key '{key}'.",
            )

        account = row["account"]
        entries = self._account_entries(account, self._shadow_keys())
        for rank, entry in enumerate(entries, start=1):
            if entry["key"] == key:
                return Explanation(
                    key=key, visible=True, reason="visible",
                    detail=(
                        f"Draft '{key}' is pending review: rank {rank} of "
                        f"{len(entries)} for account '{account}'."
                    ),
                )
        return Explanation(
            key=key, visible=False, reason="shadowed",
            detail=f"Draft '{key}' is shadowed by the verified layer.",
        )
