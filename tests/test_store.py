from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import QueueFull, Store


def test_enqueue_is_stable_across_restart_and_hides_transcription(tmp_path):
    path = tmp_path / "queue.sqlite"
    first = Store(path)
    receipt = first.enqueue("  cafe\u0301  ", "100", "ring")
    duplicate = first.enqueue("café", "100", "ring")
    assert receipt["id"] == duplicate["id"]
    assert duplicate["duplicate"] is True

    second = Store(path)
    assert second.enqueue("café", "100", "ring")["duplicate"] is True
    metadata = second.get(receipt["id"])
    assert metadata["status"] == "queued"
    assert "transcription" not in metadata
    assert "payload" not in metadata
    assert second.list_recent()[0]["id"] == receipt["id"]


def test_concurrent_duplicates_create_one_record(tmp_path):
    path = tmp_path / "queue.sqlite"

    def enqueue():
        return Store(path).enqueue("same event", "101", "ring")

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: enqueue(), range(16)))
    assert len({item["id"] for item in receipts}) == 1
    assert sum(not item["duplicate"] for item in receipts) == 1
    assert Store(path).counts()["total"] == 1


def test_claims_are_fifo_and_only_one_event_can_be_sending(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    first = store.enqueue("first", "1", "ring")
    second = store.enqueue("second", "2", "ring")
    claimed = store.claim_next()
    assert claimed["id"] == first["id"]
    assert store.claim_next() is None
    store.mark_delivered(first["id"], 202)
    claimed = store.claim_next()
    assert claimed["id"] == second["id"]


def test_concurrent_claims_are_transactionally_serialized(tmp_path):
    path = tmp_path / "queue.sqlite"
    Store(path).enqueue("one", "1", "ring")
    stores = [Store(path), Store(path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda item: item.claim_next(), stores))
    assert sum(claim is not None for claim in claims) == 1


def test_restart_recovery_preserves_ambiguous_sending_and_blocks_later(tmp_path):
    path = tmp_path / "queue.sqlite"
    store = Store(path)
    first = store.enqueue("first", "1", "ring")
    second = store.enqueue("second", "2", "ring")
    assert store.claim_next()["id"] == first["id"]

    restarted = Store(path)
    assert restarted.recover_inflight() == 1
    assert restarted.get(first["id"])["status"] == "needs_attention"
    assert restarted.claim_next() is None
    assert restarted.get(second["id"])["status"] == "queued"


def test_pending_and_record_capacity_are_bounded_without_deleting_history(tmp_path):
    path = tmp_path / "queue.sqlite"
    store = Store(path, max_pending=1, max_records=2)
    first = store.enqueue("first", "1", "ring")
    with pytest.raises(QueueFull):
        store.enqueue("second", "2", "ring")
    store.claim_next()
    store.mark_delivered(first["id"])
    second = store.enqueue("second", "2", "ring")
    assert second["duplicate"] is False
    with pytest.raises(QueueFull):
        store.enqueue("third", "3", "ring")
    assert store.counts()["total"] == 2


def test_attention_head_blocks_later_events(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    first = store.enqueue("first", "1", "ring")
    store.enqueue("second", "2", "ring")
    store.claim_next()
    store.mark_attention(first["id"], "ambiguous_timeout")
    assert store.claim_next() is None


def test_public_id_is_random_not_the_private_identity_hash(tmp_path):
    import sqlite3

    from app.store import stable_event_id

    path = tmp_path / "queue.sqlite"
    receipt = Store(path).enqueue("yes", "1790037251116", "ring")
    guessable = stable_event_id("yes", "1790037251116", "ring")
    assert receipt["id"] != guessable
    assert len(receipt["id"]) == 64 and int(receipt["id"], 16) >= 0
    assert Store(path).get(guessable) is None
    # A different store sees the same event as a distinct random receipt.
    other = Store(tmp_path / "other.sqlite").enqueue("yes", "1790037251116", "ring")
    assert other["id"] != receipt["id"]
    with sqlite3.connect(path) as db:
        identity, payload = db.execute("SELECT identity, payload FROM events").fetchone()
    assert identity == guessable
    assert guessable not in payload
    assert receipt["id"] in payload


def test_duplicate_receipt_is_stable_across_restart(tmp_path):
    path = tmp_path / "queue.sqlite"
    first = Store(path).enqueue("hello", "5", "ring")
    for _ in range(3):
        again = Store(path).enqueue("hello", "5", "ring")
        assert again["id"] == first["id"]
        assert again["duplicate"] is True
    assert Store(path).counts()["total"] == 1


def test_head_stays_visible_behind_more_than_twenty_newer_captures(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    assert store.head() is None
    blocked = store.enqueue("blocked", "1", "ring")
    store.claim_next()
    store.mark_attention(blocked["id"], "ambiguous_timeout")
    for number in range(2, 26):
        store.enqueue(f"later {number}", str(number), "ring")
    assert blocked["id"] not in {item["id"] for item in store.list_recent(20)}
    head = store.head()
    assert head["id"] == blocked["id"]
    assert head["status"] == "needs_attention"
    assert head["lastErrorCode"] == "ambiguous_timeout"
    assert "payload" not in head and "transcription" not in head


def test_head_skips_delivered_events(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    first = store.enqueue("first", "1", "ring")
    second = store.enqueue("second", "2", "ring")
    store.claim_next()
    store.mark_delivered(first["id"])
    assert store.head()["id"] == second["id"]


def test_capacity_reports_limits_usage_and_remaining_without_pruning(tmp_path):
    store = Store(tmp_path / "queue.sqlite", max_pending=3, max_records=5, max_bytes=100000)
    empty = store.capacity()
    assert empty["limits"] == {"maxPending": 3, "maxRecords": 5, "maxBytes": 100000}
    assert empty["current"] == {"pending": 0, "records": 0, "bytes": 0}
    assert empty["remaining"] == {"pending": 3, "records": 5, "bytes": 100000}
    first = store.enqueue("first", "1", "ring")
    store.enqueue("second", "2", "ring")
    store.claim_next()
    store.mark_delivered(first["id"])
    state = store.capacity()
    assert state["current"]["records"] == 2
    assert state["current"]["pending"] == 1
    assert state["current"]["bytes"] > 0
    assert state["remaining"]["pending"] == 2
    assert state["remaining"]["records"] == 3
    assert state["remaining"]["bytes"] == 100000 - state["current"]["bytes"]
    assert store.counts()["total"] == 2
