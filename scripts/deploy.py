"""Deploy an immutable release over SSH, optionally behind private Tailscale Serve."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shlex
import subprocess
from pathlib import Path


def run(args: list[str], data: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=data, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); output suppressed")
    return result.stdout


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

    state = json.loads(ssh("tailscale serve status --json")) if args.serve_port else {}
    handlers = state.get("Web", {})
    occupied = any(k.endswith(f":{args.serve_port}") for k in handlers)
    marker = root + "/serve-owner"
    owned = ssh(f"test ! -f {shlex.quote(marker)} || cat {shlex.quote(marker)}").decode().strip()
    if occupied and owned != str(args.serve_port):
        parser.error("Serve port is already owned by another service")
    ssh("command -v docker >/dev/null && command -v docker-compose >/dev/null")
    print(
        json.dumps(
            {
                "action": args.action,
                "revision": revision,
                "image": image,
                "serve_port": args.serve_port,
                "preserves_data": True,
            }
        )
    )
    if args.action != "apply":
        return
    config_hash = hashlib.sha256(
        json.dumps({"config": config, "serve_port": args.serve_port}, sort_keys=True).encode()
    ).hexdigest()[:12]
    release = root + "/releases/" + revision + "-" + config_hash
    ssh(
        f"umask 077; mkdir -p {shlex.quote(release)} {shlex.quote(root + '/data')} {shlex.quote(root + '/secrets')} {shlex.quote(root + '/archive')}; chown 10001:10001 {shlex.quote(root + '/data')}"
    )
    archive = run(["git", "-C", str(repo), "archive", "--format=tar", revision])
    ssh(f"tar -xf - -C {shlex.quote(release)}", archive)
    env = dict(config)
    env.update(
        INDEX_BRIDGE_IMAGE=image,
        INDEX_BRIDGE_DATA_DIR=root + "/data",
        BRIDGE_TOKEN_FILE=root + "/secrets/bridge-token.txt",
        GROKBOT_WEBHOOK_KEY_FILE=root + "/secrets/grok-webhook-key.txt",
        GROKBOT_WEBHOOK_URL_FILE=root + "/secrets/grok-webhook-url.txt",
    )
    text = "\n".join(f"{k}={v}" for k, v in env.items()) + "\n"
    ssh(f"umask 077; cat > {shlex.quote(release + '/.env')}", text.encode())
    for name in ["bridge-token.txt", "grok-webhook-key.txt", "grok-webhook-url.txt"]:
        destination = root + "/secrets/" + name
        ssh(
            f'test -s {shlex.quote(destination)} && test "$(stat -c %a {shlex.quote(destination)})" = 600'
        )
    ssh(f"docker build -t {shlex.quote(image)} {shlex.quote(release)}")
    compose = f"docker-compose --project-name pebble-grok --env-file {shlex.quote(release + '/.env')} -f {shlex.quote(release + '/deploy/tower-compose.yaml')}"
    old = (
        ssh(
            f"test ! -f {shlex.quote(root + '/current-release')} || cat {shlex.quote(root + '/current-release')}"
        )
        .decode()
        .strip()
    )
    ip = config["INDEX_BRIDGE_ADDRESS"]
    try:
        ssh(f"{compose} up -d --wait --wait-timeout 90")
        ssh(f"curl --fail --silent --max-time 5 http://{shlex.quote(ip)}:8000/health >/dev/null")
        if args.serve_port:
            ssh(f"tailscale serve --bg --https={args.serve_port} http://{shlex.quote(ip)}:8000")
            ssh(f"umask 077; printf %s {args.serve_port} > {shlex.quote(marker)}")
    except Exception:
        if old:
            old_compose = f"docker-compose --project-name pebble-grok --env-file {shlex.quote(old + '/.env')} -f {shlex.quote(old + '/deploy/tower-compose.yaml')}"
            ssh(f"{old_compose} up -d --wait --wait-timeout 90")
        else:
            ssh(f"{compose} stop")
        raise
    if old:
        ssh(
            f'umask 077; printf %s {shlex.quote(old)} > {shlex.quote(root + "/archive/previous-release")}; printf %s "Deployment snapshots preserve earlier release configuration." > {shlex.quote(root + "/archive/ARCHIVED.md")}'
        )
    ssh(f"umask 077; printf %s {shlex.quote(release)} > {shlex.quote(root + '/current-release')}")
    print("Deployment passed application health; persistent data retained")


if __name__ == "__main__":
    main()
