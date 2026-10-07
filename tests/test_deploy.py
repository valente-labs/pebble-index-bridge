import json
import sys
from datetime import UTC, datetime

import pytest

from scripts import deploy

ROOT = "/srv/index-bridge"
FQDN = "tower.example-tailnet.ts.net"
PRIOR = ROOT + "/releases/previous"
MUTATING = (
    "mkdir",
    "tar -xf",
    "docker build",
    " up -d",
    " stop",
    "serve --bg",
    "serve --https",
    "chown",
    "printf",
    "docker exec",
    "docker run",
    "rm -",
)


def test_snapshot_collision_never_removes_an_existing_archive():
    commands = []

    def ssh(command, data=None):
        commands.append(command)
        if command.startswith("docker ps"):
            return b"container-id"
        if "set -C" in command:
            raise RuntimeError("existing archive")
        return b""

    with pytest.raises(RuntimeError, match="existing archive"):
        deploy.snapshot(ssh, ROOT, "image", datetime(2026, 1, 1, tzinfo=UTC))
    cleanup = [command for command in commands if command.startswith("rm -f")]
    assert cleanup == []


def test_failed_backup_does_not_remove_refused_existing_destination():
    commands = []

    def ssh(command, data=None):
        commands.append(command)
        if command.startswith("docker ps"):
            return b"container-id"
        if "queue_admin.py backup" in command:
            raise RuntimeError("destination already exists")
        return b""

    with pytest.raises(RuntimeError, match="destination already exists"):
        deploy.snapshot(ssh, ROOT, "image", datetime(2026, 1, 1, tzinfo=UTC))
    assert not any(command.startswith("rm ") for command in commands)


