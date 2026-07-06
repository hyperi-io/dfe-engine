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

from pydantic import BaseModel, Field


class Org(BaseModel):
    """A customer organisation.

    Attributes:
        name: Primary identifier (filename stem).
        display_name: Human-readable label for UI display.
        org_ids: Tenant IDs used for ClickHouse row-level security filters.
        domains: Email domains this org claims (e.g. ['acme.com']). SEPARATE from
            org_ids - these are the login-email domains that map an external OIDC
            user to this org (domain -> org_ids -> tenant scope), NOT tenant IDs.
            An org may own several domains. Normalised to lowercase by OrgRegistry
            on create/update.
        enabled: Whether this org is active.
        hyperdx_team_id: HyperDX team ID for this org's connection sync.
        hyperdx_team_api_key_env: Environment variable name holding the org's HyperDX team API key.
        hyperdx_connection_id: HyperDX ClickHouse-connection ID for this org's
            per-org tenant connection. Under the GA posture every org shares ONE
            team but has its OWN connection - this id lets delete_org drop just
            that connection without touching the shared team.
        ch_password_env: Environment variable name holding the ClickHouse password.
        created_at: ISO 8601 timestamp of creation.
        updated_at: ISO 8601 timestamp of last update.
    """

    name: str
    display_name: str = ""
    org_ids: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    enabled: bool = True
    hyperdx_team_id: str = ""
    hyperdx_team_api_key_env: str = ""
    hyperdx_connection_id: str = ""
    ch_password_env: str = ""
    created_at: str = ""
    updated_at: str = ""
