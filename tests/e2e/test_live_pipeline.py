"""The 3 critical full-stack e2e tests (live deployment required).

  1. HTTPS ingest -> data lands in the ClickHouse `default` table.
  2. that same data is visible through HyperDX.
  3. dfe-ui (engine API) can see + change + deploy a SCHEMA change via the git
     deploy repo (a meta-schema deployment -> DDL).

All are skipped unless the relevant DFE_E2E_* env vars are set (see conftest).
No mocks - every step hits the real deployment.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import E2EConfig, must, poll_until, require

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# 1. HTTPS -> ClickHouse landing table
# ---------------------------------------------------------------------------
def test_https_ingest_lands_in_clickhouse(e2e: E2EConfig, ch_client) -> None:
    require(e2e, "receiver_url", "ch_host")

    # A unique marker so we can find exactly our event in the shared table.
    #
    # NO `_source` ON THE EVENT, deliberately. The receiver's shipped routing rule
    # is `_source` / key_value_use: an event carrying `_source: x` is routed to
    # topic `x_land` and lands in table `dfe.x`, not `dfe.main`. Setting it here
    # sent the event to a per-source table that does not exist, so the loader
    # DLQ'd it on schema_pending_timeout while this test polled `dfe.main` and
    # timed out - looking like a broken data path when the path was working.
    marker = f"e2e-{uuid.uuid4().hex}"
    event = {"message": marker, "severity": "info"}

    headers = {"Content-Type": "application/json"}
    if e2e.receiver_token:
        headers["Authorization"] = f"Bearer {e2e.receiver_token}"

    # HTTPS ingest. verify=e2e.verify (default off): the receiver may present the
    # deployment's own CA; this test asserts the data path, not the cert chain.
    resp = httpx.post(
        f"{must(e2e.receiver_url).rstrip('/')}/ingest",
        headers=headers,
        content=json.dumps(event),
        verify=e2e.verify,
        timeout=15.0,
    )
    assert resp.status_code in (200, 201, 202), f"ingest failed: {resp.status_code} {resp.text}"

    # The row should appear in dfe.main. Poll the real signal (row present),
    # timeout is only the stuck-dependency backstop.
    def _row():
        rows = ch_client.query(
            "SELECT _source, _raw FROM main WHERE _raw LIKE %(m)s LIMIT 1",
            parameters={"m": f"%{marker}%"},
        ).result_rows
        return rows[0] if rows else None

    row = poll_until(_row, timeout=90, interval=3, desc=f"event {marker} in dfe.main")
    assert marker in row[1]


# ---------------------------------------------------------------------------
# 2. ... and it is visible through HyperDX
# ---------------------------------------------------------------------------
def test_ingested_data_visible_in_hyperdx(e2e: E2EConfig, ch_client) -> None:
    require(e2e, "receiver_url", "ch_host", "hyperdx_url")

    # No `_source` - see the note in the ingest test above.
    marker = f"e2e-hdx-{uuid.uuid4().hex}"
    event = {"message": marker, "severity": "warning"}
    headers = {"Content-Type": "application/json"}
    if e2e.receiver_token:
        headers["Authorization"] = f"Bearer {e2e.receiver_token}"
    resp = httpx.post(
        f"{must(e2e.receiver_url).rstrip('/')}/ingest",
        headers=headers,
        content=json.dumps(event),
        verify=e2e.verify,
        timeout=15.0,
    )
    assert resp.status_code in (200, 201, 202), f"ingest failed: {resp.status_code} {resp.text}"

    # First confirm it landed in CH (HyperDX reads CH), then confirm HyperDX's
    # search API returns it - proving the HyperDX -> CH datasource wiring.
    poll_until(
        lambda: (
            ch_client.query(
                "SELECT 1 FROM main WHERE _raw LIKE %(m)s LIMIT 1",
                parameters={"m": f"%{marker}%"},
            ).result_rows
        ),
        timeout=90,
        interval=3,
        desc=f"event {marker} in CH before HyperDX check",
    )

    hdx_headers = {"Content-Type": "application/json"}
    if e2e.hyperdx_api_key:
        hdx_headers["Authorization"] = f"Bearer {e2e.hyperdx_api_key}"

    def _hyperdx_sees():
        # HyperDX search API: query for our marker across the default source.
        r = httpx.post(
            f"{must(e2e.hyperdx_url).rstrip('/')}/api/v1/search",
            headers=hdx_headers,
            content=json.dumps({"q": marker, "limit": 1}),
            verify=e2e.verify,
            timeout=15.0,
        )
        if r.status_code != 200:
            return False
        body = r.json()
        # Tolerate either {data:[...]} or {results:[...]} shapes.
        hits = body.get("data") or body.get("results") or []
        return bool(hits)

    assert poll_until(_hyperdx_sees, timeout=90, interval=3, desc=f"HyperDX showing {marker}")


# ---------------------------------------------------------------------------
# deploy-repo helper (test 3)
# ---------------------------------------------------------------------------
def _clone_deploy_repo(e2e: E2EConfig, dest: Path) -> Path:
    """Clone the deploy repo via HTTPS creds so we can assert the engine's writes."""
    url = must(e2e.deploy_repo_url)
    # Embed creds as <scheme>://user:token@host/... for EITHER scheme. Keying this
    # on https alone silently cloned anonymously whenever the repo was plain http
    # - an in-cluster forgejo, or any run reaching it through a port-forward - and
    # the resulting auth failure surfaced only as `git clone` exit 128.
    for scheme in ("https://", "http://"):
        if e2e.deploy_repo_token and url.startswith(scheme):
            creds = f"{e2e.deploy_repo_user}:{e2e.deploy_repo_token}@"
            url = url.replace(scheme, f"{scheme}{creds}", 1)
            break
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)], check=True, capture_output=True
    )
    return dest


