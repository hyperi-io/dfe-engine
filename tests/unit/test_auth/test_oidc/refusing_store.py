#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/refusing_store.py
#  Purpose:      Secret stores whose every read is refused, as OpenBao refuses one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Secret stores whose every read is refused, each the way scalo's OpenBao provider raises it."""

import httpx
from scalo.secrets.exceptions import AuthenticationError, SecretPermissionError

from dfe_engine.secrets import DfeSecrets


class PolicyRefusingSecretStore(DfeSecrets):
    """A store that answers every read with a 403, as OpenBao does for a path its policy denies."""

    def __init__(self) -> None:
        """Hold no backend: every read fails before one would be used."""

    def get(self, path: str) -> str:
        """Refuse the read."""
        raise SecretPermissionError("openbao", "read", path, "grant read on the path")


class AppRoleRefusingSecretStore(DfeSecrets):
    """A store whose AppRole login is refused, so no read gets as far as a path."""

    def __init__(self) -> None:
        """Hold no backend: every read fails before one would be used."""

    def get(self, path: str) -> str:
        """Refuse the login from the store's 400, as scalo raises it."""
        request = httpx.Request("POST", "http://openbao.invalid/v1/auth/approle/login")
        answer = httpx.Response(400, request=request)
        refusal = httpx.HTTPStatusError("400 Bad Request", request=request, response=answer)
        raise AuthenticationError("openbao", "approle login refused: HTTP 400") from refusal
