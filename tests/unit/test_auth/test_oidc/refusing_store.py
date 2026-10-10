#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/refusing_store.py
#  Purpose:      Secret stores whose every read is refused, as OpenBao refuses one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Secret stores whose every read is refused, each the way scalo's OpenBao provider raises it."""

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
        """Refuse the login."""
        raise AuthenticationError("openbao", "AppRole auth failed: 400")


class AppRoleUnreachableSecretStore(DfeSecrets):
    """A store whose AppRole login never reaches it, raised as scalo raises it: inside the socket error."""

    def __init__(self) -> None:
        """Hold no backend: every read fails before one would be used."""

    def get(self, path: str) -> str:
        """Fail the login as unreachable."""
        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except ConnectionRefusedError as exc:
            raise AuthenticationError("openbao", f"AppRole auth failed: {exc}")
