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
from dfe_engine.orgs.registry import OrgRegistry

_TEST_ENV = "test"
_SETUP_ADMIN_PASSWORD = "already_reset"


class Seed:
    """Shared stores for e2e seeders. Refuses to construct outside ``DFE_ENV=test``."""

    def __init__(
        self,
        *,
        account_store: AccountStore | FerretDBAccountStore,
        group_store: GroupStore,
        org_registry: OrgRegistry,
        env: str | None = None,
    ) -> None:
        resolved = (env if env is not None else os.environ.get("DFE_ENV", "")).strip().lower()
        if resolved != _TEST_ENV:
            raise PermissionError("e2e seed helpers require DFE_ENV=test")
        self._account_store = account_store
        self._group_store = group_store
        self._org_registry = org_registry
        self._attach_seeders(resolved)

    def _seeder_kwargs(self, env: str) -> dict[str, object]:
        return {
            "account_store": self._account_store,
            "group_store": self._group_store,
            "org_registry": self._org_registry,
            "env": env,
        }

    def _attach_seeders(self, env: str) -> None:
        """``Seed`` owns child seeders; a child instance is that seeder."""
        from dfe_engine.api.e2e.seed.accounts import Accounts
        from dfe_engine.api.e2e.seed.organisations import Organisations

        cls = type(self)
        if cls is Seed:
            kwargs = self._seeder_kwargs(env)
            self.accounts = Accounts(**kwargs)
            self.organisations = Organisations(**kwargs)
            return
        if cls is Accounts:
            self.accounts = self
            return
        if cls is Organisations:
            self.organisations = self

    def seed_static(self, script: str) -> bool:
        """Dispatch *script* to child seeders. Returns False when unknown."""
        if script == "seed_admin":
            account = self.accounts.seed_admin()
            logger.warning("e2e seed", script=script, account=account)
            return True
        if script == "seed_setup_complete":
            self.accounts.seed_admin("admin", password=_SETUP_ADMIN_PASSWORD)
            self.accounts.seed_initial_user()
            self.organisations.seed_organisation()
            return True
        return False
