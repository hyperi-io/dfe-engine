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

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scalo.logger import logger
from scalo.secrets.exceptions import (
    AuthenticationError,
    SecretNotFoundError,
    SecretPermissionError,
)

from dfe_engine.http_errors import describe_http_error

if TYPE_CHECKING:
    from dfe_engine.secrets import DfeSecrets

# ``\Z``, not ``$``: ``$`` also matches before a trailing newline.
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """One credential as it resolved: its value, absent, or unreadable.

    Attributes:
        value: The credential, or empty when nothing resolved. Left out of the repr.
        unreadable: Nothing resolved and the secret store, asked for it, gave nothing
            back, so the credential may exist all the same.
        refused: Of an unreadable credential: the store answered and refused the
            engine access to it, rather than not answering.
    """

    value: str = field(default="", repr=False)
    unreadable: bool = False
    refused: bool = False


def unreadable_credential_message(*, credential: str, refused: bool = False) -> str:
    """What an API caller is told when the secret store gave nothing back for ``credential``.

    Args:
        credential: The credential, as the caller knows it.
        refused: The store refused access rather than not answering.
    """
    if refused:
        return (
            f"The secret store refused access to the {credential}, so check the engine's "
            "secrets policy; the engine log has the reason"
        )
    return (
        f"The secret store did not answer when asked for the {credential}: check the "
        "secrets backend is reachable; the engine log has the reason"
    )


def _refused(exc: BaseException) -> bool:
    """Whether the store answered and refused, rather than not answering.

    A permission error is the store's answer to a read. scalo also raises
    ``AuthenticationError`` for a login it never sent (no secret id, no token), so one
    counts only when the store's HTTP answer is its cause.
    """
    if isinstance(exc, SecretPermissionError):
        return True
    answer = getattr(exc.__cause__, "response", None)
    return isinstance(exc, AuthenticationError) and isinstance(
        getattr(answer, "status_code", None), int
    )


def is_env_var_name(value: str) -> bool:
    """True when ``value`` is shaped like an environment variable name."""
    return bool(_ENV_VAR_NAME.match(value))


def provider_secret_path(provider_name: str, field: str) -> str:
    """The ``DfeSecrets`` path a provider's secret is written to."""
    return f"oidc/{provider_name}/{field}"


def _from_store(secret_path: str, secrets: DfeSecrets | None) -> ResolvedCredential:
    """Read a secret from the store: its value, absent, or unreadable when the store did not answer or refused."""
    if not secret_path or secrets is None:
        return ResolvedCredential()
    try:
        return ResolvedCredential(value=secrets.get(secret_path))
    except SecretNotFoundError:
        return ResolvedCredential()
    except Exception as exc:
        # The login path falls through to the env var, so an unreachable store is reported, never raised.
        fields = describe_http_error(exc)
        logger.warning(
            "OIDC credential unreadable from the secret store",
            secret_path=secret_path,
            **fields,
        )
        return ResolvedCredential(unreadable=True, refused=_refused(exc))


def resolve_credential(
    *,
    value: str = "",
    secret_path: str = "",
    env_name: str = "",
    secrets: DfeSecrets | None = None,
) -> ResolvedCredential:
    """Resolve one credential: plain value, then the secret store, then the env.

    Returns:
        The first value found. With none, an empty credential marked ``unreadable``
        when the secret store was asked and gave nothing back, and ``refused`` as well
        when the store refused access. A caller on the login
        path reads only ``value``, so an unreachable store leaves it as it would a
        credential that is not set.
    """
    if value:
        return ResolvedCredential(value=value)
    stored = _from_store(secret_path, secrets)
    if stored.value:
        return stored
    from_env = os.environ.get(env_name, "") if env_name else ""
    if from_env:
        return ResolvedCredential(value=from_env)
    return ResolvedCredential(unreadable=stored.unreadable, refused=stored.refused)


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
    belongs; that shape is named in the detail so an operator can act on it. A
    secret store that did not answer is named too, unless the env var serves the
    credential, as it does for a login.
    """
    if value:
        return True, f"{label} is set"
    stored = _from_store(secret_path, secrets)
    if stored.value:
        return True, f"{label} resolved from the secret store at {secret_path}"
    if env_name and os.environ.get(env_name):
        return True, f"{env_name} is set"
    if stored.unreadable:
        return False, unreadable_credential_message(
            credential=f"{label} at {secret_path}", refused=stored.refused
        )
    if not env_name:
        if secret_path:
            return False, f"nothing stored at {secret_path} and no env var configured"
        return False, "no env var configured"
    if not is_env_var_name(env_name):
        return (
            False,
            f"configured value is not an environment variable name -- "
            f"set {label}_env to something like OKTA_CLIENT_ID, or send the actual "
            f"{label} as '{label}' so it is written to the secret store",
        )
    return False, f"{env_name} is unset or empty"
