from __future__ import annotations

import asyncio
import logging
import os

os.environ.setdefault("BRIDGE_TOKEN", "BridgeToken_0123456789_abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("GROKBOT_WEBHOOK_URL", "https://example.invalid/routine")
os.environ.setdefault("GROKBOT_WEBHOOK_KEY", "WebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("DB_PATH", "/tmp/pebble-test-import.sqlite")

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.store import QueueFull

TOKEN = "BridgeToken_0123456789_abcdefghijklmnopqrstuvwxyz"


class FakeStore:
    def __init__(self):
        self.events = []
        self.recoveries = 0
        self.raise_full = False

    def recover_inflight(self):
        self.recoveries += 1
        return 0

    def enqueue(self, transcription, recorded_at, client):
        if self.raise_full:
            raise QueueFull()
        event_id = "a" * 64
        duplicate = any(
            item["transcription"] == transcription and item["recordedAt"] == recorded_at
            for item in self.events
        )
        if not duplicate:
            self.events.append(
                {
                    "id": event_id,
                    "status": "queued",
                    "recordedAt": recorded_at,
                    "client": client,
                    "transcription": transcription,
                    "objective": "secret objective",
                }
            )
        return {"id": event_id, "status": "queued", "duplicate": duplicate}

    def get(self, event_id):
        return next((item for item in self.events if item["id"] == event_id), None)

    def list_recent(self, limit=20):
        return list(reversed(self.events[-limit:]))

    def counts(self):
        return {"queued": len(self.events), "total": len(self.events)}


class BrokenStore(FakeStore):
    def counts(self):
        raise OSError("database unavailable")


class DeadWorker:
    async def run(self, stop_event):
        return None

    async def close(self):
        return None


def settings(**overrides):
    values = {
        "bridge_token": TOKEN,
        "grokbot_webhook_url": "https://example.invalid/routine",
        "grokbot_webhook_key": "WebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz",
        "db_path": "/tmp/pebble-intake-test.sqlite",
        "max_request_bytes": 4096,
    }
    values.update(overrides)
    return Settings(**values)


def make_app(store=None, **overrides):
    from app.main import create_app

    return create_app(settings(**overrides), store or FakeStore(), start_worker=False)


def post(http_client, **fields):
    files = {name: (None, value) for name, value in fields.items()}
    return http_client.post("/index", headers={"Authorization": f"Bearer {TOKEN}"}, files=files)


async def raw_call(app, headers, chunks, receive_delay=0):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/index",
        "raw_path": b"/index",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        if receive_delay:
            await asyncio.sleep(receive_delay)
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


def test_intake_commits_before_202_and_duplicate_is_idempotent():
    store = FakeStore()
    with TestClient(make_app(store)) as client:
        first = post(client, transcription="hello", recordedAt="1790037251116")
        duplicate = post(client, transcription="hello", recordedAt="1790037251116")
    assert first.status_code == 202
    assert first.json() == {"ok": True, "eventId": "a" * 64, "status": "queued", "duplicate": False}
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert len(store.events) == 1
    assert store.recoveries == 1


def test_non_ascii_authorization_is_a_controlled_401():
    store = FakeStore()
    with TestClient(make_app(store)) as client:
        request = client.build_request(
            "POST",
            "/index",
            headers={
                b"authorization": b"Bearer \xff",
                b"content-type": b"multipart/form-data; boundary=x",
            },
            content=b"",
        )
        response = client.send(request)
    assert response.status_code == 401
    assert store.events == []


def test_input_errors_capacity_and_metadata_redaction():
    store = FakeStore()
    with TestClient(make_app(store)) as client:
        assert (
            client.post("/index", headers={"Authorization": f"Bearer {TOKEN}"}, json={}).status_code
            == 415
        )
        assert post(client, transcription="hello", recordedAt="not-a-number").status_code == 400
        assert (
            post(
                client, transcription="hello", recordedAt="1790037251116", client="bad client"
            ).status_code
            == 400
        )
        store.raise_full = True
        assert post(client, transcription="hello", recordedAt="1790037251116").status_code == 503

    store = FakeStore()
    with TestClient(make_app(store)) as client:
        event = post(client, transcription="private speech", recordedAt="1790037251116").json()[
            "eventId"
        ]
        auth = {"Authorization": f"Bearer {TOKEN}"}
        status = client.get(f"/status/{event}", headers=auth)
        listing = client.get("/status", headers=auth)
    assert status.status_code == 200
    assert "transcription" not in status.json()
    assert "objective" not in status.json()
    assert "secret" not in status.text
    assert listing.status_code == 200
    assert "private speech" not in listing.text


def test_duplicate_multipart_field_is_rejected():
    store = FakeStore()
    with TestClient(make_app(store)) as client:
        response = client.post(
            "/index",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files=[
                ("transcription", (None, "one")),
                ("transcription", (None, "two")),
                ("recordedAt", (None, "1790037251116")),
            ],
        )
    assert response.status_code == 400
    assert store.events == []


def test_rejection_logs_are_aggregated_and_bounded(caplog):
    store = FakeStore()
    with caplog.at_level(logging.WARNING, logger="index-bridge"):
        with TestClient(make_app(store)) as client:
            for _ in range(25):
                response = client.post("/index", headers={"Authorization": "Bearer wrong"})
                assert response.status_code == 401
    security_logs = [
        record for record in caplog.records if record.message.startswith("security_rejections")
    ]
    assert len(security_logs) == 1
    assert store.events == []


def test_health_reports_database_failure_and_dead_worker():
    with TestClient(make_app(BrokenStore())) as client:
        assert client.get("/health").status_code == 503

    from app.main import create_app

    app = create_app(settings(), FakeStore(), worker=DeadWorker(), start_worker=True)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503


def test_shutdown_grace_is_longer_than_legacy_ten_seconds(monkeypatch):
    import app.main as main_module

    timeouts = []
    original_wait_for = main_module.asyncio.wait_for

    async def record_timeout(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(main_module.asyncio, "wait_for", record_timeout)
    app = main_module.create_app(settings(), FakeStore(), worker=DeadWorker(), start_worker=True)
    with TestClient(app):
        pass
    assert timeouts and timeouts[-1] >= 30


@pytest.mark.anyio
async def test_chunked_without_content_length_is_capped_and_conflicts_are_rejected():
    app = make_app(FakeStore(), max_request_bytes=32)
    common = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (b"content-type", b"multipart/form-data; boundary=x"),
    ]
    oversized = await raw_call(
        app, common + [(b"transfer-encoding", b"chunked")], [b"x" * 16, b"x" * 17]
    )
    conflict = await raw_call(
        app, common + [(b"transfer-encoding", b"chunked"), (b"content-length", b"1")], [b"x"]
    )
    assert oversized == 413
    assert conflict == 400


@pytest.mark.anyio
async def test_slow_chunked_body_times_out_and_oversized_content_length_is_bounded(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "BODY_READ_TIMEOUT_SECONDS", 0.01)
    app = make_app(FakeStore())
    common = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (b"content-type", b"multipart/form-data; boundary=x"),
    ]
    slow = await raw_call(
        app,
        common + [(b"transfer-encoding", b"chunked")],
        [b"x"],
        receive_delay=0.05,
    )
    too_many_digits = await raw_call(
        app,
        common + [(b"content-length", b"9" * 21)],
        [],
    )
    assert slow == 408
    assert too_many_digits == 400


@pytest.mark.parametrize("recorded_at", ["1", "179003725111", "17900372511166"])
def test_recording_timestamp_requires_thirteen_digit_unix_ms(recorded_at):
    with TestClient(make_app()) as client:
        assert post(client, transcription="hello", recordedAt=recorded_at).status_code == 400