def _engine_headers(e2e: E2EConfig) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if e2e.engine_token:
        h["Authorization"] = f"Bearer {e2e.engine_token}"
    return h


# ---------------------------------------------------------------------------
# 3. dfe-ui can see + change + deploy a SCHEMA change via the deploy repo
# ---------------------------------------------------------------------------
def test_ui_deploys_schema_change_via_git(e2e: E2EConfig) -> None:
    require(e2e, "engine_url", "deploy_repo_url")
    base = must(e2e.engine_url).rstrip("/")
    headers = _engine_headers(e2e)

    # CREATE a meta-schema / source via the engine API (what the UI does).
    # The create contract is `source` (not `name`) and `match` is REQUIRED - it is
    # the routing predicate that binds incoming events to this source, so a source
    # without one could never match anything.
    # Hyphen-separated: a source name is a DNS-1123 label, because a source-bound
    # app deploys an instance named for it.
    source_name = f"e2e-src-{uuid.uuid4().hex[:8]}"
    body = {
        "source": source_name,
        "match": {"field": "_source", "operator": "equals", "value": source_name},
        "schema": {
            "profile": "timeseries",
            "additional": [{"name": "e2e_marker", "type": "String"}],
        },
    }
    create = httpx.post(
        f"{base}/api/v1/sources",
        headers=headers,
        content=json.dumps(body),
        verify=e2e.verify,
        timeout=15.0,
    )
    assert create.status_code in (200, 201), (
        f"create source failed: {create.status_code} {create.text}"
    )

    # No publish call - see the note in the infra test above.

    # VERIFY the generated DDL landed in the deploy repo (ddl/<table>.sql), and
    # that it carries our additional column.
    def _ddl_committed():
        with tempfile.TemporaryDirectory() as d:
            repo = _clone_deploy_repo(e2e, Path(d) / "repo")
            ddl_dir = repo / "ddl"
            if not ddl_dir.exists():
                return False
            for sql in ddl_dir.glob("*.sql"):
                text = sql.read_text()
                if source_name in text and "e2e_marker" in text:
                    return True
            return False

    assert poll_until(
        _ddl_committed, timeout=90, interval=5, desc=f"DDL for {source_name} in deploy repo"
    )

    # cleanup: remove the test source.
    httpx.delete(
        f"{base}/api/v1/sources/{source_name}", headers=headers, verify=e2e.verify, timeout=15.0
    )
