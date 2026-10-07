"""Durable, append-only SQLite storage for Pebble intake events.

The store deliberately keeps the original payload after delivery.  A downstream
HTTP response says that the webhook accepted the event, but it does not prove
that the receiving routine completed the work.  Keeping the payload and state
locally also makes ambiguous delivery visible to an operator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

STATUSES = ("queued", "sending", "retry", "delivered", "needs_attention")
PENDING_STATUSES = ("queued", "sending", "retry", "needs_attention")
SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class QueueFull(RuntimeError):
    """The configured durable queue capacity has been reached."""

    status_code = 503


def _normalise(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} contains a NUL character")
    return value


def _safe_error_code(value: str) -> str:
    """Keep operator-visible state to a short non-sensitive code."""

    code = _normalise(value, "error_code")
    return code if SAFE_ERROR_CODE.fullmatch(code) else "operator_error"


def stable_event_id(transcription: str, recorded_at: str, client: str) -> str:
    """Return the private duplicate-suppression key for one normalized event.

    This is an unsalted hash of the speech, so it must stay inside the private
    SQLite file (``events.identity``).  It is never a public receipt or payload
    identifier because short or predictable speech could be guessed from it.
    """

    text = _normalise(transcription, "transcription")
    recorded = _normalise(recorded_at, "recorded_at")
    source_client = _normalise(client, "client")
    material = "\0".join((text, recorded, source_client)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class Store:
    """A small append-only queue backed by one private SQLite database."""

    _initialise_lock = threading.Lock()

    def __init__(
        self,
        path: str | Path,
        max_pending: int = 1000,
        max_records: int = 10000,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("path must be a string or Path")
        if max_pending < 1 or max_records < 1 or max_bytes < 1:
            raise ValueError("store limits must be positive")
        self.path = Path(path)
        self.max_pending = int(max_pending)
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent_created:
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        self._initialise()

    def _connect(self, *, configure_journal: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        if configure_journal:
            for attempt in range(8):
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or attempt == 7:
                        connection.close()
                        raise
                    time.sleep(0.05 * (attempt + 1))
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialise(self) -> None:
        # PRAGMA journal_mode changes the database header and cannot be safely
        # issued by a swarm of concurrent constructors.  Serialize setup in
        # this process; SQLite's busy timeout handles other processes.
        with self._initialise_lock:
            with closing(self._connect(configure_journal=True)) as connection, connection:
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    identity TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    client TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'queued', 'sending', 'retry', 'delivered', 'needs_attention'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    next_attempt_at INTEGER,
                    last_error_code TEXT,
                    http_status INTEGER
                );
                CREATE TABLE IF NOT EXISTS event_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    recorded_at INTEGER NOT NULL,
                    error_code TEXT,
                    http_status INTEGER,
                    FOREIGN KEY (event_id) REFERENCES events(event_id)
                );
                CREATE INDEX IF NOT EXISTS events_status_sequence
                    ON events(status, sequence);
                CREATE INDEX IF NOT EXISTS history_event_sequence
                    ON event_history(event_id, history_id);
                    """
                )
        self._private_files()

    def _private_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.chmod(str(self.path) + suffix, 0o600)
            except FileNotFoundError:
                pass
            except OSError:
                # A restrictive umask or an existing read-only volume can make
                # chmod unavailable.  SQLite still enforces the database mode.
                pass

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _payload(text: str, recorded_at: str, client: str, event_id: str) -> dict[str, Any]:
        return {
            "source": "pebble-index-01",
            "transcription": text,
            "recordedAt": int(recorded_at),
            "client": client,
            "bridgeRequestId": event_id,
        }

    @staticmethod
    def _receipt(event_id: str, status: str, duplicate: bool) -> dict[str, Any]:
        return {"id": event_id, "status": status, "duplicate": duplicate}

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        # This is intentionally assembled from columns rather than returning
        # the stored JSON.  Read/status endpoints must never expose speech or
        # credentials.
        return {
            "id": row["event_id"],
            "status": row["status"],
            "recordedAt": row["recorded_at"],
            "client": row["client"],
            "attempts": row["attempts"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "nextAttemptAt": row["next_attempt_at"],
            "lastErrorCode": row["last_error_code"],
            "httpStatus": row["http_status"],
        }

    @staticmethod
    def _append_history(
        connection: sqlite3.Connection,
        event_id: str,
        from_status: str | None,
        to_status: str,
        recorded_at: int,
        error_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO event_history
                (event_id, from_status, to_status, recorded_at, error_code, http_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, from_status, to_status, recorded_at, error_code, http_status),
        )

    def enqueue(self, transcription: str, recorded_at: str, client: str) -> dict[str, Any]:
        text = _normalise(transcription, "transcription")
        recorded = _normalise(recorded_at, "recorded_at")
        source_client = _normalise(client, "client")
        identity = stable_event_id(text, recorded, source_client)
        # The public ID is random and only meaningful once the row commits; a
        # duplicate discards it and returns the receipt stored on first intake.
        event_id = secrets.token_hex(32)
        payload = self._payload(text, recorded, source_client, event_id)
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > self.max_bytes:
            raise QueueFull("queue byte capacity reached")

        now = self._now()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id, status FROM events WHERE identity = ?", (identity,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._receipt(existing["event_id"], existing["status"], True)

            record_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE status IN ('queued', 'sending', 'retry', 'needs_attention')"
            ).fetchone()[0]
            byte_count = connection.execute(
                "SELECT COALESCE(SUM(payload_bytes), 0) FROM events"
            ).fetchone()[0]
            if record_count >= self.max_records:
                raise QueueFull("queue record capacity reached")
            if pending_count >= self.max_pending:
                raise QueueFull("queue pending capacity reached")
            if byte_count + payload_bytes > self.max_bytes:
                raise QueueFull("queue byte capacity reached")

            connection.execute(
                """
                INSERT INTO events (
                    event_id, identity, payload, payload_bytes, recorded_at, client,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                """,
                (
                    event_id,
                    identity,
                    payload_json,
                    payload_bytes,
                    recorded,
                    source_client,
                    now,
                    now,
                ),
            )
            self._append_history(connection, event_id, None, "queued", now)
            connection.commit()
        self._private_files()
        return self._receipt(event_id, "queued", False)

    def get(self, event_id: str) -> dict[str, Any] | None:
        if not isinstance(event_id, str):
            return None
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return None if row is None else self._metadata(row)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            bounded_limit = 20
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (bounded_limit,)
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def head(self) -> dict[str, Any] | None:
        """Return metadata for the oldest undelivered event, if any.

        This is the event ``claim_next`` considers first, so a blocked
        ``needs_attention`` head stays visible however many newer captures
        arrive behind it.
        """

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM events WHERE status <> 'delivered' ORDER BY sequence LIMIT 1"
            ).fetchone()
        return None if row is None else self._metadata(row)

    def capacity(self) -> dict[str, Any]:
        """Return configured limits, current usage and remaining headroom.

        The limits are deliberate backpressure: nothing is pruned or rotated
        automatically, and intake answers 503 once any limit is reached.
        """

        with closing(self._connect()) as connection, connection:
            records, pending, used_bytes = connection.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(status IN
                           ('queued', 'sending', 'retry', 'needs_attention')), 0),
                       COALESCE(SUM(payload_bytes), 0)
                FROM events
                """
            ).fetchone()
        limits = {
            "maxPending": self.max_pending,
            "maxRecords": self.max_records,
            "maxBytes": self.max_bytes,
        }
        current = {"pending": pending, "records": records, "bytes": used_bytes}
        remaining = {
            "pending": max(0, self.max_pending - pending),
            "records": max(0, self.max_records - records),
            "bytes": max(0, self.max_bytes - used_bytes),
        }
        return {"limits": limits, "current": current, "remaining": remaining}

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({row["status"]: row["count"] for row in rows})
        counts["total"] = sum(counts[status] for status in STATUSES)
        counts["pending"] = sum(counts[status] for status in PENDING_STATUSES)
        return counts

    def claim_next(self) -> dict[str, Any] | None:
        now = self._now()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # One owner at a time keeps downstream ordering deterministic and
            # prevents two workers from sending the same queue head.
            if connection.execute(
                "SELECT 1 FROM events WHERE status = 'sending' LIMIT 1"
            ).fetchone():
                connection.commit()
                return None
            head = connection.execute(
                "SELECT * FROM events WHERE status <> 'delivered' ORDER BY sequence LIMIT 1"
            ).fetchone()
            if head is None or head["status"] == "needs_attention":
                connection.commit()
                return None
            if (
                head["status"] == "retry"
                and head["next_attempt_at"] is not None
                and head["next_attempt_at"] > now
            ):
                connection.commit()
                return None
            if head["status"] not in ("queued", "retry"):
                connection.commit()
                return None

            attempts = head["attempts"] + 1
            connection.execute(
                """
                UPDATE events
                SET status = 'sending', attempts = ?, updated_at = ?, next_attempt_at = NULL
                WHERE event_id = ? AND status IN ('queued', 'retry')
                """,
                (attempts, now, head["event_id"]),
            )
            self._append_history(connection, head["event_id"], head["status"], "sending", now)
            connection.commit()
            payload = json.loads(head["payload"])
        self._private_files()
        return {"id": head["event_id"], "payload": payload, "attempts": attempts}

    def _transition(
        self,
        event_id: str,
        to_status: str,
        *,
        error_code: str | None = None,
        next_attempt_at: int | None = None,
        http_status: int | None = None,
    ) -> None:
        if to_status not in STATUSES:
            raise ValueError("unknown event status")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(event_id)
            from_status = row["status"]
            if from_status != "sending":
                connection.commit()
                return
            now = self._now()
            connection.execute(
                """
                UPDATE events
                SET status = ?, updated_at = ?, next_attempt_at = ?,
                    last_error_code = ?, http_status = ?
                WHERE event_id = ? AND status = 'sending'
                """,
                (to_status, now, next_attempt_at, error_code, http_status, event_id),
            )
            self._append_history(
                connection, event_id, from_status, to_status, now, error_code, http_status
            )
            connection.commit()
        self._private_files()

    def mark_delivered(self, event_id: str, http_status: int = 200) -> None:
        if not isinstance(http_status, int) or not 200 <= http_status <= 599:
            raise ValueError("invalid HTTP status")
        self._transition(event_id, "delivered", http_status=http_status)

    def mark_retry(self, event_id: str, error_code: str, next_attempt_at: int | float) -> None:
        safe_error = _safe_error_code(error_code)
        try:
            next_at = int(next_attempt_at)
        except (TypeError, ValueError):
            raise ValueError("next_attempt_at must be a Unix timestamp") from None
        self._transition(event_id, "retry", error_code=safe_error, next_attempt_at=next_at)

    def mark_attention(
        self, event_id: str, error_code: str, http_status: int | None = None
    ) -> None:
        safe_error = _safe_error_code(error_code)
        if http_status is not None and (
            not isinstance(http_status, int) or not 100 <= http_status <= 599
        ):
            raise ValueError("invalid HTTP status")
        self._transition(
            event_id, "needs_attention", error_code=safe_error, http_status=http_status
        )

    def recover_inflight(self) -> int:
        now = self._now()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_id FROM events WHERE status = 'sending'"
            ).fetchall()
            for row in rows:
                event_id = row["event_id"]
                connection.execute(
                    """
                    UPDATE events
                    SET status = 'needs_attention', updated_at = ?,
                        next_attempt_at = NULL, last_error_code = ?, http_status = NULL
                    WHERE event_id = ? AND status = 'sending'
                    """,
                    (now, "process_restart", event_id),
                )
                self._append_history(
                    connection, event_id, "sending", "needs_attention", now, "process_restart"
                )
            connection.commit()
        self._private_files()
        return len(rows)

    def close(self) -> None:
        """Compatibility hook for application lifespan shutdown.

        Connections are scoped to individual operations, so there is no open
        connection to drain here.
        """

        return None
