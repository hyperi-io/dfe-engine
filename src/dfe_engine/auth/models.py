"""Authentication and authorization models.

AuthContext is the canonical identity model for DFE — extracted from JWT
claims by dfe-control-plane and passed into engine for Cedar evaluation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AuthenticationError(Exception):
    """Authentication failed (bad credentials, disabled account, etc.)."""


class AuthorizationError(Exception):
    """User not authorized for the requested action."""


class AuthContext(BaseModel):
    """Authentication context extracted from JWT.

    In production, populated from Envoy OIDC headers by dfe-control-plane.
    In dev/test, omitted entirely (engine operates as root).

    These values are injected as reserved parameters (_org_id, etc.)
    and cannot be overridden by clients.
    """

    org_id: str = Field(..., description="Tenant organization ID")
    user_id: str = Field(..., description="User ID")
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list, description="OIDC groups (Entra ID, Okta)")
    request_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None


class AuthzRequest(BaseModel):
    """A single authorization request for batch evaluation."""

    principal: str = Field(..., description='Cedar principal (e.g. User::"alice")')
    action: str = Field(..., description='Cedar action (e.g. Action::"config:read")')
    resource: str = Field(
        ..., description='Cedar resource (e.g. ServiceConfig::"receiver-production")'
    )
    context: dict[str, Any] = Field(default_factory=dict)


class AuthzResult(BaseModel):
    """Result of an authorization evaluation."""

    allowed: bool
    reason: str = ""
