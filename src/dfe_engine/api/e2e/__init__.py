#  Project:      dfe-engine
#  File:         api/e2e/__init__.py
#  Purpose:      Unauthenticated Playwright helpers, mounted only in e2e-server mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""e2e-server-only API group.

Mounted by ``create_app`` only when ``DFE_E2E_SERVER`` is on and the posture is
non-production. Lives under ``/api/e2e`` (not the versioned product API). In
that process, ``/docs`` has a spec dropdown: **API** (``/openapi.json``) vs
**E2E** (``/openapi.e2e.json``). The committed product spec never includes
them; ``openapi-spec/openapi.e2e.json`` is generated alongside it.

GET  /api/e2e/status       → confirm the group is live
POST /api/e2e/seed-static  → run a named seed script (e.g. seed_admin)
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.api.e2e.seed import Seed

E2E_TAG = "E2E"
E2E_OPENAPI_TAG: dict[str, str] = {
    "name": E2E_TAG,
    "description": (
        "Playwright helpers for `make e2e-server`. Unauthenticated. "
        "Mounted and documented only in that mode — not in the product spec."
    ),
}

# Hidden from the generated `dfe` CLI; empty security so Swagger Try-it-out
# does not demand a Bearer token for these unauthenticated routes.
_E2E_OPENAPI_EXTRA: dict[str, Any] = {**CLI_HIDDEN, "security": []}

router = APIRouter(prefix="/e2e", tags=[E2E_TAG])


class E2EStatusResponse(BaseModel):
    enabled: bool


class SeedRequest(BaseModel):
    script: Literal["seed_admin", "seed_setup_complete"]


class SeedResponse(BaseModel):
    success: bool
    message: str


@router.get("/status", response_model=E2EStatusResponse, openapi_extra=_E2E_OPENAPI_EXTRA)
async def e2e_status() -> E2EStatusResponse:
    """Confirm this process mounted the e2e-server helpers."""
    return E2EStatusResponse(enabled=True)


@router.post("/seed-static", response_model=SeedResponse, openapi_extra=_E2E_OPENAPI_EXTRA)
async def seed_static(body: SeedRequest, request: Request) -> SeedResponse:
    """Run a named e2e seed script. Unauthenticated by design."""
    seeder = Seed(
        account_store=request.app.state.account_store,
        group_store=request.app.state.group_store,
        org_registry=request.app.state.org_registry,
        env=request.app.state.settings.env,
    )
    success = seeder.seed_static(body.script)
    logger.warning("e2e seed", script=body.script, success=success)
    message = "Seed successful" if success else f"Unknown seed script: {body.script}"
    return SeedResponse(success=success, message=message)