def setup(monkeypatch, tmp_path, cfg, action="apply", port="8449"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    argv = [
        "deploy.py",
        action,
        "--ssh-host",
        "example-host",
        "--remote-root",
        ROOT,
        "--config",
        str(path),
    ]
    if port:
        argv += ["--serve-port", port]
    monkeypatch.setattr(sys, "argv", argv)


def config():
    return {
        "ALLOWED_HOSTS": "bridge.example.invalid,localhost,127.0.0.1",
        "INDEX_BRIDGE_SUBNET": "172.16.242.0/29",
        "INDEX_BRIDGE_ADDRESS": "172.16.242.2",
    }


def serve_state(proxy=None, other=True):
    web = {"other.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}
    if not other:
        web = {}
    if proxy:
        web[f"{FQDN}:8449"] = {"Handlers": {"/": {"Proxy": proxy}}}
    return {"Web": web}


class Tower:
    """Stateful stand-in for the SSH host. Nothing here touches a real machine."""

    def __init__(self, **kw):
        self.calls = []
        self.stdin = {}
        self.secrets = kw.get("secrets", {})
        self.prior = kw.get("prior", "")
        self.owner = kw.get("owner", "")
        self.db = kw.get("db", False)
        self.container = kw.get("container", "")
        self.compose = kw.get("compose", {"docker compose": "v2.29.1"})
        self.help = kw.get("help", "--wait  Wait\n --wait-timeout int  Timeout")
        self.networks = kw.get("networks", [])
        self.serve = kw.get("serve", serve_state())
        self.status = kw.get("status", {"BackendState": "Running", "Self": {"DNSName": FQDN + "."}})
        self.release_exists = kw.get("release_exists", False)
        self.fail_on = kw.get("fail_on")  # substring that raises when seen
        self.fail_always = kw.get("fail_always", False)

    def git(self, args):
        return b"a" * 40 if "rev-parse" in args else b""

    def commands(self):
        return [c[-1] for c in self.calls if c[0] == "ssh"]

    def __call__(self, args, data=None):
        if args[0] == "git":
            return self.git(args)
        cmd = args[-1]
        self.calls.append(args)
        if data is not None:
            self.stdin[cmd] = data
        if self.fail_on and self.fail_on in cmd and (self.fail_always or "/previous/" not in cmd):
            raise RuntimeError("simulated failure")
        if cmd == "tailscale status --json":
            return json.dumps(self.status).encode()
        if cmd == "tailscale serve status --json":
            return json.dumps(self.serve).encode()
        if cmd.startswith("tailscale serve --bg"):
            self.serve = serve_state(cmd.split()[-1].strip("'"))
            return b""
        if cmd.startswith("tailscale serve --https") and cmd.endswith("off"):
            self.serve = serve_state()
            return b""
        for prefix, version in self.compose.items():
            if cmd == f"{prefix} version --short":
                return version.encode()
            if cmd == f"{prefix} up --help":
                return self.help.encode()
        if " version --short" in cmd or " up --help" in cmd:
            raise RuntimeError("not installed")
        if "stat -c" in cmd:
            for name, fields in self.secrets.items():
                if name in cmd:
                    return fields.encode()
            return b"missing"
        if cmd.startswith("docker network inspect"):
            return json.dumps(self.networks).encode()
        if "current-release" in cmd and cmd.startswith("test"):
            return self.prior.encode()
        if "serve-owner" in cmd and cmd.startswith("test"):
            return self.owner.encode()
        if cmd.startswith("test -f") and "sqlite3" in cmd:
            return b"yes" if self.db else b"no"
        if cmd.startswith("test -d"):
            return b"yes" if self.release_exists else b"no"
        if cmd.startswith("docker ps"):
            return self.container.encode()
        return b""


def good_secrets():
    return {n: "regular file|600|10001|40" for n in deploy.SECRET_FILES}


def run_main(monkeypatch, tower):
    monkeypatch.setattr(deploy, "run", tower)
    deploy.main()


def index(commands, needle):
    return next(i for i, c in enumerate(commands) if needle in c)


def mutations(tower):
    return [c for c in tower.commands() if any(m in c for m in MUTATING)]


def test_successful_deployment_with_snapshot_serve_and_host_validation(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), db=True, container="abc123", prior=PRIOR)
    run_main(monkeypatch, tower)
    cmds = tower.commands()
    # Preflight reads precede every mutation; snapshot precedes recreation.
    assert index(cmds, "stat -c") < index(cmds, "mkdir")
    assert index(cmds, "docker network inspect") < index(cmds, "mkdir")
    snap = index(cmds, "queue_admin.py backup")
    assert snap < index(cmds, " up -d --wait ")
    assert "docker exec abc123 python scripts/queue_admin.py backup" in cmds[snap]
    assert any("/archive/20" in c and "cat " in c and ".sqlite3" in c for c in cmds)
    assert not any("docker.sock" in c or " -v /:" in c for c in cmds)
    # Rendered .env forces loopback, bridge IP and the Serve FQDN into ALLOWED_HOSTS.
    env_cmd = next(c for c in tower.stdin if "cat > " in c and "/.staging-" in c)
    env = tower.stdin[env_cmd].decode()
    hosts = dict(line.split("=", 1) for line in env.splitlines())["ALLOWED_HOSTS"].split(",")
    assert {"127.0.0.1", "localhost", "172.16.242.2", FQDN, "bridge.example.invalid"} <= set(hosts)
    assert len(hosts) == len(set(hosts))
    # Health uses the bridge IP and the Serve Host header, then the single Serve route is set.
    assert any("-H 'Host: " + FQDN + "'" in c and "172.16.242.2:8000/health" in c for c in cmds)
    assert "tailscale serve --bg --https=8449 http://172.16.242.2:8000" in cmds
    assert index(cmds, "-H 'Host: ") < index(cmds, "tailscale serve --bg")
    assert any(c.endswith("current-release'") or "current-release.new" in c for c in cmds)
    assert any("release-history.log" in c for c in cmds)
    assert not any("reset" in c or "funnel" in c.lower() or " down" in c for c in cmds)
    # Config digest is part of the immutable release name.
    assert any(f"{'a' * 40}-" in c for c in cmds)


def test_changed_config_gets_new_release_name(monkeypatch, tmp_path, capsys):
    names = []
    for hosts in ("one.example.invalid", "two.example.invalid"):
        values = config()
        values["ALLOWED_HOSTS"] = hosts
        setup(monkeypatch, tmp_path, values, action="plan")
        run_main(monkeypatch, Tower(secrets=good_secrets()))
        names.append(json.loads(capsys.readouterr().out)["release"])
    assert names[0] != names[1]


def test_new_service_without_database_skips_snapshot(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), db=False)
    run_main(monkeypatch, tower)
    cmds = tower.commands()
    assert not any("queue_admin.py" in c for c in cmds)
    assert any(" up -d --wait " in c for c in cmds)


