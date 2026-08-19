#  Project:      dfe-engine
#  File:         api/v1/e2e.py
#  Purpose:      Unauthenticated Playwright helpers, mounted only in e2e-server mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""e2e-server-only API group.

Mounted by ``create_app`` only when ``DFE_E2E_SERVER`` is on and the posture is
non-production. Never part of the published OpenAPI/CLI surface.

GET  /api/v1/e2e/status      → confirm the group is live
POST /api/v1/e2e/seed-admin  → create or reset a local admin (no auth)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.auth.bootstrap import ensure_admin_account

router = APIRouter(prefix="/e2e", tags=["E2E"], include_in_schema=False)


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


@router.get("/status", response_model=E2EStatusResponse, openapi_extra=CLI_HIDDEN)
async def e2e_status() -> E2EStatusResponse:
    """Confirm this process mounted the e2e-server helpers."""
    return E2EStatusResponse(enabled=True)


@router.post("/seed-admin", response_model=SeedAdminResponse, openapi_extra=CLI_HIDDEN)
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
