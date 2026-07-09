#  Project:      dfe-engine
#  File:         tests/e2e/test_governed_ops_live.py
#  Purpose:      Live e2e for the CH-RBAC reconcile endpoint (governed ops)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Live e2e: the CH-RBAC reconcile endpoint against a REAL deployed engine + CH.

Complements test_live_pipeline (which covers the infra/schema DEPLOY flows over
the git deploy repo). This drives ``POST /api/v1/governance/ch-rbac/reconcile`` on
the real engine and asserts the seeded quota-tier + service roles actually
materialise in the deployment's ClickHouse - the two-axis RBAC model wired end to
end (engine API -> reconciler -> real CH objects).

Skipped unless DFE_E2E_ENGINE_URL / _TOKEN (and _CH_* for the CH assertions) are
set (see conftest). No mocks - every call hits the real deployment.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import E2EConfig, require

pytestmark = pytest.mark.live

_RECONCILE = "/api/v1/governance/ch-rbac/reconcile"


def test_reconcile_materialises_tier_and_service_roles(e2e: E2EConfig, ch_client) -> None:
    """POST reconcile -> the default tier + service CH roles exist in real CH."""
    require(e2e, "engine_url", "engine_token", "ch_host")

    resp = httpx.post(
        f"{e2e.engine_url.rstrip('/')}{_RECONCILE}",
        headers={"Authorization": f"Bearer {e2e.engine_token}"},
        verify=e2e.verify,
        timeout=60.0,  # a first reconcile creates profiles/quotas/roles/policies
    )
    assert resp.status_code == 200, f"reconcile failed: {resp.status_code} {resp.text}"

    names = {
        row[0]
        for row in ch_client.query(
            "SELECT name FROM system.roles WHERE startsWith(name, 'dfe_')"
        ).result_rows
    }
    # The opinionated seeded defaults (analyst + hunt tier families + loader svc).
    for expected in ("dfe_analyst_tier_2_role", "dfe_hunt_tier_2_role", "dfe_loader_role"):
        assert expected in names, f"{expected} missing after reconcile; got {sorted(names)}"


def test_reconcile_is_idempotent(e2e: E2EConfig) -> None:
    """A second reconcile is a no-op at the API level (all DDL is IF NOT EXISTS)."""
    require(e2e, "engine_url", "engine_token")

    headers = {"Authorization": f"Bearer {e2e.engine_token}"}
    url = f"{e2e.engine_url.rstrip('/')}{_RECONCILE}"
    first = httpx.post(url, headers=headers, verify=e2e.verify, timeout=60.0)
    second = httpx.post(url, headers=headers, verify=e2e.verify, timeout=60.0)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


def test_reconcile_requires_auth(e2e: E2EConfig) -> None:
    """The endpoint is gated (governance:write) - no bearer token -> 401."""
    require(e2e, "engine_url")
    resp = httpx.post(
        f"{e2e.engine_url.rstrip('/')}{_RECONCILE}",
        verify=e2e.verify,
        timeout=15.0,
    )
    assert resp.status_code == 401, f"expected 401 without a token, got {resp.status_code}"
