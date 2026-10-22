#  Project:      dfe-engine
#  File:         api/v1/config.py
#  Purpose:      Runtime client-config bootstrap for dfe-ui (one image, every env)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""GET /api/v1/config/client - runtime config the UI fetches on startup.

Fixes the Next.js build-time NEXT_PUBLIC_* trap: instead of baking the API/HyperDX
URLs into the image, the UI reads them at runtime here -> one image, every
environment, gitops-driven. Any authenticated caller may read it; an anonymous
request gets 401.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.api.deps import CurrentUser
from dfe_engine.api.v1.oidc_login import current_relying_party

router = APIRouter(prefix="/config", tags=["Client Config"])


class HyperDXConfig(BaseModel):
    """Whether the UI links to HyperDX, and where."""

    enabled: bool = False
    url: str = ""


class ClientConfig(BaseModel):
    """Settings the UI reads when it starts."""

    api_base: str = ""  # same-origin by default
    hyperdx: HyperDXConfig = HyperDXConfig()
    auth_mode: str = Field(
        default="jwt",
        description=(
            "'oidc' when an enabled OIDC provider is registered, 'jwt' otherwise; "
            "shown on the System Management page. Local login stays available in "
            "both."
        ),
    )
    features: dict[str, bool] = {}


async def _oidc_available(request: Request) -> bool:
    """True when a login through an enabled OIDC provider would be served.

    Reads the same two places ``_rp_or_404`` does, in the same order: the relying
    party, built again first where another replica has changed the providers, then
    the provider registry for one written out of band since the RP was built. The
    registry read runs on a worker thread.
    """
    rp = await current_relying_party(request)
    if rp is not None and rp.provider_names():
        return True
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    if registry is None:
        return False
    try:
        listed = await run_blocking(registry.list)
    except Exception as exc:  # malformed YAML on disk must not break the UI bootstrap
        logger.warning("OIDC provider registry read failed", error=str(exc))
        return False
    return any(provider.enabled for _, provider in listed)


@router.get("/client", response_model=ClientConfig, openapi_extra=CLI_HIDDEN)
async def client_config(user: CurrentUser, request: Request) -> ClientConfig:
    """Runtime config for the web UI.

    Authenticated but ungated: every console pane may read it, and the HyperDX URL
    it returns is internal to the deployment.
    """
    settings = request.app.state.settings
    hyperdx = HyperDXConfig(
        enabled=bool(getattr(settings.hyperdx, "enabled", False)),
        url=str(getattr(settings.hyperdx, "base_url", "") or ""),
    )
    features = {
        "governed_ops": getattr(request.app.state, "gitcrud", None) is not None,
        "hunts": True,
    }
    # Reported from the live provider registry, not a settings flag: an enabled
    # provider is the only thing that makes the SSO button work.
    auth_mode = "oidc" if await _oidc_available(request) else "jwt"
    return ClientConfig(
        api_base="",
        hyperdx=hyperdx,
        auth_mode=auth_mode,
        features=features,
    )
