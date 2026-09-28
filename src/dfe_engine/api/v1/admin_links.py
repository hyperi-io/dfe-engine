#  Project:      dfe-engine
#  File:         api/v1/admin_links.py
#  Purpose:      The admin UIs this deployment runs, for the console's admin panel
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deployment router -- the admin UIs this deployment runs.

GET /api/v1/deployment/admin-links -> Each admin UI the deployer listed, with whether it answers
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from dfe_engine.admin_links import AdminLinks, AdminLinkStatus
from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/deployment", tags=["Deployment"])


class AdminLinkResponse(BaseModel):
    """One admin UI a browser can open, and whether it answered its probe."""

    name: str = Field(description="What the UI is, as the deployer named it, e.g. Argo CD.")
    purpose: str = Field(description="What an admin opens it for, in one line.")
    url: str = Field(description="Where a browser reaches it.")
    status: AdminLinkStatus = Field(
        description=(
            "up when its probe got an HTTP answer below 500, a login redirect "
            "included; down on a 5xx, a timeout or no connection; unknown when the "
            "deployer gave no address to probe."
        )
    )


@router.get(
    "/admin-links",
    response_model=list[AdminLinkResponse],
    dependencies=[Depends(require_action(scopes_dict["deployment_admin_links_read"]))],
)
async def list_admin_links(user: CurrentUser, request: Request) -> list[AdminLinkResponse]:
    """Each admin UI this deployment runs, in the order its deployer listed them.

    The deployer supplies the list, so an empty one means it listed none. Gated to
    the admin-class roles, which hold ``deployment:*`` or ``*``: the list maps the
    deployment's consoles. Links only; nothing here carries a credential.
    """
    links: AdminLinks = request.app.state.admin_links
    return [
        AdminLinkResponse(name=link.name, purpose=link.purpose, url=link.url, status=status)
        for link, status in await links.statuses()
    ]
