import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.store import Store


def call(db, *args):
    script = Path(__file__).resolve().parents[1] / "scripts/queue_admin.py"
    return subprocess.run(
        [sys.executable, str(script), "--db", str(db), *args], capture_output=True, text=True
    )


def test_reconciliation_preserves_payload_and_requires_explicit_retry_risk(tmp_path):
    db = tmp_path / "queue.sqlite"
    store = Store(db)
    receipt = store.enqueue("private speech", "1", "ring")
    store.claim_next()
    store.mark_attention(receipt["id"], "ambiguous_timeout")
    refused = call(db, "resolve", receipt["id"], "--outcome", "retry")
    assert refused.returncode != 0
    assert store.get(receipt["id"])["status"] == "needs_attention"
    accepted = call(db, "resolve", receipt["id"], "--outcome", "accepted")
    assert accepted.returncode == 0
    assert json.loads(accepted.stdout)["status"] == "delivered"
    backup = tmp_path / "backup.sqlite"
    assert call(db, "backup", str(backup)).returncode == 0
    assert backup.stat().st_mode & 0o777 == 0o600
    assert Store(backup).get(receipt["id"])["status"] == "delivered"
    assert call(db, "backup", str(backup)).returncode != 0
    assert "private speech" not in call(db, "status").stdout


def test_status_always_reports_blocked_head_and_capacity_beyond_recent_window(tmp_path):
    db = tmp_path / "queue.sqlite"
    store = Store(db, max_pending=1000)
    blocked = store.enqueue("blocked speech", "1", "ring")
    store.claim_next()
    store.mark_attention(blocked["id"], "ambiguous_timeout")
    for number in range(2, 27):
        store.enqueue(f"later {number}", str(number), "ring")
    result = call(db, "status")
    assert result.returncode == 0
    status = json.loads(result.stdout)
    assert blocked["id"] not in {row["event_id"] for row in status["recent"]}
    assert status["head"]["event_id"] == blocked["id"]
    assert status["head"]["status"] == "needs_attention"
    assert status["head"]["last_error_code"] == "ambiguous_timeout"
    assert status["capacity"]["current"]["records"] == 26
    assert status["capacity"]["current"]["pending"] == 26
    assert status["capacity"]["limits"]["maxRecords"] == 10000
    assert status["capacity"]["remaining"]["records"] == 10000 - 26
    assert "blocked speech" not in result.stdout
    assert "identity" not in result.stdout


def test_status_head_is_null_when_everything_is_delivered(tmp_path):
    db = tmp_path / "queue.sqlite"
    store = Store(db)
    receipt = store.enqueue("done", "1", "ring")
    store.claim_next()
    store.mark_delivered(receipt["id"])
    assert json.loads(call(db, "status").stdout)["head"] is None


def test_backup_restore_preserves_payload_history_and_dedup_with_private_modes(tmp_path):
    db = tmp_path / "queue.sqlite"
    store = Store(db)
    first = store.enqueue("private speech", "1", "ring")
    second = store.enqueue("second", "2", "ring")
    store.claim_next()
    store.mark_attention(first["id"], "ambiguous_timeout")
    backup = tmp_path / "backup.sqlite"
    made = call(db, "backup", str(backup))
    assert made.returncode == 0
    assert backup.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("backup.sqlite-*"))
    assert not list(tmp_path.glob(".queue-copy-*"))

    restored = tmp_path / "restored" / "queue.sqlite"
    restored.parent.mkdir()
    restore_result = call(db, "restore", str(backup), str(restored))
    assert restore_result.returncode == 0, restore_result.stderr
    assert json.loads(restore_result.stdout) == {"restored": True, "events": 2, "history": 4}
    assert restored.stat().st_mode & 0o777 == 0o600
    assert not list(restored.parent.glob("queue.sqlite-*"))
    assert not list(restored.parent.glob(".queue-copy-*"))

    def snapshot(path):
        with sqlite3.connect(path) as connection:
            return (
                connection.execute(
                    "SELECT sequence, event_id, identity, payload, status FROM events ORDER BY sequence"
                ).fetchall(),
                connection.execute(
                    "SELECT history_id, event_id, from_status, to_status, error_code "
                    "FROM event_history ORDER BY history_id"
                ).fetchall(),
            )

    assert snapshot(restored) == snapshot(db) == snapshot(backup)
    reopened = Store(restored)
    assert reopened.get(second["id"])["status"] == "queued"
    assert reopened.enqueue("private speech", "1", "ring") == {
        "id": first["id"],
        "status": "needs_attention",
        "duplicate": True,
    }
    assert reopened.head()["id"] == first["id"]


def test_backup_and_restore_never_overwrite_or_follow_existing_destinations(tmp_path):
    db = tmp_path / "queue.sqlite"
    Store(db).enqueue("keep me", "1", "ring")
    backup = tmp_path / "backup.sqlite"
    assert call(db, "backup", str(backup)).returncode == 0
    original = backup.read_bytes()

    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"precious")
    for refused in (
        call(db, "backup", str(existing)),
        call(db, "backup", str(backup)),
        call(db, "restore", str(backup), str(existing)),
        call(db, "restore", str(backup), str(db)),
        call(db, "restore", str(backup), str(backup)),
    ):
        assert refused.returncode != 0
    assert existing.read_bytes() == b"precious"
    assert backup.read_bytes() == original
    assert Store(db).counts()["total"] == 1

    dangling = tmp_path / "dangling.sqlite"
    dangling.symlink_to(tmp_path / "elsewhere.sqlite")
    assert call(db, "restore", str(backup), str(dangling)).returncode != 0
    assert not (tmp_path / "elsewhere.sqlite").exists()
    assert not list(tmp_path.glob(".queue-copy-*"))


def test_restore_refuses_missing_or_foreign_sources_and_missing_parent(tmp_path):
    db = tmp_path / "queue.sqlite"
    Store(db).enqueue("x", "1", "ring")
    assert (
        call(db, "restore", str(tmp_path / "nope.sqlite"), str(tmp_path / "new.sqlite")).returncode
        != 0
    )
    foreign = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE other (x)")
    assert call(db, "restore", str(foreign), str(tmp_path / "new.sqlite")).returncode != 0
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"not a database" * 100)
    assert call(db, "restore", str(garbage), str(tmp_path / "new.sqlite")).returncode != 0
    assert call(db, "restore", str(db), str(tmp_path / "missing" / "new.sqlite")).returncode != 0
    assert not (tmp_path / "new.sqlite").exists()
    assert not list(tmp_path.glob(".queue-copy-*"))
