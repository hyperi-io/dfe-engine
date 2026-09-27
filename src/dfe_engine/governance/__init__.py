#  Project:      dfe-engine
#  File:         governance/__init__.py
#  Purpose:      Governed Ops Tier-2: defined actions + protected-var policies
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tier-2 of Governed Ops: curated ACTIONS (the big dials) + protected-var POLICIES.

Actions are named bundles of var changes over the generic git-CRUD engine, each
with its own RBAC handle; policies lock vars to defaults. Both are YAML in the
gitops governance class. See docs/architecture.md ("Governed Ops").
"""

from .actions import (
    ACTION_CLASS,
    ActionForbiddenError,
    ActionStore,
    CredentialInActionError,
    InvalidParamsError,
    resolve_params,
)
from .models import ActionDef, ParamSpec, ProtectedPolicy, VarChange, param_ref
from .policies import PolicyStore, ProtectedVarError

__all__ = [
    "ACTION_CLASS",
    "ActionDef",
    "ActionForbiddenError",
    "ActionStore",
    "CredentialInActionError",
    "InvalidParamsError",
    "ParamSpec",
    "PolicyStore",
    "ProtectedPolicy",
    "ProtectedVarError",
    "VarChange",
    "param_ref",
    "resolve_params",
]
