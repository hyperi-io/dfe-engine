#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/pipeline.py
#  Purpose:      REST API for Vector pipeline template management and generation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pipeline router -- Vector pipeline template listing and generation.

Wraps ``PipelineBuilderController`` for REST access to pipeline template
management. A build is a long-running operation (202 and the operation to poll).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from scalo.api import ACCEPTED_RESPONSES, ListParams, Page, paginate

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.task_manager import TaskManager, TaskOperation, accept_task
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


# -- Response models -----------------------------------------


class PipelineTemplate(BaseModel):
    """A discovered pipeline template."""

    type: str = Field(description="Template type (core, custom)")
    name: str = Field(description="Template filename")


class PipelineBuildRequest(BaseModel):
    """Request to build pipeline manifests."""

    config_path: str | None = Field(
        default=None,
        description="Path to dfe_package.yaml (uses default if not provided)",
    )
    output_path: str | None = Field(
        default=None,
        description="Output directory for rendered manifests",
    )
    build_core: bool = Field(
        default=False,
        description="Build core templates (vs custom only)",
    )


# -- Dependencies --------------------------------------------


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


# -- Endpoints -----------------------------------------------


@router.get(
    "/templates",
    response_model=Page[PipelineTemplate],
    dependencies=[Depends(require_action(scopes_dict["pipeline_read"]))],
)
async def list_templates(
    query: ListParams,
    user: CurrentUser,
    _auth: None = Depends(require_action(scopes_dict["pipeline_read"])),
) -> Page[PipelineTemplate]:
    """List available Vector pipeline templates."""
    from dfe_engine.pipeline.pipeline_controller import PipelineBuilderController

    try:
        result = PipelineBuilderController.list_ingestion_templates(
            args_dfe_package_file_path="",
            args_log_path="",
            args_download=False,
        )
    except Exception:
        return paginate([], query)

    templates: list[PipelineTemplate] = []
    types = result.get("type", [])
    names = result.get("templates", [])
    for ttype, name in zip(types, names, strict=False):
        templates.append(PipelineTemplate(type=ttype, name=name))
    return paginate(templates, query)


@router.post(
    "/build",
    response_model=TaskOperation,
    status_code=202,
    responses=ACCEPTED_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["pipeline_write"]))],
)
async def build_pipeline(
    body: PipelineBuildRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    _auth: None = Depends(require_action(scopes_dict["pipeline_write"])),
) -> TaskOperation:
    """Start pipeline manifest generation, answering the operation to poll."""
    manager = _get_task_manager(request)
    task_info = manager.submit(
        "pipeline:build",
        _build_pipeline,
        body.config_path,
        body.output_path,
        body.build_core,
    )
    audit_resource_change(user.user_id, "pipeline", "all", "executed")
    return accept_task(response, task_info)


async def _build_pipeline(
    config_path: str | None,
    output_path: str | None,
    build_core: bool,
    *,
    task,
) -> dict:
    """Run pipeline build. Called by TaskManager."""
    import asyncio

    from dfe_engine.pipeline.pipeline_controller import PipelineBuilderController

    task.set_progress(10, "Loading pipeline configuration")

    def _do_build():
        PipelineBuilderController.build_ingestion_pipelines(
            args_dfe_package_file_path=config_path or "",
            args_ingestion_output_path=output_path,
            args_log_path="",
            args_build_core=build_core,
        )

    task.set_progress(30, "Building pipeline manifests")
    await asyncio.to_thread(_do_build)
    task.set_progress(90, "Pipeline build complete")

    return {"status": "built", "output_path": output_path or "default"}