def test_snapshot_without_running_container_uses_narrow_one_off(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), db=True, container="")
    run_main(monkeypatch, tower)
    cmds = tower.commands()
    snap = next(c for c in cmds if "queue_admin.py backup" in c)
    assert snap.startswith("docker run --rm --network none ")
    assert f"-v {ROOT}/data:/data " in snap and "--cap-drop ALL" in snap
    assert index(cmds, "queue_admin.py backup") < index(cmds, " up -d --wait ")


def test_snapshot_failure_aborts_before_recreation(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), db=True, container="abc", fail_on="queue_admin.py")
    with pytest.raises(RuntimeError):
        run_main(monkeypatch, tower)
    cmds = tower.commands()
    assert not any(" up -d" in c or "serve --bg" in c for c in cmds)
    assert not any("rm -rf" in c and "/data" in c for c in cmds)


def test_failed_recreation_restores_prior_release(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    prior_route = "http://172.16.242.9:8000"
    tower = Tower(
        secrets=good_secrets(),
        prior=PRIOR,
        owner="8449",
        serve=serve_state(prior_route),
        fail_on=" up -d --wait ",
    )
    with pytest.raises(RuntimeError, match="simulated"):
        run_main(monkeypatch, tower)
    cmds = tower.commands()
    assert any("/previous/" in c and " up -d --wait " in c for c in cmds)
    assert not any("serve --bg" in c for c in cmds)  # Serve never reached


def test_failed_health_after_serve_change_restores_only_our_route(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    prior_route = "http://172.16.242.9:8000"
    tower = Tower(
        secrets=good_secrets(),
        prior=PRIOR,
        owner="8449",
        serve=serve_state(prior_route),
        fail_on="printf %s 8449",
    )
    with pytest.raises(RuntimeError, match="simulated"):
        run_main(monkeypatch, tower)
    cmds = tower.commands()
    assert cmds.count("tailscale serve --bg --https=8449 http://172.16.242.2:8000") == 1
    assert cmds[-2] == "tailscale serve --bg --https=8449 http://172.16.242.9:8000"
    assert "/previous/" in cmds[-1] and " up -d --wait " in cmds[-1]
    assert any("printf %s 8449" in c for c in cmds[-4:])  # marker restored
    assert not any("reset" in c or " off" in c or "funnel" in c.lower() for c in cmds)
    assert tower.serve["Web"]["other.example.ts.net:443"]  # unrelated route untouched


def test_failed_first_deploy_removes_only_new_route_and_keeps_data(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), fail_on="current-release.new", fail_always=True)
    with pytest.raises(RuntimeError, match="simulated"):
        run_main(monkeypatch, tower)
    cmds = tower.commands()
    assert "tailscale serve --https=8449 off" in cmds
    assert any(c.endswith(" stop") for c in cmds)
    assert f"rm -f '{ROOT}/serve-owner'" in cmds or f"rm -f {ROOT}/serve-owner" in cmds
    assert not any(" down" in c or ("rm -rf" in c and "/data" in c) for c in cmds)


@pytest.mark.parametrize("prior", [PRIOR, ""])
def test_pointer_rollback_after_remote_success_with_lost_ack(monkeypatch, tmp_path, prior):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), prior=prior)
    lost_ack = False

    def run(args, data=None):
        nonlocal lost_ack
        result = tower(args, data)
        command = args[-1]
        if args[0] == "ssh" and "current-release.new" in command:
            if not lost_ack:
                tower.prior = "new-release-was-written"
                lost_ack = True
                raise RuntimeError("SSH acknowledgement lost")
            tower.prior = prior
        if args[0] == "ssh" and command.startswith("rm -f") and "current-release" in command:
            tower.prior = ""
        return result

    monkeypatch.setattr(deploy, "run", run)
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        deploy.main()
    assert tower.prior == prior


@pytest.mark.parametrize(
    "fields",
    [
        "missing",
        "regular file|644|10001|40",
        "regular file|600|0|40",
        "symbolic link|600|10001|9",
        "regular file|600|10001|0",
    ],
)
@pytest.mark.parametrize("action", ["check", "plan", "apply"])
def test_secret_preflight_blocks_every_action_before_mutation(
    monkeypatch, tmp_path, fields, action
):
    setup(monkeypatch, tmp_path, config(), action=action)
    secrets = good_secrets()
    secrets["grok-webhook-key.txt"] = fields
    tower = Tower(secrets=secrets)
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)
    assert mutations(tower) == []


