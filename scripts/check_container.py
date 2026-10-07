"""Check the running container's security settings without printing its config."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", nargs="?")
    parser.add_argument("--stdin", action="store_true", help="Read docker inspect JSON from stdin")
    args = parser.parse_args()
    if args.stdin:
        records = json.load(sys.stdin)
    elif args.container:
        result = subprocess.run(
            ["docker", "inspect", args.container], capture_output=True, check=True
        )
        records = json.loads(result.stdout)
    else:
        parser.error("Container name or --stdin required")
    record = records[0]
    host = record["HostConfig"]
    config = record["Config"]
    mounts = record.get("Mounts", [])
    ports = host.get("PortBindings") or {}
    checks = {
        "running and healthy": record["State"]["Running"]
        and record["State"].get("Health", {}).get("Status") == "healthy",
        "nonroot UID": config["User"] == "10001:10001",
        "read only filesystem": host["ReadonlyRootfs"],
        "all capabilities dropped": "ALL" in (host.get("CapDrop") or []),
        "no new privileges": any(
            v.startswith("no-new-privileges") for v in (host.get("SecurityOpt") or [])
        ),
        "bounded resources": 0 < host["Memory"] <= 512 * 1024 * 1024
        and 0 < host["PidsLimit"] <= 128
        and host["NanoCpus"] > 0,
        "private listener": all(
            binding["HostIp"] in {"127.0.0.1", "::1"}
            for bindings in ports.values()
            for binding in bindings
        ),
        "narrow mounts": all(
            m["Destination"] == "/data"
            or (m["Destination"].startswith("/run/secrets/") and not m["RW"])
            for m in mounts
        ),
        "no direct credential env": not any(
            v.split("=", 1)[0] in {"BRIDGE_TOKEN", "GROKBOT_WEBHOOK_KEY", "GROKBOT_WEBHOOK_URL"}
            for v in config.get("Env", [])
        ),
        "bounded logs": host.get("LogConfig", {}).get("Config", {}).get("max-size") == "10m"
        and host.get("LogConfig", {}).get("Config", {}).get("max-file") == "3",
    }
    for name, passed in checks.items():
        print(("PASS " if passed else "FAIL ") + name)
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
