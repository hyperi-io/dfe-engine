#  Project:      dfe-engine
#  File:         sigma/providers/__init__.py
#  Purpose:      Public surface for the pluggable Sigma providers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pluggable Sigma rule providers - see base.SigmaProvider."""

from __future__ import annotations

from .base import (
    AuthKind,
    ProviderAuth,
    ProviderConfig,
    ProviderKind,
    SigmaProvider,
    SigmaRuleDoc,
    build_provider,
    docs_from_collection,
    modified_since,
    parse_sigma_dicts,
    parse_sigma_yaml,
)
from .git_repo import GitRepoProvider
from .local_files import LocalFilesProvider
from .valhalla import ValhallaProvider

__all__ = [
    "AuthKind",
    "GitRepoProvider",
    "LocalFilesProvider",
    "ProviderAuth",
    "ProviderConfig",
    "ProviderKind",
    "SigmaProvider",
    "SigmaRuleDoc",
    "ValhallaProvider",
    "build_provider",
    "docs_from_collection",
    "modified_since",
    "parse_sigma_dicts",
    "parse_sigma_yaml",
]
