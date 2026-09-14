#  Project:      dfe-engine
#  File:         api/v1/app_contracts.py
#  Purpose:      Serve each app's own container contract to the console
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The contract a pinned app image emitted for itself.

GET /api/v1/app-contracts/{service}    the app's config schema and capabilities

One contract per SERVICE rather than per instance: the contract is a property of
the image, so every instance of an app shares it. What one instance has SET lives
on the instance's own route, ``/apps/{service}/{instance}/config``.

Reading it is ``deployment:read`` - it says what an app can be asked to do, which
is the same class of fact as what is deployed, and carries no instance's values.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.appmgmt import UnknownAppError, catalogue, contract
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/app-contracts", tags=["App Management"])

_DEPLOY_READ = Depends(require_action(scopes_dict["deployment_read"]))


class AppContractResponse(BaseModel):
    """One app's contract, as the pinned image wrote it."""

    model_config = ConfigDict(populate_by_name=True)

    service: str
    available: bool = Field(
        description="False when this deployment has mounted no contract for the app"
    )
    source: str = Field(description="Where the contract came from: `mount` or `absent`")
    pinned_ref: str | None = Field(
        default=None,
        description="The image digest that emitted it, when the emit step recorded one",
    )
    schema_version: str | None = Field(
        default=None, description="The JSON Schema dialect the app emitted"
    )
    # `schema` is a deprecated method on pydantic's BaseModel, so the attribute is
    # named around it and the wire name comes from the alias.
    config_schema: dict[str, Any] = Field(
        default_factory=dict, alias="schema", description="The app's own config schema"
    )
    capabilities: list[Any] = Field(
        default_factory=list, description="The sources, sinks and transforms the app ships"
    )
    dials: list[Any] = Field(
        default_factory=list,
        description="Which options an operator is steered towards; not resolved yet",
    )


def read_contract(service: str) -> contract.AppContract:
    """The mounted contract, with an unknown app and a broken mount told apart."""
    try:
        catalogue.descriptor(service)
    except UnknownAppError as exc:
        raise HTTPException(
            404, detail={"code": "unknown_app", "message": f"no such app: {service}"}
        ) from exc
    try:
        return contract.contract(service)
    except contract.ContractError as exc:
        raise HTTPException(
            500, detail={"code": "contract_unreadable", "message": str(exc)}
        ) from exc


@router.get("/{service}", dependencies=[_DEPLOY_READ])
async def get_app_contract(service: str, user: CurrentUser) -> AppContractResponse:
    """One app's config schema and capability catalogue, or that none is mounted."""
    found = read_contract(service)
    return AppContractResponse(
        service=found.service,
        available=found.available,
        source=str(found.source),
        pinned_ref=found.pinned_ref,
        schema_version=found.schema_version,
        config_schema=found.schema,
        capabilities=found.capabilities,
        dials=[],
    )
