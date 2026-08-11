#  Project:      dfe-engine
#  File:         auth/oidc/credential_env.py
#  Purpose:      Helpers for OIDC credential env-var indirection
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import os
import re

_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def credential_env_check(label: str, env_name: str) -> tuple[bool, str]:
    """Return (ok, detail) for a configured credential env var name.

    ``client_id_env`` / ``client_secret_env`` store the *name* of an environment
    variable, not the secret value. Misconfigured YAML often puts literals there;
    we detect common mistakes and return actionable messages without echoing secrets.
    """
    if not env_name:
        return False, "no env var configured"
    if os.environ.get(env_name):
        return True, f"{env_name} is set"
    if not _ENV_VAR_NAME.match(env_name):
        return (
            False,
            f"configured value is not an environment variable name — "
            f"set {label}_env to something like OKTA_CLIENT_ID and put the "
            f"actual {label} in your environment or secrets store",
        )
    return False, f"{env_name} is unset or empty"
