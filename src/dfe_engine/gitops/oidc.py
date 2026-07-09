#  Project:      dfe-engine
#  File:         gitops/oidc.py
#  Purpose:      Render Envoy OIDC values for the deploy repo (engine = OIDC SSoT)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Render the Envoy ``oidc-values.yaml`` the deploy repo serves to the
envoy-gateway-config chart.

dfe-engine is the OIDC SSoT: it owns the providers (auth/oidc registry) and
renders the non-secret Envoy values (enabled flag, providers, jwtAuthn issuer)
into the deploy repo. The client secret itself is materialised by ESO from Vault
(``dfe-oidc-<name>`` Secret), referenced here by name only.
"""

from __future__ import annotations

import os
from typing import Any

from dfe_engine.yaml_utils import yaml_dump_string

ENVOY_OIDC_VALUES_PATH = "envoy/oidc-values.yaml"


def render_envoy_oidc_values(
    providers: list[dict[str, str]],
    *,
    secret_store_name: str = "dfe-secret-store",  # noqa: S107 -- ESO store name, not a secret
) -> str:
    """Render the Envoy OIDC values YAML.

    Args:
        providers: ``[{"name", "issuer", "client_id"}]`` (non-secret).
        secret_store_name: ESO ClusterSecretStore the chart pulls secrets from.

    Returns:
        YAML content for ``envoy/oidc-values.yaml`` in the deploy repo.
    """
    enabled = bool(providers)
    jwt_authn: dict[str, Any] = {"enabled": enabled}
    if providers:
        # EnvoyPatchPolicy supports a single issuer; use the first provider's.
        jwt_authn["issuer"] = providers[0].get("issuer", "")
    values: dict[str, Any] = {
        "oidc": {
            "enabled": enabled,
            "secretStoreName": secret_store_name,
            "providers": [
                {
                    "name": p["name"],
                    "issuerUrl": p.get("issuer", ""),
                    "clientId": p.get("client_id", ""),
                    "clientSecretName": f"dfe-oidc-{p['name']}",
                    "clientSecretKey": "client-secret",
                }
                for p in providers
            ],
        },
        "jwtAuthn": jwt_authn,
    }
    return yaml_dump_string(values)


def build_oidc_providers(registry: Any) -> list[dict[str, str]]:
    """Read enabled providers from an OIDCProviderRegistry into render dicts.

    The literal client ID (non-secret) is resolved from the provider's
    ``client_id_env`` at render time; empty if that env var is unset.
    """
    providers: list[dict[str, str]] = []
    for name, prov in registry.list():
        if not getattr(prov, "enabled", True):
            continue
        providers.append(
            {
                "name": name,
                "issuer": getattr(prov, "issuer", ""),
                "client_id": os.environ.get(getattr(prov, "client_id_env", ""), ""),
            }
        )
    return providers
