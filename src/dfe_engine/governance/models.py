#  Project:      dfe-engine
#  File:         governance/models.py
#  Purpose:      Models for Governed Ops defined actions + protected-var policies
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pydantic models for defined actions and protected-var policies (YAML in gitops)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VarChange(BaseModel):
    """One var mutation in an action: set ``cls/name`` dot-``path`` to ``value``."""

    cls: str
    name: str
    path: str
    value: Any


class ActionDef(BaseModel):
    """A curated, RBAC-gated bundle of var changes (a "big dial").

    Invoking the action applies ALL ``changes`` in ONE commit, gated on
    ``required_action`` (an RBAC string checked at invoke time).
    """

    name: str
    description: str = ""
    required_action: str
    changes: list[VarChange] = Field(default_factory=list)


class ProtectedPolicy(BaseModel):
    """Vars locked to default. Patterns match ``cls:name:path`` via fnmatch.

    e.g. ``helmvars:*:replicaCount`` or ``helmvars:receiver-default:config.kafka.*``.
    A protected var can only be changed by a caller holding the override grant.
    """

    name: str
    description: str = ""
    protected: list[str] = Field(default_factory=list)
