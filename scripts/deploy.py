"""Deploy an immutable release over SSH, optionally behind private Tailscale Serve."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

PROJECT = "pebble-grok"
SECRET_FILES = ("bridge-token.txt", "grok-webhook-key.txt", "grok-webhook-url.txt")
DB_NAME = "index-bridge.sqlite3"
SERVICE_UID = "10001"
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
Ssh = Callable[..., bytes]


def run(args: list[str], data: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=data, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); output suppressed")
    return result.stdout


def q(value: str) -> str:
    return shlex.quote(value)


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def effective_hosts(configured: str, address: str, fqdn: str | None) -> str:
    """Union of configured hosts with the names the health checks and Serve will send."""
    hosts: list[str] = []
    for host in [*configured.split(","), "127.0.0.1", "localhost", address, fqdn or ""]:
        host = host.strip()
        if not host:
            continue
        if not HOST_RE.fullmatch(host.lower()):
            fail("ALLOWED_HOSTS must contain plain host names or IPv4 addresses")
        if host not in hosts:
            hosts.append(host)
    return ",".join(hosts)


def serve_fqdn(ssh: Ssh) -> str:
    """Return this node's MagicDNS name, the Host that Tailscale Serve forwards."""
    status = json.loads(ssh("tailscale status --json"))
    if status.get("BackendState") != "Running":
        fail("Tailscale is not running on the target host")
    name = str((status.get("Self") or {}).get("DNSName", "")).rstrip(".").lower()
    if name.count(".") < 2 or not HOST_RE.fullmatch(name):
        fail("Tailscale did not report a usable MagicDNS name for the target host")
    return name


def serve_preflight(ssh: Ssh, port: int, fqdn: str, marker: str) -> tuple[str, str | None]:
    """Read-only Serve checks. Returns (recorded owner, prior proxy for our route)."""
    state = json.loads(ssh("tailscale serve status --json"))
    handlers = state.get("Web") or {}
    key = f"{fqdn}:{port}"
    occupied = any(k.endswith(f":{port}") for k in handlers) or str(port) in (
        state.get("TCP") or {}
    )
    owner = ssh(f"test ! -f {q(marker)} || cat {q(marker)}").decode().strip()
    if occupied and owner != str(port):
        fail("Serve port is already owned by another service")
    if any(str(k).endswith(f":{port}") and v for k, v in (state.get("AllowFunnel") or {}).items()):
        fail("Funnel is enabled on the Serve port; this tool never manages public exposure")
    prior = None
    if key in handlers:
        routes = handlers[key].get("Handlers") or {}
        proxy = (routes.get("/") or {}).get("Proxy")
        if set(routes) != {"/"} or not proxy:
            fail("Existing Serve route is not a single proxy; refusing to replace it")
        prior = str(proxy)
    elif occupied:
        fail("Serve port is occupied under a different host name")
    return owner, prior


def compose_command(ssh: Ssh) -> str:
    """Pick a Docker Compose v2+ invocation that supports `up --wait --wait-timeout`."""
    for prefix in ("docker compose", "docker-compose"):
        try:
            version = ssh(f"{prefix} version --short").decode().strip()
            found = re.match(r"v?(\d+)\.\d+", version)
            if not found or int(found.group(1)) < 2:
                continue
            usage = ssh(f"{prefix} up --help").decode()
        except RuntimeError:
            continue
        if re.search(r"--wait(?![-\w])", usage) and "--wait-timeout" in usage:
            return prefix
    fail("Docker Compose v2 or newer with 'up --wait --wait-timeout' is required (v1 refused)")


def check_secrets(ssh: Ssh, root: str) -> None:
    for name in SECRET_FILES:
        path = root + "/secrets/" + name
        out = ssh(
            f"if [ -e {q(path)} ] || [ -L {q(path)} ]; then stat -c '%F|%a|%u|%s' {q(path)}; "
            "else echo missing; fi"
        )
        fields = out.decode().strip().split("|")
        if fields == ["missing"]:
            fail(f"Secret file missing: {name}")
        if len(fields) != 4 or fields[0] != "regular file":
            fail(f"Secret must be a regular file: {name}")
        if fields[1] != "600" or fields[2] != SERVICE_UID:
            fail(f"Secret must be mode 0600 and owned by uid {SERVICE_UID}: {name}")
        if fields[3] == "0":
            fail(f"Secret file is empty: {name}")


def check_subnet(ssh: Ssh, subnet: ipaddress.IPv4Network) -> None:
    """Refuse a subnet overlapping any Docker network not owned by this project."""
    out = ssh("docker network inspect $(docker network ls -q --no-trunc)")
    for network in json.loads(out):
        if (network.get("Labels") or {}).get("com.docker.compose.project") == PROJECT:
            continue
        for item in (network.get("IPAM") or {}).get("Config") or []:
            try:
                other = ipaddress.ip_network(item["Subnet"])
            except (KeyError, ValueError):
                continue
            if other.version == 4 and other.overlaps(subnet):
                fail(f"Subnet overlaps Docker network {network.get('Name')}: {other}")


