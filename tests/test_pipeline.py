"""Cross-module delivery tests with a real persistent store and HTTP intake."""

import asyncio
import json
import os

import httpx
from fastapi.testclient import TestClient

os.environ.setdefault("BRIDGE_TOKEN", "BridgeToken_0123456789_abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("GROKBOT_WEBHOOK_URL", "https://example.invalid/routine")
os.environ.setdefault("GROKBOT_WEBHOOK_KEY", "WebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz")
os.environ.setdefault("DB_PATH", "/tmp/pebble-test-import.sqlite")

from app.config import Settings
from app.delivery import DeliveryWorker
from app.main import create_app
from app.store import Store, stable_event_id

TOKEN = "BridgeToken_0123456789_abcdefghijklmnopqrstuvwxyz"


def test_capture_delivery_restart_and_capacity_share_one_persistent_receipt(tmp_path):
    path = tmp_path / "queue.sqlite"
    settings = Settings(
        bridge_token=TOKEN,
        grokbot_webhook_url="https://example.invalid/routine",
        grokbot_webhook_key="WebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz",
        db_path=str(path),
    )
    store = Store(path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    files = {
        "transcription": (None, "Synthetic pipeline note"),
        "recordedAt": (None, "1790037251116"),
        "client": (None, "ring"),
    }
    with TestClient(create_app(settings, store, start_worker=False)) as client:
        first = client.post("/index", headers=headers, files=files)
        assert first.status_code == 202
        event_id = first.json()["eventId"]
        assert event_id != stable_event_id("Synthetic pipeline note", "1790037251116", "ring")
        status = client.get("/status", headers=headers).json()
        assert status["head"]["id"] == event_id
        assert status["capacity"]["current"]["records"] == 1
        assert "Synthetic pipeline note" not in json.dumps(status)

    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == f"Bearer {settings.grokbot_webhook_key}"
        return httpx.Response(200)

    async def deliver():
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as downstream:
            worker = DeliveryWorker(
                store, settings.grokbot_webhook_url, settings.grokbot_webhook_key, client=downstream
            )
            assert await worker.run_once()
            assert not await worker.run_once()

    asyncio.run(deliver())
    assert requests == [
        {
            "source": "pebble-index-01",
            "transcription": "Synthetic pipeline note",
            "recordedAt": 1790037251116,
            "client": "ring",
            "bridgeRequestId": event_id,
        }
    ]
    store.close()
    reopened = Store(path)
    with TestClient(create_app(settings, reopened, start_worker=False)) as client:
        duplicate = client.post("/index", headers=headers, files=files)
        assert duplicate.status_code == 202
        assert duplicate.json() == {
            "ok": True,
            "eventId": event_id,
            "status": "delivered",
            "duplicate": True,
        }
        status = client.get("/status", headers=headers).json()
        assert status["head"] is None
        assert status["capacity"]["current"]["records"] == 1
        assert status["counts"]["delivered"] == 1
    assert len(requests) == 1
    reopened.close()
