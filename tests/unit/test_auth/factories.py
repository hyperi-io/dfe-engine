#  Project:      dfe-engine
#  File:         tests/unit/test_auth/factories.py
#  Purpose:      Factories for auth unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for auth unit test inputs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from authlib.integrations.starlette_client import OAuth
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.oauth2 import service_account
from googleapiclient.discovery import build
from scalo.http import AsyncHttpClient

from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter, GraphCredentials
from dfe_engine.auth.oidc.adapters.generic import GenericAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.field_rules import FieldProblem
from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from dfe_engine.auth.oidc.scheduler import OidcSyncScheduler
from tests.unit.test_auth.test_oidc.local_directory import DirectoryReply, LocalDirectory
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp

# The env var an Okta provider from make_okta_directory_provider reads its API token from.
OKTA_API_TOKEN_ENV = "DFE_TEST_OKTA_API_TOKEN"


def make_directory_reply(*, body: str, **kwargs: Any) -> DirectoryReply:
    """Build a tests.unit.test_auth.test_oidc.local_directory.DirectoryReply."""
    return DirectoryReply(body=body, **kwargs)


def make_entra_adapter(*, provider: OIDCProvider) -> EntraAdapter:
    """Build an auth.oidc.adapters.entra.EntraAdapter with no secret store."""
    return EntraAdapter(provider)


def make_field_problem(*, code: str, field: str, message: str) -> FieldProblem:
    """Build an auth.oidc.field_rules.FieldProblem."""
    return FieldProblem(code=code, field=field, message=message)


def make_generic_adapter(*, provider: OIDCProvider) -> GenericAdapter:
    """Build an auth.oidc.adapters.generic.GenericAdapter with no secret store."""
    return GenericAdapter(provider)


def make_google_adapter(*, provider: OIDCProvider) -> GoogleAdapter:
    """Build an auth.oidc.adapters.google.GoogleAdapter with no secret store."""
    return GoogleAdapter(provider)


def make_google_directory_service(*, api_endpoint: str) -> Any:
    """Build a real Admin SDK Directory service whose token requests go to ``/token`` at *api_endpoint* and whose API requests go to *api_endpoint*."""
    info = json.loads(make_google_service_account_json(token_uri=f"{api_endpoint}/token"))
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/admin.directory.group.readonly"]
    )
    return build(
        cache_discovery=False,
        client_options={"api_endpoint": api_endpoint},
        credentials=credentials,
        serviceName="admin",
        version="directory_v1",
    )


def make_google_service_account_json(*, token_uri: str) -> str:
    """Build a service account JSON with a fresh RSA key whose token requests go to *token_uri*."""
    key = rsa.generate_private_key(key_size=2048, public_exponent=65537)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        encryption_algorithm=serialization.NoEncryption(),
        format=serialization.PrivateFormat.PKCS8,
    )
    return json.dumps(
        {
            "client_email": "dfe-sync@example.iam.gserviceaccount.com",
            "private_key": private_key.decode("utf-8"),
            "private_key_id": "test-key",
            "project_id": "dfe-test",
            "token_uri": token_uri,
            "type": "service_account",
        }
    )


def make_graph_credentials(
    *, client_id: str, client_secret: str, tenant_id: str
) -> GraphCredentials:
    """Build an auth.oidc.adapters.entra.GraphCredentials."""
    return GraphCredentials(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)


def make_group(*, name: str, **kwargs: object) -> Group:
    """Build an auth.groups.Group."""
    return Group.model_validate({"name": name, **kwargs})


def make_group_info(*, id: str, name: str, **kwargs: object) -> GroupInfo:
    """Build an auth.oidc.models.GroupInfo."""
    return GroupInfo.model_validate({"id": id, "name": name, **kwargs})


def make_group_store(*, directory: Path) -> GroupStore:
    """Build an auth.groups.GroupStore over a directory of group files."""
    return GroupStore(directory)


def make_http_client() -> AsyncHttpClient:
    """Build a scalo.http.AsyncHttpClient with its default timeout, retries and TLS posture."""
    return AsyncHttpClient()


def make_http_status_error(*, body: str, status: int) -> httpx.HTTPStatusError:
    """Build the httpx.HTTPStatusError a client raises for a GET answered with *status* and *body*."""
    request = httpx.Request(method="GET", url="https://directory.example/v1/groups")
    response = httpx.Response(content=body.encode("utf-8"), request=request, status_code=status)
    return httpx.HTTPStatusError(
        f"HTTP {status} from the directory", request=request, response=response
    )


def make_local_directory(*, tls_dir: Path | None) -> LocalDirectory:
    """Build a tests.unit.test_auth.test_oidc.local_directory.LocalDirectory (not yet started), over TLS when given a directory for its certificates."""
    return LocalDirectory(tls_dir=tls_dir)


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


def make_oidc_provider_registry(*, directory: Path) -> OIDCProviderRegistry:
    """Build an auth.oidc.registry.OIDCProviderRegistry over a directory of provider files."""
    return OIDCProviderRegistry(directory)


def make_oidc_sync_scheduler(
    *,
    bindings: Mapping[str, str] | None = None,
    group_store: GroupStore,
    on_groups_created: Callable[[], None] | None = None,
    registry: OIDCProviderRegistry,
    tick_seconds: float = 60,
) -> OidcSyncScheduler:
    """Build an auth.oidc.scheduler.OidcSyncScheduler with no metrics backend and no secret store."""
    return OidcSyncScheduler(
        bindings=bindings,
        group_store=group_store,
        metrics=None,
        on_groups_created=on_groups_created,
        registry=registry,
        secrets=None,
        tick_seconds=tick_seconds,
    )


def make_okta_adapter(*, provider: OIDCProvider) -> OktaAdapter:
    """Build an auth.oidc.adapters.okta.OktaAdapter with no secret store."""
    return OktaAdapter(provider)


def make_okta_directory_provider(*, okta_domain: str, **kwargs: object) -> OIDCProvider:
    """Build an api-mode Okta auth.oidc.models.OIDCProvider whose API token is read from OKTA_API_TOKEN_ENV."""
    groups = {"api_token_env": OKTA_API_TOKEN_ENV, "mode": "api", "okta_domain": okta_domain}
    return make_oidc_provider(
        groups=groups, issuer="https://example.okta.com", type="okta", **kwargs
    )
