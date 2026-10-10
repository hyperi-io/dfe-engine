#  Project:      dfe-engine
#  File:         tests/live/test_google_service_account.py
#  Purpose:      Google group resolution through the optional service account, against a real Workspace
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Google group resolution through the optional service account, against a real Workspace.

Marked ``live``, so the default run and the full tier leave it out. Select it with
``uv run pytest tests/live -m live``. It skips unless both inputs are in the environment.

``DFE_GOOGLE_SA_JSON`` holds a service account key that has a groups admin role, the
variable the provider's ``service_account_json_env`` names in a deployment.
``DFE_OIDC_GOOGLE_FIXTURE_USER`` is a user in at least one group.

The adapter gets no access token, so only the Directory API path can answer. No
assertion message names a token, key, email or group id.
"""

import os

import pytest

from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

SA_JSON_VAR = "DFE_GOOGLE_SA_JSON"
USER_VAR = "DFE_OIDC_GOOGLE_FIXTURE_USER"

_MISSING = [name for name in (SA_JSON_VAR, USER_VAR) if not os.environ.get(name, "").strip()]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        bool(_MISSING),
        reason=f"Needs Google Workspace sandbox: set {', '.join(_MISSING)}",
    ),
]


async def test_service_account_resolves_the_fixture_users_groups() -> None:
    provider = OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(
            enrich_on_login=True, mode="api", service_account_json_env=SA_JSON_VAR
        ),
    )

    groups = await GoogleAdapter(provider).resolve_user_groups(os.environ[USER_VAR].strip())

    # The adapter fails open to [], so an empty answer is either no membership or a failed lookup.
    assert groups, "the fixture user resolved to no groups"
    assert all(group.id for group in groups)
