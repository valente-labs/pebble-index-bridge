from __future__ import annotations

import pytest

from app.config import ConfigError, Settings

TOKEN = "BridgeToken_0123456789_abcdefghijklmnopqrstuvwxyz"
KEY = "WebhookKey_0123456789_abcdefghijklmnopqrstuvwxyz"


def env(monkeypatch, **overrides):
    values = {
        "BRIDGE_TOKEN": TOKEN,
        "GROKBOT_WEBHOOK_URL": "https://example.invalid/routine",
        "GROKBOT_WEBHOOK_KEY": KEY,
        "DB_PATH": "/tmp/pebble-test.sqlite",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_from_env_accepts_safe_values_and_defaults(monkeypatch):
    env(monkeypatch)
    settings = Settings.from_env()
    assert settings.allowed_hosts == ("localhost", "127.0.0.1", "testserver")
    assert settings.request_timeout_seconds == 15


def test_secret_file_is_supported_but_direct_and_file_are_exclusive(tmp_path, monkeypatch):
    env(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("BRIDGE_TOKEN")
    monkeypatch.setenv("BRIDGE_TOKEN_FILE", str(token_file))
    assert Settings.from_env().bridge_token == TOKEN

    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    with pytest.raises(ConfigError, match="mutually exclusive"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name,value",
    [
        ("BRIDGE_TOKEN", "replace-with-a-long-random-secret-at-least-32-characters"),
        ("GROKBOT_WEBHOOK_KEY", "crsr_replace_with_your_key_012345678901234567890"),
        ("BRIDGE_TOKEN", "é" * 40),
        ("GROKBOT_WEBHOOK_URL", "http://example.invalid/routine"),
        ("GROKBOT_WEBHOOK_URL", "https://user:password@example.invalid/routine"),
        ("GROKBOT_WEBHOOK_URL", "https:///missing-host"),
        ("GROKBOT_WEBHOOK_URL", "https://invalid.example.invalid/replace-me"),
        ("GROKBOT_WEBHOOK_URL", "https://bad host.example/routine"),
    ],
)
def test_from_env_rejects_placeholders_bad_secrets_and_urls(monkeypatch, name, value):
    env(monkeypatch, **{name: value})
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAX_REQUEST_BYTES", "0"),
        ("MAX_PENDING", "NaN"),
        ("REQUEST_TIMEOUT_SECONDS", "nan"),
        ("REQUEST_TIMEOUT_SECONDS", "46"),
        ("RATE_LIMIT_WINDOW_SECONDS", "0"),
    ],
)
def test_from_env_rejects_zero_nan_and_unbounded_limits(monkeypatch, name, value):
    env(monkeypatch, **{name: value})
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_db_path_must_be_absolute_and_hosts_can_be_configured(monkeypatch):
    env(monkeypatch, DB_PATH="relative.sqlite", ALLOWED_HOSTS="bridge.example, localhost")
    with pytest.raises(ConfigError, match="absolute"):
        Settings.from_env()
    env(monkeypatch, ALLOWED_HOSTS="bridge.example, localhost")
    assert Settings.from_env().allowed_hosts == ("bridge.example", "localhost")


@pytest.mark.parametrize("hosts", ["*", "bad host", "https://example.invalid", "éxample.invalid"])
def test_allowed_hosts_reject_wildcards_and_non_host_values(monkeypatch, hosts):
    env(monkeypatch, ALLOWED_HOSTS=hosts)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_settings_repr_never_contains_credentials_or_private_destination():
    from app.config import Settings

    settings = Settings(
        bridge_token="private-ring-token",
        grokbot_webhook_key="private-grok-key",
        grokbot_webhook_url="https://example.invalid/private-destination",
    )
    displayed = repr(settings)
    assert "private-ring-token" not in displayed
    assert "private-grok-key" not in displayed
    assert "private-destination" not in displayed
