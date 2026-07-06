"""Tests for OIDC header authentication path in get_current_user()."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token


class TestOidcAuthentication:
    """OIDC headers (X-Oidc-Subject, X-Oidc-Groups) authenticate without JWT."""

    def test_oidc_subject_authenticates(self, client: TestClient):
        """X-Oidc-Subject header alone is sufficient for authentication."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "alice@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "alice@example.com"

    def test_oidc_groups_resolve_roles(self, client: TestClient, app):
        """Groups from X-Oidc-Groups resolve to roles via GroupStore."""
        # dfe-admins group was seeded by bootstrap with roles=["admin"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "alice@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "dfe-admins" in data["groups"]

    def test_unknown_groups_no_roles(self, client: TestClient):
        """An unknown IdP group authenticates but grants no roles.

        Uses an opaque (non-email) subject so JIT provisions NO `org_<domain>`
        group (that behaviour is covered in test_jit); this isolates the IdP
        header-group resolution - an unknown group name maps to nothing.
        """
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "opaque-subject-unknown",
                "X-Oidc-Groups": "unknown-group-xyz",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "opaque-subject-unknown"
        assert data["roles"] == []
        assert "unknown-group-xyz" in data["groups"]

    def test_multiple_comma_separated_groups(self, client: TestClient, app):
        """Multiple groups are split on comma and all roles collected."""
        # dfe-admins has ["admin"], dfe-analysts has ["data_analyst"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "carol@example.com",
                "X-Oidc-Groups": "dfe-admins, dfe-analysts",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_analyst" in data["roles"]
        assert "dfe-admins" in data["groups"]
        assert "dfe-analysts" in data["groups"]

    def test_empty_groups_header(self, client: TestClient):
        """An empty X-Oidc-Groups header authenticates with no roles.

        Opaque (non-email) subject so no `org_<domain>` group is auto-provisioned
        - isolates the empty-header case from JIT domain-group provisioning.
        """
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "opaque-subject-empty",
                "X-Oidc-Groups": "",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "opaque-subject-empty"
        assert data["roles"] == []

    def test_oidc_takes_precedence_over_bearer(self, client: TestClient, api_settings, app):
        """OIDC headers take precedence even when a valid Bearer token is present."""
        # Create a JWT with different identity
        jwt_token = create_access_token(
            data={"sub": "jwt-user", "org_id": "jwt-org", "roles": ["admin"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "oidc-user@example.com",
                "Authorization": f"Bearer {jwt_token}",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # OIDC wins — user_id from OIDC, not JWT
        assert data["user_id"] == "oidc-user@example.com"

    def test_oidc_whitespace_in_groups_stripped(self, client: TestClient, app):
        """Whitespace around group names is stripped."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "eve@example.com",
                "X-Oidc-Groups": " dfe-admins , dfe-viewers ",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_viewer" in data["roles"]


class TestOidcAccountEnabled:
    """FIX 3: account.enabled must gate OIDC principals like it gates JWT ones."""

    def test_disabled_shadow_account_is_rejected(self, client: TestClient, app):
        from dfe_engine.auth.jit import JitProvisioner

        subject = "disableme@example.com"
        # First login provisions an enabled shadow account (JIT).
        ok = client.get("/api/v1/auth/me", headers={"X-Oidc-Subject": subject})
        assert ok.status_code == 200

        # Admin disables it — shadow accounts are keyed by account_key, not raw subject.
        key = JitProvisioner.account_key(subject)
        app.state.account_store.update(key, enabled=False)

        # Next OIDC login is rejected the SAME way a disabled local (JWT) account is.
        denied = client.get("/api/v1/auth/me", headers={"X-Oidc-Subject": subject})
        assert denied.status_code == 401
        assert denied.json()["code"] == "unauthorized"
        assert denied.json()["message"] == "Account disabled"

    def test_enabled_shadow_account_passes(self, client: TestClient):
        resp = client.get("/api/v1/auth/me", headers={"X-Oidc-Subject": "fresh@example.com"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "fresh@example.com"


def _oidc_request(app, subject: str, groups: str | None = None):
    """Build a minimal Starlette Request carrying OIDC headers, bound to ``app``.

    Lets a test drive get_current_user() directly and inspect the FULL resolved
    AuthContext (org_ids + grants), which GET /auth/me does not expose.
    """
    from starlette.requests import Request

    raw_headers: list[tuple[bytes, bytes]] = [(b"x-oidc-subject", subject.encode())]
    if groups is not None:
        raw_headers.append((b"x-oidc-groups", groups.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/auth/me",
        "raw_path": b"/api/v1/auth/me",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 5555),
        "server": ("testserver", 80),
        "scheme": "http",
        "app": app,
    }
    return Request(scope)


def _request_with_headers(app, headers: dict[str, str]):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/v1/auth/me",
            "raw_path": b"/api/v1/auth/me",
            "query_string": b"",
            "headers": raw,
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": app,
        }
    )


class TestGatewayHeaderTrust:
    """HIGH-1: when a gateway secret is set, X-Oidc-* is trusted ONLY on a matching
    X-DFE-Gateway-Auth header - a pod-network peer cannot forge headers."""

    async def test_forged_oidc_headers_without_secret_are_not_trusted(self, client, app):
        import pytest
        from fastapi import HTTPException

        from dfe_engine.api.deps import get_current_user

        app.state.settings.auth.gateway_header_secret = "s3cr3t-gw"
        # Attacker sets X-Oidc-* directly, no valid gateway secret header.
        req = _request_with_headers(app, {"X-Oidc-Subject": "attacker@evil.example"})
        with pytest.raises(HTTPException) as exc:
            await get_current_user(req)
        assert exc.value.status_code == 401  # OIDC path skipped -> unauthenticated

    async def test_oidc_headers_with_matching_secret_are_trusted(self, client, app):
        from dfe_engine.api.deps import get_current_user

        app.state.settings.auth.gateway_header_secret = "s3cr3t-gw"
        req = _request_with_headers(
            app,
            {"X-Oidc-Subject": "alice@example.com", "X-DFE-Gateway-Auth": "s3cr3t-gw"},
        )
        ctx = await get_current_user(req)
        assert ctx.user_id == "alice@example.com"

    async def test_wrong_secret_is_rejected(self, client, app):
        import pytest
        from fastapi import HTTPException

        from dfe_engine.api.deps import get_current_user

        app.state.settings.auth.gateway_header_secret = "s3cr3t-gw"
        req = _request_with_headers(
            app, {"X-Oidc-Subject": "alice@example.com", "X-DFE-Gateway-Auth": "wrong"}
        )
        with pytest.raises(HTTPException) as exc:
            await get_current_user(req)
        assert exc.value.status_code == 401


class TestOidcStoreSideGroups:
    """The OIDC path resolves the shadow account's STORE-side group memberships
    (the JIT ``org_<domain>`` org group, or any admin-assigned group) and unions
    them with the IdP-header groups - the same way the local/JWT paths already
    resolve memberships. Closes the gap where an `org_<domain>` association (its
    org_ids + role) never reached the live OIDC AuthContext.

    GET /auth/me omits org_ids, so these drive get_current_user() directly to
    assert the full AuthContext (org_ids + a query:execute-gated action).
    """

    async def test_org_domain_group_reaches_context(self, client: TestClient, app):
        """A shadow account in an ``org_<domain>`` group (org_analyst, org_ids=['acme'])
        gets those org_ids + the tenant action (query:execute) reachable, even with
        NO org group in the IdP header."""
        from dfe_engine.api.deps import get_current_user, is_action_allowed
        from dfe_engine.auth.jit import JitProvisioner

        subject = "jane@acme.com"
        key = JitProvisioner.account_key(subject)
        # Pre-seed exactly as JIT provisions a claimed-domain login: a store-side
        # group carrying the org's org_ids + org_analyst, joined by the shadow
        # account key. This membership is NOT present in any IdP header.
        app.state.group_store.create(
            "org_acme_com", roles=["org_analyst"], members=[key], org_ids=["acme"]
        )
        app.state.account_store.create(key, "", groups=[])

        req = _oidc_request(app, subject)  # no X-Oidc-Groups
        ctx = await get_current_user(req)

        assert ctx.user_id == subject
        assert ctx.org_ids == ["acme"]
        assert "org_analyst" in ctx.roles
        # query:execute is the org-isolated tenant action; it must be reachable.
        assert is_action_allowed(req, ctx, "query:execute")

    async def test_no_store_side_group_uses_idp_header(self, client: TestClient, app):
        """No store-side membership (opaque subject) -> roles come purely from the
        IdP header group, exactly as before (no regression)."""
        from dfe_engine.api.deps import get_current_user

        req = _oidc_request(app, "opaque-subject-01", groups="dfe-admins")
        ctx = await get_current_user(req)

        assert ctx.roles == ["admin"]  # from the IdP header, nothing spurious added
        assert ctx.org_ids == []

    async def test_idp_header_and_store_side_groups_union(self, client: TestClient, app):
        """An IdP-header group AND a store-side org group BOTH contribute (union)."""
        from dfe_engine.api.deps import get_current_user
        from dfe_engine.auth.jit import JitProvisioner

        subject = "carol@acme.com"
        key = JitProvisioner.account_key(subject)
        app.state.group_store.create(
            "org_acme_com", roles=["org_analyst"], members=[key], org_ids=["acme"]
        )
        app.state.account_store.create(key, "", groups=[])

        req = _oidc_request(app, subject, groups="dfe-analysts")
        ctx = await get_current_user(req)

        assert "data_analyst" in ctx.roles  # from the IdP-header group
        assert "org_analyst" in ctx.roles  # from the store-side org group
        assert ctx.org_ids == ["acme"]
