#  Project:      dfe-engine
#  File:         api/v1/e2e/__init__.py
#  Purpose:      Unauthenticated Playwright helpers, mounted only in e2e-server mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""e2e-server-only API group.

Mounted by ``create_app`` only when ``DFE_E2E_SERVER`` is on and the posture is
non-production. In that process, ``/docs`` has a spec dropdown: **API**
(``/openapi.json``) vs **E2E** (``/openapi-e2e.json``). The committed OpenAPI
spec (generated without the flag) never includes them.

GET  /api/v1/e2e/status      → confirm the group is live
POST /api/v1/e2e/seed-admin  → create or reset a local admin (no auth)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.auth.bootstrap import ensure_admin_account

E2E_TAG = "E2E"
E2E_OPENAPI_TAG: dict[str, str] = {
    "name": E2E_TAG,
    "description": (
        "Playwright helpers for `make e2e-server`. Unauthenticated. "
        "Mounted and documented only in that mode — not in the committed spec."
    ),
}

# Hidden from the generated `dfe` CLI; empty security so Swagger Try-it-out
# does not demand a Bearer token for these unauthenticated routes.
_E2E_OPENAPI_EXTRA: dict[str, Any] = {**CLI_HIDDEN, "security": []}

router = APIRouter(prefix="/e2e", tags=[E2E_TAG])


class E2EStatusResponse(BaseModel):
    enabled: bool


class SeedAdminRequest(BaseModel):
    username: str = Field(default="admin", description="Local admin username to seed")
    password: str = Field(
        default="changeme", description="Plaintext password (hashed before storage)"
    )


class SeedAdminResponse(BaseModel):
    username: str
    created: bool


@router.get("/status", response_model=E2EStatusResponse, openapi_extra=_E2E_OPENAPI_EXTRA)
async def e2e_status() -> E2EStatusResponse:
    """Confirm this process mounted the e2e-server helpers."""
    return E2EStatusResponse(enabled=True)


@router.post("/seed-admin", response_model=SeedAdminResponse, openapi_extra=_E2E_OPENAPI_EXTRA)
async def seed_admin(body: SeedAdminRequest, request: Request) -> SeedAdminResponse:
    """Create or reset a local admin in dfe-admins. Unauthenticated by design."""
    created = ensure_admin_account(
        request.app.state.account_store,
        request.app.state.group_store,
        name=body.username,
        password=body.password,
    )
    logger.warning("e2e seed-admin", username=body.username, created=created)
    return SeedAdminResponse(username=body.username, created=created)
