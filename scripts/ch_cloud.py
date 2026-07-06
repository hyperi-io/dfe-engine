#!/usr/bin/env python3
#  Project:      dfe-engine
#  File:         scripts/ch_cloud.py
#  Purpose:      Start / stop / status a ClickHouse Cloud service (cost control)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Manage a ClickHouse Cloud service from the CLI - start, stop, status.

CH Cloud services idle-stop but cost money while running; this is the manual
lever for the DFE cloud-portability test service (and any CH Cloud service). It
uses ONLY the ClickHouse Cloud management API + stdlib (no engine imports, no
extra deps), so it runs anywhere.

Credentials come from the environment (never hard-coded):
  DFE_CLICKHOUSE_CLOUD_API_KEY_ID
  DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET
The service is selected by name (default 'dfe', or --service) or by --id; the org
is auto-discovered from the key. A `.env` in the cwd or repo root is loaded if the
vars are not already exported.

Examples:
  python3 scripts/ch_cloud.py status
  python3 scripts/ch_cloud.py stop
  python3 scripts/ch_cloud.py start --service dfe
  python3 scripts/ch_cloud.py status --id 9bfffa77-a163-40c7-9b5c-d8ba69ab01ba
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.clickhouse.cloud/v1"


def _load_dotenv() -> None:
    """Populate the two API-key vars from a .env if not already in the environment."""
    if os.environ.get("DFE_CLICKHOUSE_CLOUD_API_KEY_ID") and os.environ.get(
        "DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET"
    ):
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line.startswith("DFE_CLICKHOUSE_CLOUD_API_KEY") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"'))
        break


def _auth_header() -> str:
    key_id = os.environ.get("DFE_CLICKHOUSE_CLOUD_API_KEY_ID", "")
    key_secret = os.environ.get("DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET", "")
    if not key_id or not key_secret:
        sys.exit(
            "ERROR: set DFE_CLICKHOUSE_CLOUD_API_KEY_ID + "
            "DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET (env or .env)."
        )
    return "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()


def _api(method: str, pathq: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed https API host, no user scheme
        f"{API}{pathq}",
        data=data,
        method=method,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed API host
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:  # surface the API error body
        sys.exit(f"ERROR: {method} {pathq} -> {exc.code} {exc.read().decode(errors='replace')}")


def _resolve_service(service: str, service_id: str | None) -> tuple[str, dict]:
    """Return (org_id, service_object) for the selected service."""
    org_id = _api("GET", "/organizations")["result"][0]["id"]
    services = _api("GET", f"/organizations/{org_id}/services")["result"]
    for svc in services:
        if (service_id and svc.get("id") == service_id) or (
            not service_id and svc.get("name") == service
        ):
            return org_id, svc
    names = ", ".join(f"{s.get('name')} ({s.get('id')})" for s in services)
    sys.exit(f"ERROR: service not found. Available: {names}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start/stop/status a ClickHouse Cloud service.")
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument(
        "--service",
        default=os.environ.get("DFE_CLICKHOUSE_CLOUD_SERVICE", "dfe"),
        help="Service name to select (default 'dfe' or $DFE_CLICKHOUSE_CLOUD_SERVICE).",
    )
    parser.add_argument(
        "--id", dest="service_id", default=None, help="Service UUID (overrides --service)."
    )
    args = parser.parse_args()

    _load_dotenv()
    org_id, svc = _resolve_service(args.service, args.service_id)
    sid, name, state = svc["id"], svc.get("name"), svc.get("state")

    if args.command == "status":
        print(f"{name} ({sid}): {state}")
        return

    want = args.command  # start | stop
    if (want == "start" and state in ("running", "starting")) or (
        want == "stop" and state in ("stopped", "stopping")
    ):
        print(f"{name}: already {state} - nothing to do")
        return

    resp = _api("PATCH", f"/organizations/{org_id}/services/{sid}/state", {"command": want})
    print(f"{name}: {want} issued -> {resp.get('result', {}).get('state', 'ok')}")


if __name__ == "__main__":
    main()
