import json
import sys

import pytest

from scripts import deploy


def setup(monkeypatch, tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deploy.py",
            "apply",
            "--ssh-host",
            "example-host",
            "--remote-root",
            "/srv/index-bridge",
            "--config",
            str(path),
            "--serve-port",
            "8449",
        ],
    )


def config():
    return {
        "ALLOWED_HOSTS": "bridge.example.invalid,localhost,127.0.0.1",
        "INDEX_BRIDGE_SUBNET": "172.16.242.0/29",
        "INDEX_BRIDGE_ADDRESS": "172.16.242.2",
    }


def test_failed_recreation_restores_prior_release(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    calls = []

    def fake_run(args, data=None):
        calls.append(args)
        if args[0] == "git":
            if "rev-parse" in args:
                return b"a" * 40
            return b""
        command = args[-1]
        if command == "tailscale serve status --json":
            return b"{}"
        if "current-release" in command and command.startswith("test"):
            return b"/srv/index-bridge/releases/previous"
        if " up -d --wait " in command and "/previous/" not in command:
            raise RuntimeError("simulated unhealthy new release")
        return b""

    monkeypatch.setattr(deploy, "run", fake_run)
    with pytest.raises(RuntimeError, match="simulated unhealthy"):
        deploy.main()
    assert any("/previous/" in args[-1] and " up -d --wait " in args[-1] for args in calls)
    assert not any("tailscale serve --bg" in args[-1] for args in calls)


@pytest.mark.parametrize("bad_value", ["value$SECRET", "host\nINJECT=1", "host#comment"])
def test_dotenv_injection_fails_before_remote_mutation(monkeypatch, tmp_path, bad_value):
    values = config()
    values["ALLOWED_HOSTS"] = bad_value
    setup(monkeypatch, tmp_path, values)
    calls = []
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit):
        deploy.main()
    assert calls == []
