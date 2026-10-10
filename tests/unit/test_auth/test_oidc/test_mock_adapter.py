#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_mock_adapter.py
#  Purpose:      Surface-B mock directory adapter + the Entra >200 overage path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Surface-B mock directory adapter tests.

The mock directory lets api-mode group resolution - and Entra's >200 group
overage enrichment in particular - run offline and deterministically. These
tests exercise it directly and prove the RP's overage path resolves a large
membership through it without any network.
"""

from __future__ import annotations

import json

from dfe_engine.auth.oidc.adapters import get_adapter
from dfe_engine.auth.oidc.adapters.mock import MockDirectoryAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import NormalizedIdentity, OidcRelyingParty


def _write_fixture(path, groups, members):
    path.write_text(json.dumps({"groups": groups, "members": members}), encoding="utf-8")


def _mock_provider(claim_name="groups"):
    return OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/tid/v2.0",
        groups=GroupResolutionConfig(mode="api", directory_backend="mock", claim_name=claim_name),
    )


# ── factory selection ────────────────────────────────────────────


def test_get_adapter_returns_mock_when_backend_mock():
    """directory_backend='mock' wins over the provider type in the factory."""
    adapter = get_adapter(_mock_provider())
    assert isinstance(adapter, MockDirectoryAdapter)


# ── loading + fail-open ───────────────────────────────────────────


async def test_mock_loads_groups_and_members(tmp_path, monkeypatch):
    fixture = tmp_path / "dir.json"
    _write_fixture(
        fixture,
        groups=[
            {"id": "g-admins", "name": "dfe-admins", "email": "admins@ms.test"},
            {"id": "g-viewers", "name": "dfe-viewers"},
        ],
        members={"user-oid": ["g-admins", "g-viewers"]},
    )
    monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(fixture))

    adapter = get_adapter(_mock_provider())
    all_groups = await adapter.list_all_groups()
    assert {g.name for g in all_groups} == {"dfe-admins", "dfe-viewers"}

    resolved = await adapter.resolve_user_groups("user-oid")
    assert sorted(g.id for g in resolved) == ["g-admins", "g-viewers"]

    ok, msg = await adapter.test_connection()
    assert ok is True
    assert "2 group" in msg


async def test_mock_unknown_user_has_no_groups(tmp_path, monkeypatch):
    fixture = tmp_path / "dir.json"
    _write_fixture(fixture, groups=[{"id": "g1", "name": "grp"}], members={"known": ["g1"]})
    monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(fixture))
    adapter = get_adapter(_mock_provider())
    assert await adapter.resolve_user_groups("stranger") == []


async def test_mock_fails_open_when_fixture_missing(tmp_path, monkeypatch):
    """No fixture -> empty directory + a False test_connection, never a crash."""
    monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(tmp_path / "nope.json"))
    adapter = get_adapter(_mock_provider())
    assert await adapter.list_all_groups() == []
    assert await adapter.resolve_user_groups("anyone") == []
    ok, msg = await adapter.test_connection()
    assert ok is False
    assert "not loaded" in msg


# ── the RP overage enrichment path, at >200 scale ────────────────


async def test_rp_enriches_overflowed_groups_at_scale(tmp_path, monkeypatch):
    """A 250-group user (overage) resolves the full membership via the mock.

    This is the whole point of the overage path: the token carried no groups
    array, so the RP fetches the membership from the directory keyed on the
    ``oid`` (not the pairwise ``sub``) and fills the identity with the group ids,
    which then resolve to roles by source_id exactly like a sub-200 login.
    """
    groups = [{"id": f"guid-{i}", "name": f"grp-{i}"} for i in range(250)]
    members = {"user-oid": [g["id"] for g in groups]}
    fixture = tmp_path / "dir.json"
    _write_fixture(fixture, groups=groups, members=members)
    monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(fixture))

    rp = OidcRelyingParty(OIDCProviderRegistry(tmp_path / "reg"))
    provider = _mock_provider()
    identity = NormalizedIdentity(subject="pairwise-sub", groups=[], groups_overflowed=True)
    # oid, not sub, is what the directory is keyed on.
    userinfo = {"sub": "pairwise-sub", "oid": "user-oid"}

    enriched = await rp._enrich_groups_from_directory(provider, identity, userinfo)
    assert len(enriched.groups) == 250
    assert "guid-0" in enriched.groups
    assert "guid-249" in enriched.groups


async def test_rp_overage_enrichment_fails_safe_without_fixture(tmp_path, monkeypatch):
    """An enrichment miss yields no groups (default deny), never an exception."""
    monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(tmp_path / "absent.json"))
    rp = OidcRelyingParty(OIDCProviderRegistry(tmp_path / "reg"))
    identity = NormalizedIdentity(subject="s", groups=[], groups_overflowed=True)
    enriched = await rp._enrich_groups_from_directory(_mock_provider(), identity, {"oid": "x"})
    assert enriched.groups == []