@pytest.mark.parametrize("action", ["check", "plan"])
def test_check_and_plan_are_read_only(monkeypatch, tmp_path, capsys, action):
    setup(monkeypatch, tmp_path, config(), action=action)
    tower = Tower(secrets=good_secrets(), db=True)
    run_main(monkeypatch, tower)
    assert mutations(tower) == []
    out = json.loads(capsys.readouterr().out)
    assert out["pre_deploy_snapshot"] is True and out["serve_host"] == FQDN


@pytest.mark.parametrize(
    "compose,ok",
    [
        ({"docker compose": "v2.29.1"}, True),
        ({"docker compose": "v2.0.0", "docker-compose": "v5.0.0"}, True),
        ({"docker-compose": "5.0.1"}, True),
        ({"docker-compose": "2.24.0"}, True),
        ({"docker-compose": "1.29.2"}, False),
        ({}, False),
    ],
)
def test_compose_version_support(monkeypatch, tmp_path, capsys, compose, ok):
    setup(monkeypatch, tmp_path, config(), action="plan")
    tower = Tower(secrets=good_secrets(), compose=compose)
    if ok:
        run_main(monkeypatch, tower)
        assert json.loads(capsys.readouterr().out)["compose"] in {
            "docker compose",
            "docker-compose",
        }
    else:
        with pytest.raises(SystemExit):
            run_main(monkeypatch, tower)


def test_compose_without_wait_flags_is_refused(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config(), action="check")
    tower = Tower(secrets=good_secrets(), help="--detach  Detached mode\n --wait-timeout int")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)


def test_standalone_compose_is_used_in_commands(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), compose={"docker-compose": "v2.40.0"})
    run_main(monkeypatch, tower)
    assert any(c.startswith("docker-compose --project-name pebble-grok ") for c in tower.commands())


def net(name, subnet, project=None):
    labels = {"com.docker.compose.project": project} if project else {}
    return {"Name": name, "Labels": labels, "IPAM": {"Config": [{"Subnet": subnet}]}}


@pytest.mark.parametrize("other", ["172.16.0.0/12", "172.16.242.0/24", "172.16.242.0/29"])
def test_subnet_collision_with_foreign_network_is_refused(monkeypatch, tmp_path, other):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), networks=[net("other_default", other)])
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)
    assert mutations(tower) == []


def test_own_project_network_and_disjoint_networks_are_allowed(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    networks = [
        net("pebble-grok_bridge", "172.16.242.0/29", project="pebble-grok"),
        net("bridge", "172.17.0.0/16"),
        {"Name": "host", "IPAM": {"Config": []}},
        {"Name": "v6", "IPAM": {"Config": [{"Subnet": "fd00::/64"}]}},
    ]
    run_main(monkeypatch, Tower(secrets=good_secrets(), networks=networks))


def test_serve_port_owned_by_another_service_is_refused(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), serve=serve_state("http://127.0.0.1:9"), owner="")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)
    assert mutations(tower) == []


def test_funnel_on_our_port_is_refused(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    state = serve_state("http://172.16.242.9:8000")
    state["AllowFunnel"] = {f"{FQDN}:8449": True}
    tower = Tower(secrets=good_secrets(), serve=state, owner="8449")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)
    assert mutations(tower) == []


@pytest.mark.parametrize(
    "status",
    [
        {"BackendState": "Stopped", "Self": {"DNSName": FQDN + "."}},
        {"BackendState": "Running", "Self": {"DNSName": "bad host.ts.net."}},
        {"BackendState": "Running", "Self": {"DNSName": "single."}},
        {"BackendState": "Running"},
    ],
)
def test_serve_fqdn_must_be_valid(monkeypatch, tmp_path, status):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), status=status)
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tower)
    assert mutations(tower) == []


def test_without_serve_port_no_tailscale_calls(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config(), port=None)
    tower = Tower(secrets=good_secrets())
    run_main(monkeypatch, tower)
    assert not any("tailscale" in c for c in tower.commands())
    assert any("172.16.242.2:8000/health" in c for c in tower.commands())


def test_existing_release_with_different_env_is_not_overwritten(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, config())
    tower = Tower(secrets=good_secrets(), release_exists=True, fail_on="cmp -s -", fail_always=True)
    with pytest.raises(RuntimeError, match="different configuration"):
        run_main(monkeypatch, tower)
    assert not any("tar -xf" in c or " up -d" in c for c in tower.commands())


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
