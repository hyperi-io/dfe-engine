#  Project:      dfe-engine
#  File:         env_refs.py
#  Purpose:      Resolve a config-declared env-var NAME to its runtime value
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve an ``*_env`` config reference to its runtime value.

DFE config never stores a secret inline; it stores the NAME of an environment
variable that holds it (``password_env = "DFE_CH_PW"``, ``client_secret_env``,
``api_key_env``, ...), and ESO / compose fills that env at runtime. The
name->value read was reinvented across the connection registry, the HyperDX
client, the OIDC adapters, and the gitops OIDC renderer in three slightly
different defensive styles; this is the single resolver they share.

Longer-term the secret-bearing refs should route through ``DfeSecrets.get`` (a
secret PATH, not an env name) the way the sigma provider already does - see
[[project_secrets_architecture]]. This helper is the interim single seam.
"""

from __future__ import annotations

import os


def resolve_env_ref(env_var_name: str | None, default: str | None = "") -> str | None:
    """Return the value of the env var NAMED by ``env_var_name`` (config-held).

    Returns ``default`` when the name is empty/None (nothing configured) OR when
    the named variable is unset. Pass ``default=None`` for the callers that treat
    "unconfigured" and "empty" alike as a hard None (the OIDC adapters); the
    default ``""`` suits the callers that want a plain empty string.
    """
    if not env_var_name:
        return default
    return os.environ.get(env_var_name, default)
