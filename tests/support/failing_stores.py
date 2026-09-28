#  Project:      dfe-engine
#  File:         tests/support/failing_stores.py
#  Purpose:      Real account stores with one write that fails as a storage fault
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real account stores whose one write fails the way a full disk or a lost volume fails it.

Every other operation is the store's own, so a test sees exactly what the failure leaves.
"""

from typing import Any

from dfe_engine.auth.accounts import Account, AccountStore


class StampFailingAccountStore(AccountStore):
    """An account store that cannot write an identity-provider stamp onto an account."""

    def update(self, username: str, *, allow_protected: bool = False, **fields: Any) -> Account:
        """Fail any update carrying ``source_provider``; apply every other update."""
        if "source_provider" in fields:
            raise OSError(28, "No space left on device")
        return super().update(username, allow_protected=allow_protected, **fields)
