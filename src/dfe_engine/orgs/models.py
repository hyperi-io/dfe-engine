#  Project:      dfe-engine
#  File:         orgs/models.py
#  Purpose:      Pydantic model for customer organisations
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
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
        enabled: Whether this org is active.
        created_at: ISO 8601 timestamp of creation.
        updated_at: ISO 8601 timestamp of last update.
    """

    name: str
    display_name: str = ""
    org_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
