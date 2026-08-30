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
from typing import TYPE_CHECKING, TypeVar

from scalo.logger import logger

from dfe_engine.auth.accounts import AccountStore, DocuStoreAccountStore
from dfe_engine.auth.groups import DocuStoreGroupStore, GroupStore
from dfe_engine.orgs.registry import OrgRegistry

if TYPE_CHECKING:
    from dfe_engine.gitcrud import GitCrud
    from dfe_engine.settings import DFESettings
    from dfe_engine.source.registry import SourceRegistry

_TEST_ENV = "test"
_SETUP_ADMIN_PASSWORD = "already_reset"

_S = TypeVar("_S", bound="Seed")


class Seed:
    """Shared stores for e2e seeders. Refuses to construct outside ``DFE_ENV=test``.

    The account, group and organisation stores are always present. The deploy
    repo, the source registry and the settings are optional, because the auth and
    setup scripts predate them and must keep working on a process with gitops
    off; the app-management scripts ask for them explicitly and raise a named
    error when the deployment does not have them.
    """

    def __init__(
        self,
        *,
        account_store: AccountStore | DocuStoreAccountStore,
        group_store: GroupStore | DocuStoreGroupStore,
        org_registry: OrgRegistry,
        env: str | None = None,
        gitcrud: GitCrud | None = None,
        source_registry: SourceRegistry | None = None,
        settings: DFESettings | None = None,
    ) -> None:
        resolved = (env if env is not None else os.environ.get("DFE_ENV", "")).strip().lower()
        if resolved != _TEST_ENV:
            raise PermissionError("e2e seed helpers require DFE_ENV=test")
        self._account_store = account_store
        self._group_store = group_store
        self._org_registry = org_registry
        self._gitcrud = gitcrud
        self._source_registry = source_registry
        self._settings = settings
        self._attach_seeders(resolved)

    def _child(self, seeder: type[_S], env: str) -> _S:
        """Build one child seeder over the same stores."""
        return seeder(
            account_store=self._account_store,
            group_store=self._group_store,
            org_registry=self._org_registry,
            env=env,
            gitcrud=self._gitcrud,
            source_registry=self._source_registry,
            settings=self._settings,
        )

    def _attach_seeders(self, env: str) -> None:
        """``Seed`` owns child seeders; a child instance is that seeder."""
        from dfe_engine.api.e2e.seed.accounts import Accounts
        from dfe_engine.api.e2e.seed.apps import Apps
        from dfe_engine.api.e2e.seed.artefacts import Artefacts
        from dfe_engine.api.e2e.seed.organisations import Organisations
        from dfe_engine.api.e2e.seed.sources import Sources

        cls = type(self)
        if cls is Seed:
            self.accounts = self._child(Accounts, env)
            self.organisations = self._child(Organisations, env)
            self.sources = self._child(Sources, env)
            self.apps = self._child(Apps, env)
            self.artefacts = self._child(Artefacts, env)
            return
        if cls is Accounts:
            self.accounts = self
            return
        if cls is Organisations:
            self.organisations = self
            return
        if cls is Sources:
            self.sources = self
            return
        if cls is Apps:
            self.apps = self
            return
        if cls is Artefacts:
            self.artefacts = self

    def _require_gitcrud(self) -> GitCrud:
        """The deploy repo, or a named error saying which setting is off."""
        if self._gitcrud is None:
            raise RuntimeError(
                "this e2e seed writes to the gitops deploy repo, which this process "
                "does not have; set DFE_GITOPS_ENABLED=true and DFE_GITOPS_LOCAL_PATH"
            )
        return self._gitcrud

    def _require_source_registry(self) -> SourceRegistry:
        """The source registry, or a named error saying which setting is off."""
        if self._source_registry is None:
            raise RuntimeError(
                "this e2e seed writes source definitions, and no source registry is "
                "configured; set DFE_SOURCES_DIR or enable gitops"
            )
        return self._source_registry

    def _require_settings(self) -> DFESettings:
        """The engine settings, which the routing compilers read the data database from."""
        if self._settings is None:
            raise RuntimeError("this e2e seed needs the engine settings, which were not passed")
        return self._settings

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
        if script == "seed_organisation":
            organisation = self.organisations.seed_organisation()
            logger.info("e2e seed", script=script, organisation=organisation)
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
        if script == "seed_source_with_transform":
            # ------------------------------------------------------------
            # Everything the Sources/<source> page renders, in one call
            # - The source definition, with the match rule and a fetcher
            # - Its bound transform and fetcher instances, and a program
            # - The receiver and loader pools, with routing compiled from it
            # ------------------------------------------------------------
            source = self.sources.seed_source()
            source_apps = self.apps.seed_source_apps()
            pools = self.apps.seed_pools()
            logger.info(
                "e2e seed",
                script=script,
                source=source,
                source_apps=source_apps,
                pools=pools,
            )
            return True
        if script == "seed_library_artefact":
            # The link needs a file set to resolve into, so the instance that
            # consumes the artefact is seeded before the artefact itself.
            source = self.sources.seed_source()
            source_apps = self.apps.seed_source_apps()
            artefact = self.artefacts.seed_library_artefact()
            logger.info(
                "e2e seed",
                script=script,
                source=source,
                source_apps=source_apps,
                artefact=artefact,
            )
            return True
        if script == "seed_app_scaling_state":
            pools = self.apps.seed_pools()
            dials = self.apps.seed_app_scaling_state()
            logger.info("e2e seed", script=script, pools=pools, dials=dials)
            return True
        if script == "reset_all":
            # Delete all accounts and organisations
            self.accounts.delete_all()
            self.organisations.delete_all()

            # Then the deploy repo. Instances first: an overlay carries the
            # content a library link resolved into it, so removing the artefact
            # while an overlay still references it would leave a dangling link.
            self.apps.delete_all()
            self.artefacts.delete_all()
            self.sources.delete_all()

            # Re-add the break-glass admin account
            self.accounts.reset_break_glass_admin()
            logger.info("e2e seed", script=script)
            return True
        return False
