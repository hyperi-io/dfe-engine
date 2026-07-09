"""The 4 critical full-stack e2e tests (live deployment required).

  1. HTTPS ingest -> data lands in the ClickHouse `default` table.
  2. that same data is visible through HyperDX.
  3. dfe-ui (engine API) can see + change + deploy an INFRA change via the git
     deploy repo (a Helm-values change).
  4. dfe-ui (engine API) can see + change + deploy a SCHEMA change via the git
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

from tests.e2e.conftest import E2EConfig, poll_until, require

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# 1. HTTPS -> ClickHouse default table
# ---------------------------------------------------------------------------
def test_https_ingest_lands_in_clickhouse(e2e: E2EConfig, ch_client) -> None:
    require(e2e, "receiver_url", "ch_host")

    # A unique marker so we can find exactly our event in the shared table.
    marker = f"e2e-{uuid.uuid4().hex}"
    event = {"_source": "e2e-https", "message": marker, "severity": "info"}

    headers = {"Content-Type": "application/json"}
    if e2e.receiver_token:
        headers["Authorization"] = f"Bearer {e2e.receiver_token}"

    # HTTPS ingest. verify=e2e.verify (default off): the receiver may present the
    # deployment's own CA; this test asserts the data path, not the cert chain.
    resp = httpx.post(
        f"{e2e.receiver_url.rstrip('/')}/ingest",
        headers=headers,
        content=json.dumps(event),
        verify=e2e.verify,
        timeout=15.0,
    )
    assert resp.status_code in (200, 201, 202), f"ingest failed: {resp.status_code} {resp.text}"

    # The row should appear in dfe.default. Poll the real signal (row present),
    # timeout is only the stuck-dependency backstop.
    def _row():
        rows = ch_client.query(
            "SELECT _source, _raw FROM default WHERE _raw LIKE %(m)s LIMIT 1",
            parameters={"m": f"%{marker}%"},
        ).result_rows
        return rows[0] if rows else None

    row = poll_until(_row, timeout=90, interval=3, desc=f"event {marker} in dfe.default")
    assert marker in row[1]


# ---------------------------------------------------------------------------
# 2. ... and it is visible through HyperDX
# ---------------------------------------------------------------------------
def test_ingested_data_visible_in_hyperdx(e2e: E2EConfig, ch_client) -> None:
    require(e2e, "receiver_url", "ch_host", "hyperdx_url")

    marker = f"e2e-hdx-{uuid.uuid4().hex}"
    event = {"_source": "e2e-hyperdx", "message": marker, "severity": "warning"}
    headers = {"Content-Type": "application/json"}
    if e2e.receiver_token:
        headers["Authorization"] = f"Bearer {e2e.receiver_token}"
    resp = httpx.post(
        f"{e2e.receiver_url.rstrip('/')}/ingest",
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
                "SELECT 1 FROM default WHERE _raw LIKE %(m)s LIMIT 1",
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
            f"{e2e.hyperdx_url.rstrip('/')}/api/v1/search",
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
# deploy-repo helper (tests 3 + 4)
# ---------------------------------------------------------------------------
def _clone_deploy_repo(e2e: E2EConfig, dest: Path) -> Path:
    """Clone the deploy repo via HTTPS creds so we can assert the engine's writes."""
    url = e2e.deploy_repo_url
    if e2e.deploy_repo_token and url.startswith("https://"):
        # embed creds: https://user:token@host/...
        url = url.replace("https://", f"https://{e2e.deploy_repo_user}:{e2e.deploy_repo_token}@", 1)
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
# 3. dfe-ui can see + change + deploy an INFRA change via the deploy repo
# ---------------------------------------------------------------------------
def test_ui_deploys_infra_change_via_git(e2e: E2EConfig) -> None:
    require(e2e, "engine_url", "deploy_repo_url")
    base = e2e.engine_url.rstrip("/")
    headers = _engine_headers(e2e)
    svc, inst = "receiver", "production"

    # READ current deployment config (what the UI shows).
    cur = httpx.get(
        f"{base}/api/v1/deployments/{svc}-{inst}", headers=headers, verify=e2e.verify, timeout=15.0
    )
    assert cur.status_code == 200, f"read deployment failed: {cur.status_code} {cur.text}"

    # CHANGE a Helm var (resources.requests.cpu) to a unique sentinel value.
    sentinel = "137m"
    patch = {"resources": {"requests": {"cpu": sentinel}}}
    upd = httpx.patch(
        f"{base}/api/v1/deployments/{svc}-{inst}",
        headers=headers,
        content=json.dumps(patch),
        verify=e2e.verify,
        timeout=15.0,
    )
    assert upd.status_code in (200, 202), f"update failed: {upd.status_code} {upd.text}"

    # DEPLOY: trigger the gitops publish so the engine writes the overlay to git.
    pub = httpx.post(
        f"{base}/api/v1/deployments/publish", headers=headers, verify=e2e.verify, timeout=60.0
    )
    assert pub.status_code in (200, 202), f"publish failed: {pub.status_code} {pub.text}"

    # VERIFY the change reached the deploy repo (the GitOps hand-off surface).
    def _committed():
        with tempfile.TemporaryDirectory() as d:
            repo = _clone_deploy_repo(e2e, Path(d) / "repo")
            vf = repo / "values" / f"{svc}-{inst}-values.yaml"
            return vf.exists() and sentinel in vf.read_text()

    assert poll_until(_committed, timeout=90, interval=5, desc=f"{sentinel} in deploy repo values")


# ---------------------------------------------------------------------------
# 4. dfe-ui can see + change + deploy a SCHEMA change via the deploy repo
# ---------------------------------------------------------------------------
def test_ui_deploys_schema_change_via_git(e2e: E2EConfig) -> None:
    require(e2e, "engine_url", "deploy_repo_url")
    base = e2e.engine_url.rstrip("/")
    headers = _engine_headers(e2e)

    # CREATE a meta-schema / source via the engine API (what the UI does).
    source_name = f"e2e_src_{uuid.uuid4().hex[:8]}"
    body = {
        "name": source_name,
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

    # DEPLOY: publish -> the engine compiles DDL and writes it to the deploy repo.
    pub = httpx.post(
        f"{base}/api/v1/deployments/publish", headers=headers, verify=e2e.verify, timeout=60.0
    )
    assert pub.status_code in (200, 202), f"publish failed: {pub.status_code} {pub.text}"

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
