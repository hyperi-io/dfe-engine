#  Project:      dfe-engine
#  File:         tests/support/racing_stores.py
#  Purpose:      Real stores that lose the race between their lookup and their create
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real stores that see a record another replica created a moment too late.

Every operation but the one missed lookup is the store's own, so the create that
follows meets the record exactly as it would on a second replica.
"""

from pathlib import Path

from scalo.secrets.exceptions import SecretNotFoundError

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.secrets import DfeSecrets


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


class RacingSecrets(DfeSecrets):
    """A real secret store whose first read of one path misses.

    What a replica sees when another mints the same secret between its own read
    and its create.
    """

    def __init__(self, *args, blind_to: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._blind_to = blind_to

    def get(self, path: str) -> str:
        """Miss *blind_to* once, then answer every read from the store."""
        if path == self._blind_to:
            self._blind_to = ""
            raise SecretNotFoundError(path, "racing")
        return super().get(path)
