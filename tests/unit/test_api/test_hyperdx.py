#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hyperdx.py
#  Purpose:      The per-org HyperDX connection endpoint resolves the caller's org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for GET /api/v1/hyperdx/connection.

The handler is called directly with real fakes (a file-backed secrets store, a
list-only org registry) - the RBAC gate is a standard require_action dependency
covered elsewhere. The invariant under test: a caller only ever resolves to its
OWN org's credential, and anything ambiguous fails closed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dfe_engine.api.v1.hyperdx import hyperdx_connection
from dfe_engine.auth.models import AuthContext
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


def _settings(tmp_path):
    return SimpleNamespace(
        clickhouse=SimpleNamespace(host="ch.example", port=8123, secure=False),
        secrets=SecretsSettings(provider="file", path=str(tmp_path)),
    )


def _request(orgs):
    # The scope-aware query:execute gate (moved into the handler) resolves via
    # is_action_allowed, which reads settings.auth.enabled + role_config off
    # app.state - provide both so the gate can evaluate.
    from dfe_engine.auth.roles import RoleConfig

    reg = SimpleNamespace(list=lambda: orgs)
    state = SimpleNamespace(
        org_registry=reg,
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=True)),
        role_config=RoleConfig.load_builtin(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_org_viewer_gets_only_its_own_org_connection(tmp_path):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/acme", "acme-pw")
    store.put("ch/orgs/nerk", "nerk-pw")
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "acme"
    assert conn.username == "dfe_org_acme"
    assert conn.password == "acme-pw"
    assert conn.host == "http://ch.example:8123"


async def test_org_viewer_with_org_scoped_grant_passes_the_gate(tmp_path):
    """Regression (found by driving): an org_viewer resolves query:execute only at
    its OWN org scope (org-scoped group -> org-scoped grant). The old gate checked
    query:execute at SYSTEM scope, so every org_viewer got 403 and the embed showed
    'No available connections'. The gate must accept the org-scoped grant.
    """
    from dfe_engine.auth import Scope, ScopedGrant

    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(
        user_id="acme-viewer",
        roles=["org_viewer"],
        org_ids=["acme"],
        grants=[ScopedGrant(role="org_viewer", scope=Scope(type="org", id="acme"))],
    )
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "acme"
    assert conn.username == "dfe_org_acme"


async def test_platform_role_gets_the_unrestricted_reader(tmp_path):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    user = AuthContext(user_id="u", roles=["data_analyst"], org_ids=[])
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "platform"
    assert conn.username == "dfe_query_reader"
    assert conn.password == "qr-pw"


async def test_any_role_beyond_org_viewer_reads_unrestricted(tmp_path):
    """Mirrors derive_group_bindings: org_viewer + a platform role -> unrestricted."""
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(user_id="u", roles=["org_viewer", "infra_admin"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.username == "dfe_query_reader"


async def test_caller_with_no_resolvable_org_is_refused(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=[])
    req = _request([_org("acme", ["acme"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 403


async def test_caller_spanning_two_separate_orgs_fails_closed(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme", "nerk"])
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 403


async def test_org_with_many_ids_still_resolves_to_one_connection(tmp_path):
    """A single org spanning many tenant ids is ONE org, not an ambiguous span."""
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/span", "span-pw")
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["t1", "t2"])
    req = _request([_org("span", ["t1", "t2"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "span"
    assert conn.username == "dfe_org_span"


async def test_org_without_a_credential_is_503(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 503
