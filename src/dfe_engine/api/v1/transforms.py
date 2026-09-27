# Project:   dfe-engine
# File:      src/dfe_engine/api/v1/transforms.py
# Purpose:   WASM transform compile and test endpoints
# Language:  Python
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""WASM transform compilation and test endpoints.

POST /api/v1/transforms/compile  -> Compile source code to WASM via compiler service
POST /api/v1/transforms/test     -> Run a compiled WASM against sample records
"""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from scalo.crypto import ssl_context

from dfe_engine.api.deps import CurrentUser, Settings, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/transforms", tags=["Transforms"])

Language = Literal["rust", "go", "assemblyscript"]


# -- Request / response models ---------------------------------


class CompileRequest(BaseModel):
    language: Language
    files: dict[str, str] = Field(
        description="Source files. Key = filename, value = source content.",
    )


class CompileResponse(BaseModel):
    wasm_base64: str
    wasm_bytes: int


class SampleRecord(BaseModel):
    key: str = ""
    value: str
    timestamp: int = 0
    headers: dict[str, str] = Field(default_factory=dict)


class TestRequest(BaseModel):
    wasm_base64: str = Field(description="Base64-encoded WASM binary from /compile")
    records: list[SampleRecord] = Field(description="Sample input records")


class EmittedRecord(BaseModel):
    key: str
    value: str
    headers: dict[str, str] = Field(default_factory=dict)


class TestResponse(BaseModel):
    emitted: list[EmittedRecord]
    duration_ms: float
    wasm_memory_bytes: int


# -- Endpoints -------------------------------------------------


@router.post(
    "/compile",
    response_model=CompileResponse,
    dependencies=[Depends(require_action(scopes_dict["transform_compile"]))],
)
async def compile_transform(
    request: CompileRequest,
    settings: Settings,
    user: CurrentUser,
) -> CompileResponse:
    """Compile user source code to a WASM binary.

    Proxies to the dfe-transform-compiler service. The response includes
    a base64-encoded WASM binary that can be passed directly to /test.
    """
    compiler_url = settings.services.transform_wasm_compiler_url

    # Bounded proxy to the WASM compiler. The timeout is raw-httpx here (a long-but-
    # bounded compile) and MUST stay UNDER the pytest backstop (300s) so a hung
    # compiler service fails a test FAST rather than stalling it -- the testing
    # standard's wait-bound rule (raw httpx does not get scalo's stamina test-mode).
    async with httpx.AsyncClient(timeout=120.0, verify=ssl_context()) as client:
        try:
            resp = await client.post(
                f"{compiler_url}/compile",
                json={"language": request.language, "files": request.files},
            )
        except httpx.ConnectError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "COMPILER_UNAVAILABLE",
                    "message": "Compilation service is unavailable",
                },
            ) from exc

    if resp.status_code == 422:
        body = resp.json()
        raise HTTPException(status_code=422, detail=body.get("error", body))

    if not resp.is_success:
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        raise HTTPException(
            status_code=resp.status_code,
            detail=body.get(
                "error", {"code": "COMPILATION_FAILED", "message": "Compilation failed"}
            ),
        )

    data = resp.json()
    audit_resource_change(user.user_id, "transform", request.language, "compiled")
    return CompileResponse(
        wasm_base64=data["wasm_base64"],
        wasm_bytes=data["wasm_bytes"],
    )


@router.post(
    "/test",
    response_model=TestResponse,
    dependencies=[Depends(require_action(scopes_dict["transform_test"]))],
)
async def test_transform(
    request: TestRequest,
    settings: Settings,
    user: CurrentUser,
) -> TestResponse:
    """Run a compiled WASM transform against sample records.

    Proxies to the dfe-transform-wasm service's /test endpoint.
    The wasm_base64 field should come directly from /compile.
    """
    wasm_url = settings.services.transform_wasm_url

    # Bounded proxy to the transform host (60s, well under the 300s pytest backstop).
    async with httpx.AsyncClient(timeout=60.0, verify=ssl_context()) as client:
        try:
            resp = await client.post(
                f"{wasm_url}/test",
                json={
                    "wasm_base64": request.wasm_base64,
                    "records": [r.model_dump() for r in request.records],
                    # JSON is the only payload format the engine emits or accepts.
                    "source_format": "json",
                    "sink_format": "json",
                },
            )
        except httpx.ConnectError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "TRANSFORM_HOST_UNAVAILABLE",
                    "message": "Transform host is unavailable",
                },
            ) from exc

    if not resp.is_success:
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        raise HTTPException(
            status_code=resp.status_code,
            detail=body.get("error", {"code": "TEST_FAILED", "message": "Transform test failed"}),
        )

    data = resp.json()
    audit_resource_change(user.user_id, "transform", "wasm", "tested")
    return TestResponse(
        emitted=data["emitted"],
        duration_ms=data["duration_ms"],
        wasm_memory_bytes=data["wasm_memory_bytes"],
    )
