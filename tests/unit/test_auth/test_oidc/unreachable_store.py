#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/unreachable_store.py
#  Purpose:      A secret store whose every read fails as an unreachable OpenBao does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A secret store whose every read fails as an unreachable OpenBao does."""

from dfe_engine.secrets import DfeSecrets

STORE_ADDRESS = "https://bao.store-sentinel.example:8200"
"""The address an unreachable store's error text names, which no log line may carry."""


class UnreachableSecretStore(DfeSecrets):
    """A store that refuses every read with a connection error naming its address and the path.

    The error is raised from a ``ConnectionRefusedError``, the shape an HTTP client
    gives a refused socket, so the engine can say why without reading the text.
    """

    def __init__(self) -> None:
        """Hold no backend: every read fails before one would be used."""

    def get(self, path: str) -> str:
        """Refuse the read."""
        raise ConnectionError(f"cannot reach {STORE_ADDRESS}/v1/dfe/data/{path}") from (
            ConnectionRefusedError(111, "Connection refused")
        )
