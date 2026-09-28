#  Project:      dfe-engine
#  File:         tests/support/racing_stores.py
#  Purpose:      A real account store that loses the race between its lookup and its create
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real account store that sees an account another request created a moment too late.

Every operation but the one missed lookup is the store's own, so the create that
follows meets the account on disk exactly as it would on a second replica.
"""

from pathlib import Path

from dfe_engine.auth.accounts import Account, AccountStore


class RacingAccountStore(AccountStore):
    """A real store whose first ``get`` of one username misses.

    What a request sees when another creates the account between its own lookup
    and its create.
    """

    def __init__(self, accounts_dir: Path, *, blind_to: str) -> None:
        super().__init__(accounts_dir)
        self._blind_to = blind_to

    def get(self, username: str) -> Account | None:
        """Miss *blind_to* once, then answer every lookup from the store."""
        if username == self._blind_to:
            self._blind_to = ""
            return None
        return super().get(username)
