#  Project:      dfe-engine
#  File:         orgs/models.py
#  Purpose:      Pydantic model for customer organisations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Org model for customer organisation management.

Each org is a YAML file in the orgs directory.  The ``name`` field is
the filename stem and is NOT stored inside the YAML body (same pattern
as AccountStore).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# The name is a filename stem, so it may not carry a path separator or start with a dot.
ORG_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

# Only what breaks the tenant fence is refused: empty, a comma, a control char, edge space.
TENANT_ID_PATTERN = r"^[^\s,\x00-\x1f\x7f](?:[^,\x00-\x1f\x7f]*[^\s,\x00-\x1f\x7f])?$"

TenantId = Annotated[str, StringConstraints(pattern=TENANT_ID_PATTERN)]


class Org(BaseModel):
    """A customer organisation.

    Attributes:
        name: Primary identifier (filename stem).
        display_name: Human-readable label for UI display.
        org_ids: Tenant IDs used for ClickHouse row-level security filters.
        enabled: Whether this org is active.
        hyperdx_team_id: HyperDX team ID for this org's connection sync.
        hyperdx_team_api_key_env: Environment variable name holding the org's HyperDX team API key.
        ch_password_env: Environment variable name holding the ClickHouse password.
        created_at: ISO 8601 timestamp of creation.
        updated_at: ISO 8601 timestamp of last update.
    """

    name: str
    display_name: str = ""
    org_ids: list[TenantId] = Field(default_factory=list)
    enabled: bool = True
    hyperdx_team_id: str = ""
    hyperdx_team_api_key_env: str = ""
    ch_password_env: str = ""
    created_at: str = ""
    updated_at: str = ""
