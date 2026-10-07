from __future__ import annotations

import asyncio
import json
import time

import httpx

from app.delivery import DeliveryWorker
from app.store import Store


def run(coro):
    return asyncio.run(coro)


def test_200_marks_webhook_accepted_and_reuses_client(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, content=b"an arbitrarily large body is never inspected")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = Store(tmp_path / "queue.sqlite")
    receipt = store.enqueue("hello", "1", "ring")
    worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
    assert run(worker.run_once()) is True
    assert store.get(receipt["id"])["status"] == "delivered"
    assert seen[0][0] == "Bearer secret"
    assert seen[0][1]["bridgeRequestId"] == receipt["id"]
    run(worker.close())
    assert client.is_closed is False
    run(client.aclose())


def test_non_2xx_is_attention_without_reading_response_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"private downstream details")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = Store(tmp_path / "queue.sqlite")
    receipt = store.enqueue("hello", "1", "ring")
    worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
    assert run(worker.run_once()) is True
    metadata = store.get(receipt["id"])
    assert metadata["status"] == "needs_attention"
    assert metadata["httpStatus"] == 500
    assert metadata["lastErrorCode"] == "http_500"
    run(client.aclose())


def test_connect_failure_retries_but_read_timeout_needs_attention(tmp_path):
    def connect_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    path = tmp_path / "queue.sqlite"
    store = Store(path)
    receipt = store.enqueue("connect", "1", "ring")
    client = httpx.AsyncClient(transport=httpx.MockTransport(connect_failure))
    worker = DeliveryWorker(
        store, "https://example.invalid/hook", "secret", retry_base=2, client=client
    )
    assert run(worker.run_once()) is True
    retry = store.get(receipt["id"])
    assert retry["status"] == "retry"
    assert retry["nextAttemptAt"] >= int(time.time()) + 1
    run(client.aclose())

    def accepted_then_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out", request=request)

    store = Store(path)
    # The first event is still the retry head, so use a fresh database for the
    # ambiguity check and prove it is never automatically resent.
    ambiguous_store = Store(tmp_path / "ambiguous.sqlite")
    ambiguous = ambiguous_store.enqueue("accepted", "2", "ring")
    timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(accepted_then_timeout))
    timeout_worker = DeliveryWorker(
        ambiguous_store, "https://example.invalid/hook", "secret", client=timeout_client
    )
    assert run(timeout_worker.run_once()) is True
    assert ambiguous_store.get(ambiguous["id"])["status"] == "needs_attention"
    assert run(timeout_worker.run_once()) is False
    run(timeout_client.aclose())


def test_fifo_delivery_order(tmp_path):
    delivered = []

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content)["transcription"])
        return httpx.Response(200)

    store = Store(tmp_path / "queue.sqlite")
    store.enqueue("first", "1", "ring")
    store.enqueue("second", "2", "ring")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
    assert run(worker.run_once()) is True
    assert run(worker.run_once()) is True
    assert delivered == ["first", "second"]
    run(client.aclose())


def test_cancellation_settles_inflight_event_as_attention(tmp_path):
    async def exercise():
        class HangingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        store = Store(tmp_path / "queue.sqlite")
        receipt = store.enqueue("cancel me", "1", "ring")
        client = httpx.AsyncClient(transport=HangingTransport())
        worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
        task = asyncio.create_task(worker.run_once())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert store.get(receipt["id"])["status"] == "needs_attention"
        await client.aclose()

    run(exercise())


def test_grok_other_2xx_is_not_documented_acceptance(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    receipt = store.enqueue("hello", "1", "ring")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202)))
    worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
    assert run(worker.run_once()) is True
    assert store.get(receipt["id"])["status"] == "needs_attention"
    assert store.get(receipt["id"])["httpStatus"] == 202
    run(client.aclose())


def test_numeric_timestamp_and_sanitized_unexpected_error(tmp_path, capsys):
    def unexpected(request):
        payload = json.loads(request.content)
        assert isinstance(payload["recordedAt"], int)
        raise RuntimeError("private webhook URL and key must not escape")

    store = Store(tmp_path / "queue.sqlite")
    receipt = store.enqueue("hello", "1790037251116", "ring")
    client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
    worker = DeliveryWorker(store, "https://example.invalid/hook", "secret", client=client)
    assert run(worker.run_once()) is True
    assert store.get(receipt["id"])["lastErrorCode"] == "delivery_error"
    assert "private webhook" not in capsys.readouterr().err
    run(client.aclose())
