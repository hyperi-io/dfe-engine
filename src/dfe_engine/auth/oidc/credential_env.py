#  Project:      dfe-engine
#  File:         auth/oidc/credential_env.py
#  Purpose:      Resolve OIDC credentials - secret store first, then environment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Credential resolution for OIDC providers.

A provider names each credential in up to three ways: a plain value on the model
for the things that are not secret (``client_id``), a PATH into the
``DfeSecrets`` seam for the secret material, and the NAME of an environment
variable. The store is read BEFORE the environment so a secret written through
the API takes effect in the running process, while an ESO-mounted env var keeps
serving a provider that was configured that way.

Secret values are never returned to a caller - ``credential_check`` reports only
whether a credential resolved and from where.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from scalo.logger import logger
from scalo.secrets.exceptions import SecretNotFoundError

if TYPE_CHECKING:
    from dfe_engine.secrets import DfeSecrets

_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_env_var_name(value: str) -> bool:
    """True when ``value`` is shaped like an environment variable name."""
    return bool(_ENV_VAR_NAME.match(value))


def provider_secret_path(provider_name: str, field: str) -> str:
    """The ``DfeSecrets`` path a provider's secret is written to."""
    return f"oidc/{provider_name}/{field}"


def _from_store(secret_path: str, secrets: DfeSecrets | None) -> str:
    """Read a secret from the store, or '' when it is absent or unreachable."""
    if not secret_path or secrets is None:
        return ""
    try:
        return secrets.get(secret_path)
    except SecretNotFoundError:
        return ""
    except Exception as exc:
        # An unreachable store must not take the login path down with it.
        logger.warning(
            "OIDC credential unreadable from the secret store",
            secret_path=secret_path,
            error=str(exc),
        )
        return ""


def resolve_credential(
    *,
    value: str = "",
    secret_path: str = "",
    env_name: str = "",
    secrets: DfeSecrets | None = None,
) -> str:
    """Resolve one credential: plain value, then the secret store, then the env.

    Returns '' when none of the three yields anything.
    """
    if value:
        return value
    stored = _from_store(secret_path, secrets)
    if stored:
        return stored
    if env_name:
        return os.environ.get(env_name) or ""
    return ""


def credential_check(
    label: str,
    *,
    value: str = "",
    secret_path: str = "",
    env_name: str = "",
    secrets: DfeSecrets | None = None,
) -> tuple[bool, str]:
    """Return (ok, detail) for a configured credential, never echoing its value.

    Misconfigured YAML often puts the secret itself where the env var NAME
    belongs; that shape is named in the detail so an operator can act on it.
    """
    if value:
        return True, f"{label} is set"
    if _from_store(secret_path, secrets):
        return True, f"{label} resolved from the secret store at {secret_path}"
    if not env_name:
        if secret_path:
            return False, f"nothing stored at {secret_path} and no env var configured"
        return False, "no env var configured"
    if os.environ.get(env_name):
        return True, f"{env_name} is set"
    if not is_env_var_name(env_name):
        return (
            False,
            f"configured value is not an environment variable name -- "
            f"set {label}_env to something like OKTA_CLIENT_ID, or send the actual "
            f"{label} as '{label}' so it is written to the secret store",
        )
    return False, f"{env_name} is unset or empty"
