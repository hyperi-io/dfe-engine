#  Project:      dfe-engine
#  File:         gitcrud/__init__.py
#  Purpose:      Generic YAML-in-git CRUD engine - the Governed Ops foundation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Governed Ops: one generic git-native CRUD engine for every YAML resource class.

Every CRUD resource in DFE is YAML in git, so a single engine handles them all the
same way and RBAC is bound once at the class level. See docs/ARCHITECTURE.md
("Governed Ops") and docs/GITOPS-COMMIT-STANDARD.md.
"""

from .engine import GitCrud, ResourceNotFoundError, flatten
from .models import ResourceClass
from .registry import (
    ResourceClassRegistry,
    UnknownResourceClassError,
    default_registry,
)

__all__ = [
    "GitCrud",
    "ResourceClass",
    "ResourceClassRegistry",
    "ResourceNotFoundError",
    "UnknownResourceClassError",
    "default_registry",
    "flatten",
]
