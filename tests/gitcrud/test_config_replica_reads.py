#  Project:      dfe-engine
#  File:         tests/gitcrud/test_config_replica_reads.py
#  Purpose:      Field maps, alert destinations, meta schemas and service configs agree across replicas
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two engine replicas, each with its own config volume, over one deploy repo (real dulwich).

Each replica is built the way the lifespan builds it, through ``bootstrap_registries``
with its own settings and its own clone of one bare remote. Nothing is shared but the
remote and the secrets backend, so a write on A is read on B only when the registry
keeps it in one of those.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dulwich import porcelain
from dulwich.repo import Repo
from scalo.secrets.exceptions import ProviderError

from dfe_engine.api import deps
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.alert import ALERT_CLASS, AlertDestination
from dfe_engine.schema.derived_registry import derived_reference_root
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import (
    DFESettings,
    FieldMapSettings,
    GitopsSettings,
    HuntsSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
)
from dfe_engine.source.models import Source
from dfe_engine.yaml_utils import yaml_dump


def _seeded_remote(tmp_path: Path) -> tuple[str, str]:
    """A bare deploy repo with one commit, and the branch it lives on."""
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    seed = tmp_path / "seed"
    porcelain.clone(str(remote), str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(remote), f"refs/heads/{branch}".encode())
    return str(remote), branch


def _settings(root: Path, remote: str, branch: str, secrets_root: Path) -> DFESettings:
    """One replica's settings: its own config volume and clone, the shared remote and secrets."""
    config = root / "config"
    return DFESettings(
        env="test",
        gitops=GitopsSettings(
            enabled=True,
            repo_url=remote,
            branch=branch,
            local_path=str(root / "deploy-repo"),
            push=True,
        ),
        schemas=SchemasSettings(schemas_dir=str(root / "schemas")),
        fieldmap=FieldMapSettings(fieldmaps_dir=str(config / "fieldmaps")),
        services=ServicesSettings(config_yaml_dir=str(config / "services")),
        hunts=HuntsSettings(alert_destinations_dir=str(config / "alert-destinations")),
        secrets=SecretsSettings(provider="file", path=str(secrets_root)),
    )


class _Replica:
    """The registries one engine pod builds at boot, and the settings it built them from."""

    def __init__(self, settings: DFESettings, crud: GitCrud, registries: dict[str, Any]) -> None:
        self.settings = settings
        self.crud = crud
        self.registries = registries

    def __getitem__(self, key: str) -> Any:
        return self.registries[key]


def _boot(settings: DFESettings) -> _Replica:
    """Run the lifespan's registry bootstrap for one replica and keep what it built."""
    repo = GitopsRepo(
        local_path=settings.gitops.local_path,
        repo_url=settings.gitops.repo_url,
        branch=settings.gitops.branch,
        push=True,
    )
    crud = GitCrud(repo)
    deps.bootstrap_registries(settings, gitcrud=crud)
    built = dict(deps._registries)
    # The registries are module-global, so the next replica's bootstrap needs an empty dict.
    deps._registries.clear()
    return _Replica(settings, crud, built)


def _close(replica: _Replica) -> None:
    for registry in replica.registries.values():
        if hasattr(registry, "close"):
            registry.close()


@pytest.fixture
def replicas(tmp_path: Path) -> Iterator[tuple[_Replica, _Replica]]:
    remote, branch = _seeded_remote(tmp_path)
    deps._registries.clear()
    a = _boot(_settings(tmp_path / "a", remote, branch, tmp_path / "secrets"))
    b = _boot(_settings(tmp_path / "b", remote, branch, tmp_path / "secrets"))
    yield a, b
    for replica in (a, b):
        _close(replica)


def _every_object(repo_path: Path) -> list[bytes]:
    """The raw content of every object a repo holds, packed or loose, on any ref or none."""
    repo = Repo(str(repo_path))
    try:
        return [repo.object_store[sha].as_raw_string() for sha in repo.object_store]
    finally:
        repo.close()


def _stored_url_path(replica: _Replica, name: str) -> str:
    """The secrets-backend path a destination's deploy-repo document keeps."""
    return replica.crud.get(ALERT_CLASS, name)["url_path"]


def _meta_schema_doc(column: str) -> dict[str, Any]:
    return {
        "current": "1.0.0",
        "resource_type": "custom",
        "versions": {
            "1.0.0": {
                "date": "2026-10-22",
                "type": "model",
                "summary": "first",
                "columns": [{"name": column, "type": "string", "_field_type": "base"}],
            }
        },
    }


