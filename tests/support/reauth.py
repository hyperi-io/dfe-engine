#  Project:      dfe-engine
#  File:         tests/support/reauth.py
#  Purpose:      Lift the re-auth grant check from a test app whose tests are about something else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Lift the re-auth grant check from a test app whose tests are about something else."""

from typing import Any

from fastapi import FastAPI

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import (
    require_account_reauth,
    require_api_key_reauth,
    require_credential_reauth,
    require_group_reauth,
    require_org_reauth,
    require_provider_reauth,
    require_retention_setting_reauth,
    require_role_reauth,
    require_scim_group_reauth,
    require_scim_user_reauth,
)
from dfe_engine.api.v1.account_groups import require_new_group_reauth
from dfe_engine.api.v1.accounts import require_new_account_reauth
from dfe_engine.api.v1.api_keys import require_new_api_key_reauth
from dfe_engine.api.v1.auth import require_retire_reauth
from dfe_engine.api.v1.oidc_providers import require_new_provider_reauth
from dfe_engine.api.v1.orgs import require_org_change_reauth
from dfe_engine.api.v1.scim import require_new_scim_group_reauth, require_new_scim_user_reauth
from dfe_engine.api.v1.system import (
    require_retention_change_reauth,
    require_your_org_change_reauth,
)

# tests/unit/test_api/test_account_reauth.py holds every one of these to its grant.
REAUTH_CHECKS = (
    require_account_reauth,
    require_api_key_reauth,
    require_credential_reauth,
    require_group_reauth,
    require_new_account_reauth,
    require_new_api_key_reauth,
    require_new_group_reauth,
    require_new_provider_reauth,
    require_new_scim_group_reauth,
    require_new_scim_user_reauth,
    require_org_change_reauth,
    require_org_reauth,
    require_provider_reauth,
    require_retention_change_reauth,
    require_retention_setting_reauth,
    require_retire_reauth,
    require_role_reauth,
    require_scim_group_reauth,
    require_scim_user_reauth,
    require_your_org_change_reauth,
)


def _no_reauth() -> None:
    """Stand in for a re-auth check."""


def waive_reauth(app: FastAPI) -> FastAPI:
    """Let *app*'s access changes through without a re-auth grant."""
    for check in REAUTH_CHECKS:
        app.dependency_overrides[check] = _no_reauth
    return app


def create_app_without_reauth(*args: Any, **kwargs: Any) -> FastAPI:
    """An engine app, built as ``create_app`` builds it, that lets access changes through without a grant."""
    return waive_reauth(create_app(*args, **kwargs))
