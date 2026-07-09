#  Project:      dfe-engine
#  File:         gitops/__init__.py
#  Purpose:      Deploy-specific gitops render+commit bridge
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Deploy-specific gitops render+commit bridge.

dfe-engine authors declarative artifacts and commits them to the deploy repo;
Argo CD applies them. The engine needs only git-write, never kubectl.
"""

from dfe_engine.gitops.artifacts import collect_deploy_artifacts
from dfe_engine.gitops.oidc import (
    ENVOY_OIDC_VALUES_PATH,
    build_oidc_providers,
    render_envoy_oidc_values,
)
from dfe_engine.gitops.repo import GitopsRepo, PublishResult

__all__ = [
    "ENVOY_OIDC_VALUES_PATH",
    "GitopsRepo",
    "PublishResult",
    "build_oidc_providers",
    "collect_deploy_artifacts",
    "render_envoy_oidc_values",
]
