#  Project:      dfe-engine
#  File:         auth/oidc/__init__.py
#  Purpose:      OIDC provider registry package exports
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider

__all__ = [
    "GroupInfo",
    "GroupResolutionConfig",
    "OIDCProvider",
    "OIDCProviderRegistry",
]


def __getattr__(name: str) -> object:
    if name == "OIDCProviderRegistry":
        from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

        return OIDCProviderRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