class TestFieldMapsAcrossReplicas:
    def test_a_field_map_saved_on_one_replica_is_read_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["fieldmap"].save_map(
            {"standard": "sigma", "source": "windows-audit", "mappings": {"User": "user_name"}}
        )

        assert b["fieldmap"].get_map("sigma", "windows-audit").mappings == {"User": "user_name"}
        assert [row["source"] for row in b["fieldmap"].list_maps("sigma")] == ["windows-audit"]

    def test_a_field_map_deleted_on_one_replica_is_gone_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["fieldmap"].save_map({"standard": "ecs", "mappings": {"user.name": "user_name"}})

        b["fieldmap"].delete_map("ecs")

        assert a["fieldmap"].map_exists("ecs") is False


class TestAlertDestinationsAcrossReplicas:
    def test_a_destination_added_on_one_replica_resolves_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["alert_destinations"].add(
            AlertDestination(name="oncall", url="slack://T/B/C/#alerts", description="pager")
        )

        assert "oncall" in b["alert_destinations"]
        assert b["alert_destinations"].resolve("oncall") == "slack://T/B/C/#alerts"
        assert [d.name for d in b["alert_destinations"].list()] == ["oncall"]
        assert b["alert_destinations"].get("oncall").url_scheme == "slack"

    def test_a_destination_removed_on_one_replica_is_gone_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/C"))

        assert b["alert_destinations"].remove("oncall") is True

        assert "oncall" not in a["alert_destinations"]

    def test_no_url_reaches_any_object_in_the_deploy_repo(self, replicas, tmp_path) -> None:
        """Every commit and blob, on the remote and in the writer's clone."""
        a, _ = replicas
        tokens = ("created-4911", "replaced-4912")
        a["alert_destinations"].add(
            AlertDestination(name="oncall", url=f"slack://T1/B1/{tokens[0]}", description="pager")
        )
        a["alert_destinations"].add(
            AlertDestination(name="oncall", url=f"slack://T2/B2/{tokens[1]}")
        )

        stored = a.crud.get(ALERT_CLASS, "oncall")
        assert "url" not in stored
        assert stored["url_path"].startswith("alerts/oncall/")
        assert stored["url_scheme"] == "slack"
        objects = [*_every_object(tmp_path / "remote.git"), *_every_object(a.crud.repo_path)]
        assert not [raw for raw in objects for token in tokens if token.encode() in raw]

    def test_no_url_reaches_a_review_branch(self, replicas, tmp_path) -> None:
        from dfe_engine.gitcrud.routing import ReviewRouting

        a, _ = replicas
        routing = ReviewRouting()

        _registry_at(a, "alert_destinations", environment="production", mode="team").add(
            AlertDestination(name="oncall", url="slack://T3/B3/proposed-4913"), routing=routing
        )

        remote = Repo(str(tmp_path / "remote.git"))
        try:
            assert f"refs/heads/{routing.branches[0]}".encode() in remote.refs.keys()
        finally:
            remote.close()
        objects = [*_every_object(tmp_path / "remote.git"), *_every_object(a.crud.repo_path)]
        assert not [raw for raw in objects if b"proposed-4913" in raw]

    def test_a_new_url_on_one_replica_resolves_on_the_other_and_retires_the_old(
        self, replicas
    ) -> None:
        a, b = replicas
        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/old-4921"))
        old_path = _stored_url_path(b, "oncall")

        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/new-4922"))

        assert b["alert_destinations"].resolve("oncall") == "slack://T/B/new-4922"
        assert build_secrets(b.settings.secrets).get_or_none(old_path) is None

    def test_a_write_that_keeps_the_url_moves_no_secret(self, replicas) -> None:
        a, b = replicas
        a["alert_destinations"].add(
            AlertDestination(name="oncall", url="slack://T/B/kept-4923", description="first")
        )
        path = _stored_url_path(a, "oncall")

        b["alert_destinations"].add(AlertDestination(name="oncall", description="second"))

        assert _stored_url_path(a, "oncall") == path
        assert a["alert_destinations"].get("oncall").description == "second"
        assert a["alert_destinations"].resolve("oncall") == "slack://T/B/kept-4923"

    def test_a_removed_destination_takes_its_url_out_of_the_secrets_backend(self, replicas) -> None:
        a, b = replicas
        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/gone-4924"))
        path = _stored_url_path(a, "oncall")

        assert b["alert_destinations"].remove("oncall") is True

        assert build_secrets(a.settings.secrets).get_or_none(path) is None

    def test_a_url_the_secrets_backend_refuses_commits_nothing(self, replicas, tmp_path) -> None:
        """The URL is stored before the document, so a failed store leaves no document."""
        a, b = replicas
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        c = _boot(
            _settings(tmp_path / "c", a.settings.gitops.repo_url, a.settings.gitops.branch, blocked)
        )
        try:
            with pytest.raises(ProviderError):
                c["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/C"))
        finally:
            _close(c)

        assert "oncall" not in b["alert_destinations"]

    def test_a_url_changed_on_a_review_branch_is_not_sent_until_it_merges(self, replicas) -> None:
        from dfe_engine.gitcrud.routing import ReviewRouting

        a, b = replicas
        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/live-4931"))
        routing = ReviewRouting()

        _registry_at(a, "alert_destinations", environment="production", mode="team").add(
            AlertDestination(name="oncall", url="slack://T/B/proposed-4932"), routing=routing
        )

        assert routing.review_required is True
        assert b["alert_destinations"].resolve("oncall") == "slack://T/B/live-4931"

    def test_a_delete_on_a_review_branch_keeps_the_url_main_still_sends_to(self, replicas) -> None:
        from dfe_engine.gitcrud.routing import ReviewRouting

        a, b = replicas
        a["alert_destinations"].add(AlertDestination(name="oncall", url="slack://T/B/live-4933"))
        routing = ReviewRouting()

        registry = _registry_at(a, "alert_destinations", environment="production", mode="team")
        assert registry.remove("oncall", routing=routing) is True

        assert routing.review_required is True
        assert b["alert_destinations"].resolve("oncall") == "slack://T/B/live-4933"


