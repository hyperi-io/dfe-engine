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

from dfe_engine.auth.accounts import AccountStore, DocuStoreAccountStore
from dfe_engine.auth.groups import DocuStoreGroupStore, GroupStore
from dfe_engine.orgs.registry import OrgRegistry

_TEST_ENV = "test"
_SETUP_ADMIN_PASSWORD = "already_reset"


class Seed:
    """Shared stores for e2e seeders. Refuses to construct outside ``DFE_ENV=test``."""

    def __init__(
        self,
        *,
        account_store: AccountStore | DocuStoreAccountStore,
        group_store: GroupStore | DocuStoreGroupStore,
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
        if script == "seed_dfe_admin_user":
            account = self.accounts.seed_dfe_admin_user()
            logger.info("e2e seed", script=script, account=account)
            return True
        if script == "seed_dfe_analyst_user":
            account = self.accounts.seed_dfe_analyst_user()
            logger.info("e2e seed", script=script, account=account)
            return True
        if script == "seed_dfe_infra_user":
            account = self.accounts.seed_dfe_infra_user()
            logger.info("e2e seed", script=script, account=account)
            return True
        if script == "seed_dfe_viewers_user":
            account = self.accounts.seed_dfe_viewers_user()
            logger.info("e2e seed", script=script, account=account)
            return True
        if script == "seed_setup_complete":
            # ------------------------------------------------------------
            # Pre-requisites before seeding the setup complete account
            # - Seed the admin account
            # - Seed the initial user
            # ------------------------------------------------------------

            # Resets admin account password (setup steps check if admin password is same as env provided/default)
            admin = self.accounts.seed_dfe_admin_user("admin", password=_SETUP_ADMIN_PASSWORD)
            initial_user = self.accounts.seed_initial_user()
            organisation = self.organisations.seed_organisation()
            logger.info(
                "e2e seed",
                script=script,
                admin=admin,
                initial_user=initial_user,
                organisation=organisation,
            )
            return True
        if script == "reset_all":
            # Delete all accounts and organisations
            self.accounts.delete_all()
            self.organisations.delete_all()

            # Re-add the break-glass admin account
            self.accounts.reset_break_glass_admin()
            logger.info("e2e seed", script=script)
            return True
        return False
