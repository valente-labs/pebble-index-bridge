import json
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