class TestMetaSchemasAcrossReplicas:
    def test_a_meta_schema_created_on_one_replica_is_read_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["meta_schema"].save_schema({"path": "meta/acme/firewall", **_meta_schema_doc("rule")})

        schema = b["meta_schema"].get_schema("meta/acme/firewall")
        assert [c.name for c in schema.versions["1.0.0"].columns] == ["rule"]
        assert "meta/acme/firewall" in [row["path"] for row in b["meta_schema"].list_schemas()]

    def test_a_version_added_on_one_replica_is_current_on_the_other(self, replicas) -> None:
        from dfe_engine.schema.schema_manager import SchemaManager

        a, b = replicas
        a["meta_schema"].save_schema({"path": "meta/acme/firewall", **_meta_schema_doc("rule")})

        with a["meta_schema"].editing("meta/acme/firewall") as edit:
            SchemaManager.add_version(
                edit.path,
                "1.1.0",
                [
                    {"name": "rule", "type": "string", "_field_type": "base"},
                    {"name": "action", "type": "string", "_field_type": "base"},
                ],
                type="addition",
                summary="add action",
                set_current=True,
            )

        assert b["meta_schema"].get_schema_current_version("meta/acme/firewall") == "1.1.0"

    def test_a_source_on_the_other_replica_builds_against_it(self, replicas) -> None:
        """The build reads the schema file by path, so the file has to reach B's tree.

        A build on B starts from B reading the source through the deploy repo, which
        takes the remote's head into B's clone, as a deploy request does.
        """
        a, b = replicas
        a["meta_schema"].save_schema({"path": "meta/acme/firewall", **_meta_schema_doc("rule")})
        a["source"].save_source(
            Source.model_validate(
                {
                    "source": "acme-firewall",
                    "match": {"field": "tags.collector.type", "value": "acme-firewall"},
                    "header": {"type": "minimal", "version": "1.0.0"},
                    "schema": {"meta_schema": "meta/acme/firewall", "ttl_days": 0},
                }
            )
        )
        source = b["source"].get_source("acme-firewall")
        builder = SchemaBuilderV2(
            schemas_base_dir=b.settings.schemas.schemas_dir,
            derived_base_dir=derived_reference_root(b.settings),
        )

        assert "rule" in [c.name for c in builder.load_columns_for_source_version(source)]

    def test_an_edit_to_a_shipped_schema_wins_over_the_other_replicas_shipped_copy(
        self, replicas
    ) -> None:
        """Both pods ship the schema in their own image tree, and the edit is the deployment's."""
        a, b = replicas
        for replica in (a, b):
            shipped = Path(replica.settings.schemas.schemas_dir) / "meta" / "acme" / "proxy.yaml"
            shipped.parent.mkdir(parents=True, exist_ok=True)
            yaml_dump(_meta_schema_doc("url"), shipped)
            replica["meta_schema"]._store._refresh_all()

        with a["meta_schema"].editing("meta/acme/proxy") as edit:
            from dfe_engine.schema.schema_manager import SchemaManager

            SchemaManager.update_version_summary(edit.path, "1.0.0", "edited on a")

        assert b["meta_schema"].get_schema("meta/acme/proxy").versions["1.0.0"].summary == (
            "edited on a"
        )


