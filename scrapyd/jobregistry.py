import datetime
import json
import sqlite3
from pathlib import Path

from zope.interface import implementer

from scrapyd.interfaces import IJobRegistry


@implementer(IJobRegistry)
class JobRegistry:
    """Persistent record of the egg content bound to each accepted job.

    The spider queue alone only stores a version name, which can be withdrawn or republished before the job starts.
    This registry keeps, across daemon restarts, the exact content fingerprint accepted for each job, the caller's
    request ID (for idempotent retries) and the evidence that a job's process actually started.
    """

    PENDING = "pending"
    HELD = "held"
    RUNNING = "running"
    FINISHED = "finished"

    def __init__(self, config, database="job_registry", table="job_registry"):
        dbs_dir = config.get("dbs_dir", "dbs")
        if dbs_dir == ":memory:":
            connection_string = dbs_dir
        else:
            dbs_path = Path(dbs_dir)
            if not dbs_path.exists():
                dbs_path.mkdir(parents=True, exist_ok=True)
            connection_string = str(dbs_path / f"{database}.db")

        self.table = table
        self.conn = sqlite3.connect(connection_string, check_same_thread=False)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "job_id text PRIMARY KEY, project text NOT NULL, spider text NOT NULL, version text, fingerprint text, "
            "caller text, request_id text, state text NOT NULL, started_at text, response_json text NOT NULL, "
            "created_at text NOT NULL)"
        )
        self.conn.commit()
        self._reconcile_restart()

    def _reconcile_restart(self):
        # A new registry instance means the daemon (re)started. Jobs accepted earlier but never started have no
        # evidence of running, so they are held for coordination instead of being launched blindly.
        self.conn.execute(
            f"UPDATE {self.table} SET state = ? WHERE state = ? AND started_at IS NULL",
            (self.HELD, self.PENDING),
        )
        self.conn.commit()

    def bind(self, binding):
        """Record a newly accepted job. Return False (without changes) if the job ID is already registered."""
        try:
            self.conn.execute(
                f"INSERT INTO {self.table} (job_id, project, spider, version, fingerprint, caller, request_id, "
                "state, started_at, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding["job_id"],
                    binding["project"],
                    binding["spider"],
                    binding.get("version"),
                    binding.get("fingerprint"),
                    binding.get("caller"),
                    binding.get("request_id"),
                    self.PENDING,
                    None,
                    json.dumps(binding["response"]),
                    datetime.datetime.now().isoformat(),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, job_id):
        row = self.conn.execute(f"SELECT * FROM {self.table} WHERE job_id = ?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def get_by_request(self, caller, request_id):
        row = self.conn.execute(
            f"SELECT * FROM {self.table} WHERE caller = ? AND request_id = ?", (caller, request_id)
        ).fetchone()
        return self._decode(row) if row else None

    def mark_running(self, job_id):
        self.conn.execute(
            f"UPDATE {self.table} SET state = ?, started_at = ? WHERE job_id = ?",
            (self.RUNNING, datetime.datetime.now().isoformat(), job_id),
        )
        self.conn.commit()

    def mark_held(self, job_id):
        self.conn.execute(
            f"UPDATE {self.table} SET state = ? WHERE job_id = ? AND state = ?",
            (self.HELD, job_id, self.PENDING),
        )
        self.conn.commit()

    def is_held(self, job_id):
        binding = self.get(job_id)
        return bool(binding and binding["state"] == self.HELD)

    def mark_finished(self, job_id):
        self.conn.execute(
            f"UPDATE {self.table} SET state = ? WHERE job_id = ?",
            (self.FINISHED, job_id),
        )
        self.conn.commit()

    def release(self, job_id):
        """Remove the job's registration and return it, or None if it was never registered."""
        binding = self.get(job_id)
        if binding is None:
            return None
        self.conn.execute(f"DELETE FROM {self.table} WHERE job_id = ?", (job_id,))
        self.conn.commit()
        return binding

    def list(self, state=None):
        if state is None:
            rows = self.conn.execute(f"SELECT * FROM {self.table}").fetchall()
        else:
            rows = self.conn.execute(f"SELECT * FROM {self.table} WHERE state = ?", (state,)).fetchall()
        return [self._decode(row) for row in rows]

    def has_started(self, job_id):
        binding = self.get(job_id)
        return bool(binding and binding["started_at"])

    def _decode(self, row):
        cursor = self.conn.execute(f"SELECT * FROM {self.table} LIMIT 0")
        columns = [description[0] for description in cursor.description]
        result = dict(zip(columns, row, strict=False))
        result["response"] = json.loads(result.pop("response_json"))
        return result
