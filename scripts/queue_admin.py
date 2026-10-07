"""Local operator actions. Run inside the container; no remote admin endpoint."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

SIDECARS = ("-wal", "-shm", "-journal")
REQUIRED_TABLES = ("events", "event_history")
# Same names and defaults as app.config; the script stays standalone so it runs
# as `python scripts/queue_admin.py` without the app package on the path.
CAPACITY_ENV = (
    ("maxPending", "MAX_PENDING", 1000),
    ("maxRecords", "MAX_RECORDS", 10000),
    ("maxBytes", "MAX_BYTES", 64 * 1024 * 1024),
)
EVENT_COLUMNS = (
    "event_id, status, attempts, last_error_code, http_status, "
    "recorded_at, client, created_at, updated_at, next_attempt_at"
)


def _capacity(connection: sqlite3.Connection) -> dict:
    limits: dict[str, int | None] = {}
    for key, name, default in CAPACITY_ENV:
        raw = os.environ.get(name)
        try:
            limits[key] = default if raw is None else int(raw)
        except ValueError:
            limits[key] = None
    records, pending, used = connection.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(status IN ('queued','sending','retry','needs_attention')), 0),"
        " COALESCE(SUM(payload_bytes), 0) FROM events"
    ).fetchone()
    current = {"pending": pending, "records": records, "bytes": used}
    remaining = {
        short: None if limits[key] is None else max(0, limits[key] - current[short])
        for key, short in (
            ("maxPending", "pending"),
            ("maxRecords", "records"),
            ("maxBytes", "bytes"),
        )
    }
    return {"limits": limits, "current": current, "remaining": remaining}


def _verify(connection: sqlite3.Connection) -> dict:
    """Check integrity and the bridge schema; return row counts."""

    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("database failed its integrity check")
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise ValueError("not a bridge queue database")
    return {
        "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "history": connection.execute("SELECT COUNT(*) FROM event_history").fetchone()[0],
    }


def _copy_to_new_path(source: sqlite3.Connection, destination: Path) -> dict:
    """Write a consistent private copy of ``source`` to a path that must not exist.

    The copy is built in a 0600 temporary file next to the destination, verified,
    and then hard-linked into place, so an existing file (or sidecar, or
    dangling symlink) is never overwritten and a half-written copy never appears
    at the destination name.
    """

    parent = destination.parent
    if not parent.is_dir():
        raise ValueError("destination directory must already exist")
    for suffix in ("", *SIDECARS):
        if os.path.lexists(str(destination) + suffix):
            raise ValueError("destination already exists; choose a new path")
    fd, temp_name = tempfile.mkstemp(prefix=".queue-copy-", suffix=".tmp", dir=parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with contextlib.closing(sqlite3.connect(temp)) as copy:
            source.backup(copy)
            # The source is WAL; keep the copy self-contained in one file.
            copy.execute("PRAGMA journal_mode = DELETE")
            counts = _verify(copy)
        for suffix in SIDECARS:
            Path(temp_name + suffix).unlink(missing_ok=True)
        os.chmod(temp, 0o600)
        fd = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temp, destination)
        except FileExistsError:
            raise ValueError("destination already exists; choose a new path") from None
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "/data/index-bridge.sqlite3"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    backup = sub.add_parser("backup")
    backup.add_argument("destination", help="new path; an existing file is never overwritten")
    restore = sub.add_parser(
        "restore", help="copy a backup to a NEW database path; never overwrites"
    )
    restore.add_argument("source", help="backup file to read (left unchanged)")
    restore.add_argument("destination", help="new database path; must not exist")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("event_id")
    resolve.add_argument("--outcome", choices=["accepted", "retry"], required=True)
    resolve.add_argument("--acknowledge-duplicate-risk", action="store_true")
    args = parser.parse_args()
    if args.action == "restore":
        source_path = Path(args.source)
        if not source_path.is_file():
            parser.error("Existing backup file required")
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
            ) as source:
                counts = _copy_to_new_path(source, Path(args.destination))
        except (ValueError, sqlite3.Error) as exc:
            parser.error(f"restore refused: {exc}")
        print(json.dumps({"restored": True, **counts}))
        return
    path = Path(args.db)
    if not path.is_file():
        parser.error("Existing database required")
    connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    if args.action == "status":
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM events GROUP BY status"))
        # The head is the oldest undelivered event, i.e. what blocks the queue.
        # It is reported separately because it may be older than the recent list.
        head = connection.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE status <> 'delivered' "
            "ORDER BY sequence LIMIT 1"
        ).fetchone()
        rows = connection.execute(
            "SELECT event_id, status, attempts, last_error_code, http_status FROM events ORDER BY sequence DESC LIMIT 20"
        ).fetchall()
        print(
            json.dumps(
                {
                    "counts": counts,
                    "head": None if head is None else dict(head),
                    "capacity": _capacity(connection),
                    "recent": [dict(r) for r in rows],
                }
            )
        )
    elif args.action == "backup":
        try:
            counts = _copy_to_new_path(connection, Path(args.destination))
        except (ValueError, sqlite3.Error) as exc:
            parser.error(f"backup refused: {exc}")
        print(json.dumps({"backup": "Private consistent database backup created", **counts}))
    else:
        if args.outcome == "retry" and not args.acknowledge_duplicate_risk:
            parser.error("Retry may repeat an accepted task; --acknowledge-duplicate-risk required")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM events WHERE event_id=?", (args.event_id,)
        ).fetchone()
        if row is None or row["status"] != "needs_attention":
            parser.error("Only an existing needs_attention event can be reconciled")
        outcome = "delivered" if args.outcome == "accepted" else "queued"
        now = int(time.time())
        code = (
            "operator_confirmed_acceptance"
            if outcome == "delivered"
            else "operator_authorized_retry"
        )
        connection.execute(
            "UPDATE events SET status=?, updated_at=?, next_attempt_at=NULL, last_error_code=? WHERE event_id=? AND status=?",
            (outcome, now, code, args.event_id, "needs_attention"),
        )
        connection.execute(
            "INSERT INTO event_history(event_id,from_status,to_status,recorded_at,error_code) VALUES(?,?,?,?,?)",
            (args.event_id, "needs_attention", outcome, now, code),
        )
        connection.commit()
        print(json.dumps({"id": args.event_id, "status": outcome}))
    connection.close()


if __name__ == "__main__":
    main()
