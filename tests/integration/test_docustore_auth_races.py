#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_auth_races.py
#  Purpose:      Auth store writes from two engine replicas at once, against a real store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Accounts, groups, API keys and roles written through two replicas' clients at once.

Runs against the harness's document store (``mongo_uri``: ``DFE_TEST_MONGO_URI``, else
DocumentDB on docker). Each race runs many rounds, because a lost update needs the two
writes to overlap and one round rarely shows it.
"""

from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

from dfe_engine.auth import account_durability
from dfe_engine.auth.accounts import DocuStoreAccountStore
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import DocuStoreGroupStore, GroupExistsError
from dfe_engine.auth.sessions import session_claims, session_ended
from dfe_engine.gitcrud.engine import GitCrud
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.store.documents import DocuStore
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

ROUNDS = 40
_ADMIN_PASSWORD = "issued-admin-password-1"
_DEFAULT_GROUPS = ["dfe-admins", "dfe-analysts", "dfe-infra", "dfe-viewers"]


def _accounts(pair: tuple[DocuStore, DocuStore]) -> list[DocuStoreAccountStore]:
    return [DocuStoreAccountStore(c, collection="accounts") for c in pair]


def _groups(pair: tuple[DocuStore, DocuStore]) -> list[DocuStoreGroupStore]:
    return [DocuStoreGroupStore(c, collection="groups") for c in pair]


def _deploy_repo(tmp_path: Path) -> GitCrud:
    """A local deploy repo, as the boot hydrates accounts from and publishes them to."""
    return GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), repo_url="", push=False))


def _boot(auth_dir: Path, docu: DocuStore, gitcrud: GitCrud | None = None) -> None:
    bootstrap_auth(
        auth_dir,
        default_admin_password=_ADMIN_PASSWORD,
        account_store=DocuStoreAccountStore(docu, collection="accounts"),
        group_store=DocuStoreGroupStore(docu, collection="groups"),
        gitcrud=gitcrud,
    )


class TestAccountWritesFromTwoReplicas:
    """Every account change reads, applies and swaps on the revision it read."""

    def test_ending_sessions_and_stamping_a_login_at_once_keep_both(self, docustore_pair):
        first, second = _accounts(docustore_pair)

        for round_ in range(ROUNDS):
            name = f"user-{round_}"
            before = first.create(name, "")
            stamp = f"2026-10-22T00:00:{round_:02d}+00:00"

            results = race(
                [
                    lambda name=name: first.end_sessions(name),
                    lambda name=name, stamp=stamp: second.update(name, last_login_at=stamp),
                ]
            )

            assert raised(results) == []
            after = first.get(name)
            assert after is not None
            assert (after.session_generation, after.last_login_at) == (
                before.session_generation + 1,
                stamp,
            ), round_


class TestGroupWritesFromTwoReplicas:
    """Group creates are decided by the unique index, membership changes by revision."""

    def test_adding_two_members_at_once_keeps_both(self, docustore_pair):
        first, second = _groups(docustore_pair)

        for round_ in range(ROUNDS):
            name = f"team-{round_}"
            first.create(name, ["data_viewer"])

            results = race(
                [
                    lambda name=name: first.add_member(name, "alice"),
                    lambda name=name: second.add_member(name, "bob"),
                ]
            )

            assert raised(results) == []
            group = first.get(name)
            assert group is not None
            assert sorted(group.members) == ["alice", "bob"], round_

    def test_two_replicas_creating_one_group_exactly_one_wins(self, docustore_pair):
        first, second = _groups(docustore_pair)

        for round_ in range(ROUNDS):
            name = f"team-{round_}"
            results = race(
                [
                    lambda name=name: first.create(name, ["admin"]),
                    lambda name=name: second.create(name, ["data_viewer"]),
                ]
            )

            refused = [r for r in results if isinstance(r, GroupExistsError)]
            assert len(refused) == 1, (round_, results)
            winner = next(r for r in results if not isinstance(r, BaseException))
            assert first.get(name) == winner

    def test_a_delete_and_a_new_member_at_once_never_both_land(self, docustore_pair):
        first, second = _groups(docustore_pair)

        for round_ in range(ROUNDS):
            name = f"team-{round_}"
            first.create(name, ["data_viewer"])

            deleted, added = race(
                [
                    lambda name=name: first.delete(name),
                    lambda name=name: second.add_member(name, "alice"),
                ]
            )

            group = first.get(name)
            if isinstance(deleted, ValueError):
                assert added is None, round_
                assert group is not None
                assert group.members == ["alice"], round_
            else:
                assert (deleted, type(added), group) == (None, KeyError, None), round_


class TestTwoBootsAtOnce:
    """Two replicas starting on one empty store both come up, seeding everything once."""

    @pytest.mark.parametrize("round_", range(3))
    def test_both_return_with_four_groups_and_one_admin(self, docustore_pair, tmp_path, round_):
        results = race(
            [
                lambda: _boot(tmp_path / "a" / "auth", docustore_pair[0]),
                lambda: _boot(tmp_path / "b" / "auth", docustore_pair[1]),
            ]
        )

        assert raised(results) == []
        accounts, groups = _accounts(docustore_pair)[0], _groups(docustore_pair)[0]
        assert [g.name for g in groups.list()] == _DEFAULT_GROUPS
        assert [a.username for a in accounts.list()] == ["admin"]
        admins = groups.get("dfe-admins")
        assert admins is not None
        assert admins.members == ["admin"]


class TestLogoutAndBoot:
    """A logout holds across the next boot, on this replica or another."""

    def test_a_token_from_before_logout_is_refused_after_the_next_boot(
        self, docustore_pair, tmp_path
    ):
        docu = docustore_pair[0]
        crud = _deploy_repo(tmp_path)
        _boot(tmp_path / "auth", docu, crud)
        store = DocuStoreAccountStore(docu, collection="accounts")
        admin = store.get("admin")
        assert admin is not None
        claims = session_claims(admin, auth_time=0)

        # What the logout route does.
        ended = store.end_sessions("admin")
        account_durability.publish_session_end(crud, ended, actor="admin")
        _boot(tmp_path / "auth", docu, crud)

        assert session_ended(store.get("admin"), claims)

    def test_a_logout_during_another_replicas_boot_holds(self, docustore_pair, tmp_path):
        crud = _deploy_repo(tmp_path)
        _boot(tmp_path / "auth", docustore_pair[0], crud)
        first, second = _accounts(docustore_pair)

        for round_ in range(ROUNDS):
            admin = first.get("admin")
            assert admin is not None
            claims = session_claims(admin, auth_time=0)

            results = race(
                [
                    lambda: first.end_sessions("admin"),
                    lambda: account_durability.hydrate_from_deploy_repo(crud, second),
                ]
            )

            assert raised(results) == []
            assert session_ended(first.get("admin"), claims), round_

    def test_a_login_recorded_after_the_deploy_repo_copy_survives_the_next_boot(
        self, docustore_pair, tmp_path
    ):
        docu = docustore_pair[0]
        crud = _deploy_repo(tmp_path)
        _boot(tmp_path / "auth", docu, crud)
        store = DocuStoreAccountStore(docu, collection="accounts")
        stamp = "2026-10-22T09:00:00+00:00"
        store.update("admin", last_login_at=stamp, name="Ops Admin")

        _boot(tmp_path / "auth", docu, crud)

        admin = store.get("admin")
        assert admin is not None
        assert (admin.last_login_at, admin.name) == (stamp, "Ops Admin")


class TestApiKeysAcrossReplicas:
    """An API key made through one replica works and is revoked through any other."""

    def test_a_key_made_on_one_replica_verifies_on_the_other(self, docustore_pair):
        from dfe_engine.auth.api_keys import DocuStoreAPIKeyStore

        first, second = (DocuStoreAPIKeyStore(c) for c in docustore_pair)
        meta, full_key = first.create("ci-bot", groups=["dfe-viewers"])

        verified, reason = second.verify_detailed(full_key)

        assert (reason, verified) == ("ok", meta)

    def test_a_key_revoked_on_one_replica_stops_verifying_on_the_other(self, docustore_pair):
        from dfe_engine.auth.api_keys import DocuStoreAPIKeyStore

        first, second = (DocuStoreAPIKeyStore(c) for c in docustore_pair)
        meta, full_key = first.create("ci-bot")

        second.revoke(meta.short_token)

        assert first.verify_detailed(full_key) == (None, "unknown_key")
        with pytest.raises(KeyError):
            second.revoke(meta.short_token)

    def test_two_replicas_making_one_name_exactly_one_wins(self, docustore_pair):
        from dfe_engine.auth.api_keys import DocuStoreAPIKeyStore

        first, second = (DocuStoreAPIKeyStore(c) for c in docustore_pair)

        for round_ in range(ROUNDS):
            name = f"bot-{round_}"
            results = race([lambda name=name, s=s: s.create(name) for s in (first, second)])

            refused = [r for r in results if isinstance(r, ValueError)]
            assert len(refused) == 1, (round_, results)
        assert len(first.list()) == ROUNDS

    def test_the_short_token_is_unique_across_names(self, docustore_pair):
        from dfe_engine.auth.api_keys import DocuStoreAPIKeyStore

        store = DocuStoreAPIKeyStore(docustore_pair[0])
        meta, _ = store.create("first")
        raw = docustore_pair[0].collection("api_keys")
        copy = raw.find_one({"name": "first"}, {"_id": 0}) | {"name": "second"}

        with pytest.raises(DuplicateKeyError):
            raw.insert_one(copy)
        assert [k.name for k in store.list()] == ["first"]
        assert meta.short_token


class TestCustomRolesAcrossReplicas:
    """A custom role written through one replica reaches every other's role config."""

    def test_a_role_made_on_one_replica_grants_on_the_other(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore

        first = DocuStoreRoleStore(docustore_pair[0], refresh_seconds=0)
        second = DocuStoreRoleStore(docustore_pair[1], refresh_seconds=0)
        config = second.load_config()
        assert not config.has_permission("auditor", "source:read")

        first.create("auditor", description="Reads sources", permissions=["source:read"])

        assert config.has_permission("auditor", "source:read")
        first.delete("auditor", holders=lambda _role: [])
        assert not config.has_permission("auditor", "source:read")

    def test_within_the_refresh_floor_the_config_is_not_read_again(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore

        first = DocuStoreRoleStore(docustore_pair[0], refresh_seconds=0)
        second = DocuStoreRoleStore(docustore_pair[1], refresh_seconds=3600)
        config = second.load_config()
        assert config.roles

        first.create("auditor", description="", permissions=["source:read"])

        assert "auditor" not in config.roles

    def test_core_roles_come_from_the_image_and_refuse_every_write(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore
        from dfe_engine.auth.roles import RoleConfig, builtin_core_role_names

        store = DocuStoreRoleStore(docustore_pair[0])
        listed = {role.name: role for role in store.list()}

        assert set(builtin_core_role_names()) <= set(listed)
        assert all(listed[n].resource_type == "core" for n in builtin_core_role_names())
        assert store.load_config().roles == RoleConfig.load_builtin().roles
        with pytest.raises(ValueError):
            store.create("admin", description="", permissions=["*"])
        with pytest.raises(ValueError):
            store.update("admin", permissions=["*"])
        with pytest.raises(ValueError):
            store.delete("admin", holders=lambda _role: [])
        assert docustore_pair[0].collection("roles").count_documents({}) == 0

    def test_a_delete_and_a_group_given_the_role_at_once_never_both_land(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore, RoleUnavailableError

        roles = [DocuStoreRoleStore(c) for c in docustore_pair]
        groups = [
            DocuStoreGroupStore(c, collection="groups", roles=r)
            for c, r in zip(docustore_pair, roles, strict=True)
        ]

        def holders(role: str) -> list[str]:
            return [g.name for g in groups[0].list() if role in g.roles]

        for round_ in range(ROUNDS):
            role, team = f"role-{round_}", f"team-{round_}"
            roles[0].create(role, description="", permissions=["source:read"])
            groups[0].create(team, [])

            deleted, given = race(
                [
                    lambda role=role: roles[0].delete(role, holders=holders),
                    lambda role=role, team=team: groups[1].update(team, roles=[role]),
                ]
            )

            gone = roles[0].get(role) is None
            stored = groups[0].get(team)
            assert stored is not None
            assert not (gone and role in stored.roles), round_
            if deleted is None:
                assert gone, round_
                assert isinstance(given, RoleUnavailableError), (round_, given)
            if not isinstance(given, BaseException):
                assert (role in stored.roles, gone) == (True, False), round_

    def test_a_binding_whose_group_write_never_landed_holds_the_role_until_the_grace(
        self, docustore_pair
    ):
        from dfe_engine.auth.role_store import DocuStoreRoleStore, RoleInUseError

        patient = DocuStoreRoleStore(docustore_pair[0])
        prompt = DocuStoreRoleStore(docustore_pair[0], binding_grace_seconds=0)
        patient.create("auditor", description="", permissions=["source:read"])
        # A replica that claimed the binding and died before writing its group.
        patient.bind("auditor", "ghost")

        with pytest.raises(RoleInUseError):
            patient.delete("auditor", holders=lambda _role: [])
        prompt.delete("auditor", holders=lambda _role: [])

        assert patient.get("auditor") is None

    def test_a_refused_delete_leaves_the_role_bindable(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore, RoleInUseError

        roles = DocuStoreRoleStore(docustore_pair[0])
        groups = DocuStoreGroupStore(docustore_pair[0], collection="groups", roles=roles)
        roles.create("auditor", description="", permissions=["source:read"])
        groups.create("analysts", ["auditor"])

        with pytest.raises(RoleInUseError):
            roles.delete("auditor", holders=lambda _role: ["analysts"])
        groups.create("auditors", ["auditor"])

        assert [g.name for g in groups.list() if "auditor" in g.roles] == ["analysts", "auditors"]

    def test_two_replicas_making_one_role_exactly_one_wins(self, docustore_pair):
        from dfe_engine.auth.role_store import DocuStoreRoleStore

        stores = [DocuStoreRoleStore(c) for c in docustore_pair]

        for round_ in range(ROUNDS):
            name = f"role-{round_}"
            results = race(
                [
                    lambda name=name, s=s: s.create(name, description="", permissions=[])
                    for s in stores
                ]
            )

            refused = [r for r in results if isinstance(r, ValueError)]
            assert len(refused) == 1, (round_, results)
