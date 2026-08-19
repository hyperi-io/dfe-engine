#  Project:      dfe-engine
#  File:         api/e2e/seed/base.py
#  Purpose:      Test-env-gated base for e2e-server seeders
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Base seeder: construction is refused unless the posture is ``test``."""

from __future__ import annotations

import os

from scalo.logger import logger

from dfe_engine.auth.accounts import AccountStore, FerretDBAccountStore
from dfe_engine.auth.groups import GroupStore

_TEST_ENV = "test"


class Seed:
    """Shared stores for e2e seeders. Refuses to construct outside ``DFE_ENV=test``."""

    def __init__(
        self,
        *,
        account_store: AccountStore | FerretDBAccountStore,
        group_store: GroupStore,
        env: str | None = None,
    ) -> None:
        resolved = (env if env is not None else os.environ.get("DFE_ENV", "")).strip().lower()
        if resolved != _TEST_ENV:
            raise PermissionError("e2e seed helpers require DFE_ENV=test")
        self._account_store = account_store
        self._group_store = group_store
        self._attach_accounts(resolved)

    def _attach_accounts(self, env: str) -> None:
        """``Seed`` owns an ``Accounts`` seeder; an ``Accounts`` instance is that seeder."""
        if type(self) is Seed:
            from dfe_engine.api.e2e.seed.accounts import Accounts

            self.accounts = Accounts(
                account_store=self._account_store,
                group_store=self._group_store,
                env=env,
            )
        else:
            self.accounts = self

    def seed_static(self, script: str) -> bool:
        """Dispatch *script* to ``Accounts``. Returns False when unknown."""
        if script == "seed_admin":
            account = self.accounts.seed_admin()
            logger.warning("e2e seed", script=script, account=account)
            return True
        if script == "seed_setup_complete":
            self.accounts.seed_admin()
            self.accounts.seed_initial_user()
            return True
        return False
