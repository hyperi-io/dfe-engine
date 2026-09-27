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

GET  /api/e2e/status       -> confirm the group is live
POST /api/e2e/seed-static  -> run a named seed script (e.g. seed_admin)
"""

import functools
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.api.deps import get_source_registry_optional
from dfe_engine.api.e2e.seed import Seed
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt.routing import RoutingNotApplicableError
from dfe_engine.source.flow import FlowError
from dfe_engine.source.registry import SourceValidationError

E2E_TAG = "E2E"
E2E_OPENAPI_TAG: dict[str, str] = {
    "name": E2E_TAG,
    "description": (
        "Playwright helpers for `make e2e-server`. Unauthenticated. "
        "Mounted and documented only in that mode -- not in the product spec."
    ),
}

# Hidden from the generated `dfe` CLI; empty security so Swagger Try-it-out
# does not demand a Bearer token for these unauthenticated routes.
_E2E_OPENAPI_EXTRA: dict[str, Any] = {**CLI_HIDDEN, "security": []}

router = APIRouter(prefix="/e2e", tags=[E2E_TAG])


class E2EStatusResponse(BaseModel):
    enabled: bool


class SeedRequest(BaseModel):
    script: Literal[
        "seed_setup_complete",
        "seed_organisation",
        "seed_dfe_admin_user",
        "seed_dfe_analyst_user",
        "seed_dfe_infra_user",
        "seed_dfe_viewers_user",
        "seed_source_with_transform",
        "seed_three_transforms",
        "seed_library_artefact",
        "seed_app_scaling_state",
        "reset_all",
    ]


class SeedResponse(BaseModel):
    success: bool
    message: str


@router.get("/status", response_model=E2EStatusResponse, openapi_extra=_E2E_OPENAPI_EXTRA)
async def e2e_status() -> E2EStatusResponse:
    """Confirm this process mounted the e2e-server helpers."""
    return E2EStatusResponse(enabled=True)


@router.post(
    "/seed-static",
    response_model=SeedResponse,
    openapi_extra=_E2E_OPENAPI_EXTRA,
    dependencies=[WRITE_TURN],
)
async def seed_static(body: SeedRequest, request: Request) -> SeedResponse:
    """Run a named e2e seed script. Unauthenticated by design.

    The script runs on a worker thread: it writes the deploy repo over the network,
    and on the event loop every other request this process serves would wait for it.
    It takes the same write turn as the product's own writes, so a seed never runs
    beside one.
    """
    seeder = Seed(
        account_store=request.app.state.account_store,
        group_store=request.app.state.group_store,
        org_registry=request.app.state.org_registry,
        env=request.app.state.settings.env,
        gitcrud=getattr(request.app.state, "gitcrud", None),
        source_registry=get_source_registry_optional(),
        settings=request.app.state.settings,
        forge=getattr(request.app.state, "forge", None),
        metrics=getattr(request.app.state, "seed_metrics", None),
    )
    try:
        success = await run_blocking(functools.partial(seeder.seed_static, body.script))
    except (SourceValidationError, FlowError, RoutingNotApplicableError) as exc:
        # A seed the engine refused and a seed that crashed are both 500 without
        # this, and the Playwright suite runs against this endpoint. Narrow on
        # purpose: SourceRegistryError's other subclasses are server faults, and
        # answering those 422 would call a misconfiguration the caller's fault.
        # The two conflict subclasses answer 409 on the product routes and 422
        # here: on a seed endpoint a collision is the script's own doing, and the
        # reason in the body matters more than the code.
        # RoutingNotApplicableError is in the tuple because a FlowError never
        # reaches here -- routing.py:164 wraps it, so catching FlowError alone
        # left the refusal this endpoint exists to report answering 500.
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    logger.warning("e2e seed", script=body.script, success=success)
    message = f"Seed {body.script} successful" if success else f"Unknown seed script: {body.script}"
    return SeedResponse(success=success, message=message)
