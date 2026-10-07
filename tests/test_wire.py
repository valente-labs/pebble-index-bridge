"""Wire-level intake tests against a real uvicorn process on a loopback socket.

The ASGI test client cannot send conflicting framing, repeated headers, raw
non-ASCII header bytes, or a request whose body never arrives. These tests use
plain sockets so the HTTP parser, the middleware, and the durable store are all
exercised together. The server never starts the delivery worker, so nothing in
this module makes an outbound request.
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "WireToken_0123456789_abcdefghijklmnopqrstuvwxyz"
WEBHOOK_KEY = "WireWebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz"
MAX_REQUEST_BYTES = 2048
BOUNDARY = "wire-boundary"
SOCKET_TIMEOUT = 3.0
STARTUP_DEADLINE = 20.0

# Same listener limits as the container command, on a socket the launcher binds
# itself so the ephemeral port cannot be taken between discovery and use.
LAUNCHER = """
import socket, sys, uvicorn
from app.main import create_app

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
sock.listen(32)
print(sock.getsockname()[1], flush=True)
config = uvicorn.Config(
    create_app(start_worker=False),
    access_log=False,
    limit_concurrency=32,
    timeout_keep_alive=5,
    h11_max_incomplete_event_size=16384,
    log_level="warning",
)
uvicorn.Server(config).run(sockets=[sock])
"""


def multipart(fields: list[tuple[str, str]]) -> bytes:
    parts = []
    for name, value in fields:
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    parts.append(f"--{BOUNDARY}--\r\n")
    return "".join(parts).encode("utf-8")


def good_fields(text: str = "synthetic wire check", recorded_at: str = "1790037251116"):
    return [("transcription", text), ("recordedAt", recorded_at), ("client", "ring")]


class Server:
    def __init__(self, process: subprocess.Popen[str], port: int, log: Path) -> None:
        self.process = process
        self.port = port
        self.log = log

    def connect(self) -> socket.socket:
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=SOCKET_TIMEOUT)
        conn.settimeout(SOCKET_TIMEOUT)
        return conn

    def exchange(self, raw: bytes, *, half_close: bool = False) -> tuple[int | None, bytes]:
        """Send raw bytes and return the status code and body of the reply."""
        with self.connect() as conn:
            conn.sendall(raw)
            if half_close:
                conn.shutdown(socket.SHUT_WR)
            return read_response(conn)

    def request(
        self,
        headers: list[tuple[str, str | bytes]],
        body: bytes = b"",
        *,
        method: str = "POST",
        path: str = "/index",
    ) -> tuple[int | None, bytes]:
        return self.exchange(build_request(method, path, headers, body))

    def recent(self) -> list[object]:
        status, body = self.request(
            [("Host", "127.0.0.1"), ("Authorization", f"Bearer {TOKEN}")],
            method="GET",
            path="/status",
        )
        assert status == 200, self.log.read_text()
        return json.loads(body)["recent"]

    def assert_alive_and_empty(self) -> None:
        assert self.process.poll() is None, self.log.read_text()
        assert self.recent() == []


def build_request(
    method: str, path: str, headers: list[tuple[str, str | bytes]], body: bytes = b""
) -> bytes:
    lines = [f"{method} {path} HTTP/1.1".encode("ascii")]
    for name, value in headers:
        lines.append(
            name.encode("ascii")
            + b": "
            + (value if isinstance(value, bytes) else value.encode("latin-1"))
        )
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def read_response(conn: socket.socket) -> tuple[int | None, bytes]:
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, TimeoutError, socket.timeout):
        pass
    if not data.startswith(b"HTTP/1."):
        return None, data
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def auth_headers(length: int | None = None) -> list[tuple[str, str | bytes]]:
    headers: list[tuple[str, str | bytes]] = [
        ("Host", "127.0.0.1"),
        ("Authorization", f"Bearer {TOKEN}"),
        ("Content-Type", f"multipart/form-data; boundary={BOUNDARY}"),
    ]
    if length is not None:
        headers.append(("Content-Length", str(length)))
    return headers


def chunked(payload: bytes, size: int) -> bytes:
    out = b""
    for start in range(0, len(payload), size):
        piece = payload[start : start + size]
        out += f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n"
    return out + b"0\r\n\r\n"


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[Server]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BRIDGE_", "GROKBOT_", "DB_PATH"))
    }
    env.update(
        BRIDGE_TOKEN=TOKEN,
        GROKBOT_WEBHOOK_URL="https://grok.invalid/routine",
        GROKBOT_WEBHOOK_KEY=WEBHOOK_KEY,
        DB_PATH=str(tmp_path / "wire.sqlite3"),
        ALLOWED_HOSTS="127.0.0.1,localhost",
        MAX_REQUEST_BYTES=str(MAX_REQUEST_BYTES),
        RATE_LIMIT_REQUESTS="1000",
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=log_file,
            text=True,
        )
        try:
            assert process.stdout is not None
            deadline = time.monotonic() + STARTUP_DEADLINE
            assert select.select([process.stdout], [], [], STARTUP_DEADLINE)[0], "startup timed out"
            port_line = process.stdout.readline()
            assert port_line.strip().isdigit(), log_path.read_text()
            running = Server(process, int(port_line), log_path)
            while True:
                try:
                    status, _ = running.request(
                        [("Host", "127.0.0.1")], method="GET", path="/health"
                    )
                except OSError:
                    status = None
                if status == 200:
                    break
                assert process.poll() is None and time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            yield running
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_valid_request_is_durably_accepted_and_duplicate_reuses_receipt(server: Server) -> None:
    body = multipart(good_fields())
    first_status, first_body = server.request(auth_headers(len(body)), body)
    second_status, second_body = server.request(auth_headers(len(body)), body)
    assert (first_status, second_status) == (202, 202)
    first, second = json.loads(first_body), json.loads(second_body)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["eventId"] == first["eventId"]
    assert len(server.recent()) == 1


def test_chunked_body_over_cap_is_rejected_without_content_length(server: Server) -> None:
    padding = "x" * (MAX_REQUEST_BYTES * 2)
    body = multipart(good_fields(padding))
    headers = auth_headers()
    headers.append(("Transfer-Encoding", "chunked"))
    status, _ = server.request(headers, chunked(body, 512))
    assert status == 413
    server.assert_alive_and_empty()


def test_chunked_body_within_cap_is_accepted(server: Server) -> None:
    headers = auth_headers()
    headers.append(("Transfer-Encoding", "chunked"))
    status, _ = server.request(headers, chunked(multipart(good_fields()), 64))
    assert status == 202


@pytest.mark.parametrize(
    "framing",
    [
        [("Content-Length", "BODY"), ("Transfer-Encoding", "chunked")],
        [("Content-Length", "5"), ("Content-Length", "6")],
        [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")],
    ],
    ids=["content-length-and-chunked", "two-content-lengths", "two-transfer-encodings"],
)
def test_conflicting_framing_is_rejected(server: Server, framing: list[tuple[str, str]]) -> None:
    body = multipart(good_fields())
    # The first case declares a length equal to the decoded body, so a server that
    # tolerated both headers would accept it. The payload is valid in every case,
    # which leaves the framing as the only reason for rejection.
    headers = [h for h in auth_headers() if h[0] != "Content-Length"]
    headers += [(name, str(len(body)) if value == "BODY" else value) for name, value in framing]
    status, _ = server.request(headers, chunked(body, 64))
    assert status == 400
    server.assert_alive_and_empty()


@pytest.mark.parametrize(
    "authorization",
    [
        [f"Bearer {TOKEN}", "Bearer wrong-token-value"],
        ["Bearer wrong-token-value", f"Bearer {TOKEN}"],
        [f"Bearer {TOKEN}", f"Bearer {TOKEN}"],
    ],
    ids=["valid-then-wrong", "wrong-then-valid", "valid-twice"],
)
def test_duplicated_authorization_header_is_rejected(
    server: Server, authorization: list[str]
) -> None:
    body = multipart(good_fields())
    headers = [h for h in auth_headers(len(body)) if h[0] != "Authorization"]
    headers += [("Authorization", value) for value in authorization]
    status, _ = server.request(headers, body)
    assert status == 401
    server.assert_alive_and_empty()


def test_non_ascii_authorization_bytes_are_unauthorized_not_a_server_error(
    server: Server,
) -> None:
    body = multipart(good_fields())
    headers = [h for h in auth_headers(len(body)) if h[0] != "Authorization"]
    headers.append(("Authorization", b"Bearer " + TOKEN.encode("ascii") + b"\xff\xfe"))
    status, _ = server.request(headers, body)
    assert status == 401
    server.assert_alive_and_empty()


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f"], ids=repr)
def test_control_characters_in_transcription_are_rejected(server: Server, control: str) -> None:
    body = multipart(good_fields(f"synthetic{control}text"))
    status, _ = server.request(auth_headers(len(body)), body)
    assert status == 400
    server.assert_alive_and_empty()


def test_tab_and_newline_in_transcription_are_accepted(server: Server) -> None:
    body = multipart(good_fields("synthetic\tfirst line\nsecond line"))
    status, _ = server.request(auth_headers(len(body)), body)
    assert status == 202


@pytest.mark.parametrize(
    "body",
    [
        b"this is not multipart at all",
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="transcription"\r\n\r\ntext'.encode(),
        f"--{BOUNDARY}\r\nContent-Disposition: form-data\r\n\r\ntext\r\n--{BOUNDARY}--\r\n".encode(),
        b"--other-boundary\r\n\r\n--other-boundary--\r\n",
    ],
    ids=["not-multipart", "no-closing-boundary", "no-field-name", "wrong-boundary"],
)
def test_malformed_multipart_is_a_client_error(server: Server, body: bytes) -> None:
    status, _ = server.request(auth_headers(len(body)), body)
    assert status is not None and 400 <= status < 500
    server.assert_alive_and_empty()


def test_truncated_body_followed_by_disconnect_stores_nothing(server: Server) -> None:
    body = multipart(good_fields())
    raw = build_request("POST", "/index", auth_headers(len(body) + 200), body)
    server.exchange(raw, half_close=True)
    server.assert_alive_and_empty()


def test_missing_or_wrong_credentials_are_rejected_before_the_body_is_sent(
    server: Server,
) -> None:
    # The declared body never arrives. A server that read the body before
    # checking credentials would hold this connection until its body timeout,
    # which is longer than the socket timeout used here.
    for authorization in (None, "Bearer wrong-token-value", "Basic d3Jvbmc6d3Jvbmc="):
        headers = [h for h in auth_headers(1024) if h[0] != "Authorization"]
        if authorization is not None:
            headers.append(("Authorization", authorization))
        started = time.monotonic()
        status, _ = server.request(headers)
        assert status == 401
        assert time.monotonic() - started < SOCKET_TIMEOUT


def test_oversized_declared_length_is_rejected_before_the_body_is_sent(server: Server) -> None:
    status, _ = server.request(auth_headers(MAX_REQUEST_BYTES + 1))
    assert status == 413
    server.assert_alive_and_empty()


def test_unauthenticated_oversized_declared_length_is_unauthorized(server: Server) -> None:
    headers = [h for h in auth_headers(MAX_REQUEST_BYTES + 1) if h[0] != "Authorization"]
    status, _ = server.request(headers)
    assert status == 401