def running_container(ssh: Ssh) -> str | None:
    out = ssh(
        f"docker ps -q --filter label=com.docker.compose.project={PROJECT} "
        "--filter label=com.docker.compose.service=index-bridge"
    )
    ids = out.decode().split()
    if len(ids) > 1:
        fail("More than one running index-bridge container; refusing to guess")
    return ids[0] if ids else None


def snapshot(ssh: Ssh, root: str, image: str, now: datetime) -> str:
    """Consistent private DB backup into root/archive/<year> before any recreation."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    name = f".pre-deploy-{stamp}.sqlite3"
    source = f"{root}/data/{name}"
    target_dir = f"{root}/archive/{now.year}"
    target = f"{target_dir}/pre-deploy-{stamp}.sqlite3"
    container = running_container(ssh)
    if container:
        prefix = f"docker exec {q(container)}"
    else:
        prefix = (
            "docker run --rm --network none --user 10001:10001 --read-only --cap-drop ALL "
            "--security-opt no-new-privileges:true --pids-limit 64 --memory 128m "
            "--tmpfs /tmp:size=16m,noexec,nosuid,nodev "
            f"-v {q(root + '/data')}:/data {q(image)}"
        )
    check = (
        "import sqlite3,sys;c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
        "sys.exit(0 if c.execute('PRAGMA integrity_check').fetchone()[0]=='ok' else 1)"
    )
    try:
        ssh(f"{prefix} python scripts/queue_admin.py backup /data/{q(name)}")
        ssh(f"{prefix} python -c {q(check)} /data/{q(name)}")
        ssh(
            f"umask 077; mkdir -p {q(target_dir)} && test ! -e {q(target)} "
            f"&& cat {q(source)} > {q(target)} && cmp -s {q(source)} {q(target)} "
            f"&& test -s {q(target)} && rm -f {q(source)}"
        )
    except BaseException:
        for leftover in (source, target):
            try:
                ssh(f"rm -f {q(leftover)}")
            except Exception:
                pass
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "plan", "apply"])
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument(
        "--config", type=Path, required=True, help="Private local JSON deployment config"
    )
    parser.add_argument("--serve-port", type=int)
    args = parser.parse_args()
    root = args.remote_root
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", root) or ".." in root.split("/"):
        parser.error("Remote root must be a simple absolute path")
    if len([part for part in root.split("/") if part]) < 2:
        parser.error("Remote root must be a dedicated service directory")
    if args.serve_port and not 1024 <= args.serve_port <= 65535:
        parser.error("Serve port must be 1024..65535")
    config = json.loads(args.config.read_text())
    allowed = {"ALLOWED_HOSTS", "INDEX_BRIDGE_SUBNET", "INDEX_BRIDGE_ADDRESS"}
    if set(config) != allowed or any(not isinstance(v, str) or "\n" in v for v in config.values()):
        parser.error("Config requires ALLOWED_HOSTS, INDEX_BRIDGE_SUBNET, INDEX_BRIDGE_ADDRESS")
    for value in config.values():
        if not re.fullmatch(r"[A-Za-z0-9.,:/-]+", value):
            parser.error("Config contains unsupported characters")
    try:
        subnet = ipaddress.IPv4Network(config["INDEX_BRIDGE_SUBNET"])
        address = ipaddress.IPv4Address(config["INDEX_BRIDGE_ADDRESS"])
        if (
            not address.is_private
            or address not in subnet
            or address in {subnet.network_address, subnet.broadcast_address}
        ):
            parser.error("Private address must be a usable member of the subnet")
    except ValueError:
        parser.error("Invalid IPv4 network configuration")
    repo = Path(__file__).resolve().parent.parent
    revision = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    if run(["git", "-C", str(repo), "status", "--porcelain"]).strip():
        parser.error("Commit the tested release before deployment")
    image = f"pebble-index-bridge:{revision[:12]}"

    def ssh(command: str, data: bytes | None = None) -> bytes:
        return run(["ssh", args.ssh_host, command], data)

    # Read-only preflight: runs for check, plan and apply before any remote mutation.
    ip = config["INDEX_BRIDGE_ADDRESS"]
    marker = root + "/serve-owner"
    fqdn = serve_fqdn(ssh) if args.serve_port else None
    owner, prior_proxy = "", None
    if args.serve_port and fqdn:
        owner, prior_proxy = serve_preflight(ssh, args.serve_port, fqdn, marker)
    compose_bin = compose_command(ssh)
    check_secrets(ssh, root)
    check_subnet(ssh, subnet)
    pointer = root + "/current-release"
    old = ssh(f"test ! -f {q(pointer)} || cat {q(pointer)}").decode().strip()
    if old and (not old.startswith(root + "/releases/") or ".." in old.split("/")):
        fail("current-release does not point inside the release directory")
    db_probe = ssh(f"test -f {q(root + '/data/' + DB_NAME)} && echo yes || echo no")
    has_db = db_probe.decode().strip() == "yes"

    env = dict(config)
    env["ALLOWED_HOSTS"] = effective_hosts(config["ALLOWED_HOSTS"], ip, fqdn)
    config_hash = hashlib.sha256(
        json.dumps({"config": env, "serve_port": args.serve_port}, sort_keys=True).encode()
    ).hexdigest()[:12]
    release = root + "/releases/" + revision + "-" + config_hash
    print(
        json.dumps(
            {
                "action": args.action,
                "revision": revision,
                "image": image,
                "release": release,
                "serve_port": args.serve_port,
                "serve_host": fqdn,
                "compose": compose_bin,
                "pre_deploy_snapshot": has_db,
                "preserves_data": True,
            }
        )
    )
    if args.action != "apply":
        return

    env.update(
        INDEX_BRIDGE_IMAGE=image,
        INDEX_BRIDGE_DATA_DIR=root + "/data",
        BRIDGE_TOKEN_FILE=root + "/secrets/bridge-token.txt",
        GROKBOT_WEBHOOK_KEY_FILE=root + "/secrets/grok-webhook-key.txt",
        GROKBOT_WEBHOOK_URL_FILE=root + "/secrets/grok-webhook-url.txt",
    )
    text = ("\n".join(f"{k}={v}" for k, v in env.items()) + "\n").encode()
    ssh(
        f"umask 077; mkdir -p {q(root + '/releases')} {q(root + '/data')} {q(root + '/archive')}; "
        f"chown {SERVICE_UID}:{SERVICE_UID} {q(root + '/data')}"
    )
    if ssh(f"test -d {q(release)} && echo yes || echo no").decode().strip() == "yes":
        try:
            ssh(f"cmp -s - {q(release + '/.env')}", text)
        except RuntimeError:
            raise RuntimeError("Existing immutable release has different configuration") from None
    else:
        stage = root + "/releases/.staging-" + revision + "-" + config_hash
        ssh(f"umask 077; rm -rf {q(stage)}; mkdir -p {q(stage)}")
        archive = run(["git", "-C", str(repo), "archive", "--format=tar", revision])
        ssh(f"tar -xf - -C {q(stage)}", archive)
        ssh(f"umask 077; cat > {q(stage + '/.env')}", text)
        ssh(f"mv {q(stage)} {q(release)}")
    ssh(f"docker build -t {q(image)} {q(release)}")
    if has_db:
        snapshot(ssh, root, image, datetime.now(UTC))

    def compose_for(directory: str) -> str:
        return (
            f"{compose_bin} --project-name {PROJECT} --env-file {q(directory + '/.env')} "
            f"-f {q(directory + '/deploy/tower-compose.yaml')}"
        )

    target = f"http://{ip}:8000"
    port = args.serve_port
    touched: list[str] = []
    try:
        touched.append("compose")
        ssh(f"{compose_for(release)} up -d --wait --wait-timeout 90")
        health = f"curl --fail --silent --max-time 5 --output /dev/null {q(target + '/health')}"
        ssh(health)
        if port and fqdn:
            ssh(health + f" -H {q('Host: ' + fqdn)}")
            touched.append("serve")
            ssh(f"tailscale serve --bg --https={port} {q(target)}")
            state = json.loads(ssh("tailscale serve status --json"))
            route = ((state.get("Web") or {}).get(f"{fqdn}:{port}") or {}).get("Handlers") or {}
            if (route.get("/") or {}).get("Proxy") != target:
                raise RuntimeError("Serve route did not match the requested target")
            touched.append("marker")
            ssh(f"umask 077; printf %s {port} > {q(marker)}")
        touched.append("pointer")
        ssh(
            f"umask 077; printf %s {q(release)} > {q(pointer + '.new')} "
            f"&& mv {q(pointer + '.new')} {q(pointer)}"
        )
    except BaseException:
        failed: list[str] = []

        def attempt(step: str, command: str) -> None:
            try:
                ssh(command)
            except Exception:
                failed.append(step)

        # Undo in reverse order; only this service's single Serve route is ever touched.
        if "marker" in touched:
            attempt(
                "marker",
                f"umask 077; printf %s {q(owner)} > {q(marker)}" if owner else f"rm -f {q(marker)}",
            )
        if "serve" in touched and port:
            attempt(
                "serve",
                f"tailscale serve --bg --https={port} {q(prior_proxy)}"
                if prior_proxy
                else f"tailscale serve --https={port} off",
            )
        # Data is never removed: a failed first deploy is only stopped.
        attempt(
            "compose",
            f"{compose_for(old)} up -d --wait --wait-timeout 90"
            if old
            else f"{compose_for(release)} stop",
        )
        if failed:
            print(f"error: rollback incomplete: {', '.join(failed)}", file=sys.stderr)
        raise
    log = root + "/archive/release-history.log"
    try:
        ssh(
            f"umask 077; printf '%s %s -> %s\\n' {q(datetime.now(UTC).isoformat())} "
            f"{q(old or '-')} {q(release)} >> {q(log)}"
        )
    except Exception:
        print("warning: release history entry was not written", file=sys.stderr)
    print("Deployment passed application health; persistent data retained")


if __name__ == "__main__":
    main()
