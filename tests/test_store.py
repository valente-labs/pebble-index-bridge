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
