#  Project:      dfe-engine
#  File:         tests/unit/test_auth/factories.py
#  Purpose:      Factories for auth unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for auth unit test inputs."""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth

from dfe_engine.auth.oidc.adapters.entra import GraphCredentials
from dfe_engine.auth.oidc.field_rules import FieldProblem
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp


def make_field_problem(*, code: str, field: str, message: str) -> FieldProblem:
    """Build an auth.oidc.field_rules.FieldProblem."""
    return FieldProblem(code=code, field=field, message=message)


def make_graph_credentials(
    *, client_id: str, client_secret: str, tenant_id: str
) -> GraphCredentials:
    """Build an auth.oidc.adapters.entra.GraphCredentials."""
    return GraphCredentials(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)


def make_local_idp() -> LocalIdp:
    """Build a tests.unit.test_auth.test_oidc.local_idp.LocalIdp (not yet started)."""
    return LocalIdp()


def make_normalized_identity(*, subject: str, **kwargs: object) -> NormalizedIdentity:
    """Build an auth.oidc.rp.NormalizedIdentity."""
    return NormalizedIdentity.model_validate({"subject": subject, **kwargs})


def make_oauth_client(*, server_metadata_url: str) -> Any:
    """Build a real Authlib Starlette OAuth client that discovers its endpoints at the URL."""
    oauth = OAuth()
    oauth.register(
        client_id="dfe", client_secret="secret", name="idp", server_metadata_url=server_metadata_url
    )
    return oauth.create_client("idp")


def make_oidc_provider(
    *, issuer: str = "https://idp.example", type: str = "generic", **kwargs: object
) -> OIDCProvider:
    """Build an auth.oidc.models.OIDCProvider."""
    return OIDCProvider.model_validate({"issuer": issuer, "type": type, **kwargs})
