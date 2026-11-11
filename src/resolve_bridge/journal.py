"""Durable at-most-once mutation attempts, including failed verification.

Persist intent before calling Resolve. A crashed/failed attempt is never retried
automatically: the API may have changed Resolve before the error was reported.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .errors import IdempotencyConflictError, InvalidOperationError, OperationUncertainError


class OperationJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL, operation_type TEXT NOT NULL,
                parameters TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT
            )""")

    def execute(self, kind: str, parameters: dict, idempotency_key: str, action) -> dict:
        # A separate SQLite database provides a process-wide write lock without
        # holding the journal transaction open across the native API call. Intent
        # remains durable if the process crashes; the OS releases this lock.
        lock = sqlite3.connect(self.path.with_suffix(".lock.sqlite3"), timeout=0)
        try:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as exc:
                raise OperationUncertainError("Another bridge mutation is in progress. Read fresh state after it finishes.") from exc
            return self._execute(kind, parameters, idempotency_key, action)
        finally:
            lock.close()

    def _execute(self, kind: str, parameters: dict, idempotency_key: str, action) -> dict:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise InvalidOperationError("Every mutation needs an idempotency key of 1–256 characters.")
        encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256((kind + "\n" + encoded).encode()).hexdigest()
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT operation_id, fingerprint, status, result FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                operation_id, prior_fingerprint, status, result = row
                if fingerprint != prior_fingerprint:
                    raise IdempotencyConflictError("Idempotency key was already used for different parameters.")
                if status != "verified":
                    raise OperationUncertainError(f"Operation {operation_id} is {status}; inspect Resolve before any new attempt.")
                return {"operation_id": operation_id, "status": status, "replayed": True, "result": json.loads(result)}
            operation_id = str(uuid.uuid4())
            db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, NULL)",
                       (operation_id, idempotency_key, datetime.now(timezone.utc).isoformat(), kind, encoded, fingerprint))
        try:
            result = action(operation_id)
            encoded_result = json.dumps(result, sort_keys=True, allow_nan=False)
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE operations SET status='verified', result=? WHERE operation_id=?", (encoded_result, operation_id))
        except Exception as exc:
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE operations SET status='failed', error=? WHERE operation_id=?", (f"{type(exc).__name__}: {exc}", operation_id))
            raise
        return {"operation_id": operation_id, "status": "verified", "replayed": False, "result": result}

    def recent(self, limit: int = 20) -> list[dict]:
        """Newest attempts first, without parameters/results, for inspection."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidOperationError("Journal listing limit must be an integer between 1 and 200.")
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT operation_id, idempotency_key, timestamp, operation_type, status, error "
                              "FROM operations ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, operation_id: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("parameters", "result"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result
