#  Project:      dfe-engine
#  File:         auth/setup_status.py
#  Purpose:      Detect whether first-run / initial setup is still required
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Initial setup detection for the control-plane UI.

The UI calls this before login to decide whether to show the first-run wizard
(change break-glass admin password, create organisations, configure OIDC, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dfe_engine.auth.bootstrap import _DEFAULT_PASSWORD

if TYPE_CHECKING:
    from fastapi import Request

# Stable step ids returned in pending_steps / completed_steps.
STEP_ADMIN_PASSWORD = "admin_password"
STEP_ORGANISATIONS = "organisations"
STEP_OIDC_PROVIDER = "oidc_provider"


class InitialSetupStatus(BaseModel):
    """Whether the deployment still needs first-run configuration."""

    setup_complete: bool = Field(
        description="True when mandatory first-run steps appear done.",
    )
    initial_setup_required: bool = Field(
        description="True when the UI should show the initial setup flow.",
    )
    pending_steps: list[str] = Field(
        default_factory=list,
        description="Setup step ids still required (subset of evaluated steps).",
    )
    completed_steps: list[str] = Field(
        default_factory=list,
        description="Setup step ids already satisfied (subset of evaluated steps).",
    )


def evaluate_initial_setup(request: Request) -> InitialSetupStatus:
    """Return setup status from bootstrapped app state (no authentication)."""
    settings = request.app.state.settings
    checks: list[tuple[str, bool]] = []

    org_registry = getattr(request.app.state, "org_registry", None)
    if org_registry is not None:
        checks.append((STEP_ORGANISATIONS, len(org_registry.list()) > 0))

    if settings.auth.enabled:
        if settings.auth.local.enabled:
            account_store = getattr(request.app.state, "account_store", None)
            admin_ok = False
            if account_store is not None and account_store.get("admin") is not None:
                admin_ok = not account_store.verify_password("admin", _DEFAULT_PASSWORD)
            checks.append((STEP_ADMIN_PASSWORD, admin_ok))
        else:
            oidc_registry = getattr(request.app.state, "oidc_provider_registry", None)
            oidc_ok = False
            if oidc_registry is not None:
                oidc_ok = any(p.enabled for _, p in oidc_registry.list())
            checks.append((STEP_OIDC_PROVIDER, oidc_ok))

    completed = [step_id for step_id, done in checks if done]
    pending = [step_id for step_id, done in checks if not done]

    return InitialSetupStatus(
        setup_complete=not pending,
        initial_setup_required=bool(pending),
        pending_steps=pending,
        completed_steps=completed,
    )
