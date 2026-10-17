#  Project:      dfe-engine
#  File:         tests/support/failing_stores.py
#  Purpose:      Real stores with one operation that fails as a storage fault
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real stores whose one operation fails the way a full disk, a lost volume or an unreachable backend fails it.

Every other operation is the store's own, so a test sees exactly what the failure leaves.
"""

from typing import Any

from scalo.secrets import SecretsManager

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.secrets import DfeSecrets, SecretNameHeldError


class DeleteFailingSecrets(DfeSecrets):
    """A file-backed secret store whose deletes fail as an unreachable backend fails them."""

    def delete(self, path: str) -> None:
        """Fail the delete, leaving the secret in place."""
        raise ConnectionError("the secrets backend did not answer")


class NameHeldSecrets(DfeSecrets):
    """A file-backed secret store whose writes meet a name a deleted secret still holds."""

    def put(self, path: str, value: str) -> None:
        """Refuse the write as a secret inside its recovery window refuses it."""
        raise SecretNameHeldError("aws", f"acme/dfe/test/engine/{path}", 7)


class StampFailingAccountStore(AccountStore):
    """An account store that cannot write an identity-provider stamp onto an account."""

    def update(self, username: str, *, allow_protected: bool = False, **fields: Any) -> Account:
        """Fail any update carrying ``source_provider``; apply every other update."""
        if "source_provider" in fields:
            raise OSError(28, "No space left on device")
        return super().update(username, allow_protected=allow_protected, **fields)


def make_delete_failing_secrets(*, root: str) -> DeleteFailingSecrets:
    """Build a DeleteFailingSecrets over the file secret store at *root*."""
    manager = SecretsManager.from_config({"cache": {"enabled": False}})
    return DeleteFailingSecrets(manager=manager, provider="file", root=root)


def make_name_held_secrets(*, root: str) -> NameHeldSecrets:
    """Build a NameHeldSecrets over the file secret store at *root*."""
    manager = SecretsManager.from_config({"cache": {"enabled": False}})
    return NameHeldSecrets(manager=manager, provider="file", root=root)
