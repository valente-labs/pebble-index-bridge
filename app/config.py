"""Configuration and validation for the Pebble Index bridge."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the bridge configuration is unsafe or incomplete."""


_PLACEHOLDER_MARKERS = (
    "replace-with",
    "replace_with",
    "your-",
    "your_",
    "example",
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "insert-",
    "insert_",
)
_INTEGER_FIELDS = {
    "max_request_bytes": (1, 16 * 1024 * 1024),
    "max_transcription_chars": (1, 8000),
    "rate_limit_requests": (1, 10000),
    "rate_limit_window_seconds": (1, 86400),
    "max_pending": (1, 100000),
    "max_records": (1, 10_000_000),
    "max_bytes": (1, 1024 * 1024 * 1024),
}
_HOSTNAME_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def _env_value(name: str, *, required: bool = True) -> str | None:
    direct = os.environ.get(name)
    file_name = f"{name}_FILE"
    file_value = os.environ.get(file_name)
    if direct is not None and file_value is not None:
        raise ConfigError(f"{name} and {file_name} are mutually exclusive")
    if file_value is not None:
        path = file_value.strip()
        if not path:
            raise ConfigError(f"{file_name} must not be empty")
        try:
            value = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"unable to read {file_name}") from exc
        value = value.strip()
    elif direct is not None:
        value = direct.strip()
    elif required:
        raise ConfigError(f"{name} is required")
    else:
        return None
    if required and not value:
        raise ConfigError(f"{name} must not be empty")
    return value


def _reject_placeholder(name: str, value: str) -> None:
    lowered = value.casefold()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise ConfigError(f"{name} contains a placeholder value")
    if lowered in {"secret", "token", "password", "crsr_...", "crsr_your_key"}:
        raise ConfigError(f"{name} contains a placeholder value")


def _secret(name: str) -> str:
    value = _env_value(name)
    assert value is not None
    _reject_placeholder(name, value)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{name} must contain ASCII characters only") from exc
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ConfigError(f"{name} must contain printable non-whitespace characters only")
    if len(value) < 32:
        raise ConfigError(f"{name} must be at least 32 characters")
    # This is deliberately only a cheap policy check.  It does not claim to
    # prove entropy, which is a property of how the secret was generated.
    if len(set(value)) < 8:
        raise ConfigError(f"{name} does not meet the minimum diversity policy")
    return value


def _https_url(name: str) -> str:
    value = _env_value(name)
    assert value is not None
    lowered = value.casefold()
    if (
        any(
            marker in lowered
            for marker in (
                "replace-with",
                "replace_with",
                "replace-me",
                "replace_me",
                "your-",
                "your_",
                "placeholder",
            )
        )
        or "invalid.example.invalid" in lowered
    ):
        raise ConfigError(f"{name} contains a placeholder value")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name} has an invalid port") from exc
    del port
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ConfigError(f"{name} must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ConfigError(f"{name} must not contain userinfo or a fragment")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{name} must be an ASCII URL") from exc
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ConfigError(f"{name} must not contain whitespace or control characters")
    _validate_host(parsed.hostname, name)
    return value


def _bounded_int(name: str, default: int) -> int:
    raw = os.environ.get(name.upper())
    if raw is None:
        value = default
    else:
        raw = raw.strip()
        if not re.fullmatch(r"[0-9]+", raw):
            raise ConfigError(f"{name.upper()} must be a bounded integer")
        try:
            value = int(raw, 10)
        except ValueError as exc:
            raise ConfigError(f"{name.upper()} must be a bounded integer") from exc
    low, high = _INTEGER_FIELDS[name]
    if value < low or value > high:
        raise ConfigError(f"{name.upper()} must be between {low} and {high}")
    return value


def _timeout() -> float:
    raw = os.environ.get("REQUEST_TIMEOUT_SECONDS", "15").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("REQUEST_TIMEOUT_SECONDS must be finite and positive") from exc
    if not math.isfinite(value) or value <= 0 or value > 45:
        raise ConfigError("REQUEST_TIMEOUT_SECONDS must be finite and positive")
    return value


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get("ALLOWED_HOSTS")
    if raw is None:
        return ("localhost", "127.0.0.1", "testserver")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not hosts:
        raise ConfigError("ALLOWED_HOSTS must contain host names")
    for host in hosts:
        _validate_host(host, "ALLOWED_HOSTS")
    return hosts


def _validate_host(host: str, name: str) -> None:
    if host == "*" or not host or host.endswith("."):
        raise ConfigError(f"{name} must contain valid host names")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{name} must contain ASCII host names") from exc
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
        return
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(host):
        raise ConfigError(f"{name} must contain valid host names")


@dataclass(frozen=True, slots=True)
class Settings:
    bridge_token: str
    grokbot_webhook_url: str
    grokbot_webhook_key: str
    request_timeout_seconds: float = 15.0
    max_request_bytes: int = 65536
    max_transcription_chars: int = 8000
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60
    db_path: str = "/tmp/pebble-index-bridge.sqlite3"
    max_pending: int = 1000
    max_records: int = 10000
    max_bytes: int = 64 * 1024 * 1024
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = os.environ.get("DB_PATH")
        if not db_path:
            raise ConfigError("DB_PATH is required")
        db = Path(db_path.strip())
        if not db.is_absolute():
            raise ConfigError("DB_PATH must be absolute")
        if db.exists() and db.is_dir():
            raise ConfigError("DB_PATH must name a private file")
        if db.name in {"", ".", ".."}:
            raise ConfigError("DB_PATH must name a private file")

        return cls(
            bridge_token=_secret("BRIDGE_TOKEN"),
            grokbot_webhook_url=_https_url("GROKBOT_WEBHOOK_URL"),
            grokbot_webhook_key=_secret("GROKBOT_WEBHOOK_KEY"),
            request_timeout_seconds=_timeout(),
            max_request_bytes=_bounded_int("max_request_bytes", 65536),
            max_transcription_chars=_bounded_int("max_transcription_chars", 8000),
            rate_limit_requests=_bounded_int("rate_limit_requests", 10),
            rate_limit_window_seconds=_bounded_int("rate_limit_window_seconds", 60),
            db_path=str(db),
            max_pending=_bounded_int("max_pending", 1000),
            max_records=_bounded_int("max_records", 10000),
            max_bytes=_bounded_int("max_bytes", 64 * 1024 * 1024),
            allowed_hosts=_allowed_hosts(),
        )
