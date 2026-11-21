#  Project:      dfe-engine
#  File:         tests/unit/test_auth/factories.py
#  Purpose:      Factories for auth unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for auth unit test inputs."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import OAuth

from dfe_engine.auth.accounts import Account, AccountStore, LinkedIdentity
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.identity_links import LinkTicket
from dfe_engine.auth.jit import JitProvisioner
from dfe_engine.auth.oidc.adapters.entra import GraphCredentials
from dfe_engine.auth.oidc.field_rules import FieldProblem
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from dfe_engine.auth.oidc.scheduler import OidcSyncScheduler
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp

# The env var an Okta provider from make_okta_directory_provider reads its API token from.
OKTA_API_TOKEN_ENV = "DFE_TEST_OKTA_API_TOKEN"

# What an account with no usable password stores, so verify_password never matches it.
PASSWORDLESS_HASH = "!"


def make_account(
    *, username: str, password_hash: str = PASSWORDLESS_HASH, **kwargs: object
) -> Account:
    """Build an auth.accounts.Account, passwordless unless a hash is given."""
    return Account.model_validate({"password_hash": password_hash, "username": username, **kwargs})


def make_account_store(*, directory: Path) -> AccountStore:
    """Build an auth.accounts.AccountStore over a directory of account files."""
    return AccountStore(directory)


def make_field_problem(*, code: str, field: str, message: str) -> FieldProblem:
    """Build an auth.oidc.field_rules.FieldProblem."""
    return FieldProblem(code=code, field=field, message=message)


def make_graph_credentials(
    *, client_id: str, client_secret: str, tenant_id: str
) -> GraphCredentials:
    """Build an auth.oidc.adapters.entra.GraphCredentials."""
    return GraphCredentials(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)


def make_group(*, name: str, **kwargs: object) -> Group:
    """Build an auth.groups.Group."""
    return Group.model_validate({"name": name, **kwargs})


def make_group_store(*, directory: Path) -> GroupStore:
    """Build an auth.groups.GroupStore over a directory of group files."""
    return GroupStore(directory)


def make_jit_provisioner(*, account_store: AccountStore, group_store: GroupStore) -> JitProvisioner:
    """Build an auth.jit.JitProvisioner with no HyperDX client and no provider bindings."""
    return JitProvisioner(account_store, group_store)


def make_link_ticket(
    *, account: str, email: str, provider: str, subject: str, **kwargs: object
) -> LinkTicket:
    """Build an auth.identity_links.LinkTicket."""
    return LinkTicket.model_validate(
        {"account": account, "email": email, "provider": provider, "subject": subject, **kwargs}
    )


def make_linked_identity(
    *, key: str, provider: str, subject: str, **kwargs: object
) -> LinkedIdentity:
    """Build an auth.accounts.LinkedIdentity."""
    return LinkedIdentity.model_validate(
        {"key": key, "provider": provider, "subject": subject, **kwargs}
    )


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
        bindings=bindings if bindings is not None else {},
        group_store=group_store,
        metrics=None,
        on_groups_created=on_groups_created,
        registry=registry,
        secrets=None,
        tick_seconds=tick_seconds,
    )


def make_okta_directory_provider(*, okta_domain: str, **kwargs: object) -> OIDCProvider:
    """Build an api-mode Okta auth.oidc.models.OIDCProvider whose API token is read from OKTA_API_TOKEN_ENV."""
    groups = {"api_token_env": OKTA_API_TOKEN_ENV, "mode": "api", "okta_domain": okta_domain}
    return make_oidc_provider(
        groups=groups, issuer="https://example.okta.com", type="okta", **kwargs
    )
