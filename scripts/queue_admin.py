"""Local operator actions. Run inside the container; no remote admin endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "/data/index-bridge.sqlite3"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("event_id")
    resolve.add_argument("--outcome", choices=["accepted", "retry"], required=True)
    resolve.add_argument("--acknowledge-duplicate-risk", action="store_true")
    args = parser.parse_args()
    path = Path(args.db)
    if not path.is_file():
        parser.error("Existing database required")
    connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    if args.action == "status":
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM events GROUP BY status"))
        rows = connection.execute(
            "SELECT event_id, status, attempts, last_error_code, http_status FROM events ORDER BY sequence DESC LIMIT 20"
        ).fetchall()
        print(json.dumps({"counts": counts, "recent": [dict(r) for r in rows]}))
    elif args.action == "backup":
        target = Path(args.destination)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as backup_connection:
            connection.backup(backup_connection)
        print("Private consistent database backup created")
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