class TestServiceConfigsAcrossReplicas:
    def test_a_config_saved_on_one_replica_is_read_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["service_config"].save_config("new-service", {"alpha": 1}, instance="prod")

        assert b["service_config"].get_config("new-service", "prod") == {"alpha": 1}
        listed = b["service_config"].list_configs()
        assert [(row["service"], row["instance"]) for row in listed] == [("new-service", "prod")]

    def test_a_config_deleted_on_one_replica_is_gone_on_the_other(self, replicas) -> None:
        from dfe_engine.services.registry import ConfigNotFoundError

        a, b = replicas
        a["service_config"].save_config("new-service", {"alpha": 1}, instance="prod")

        b["service_config"].delete_config("new-service", "prod")

        with pytest.raises(ConfigNotFoundError):
            a["service_config"].get_config("new-service", "prod")

    def test_the_history_one_replica_wrote_is_read_on_the_other(self, replicas) -> None:
        a, b = replicas
        a["service_config"].save_config("receiver", {"kafka": {"brokers": ["k:9092"]}})

        history = b["service_config"].get_config_history("receiver", "default")

        assert len(history) == 1


def _registry_at(replica: _Replica, key: str, *, environment: str, mode: str) -> Any:
    """Replica A's registry for *key*, rebuilt over the deploy repo at another posture."""
    from dfe_engine.fieldmap.registry import FIELDMAP_CLASS, FieldMapRegistry
    from dfe_engine.hunts.alert import AlertDestinationRegistry
    from dfe_engine.hunts.deploy_repo import DeployRepoStore
    from dfe_engine.schema.registry import META_CLASS, SchemaRegistry
    from dfe_engine.services.registry import SERVICE_CONFIG_CLASS, ServiceConfigRegistry

    def store(cls_name: str) -> DeployRepoStore:
        return DeployRepoStore(replica.crud, cls_name, environment=environment, mode=mode)

    if key == "fieldmap":
        return FieldMapRegistry(deploy_repo=store(FIELDMAP_CLASS))
    if key == "alert_destinations":
        return AlertDestinationRegistry(
            deploy_repo=store(ALERT_CLASS), secrets=build_secrets(replica.settings.secrets)
        )
    if key == "service_config":
        return ServiceConfigRegistry(deploy_repo=store(SERVICE_CONFIG_CLASS))
    return SchemaRegistry(replica.settings.schemas.schemas_dir, deploy_repo=store(META_CLASS))


def _write(registry: Any, key: str, routing: Any) -> None:
    """One write through *registry*, routed into *routing*."""
    if key == "fieldmap":
        registry.save_map({"standard": "ecs", "mappings": {"a": "b"}}, routing=routing)
    elif key == "alert_destinations":
        registry.add(AlertDestination(name="oncall", url="slack://T/B/C"), routing=routing)
    elif key == "service_config":
        registry.save_config("new-service", {"alpha": 1}, instance="prod", routing=routing)
    else:
        registry.save_schema(
            {"path": "meta/acme/firewall", **_meta_schema_doc("rule")}, routing=routing
        )


def _read_on(replica: _Replica, key: str) -> bool:
    """Whether *replica* reads the doc :func:`_write` writes."""
    if key == "fieldmap":
        return replica["fieldmap"].map_exists("ecs")
    if key == "alert_destinations":
        return "oncall" in replica["alert_destinations"]
    if key == "service_config":
        listed = replica["service_config"].list_configs()
        return [(row["service"], row["instance"]) for row in listed] == [("new-service", "prod")]
    return replica["meta_schema"].stored_config("meta/acme/firewall") is not None


_KEYS = ("fieldmap", "alert_destinations", "service_config", "meta_schema")


class TestGovernance:
    """The commit policy every governed class takes: posture decides main or review."""

    @pytest.mark.parametrize("key", _KEYS)
    def test_production_team_lands_on_a_review_branch_not_main(self, replicas, key) -> None:
        from dfe_engine.gitcrud.routing import ReviewRouting

        a, b = replicas
        routing = ReviewRouting()
        main_before = a.crud.head_revision()

        _write(_registry_at(a, key, environment="production", mode="team"), key, routing)

        assert routing.review_required is True
        assert routing.branches
        assert routing.branches[0].startswith("dfe/")
        assert a.crud.head_revision() == main_before
        assert _read_on(b, key) is False

    @pytest.mark.parametrize("key", _KEYS)
    def test_production_solo_lands_on_main_for_every_replica(self, replicas, key) -> None:
        from dfe_engine.gitcrud.routing import ReviewRouting

        a, b = replicas
        routing = ReviewRouting()

        _write(_registry_at(a, key, environment="production", mode="solo"), key, routing)

        assert routing.review_required is False
        assert _read_on(b, key) is True

    @pytest.mark.parametrize("key", _KEYS)
    def test_each_write_is_one_commit_to_the_commit_standard(self, replicas, key) -> None:
        """Through the registry the lifespan built, which picks each class's commit type."""
        from dfe_engine.gitcrud.log import read_log

        a, _ = replicas
        _write(a[key], key, None)

        entries, _ = read_log(a.crud, limit=1)

        assert entries[0].conforming is True
        assert entries[0].ctype == {"service_config": "cfg", "alert_destinations": "hunt"}.get(
            key, "schema"
        )
