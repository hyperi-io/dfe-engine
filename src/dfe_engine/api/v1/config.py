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
environment, gitops-driven. Public (no secrets) so it can load before auth.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/config", tags=["Client Config"])


class HyperDXConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class ClientConfig(BaseModel):
    api_base: str = ""  # same-origin by default
    hyperdx: HyperDXConfig = HyperDXConfig()
    auth_mode: str = "jwt"
    features: dict[str, bool] = {}


@router.get("/client", response_model=ClientConfig)
async def client_config(request: Request) -> ClientConfig:
    """Runtime config for the web UI (no secrets)."""
    settings = request.app.state.settings
    hyperdx = HyperDXConfig(
        enabled=bool(getattr(settings.hyperdx, "enabled", False)),
        url=str(getattr(settings.hyperdx, "base_url", "") or ""),
    )
    features = {
        "governed_ops": getattr(request.app.state, "gitcrud", None) is not None,
        "hunts": True,
    }
    auth_mode = "oidc" if getattr(settings.auth, "oidc_enabled", False) else "jwt"
    return ClientConfig(
        api_base="",
        hyperdx=hyperdx,
        auth_mode=auth_mode,
        features=features,
    )
