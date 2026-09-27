#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_appconfig.py
#  Purpose:      Tests for rendering an app's native config where no chart does it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a Compose container actually reads after the engine has written.

Every assertion here is on the RENDERED FILE, not on the overlay it came from:
the whole point of this layer is that the overlay stopped short of the container,
so proving the overlay changed would prove nothing.
"""

import os
import uuid
from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.appmgmt import appconfig, files, instances, routing
from dfe_engine.appmgmt.catalogue import descriptor, file_set
from dfe_engine.gitcrud.engine import set_path
from dfe_engine.settings import DFESettings
from dfe_engine.source.models import Source
from dfe_engine.yaml_utils import yaml_load

from .conftest import FakeRegistry

ACTOR = "test"
RECEIVER = "dfe-receiver"
LOADER = "dfe-loader"
VRL = "dfe-transform-vrl"

MOUNT = "/etc/dfe/apps"


@pytest.fixture
def captured_logs():
    """Lines scalo's logger emits during the test, with the fields bound to each."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{level} {message} {extra}")
    yield lines
    logger.remove(sink_id)


def _settings(
    tmp_path,
    *,
    target: str = "docker",
    app_config_dir: str | None = None,
    app_env_dir: str | None = None,
):
    """A Compose deployment that renders its apps' config into tmp_path."""
    out = tmp_path / "app-config" if app_config_dir is None else app_config_dir
    env_out = tmp_path / "app-env" if app_env_dir is None else app_env_dir
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    return DFESettings(
        env="dev",
        transport={"default": "bus"},
        deployment={
            "profile": "docker-single",
            "target": target,
            "app_config_dir": str(out),
            "app_config_base_dir": str(base),
            "app_config_mount": MOUNT,
            "app_env_dir": str(env_out),
        },
    )


def _source(name: str) -> Source:
    """One receiver-matched active source, which is all the loader compile reads."""
    return Source.model_validate(
        {
            "source": name,
            "state": "active",
            "match": {"field": "_json.app", "operator": "equals", "value": name},
        }
    )


def _env_file(settings, service: str) -> Path:
    return Path(settings.deployment.app_env_dir) / f"{service}{appconfig.CUSTOM_ENV_SUFFIX}"


def _base(settings, service: str, body: str) -> None:
    base = Path(settings.deployment.app_config_base_dir)
    (base / f"{service}.yaml").write_text(body, encoding="utf-8")


def _put(crud, app, doc) -> None:
    crud.put(instances.HELMVARS_CLASS, app.overlay_name, doc, ACTOR, message="test: put")


def _deploy(crud, service: str, instance: str = "default", **values):
    app = instances.instance_of(service, instance)
    doc = instances.initial_overlay(app)
    for path, value in values.items():
        set_path(doc, path.replace("__", "."), value)
    _put(crud, app, doc)
    return app


def _app_dir(settings, service: str, instance: str = "") -> Path:
    """Where this app's container reads from: per instance for a per-config app."""
    root = Path(settings.deployment.app_config_dir) / service
    return root / instance if instance else root


def _rendered(settings, service: str, instance: str = ""):
    app = descriptor(service)
    return yaml_load(_app_dir(settings, service, instance) / app.config_file)


def _index(settings, service: str) -> Path:
    return Path(settings.deployment.app_env_dir) / f"{service}{appconfig.INSTANCE_INDEX_SUFFIX}"


class TestWhetherItRunsAtAll:
    def test_a_deployment_with_a_chart_renders_nothing_here(self, tmp_path):
        assert not appconfig.enabled(_settings(tmp_path, target="kubernetes"))

    def test_a_compose_stack_that_names_no_directory_renders_nothing(self, tmp_path):
        assert not appconfig.enabled(_settings(tmp_path, app_config_dir=""))

    def test_a_compose_stack_that_names_one_renders(self, tmp_path):
        assert appconfig.enabled(_settings(tmp_path))

    def test_only_the_apps_that_read_a_config_file_are_rendered(self, tmp_path):
        rendered = {a.service for a in appconfig.renderable(_settings(tmp_path))}
        assert RECEIVER in rendered
        # The console carries no data-plane config, so nothing is rendered for it.
        assert "dfe-ui" not in rendered
        assert "hyperdx" not in rendered

    def test_the_elastic_transform_is_rendered_on_docker_single(self, tmp_path):
        # It reads config.yaml and the profile seeds it idle, so the stack writes
        # the file the app exits without rather than leaving the directory absent.
        rendered = {a.service for a in appconfig.renderable(_settings(tmp_path))}

        assert "dfe-transform-elastic" in rendered


class TestWhatTheContainerReads:
    def test_the_deployment_base_survives_and_the_overlay_wins(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _base(settings, LOADER, "clickhouse:\n  hosts: [clickhouse:8123]\nkafka:\n  group: old\n")
        _deploy(crud, LOADER, config__kafka__group="new")

        appconfig.render(crud, settings)

        config = _rendered(settings, LOADER)
        assert config["clickhouse"]["hosts"] == ["clickhouse:8123"]
        assert config["kafka"]["group"] == "new"

    def test_a_list_the_overlay_names_replaces_the_base_rather_than_extending_it(
        self, crud, tmp_path
    ):
        # The engine owns a routing block WHOLE, so an appended base rule would
        # route records the sources no longer name.
        settings = _settings(tmp_path)
        _base(settings, RECEIVER, "routing:\n  source_rules:\n    - field: _source\n")
        _deploy(crud, RECEIVER, config__routing__source_rules=[{"field": "app"}])

        appconfig.render(crud, settings)

        assert _rendered(settings, RECEIVER)["routing"]["source_rules"] == [{"field": "app"}]

    def test_a_setting_the_sources_do_not_derive_survives_a_source_deploy(self, crud, tmp_path):
        # The deployment turns the loader's dead letter off in its base config;
        # the compiled block owns the table map and must leave that switch alone.
        settings = _settings(tmp_path)
        _base(settings, LOADER, "routing:\n  dlq:\n    enabled: false\n")
        app = instances.instance_of(LOADER, "default")
        doc = instances.initial_overlay(app)
        routing.sync(app.descriptor, doc, FakeRegistry([_source("filebeat")]), settings)
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        config = _rendered(settings, LOADER)
        assert config["routing"]["dlq"] == {"enabled": False}
        assert config["routing"]["source_to_table"] == {"filebeat": "filebeat"}

    def test_an_app_with_no_overlay_still_gets_the_deployment_base(self, crud, tmp_path):
        # The resident container is started before any source exists, so it needs
        # a config file to idle on.
        settings = _settings(tmp_path)
        _base(settings, VRL, "source:\n  topics: []\n")

        appconfig.render(crud, settings)

        assert _rendered(settings, VRL)["source"]["topics"] == []


class TestFileSets:
    """A per-config app's file sets sit under its own instance directory."""

    def test_a_program_reaches_the_directory_the_config_names(self, crud, tmp_path):
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(VRL, "transforms"), "100_filebeat.vrl", ".marked = true\n")
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        config = _rendered(settings, VRL, "filebeat")
        assert config["transforms"]["dir"] == f"{MOUNT}/{VRL}/filebeat/transforms"
        written = _app_dir(settings, VRL, "filebeat") / "transforms" / "100_filebeat.vrl"
        assert written.read_text(encoding="utf-8") == ".marked = true\n"

    def test_a_removed_program_leaves_the_directory(self, crud, tmp_path):
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(VRL, "transforms"), "gone.vrl", ".a = 1\n")
        _put(crud, app, doc)
        appconfig.render(crud, settings)

        files.delete_file(doc, file_set(VRL, "transforms"), "gone.vrl")
        _put(crud, app, doc)
        appconfig.render(crud, settings)

        directory = _app_dir(settings, VRL, "filebeat") / "transforms"
        assert list(directory.iterdir()) == []

    def test_a_new_program_rewrites_the_config_the_app_watches(self, crud, tmp_path):
        # The app polls its config file, and a program appearing in a directory
        # that file already names moves nothing it can see.
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        appconfig.render(crud, settings)
        target = _app_dir(settings, VRL, "filebeat") / "config.yaml"
        before = target.stat().st_mtime_ns

        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(VRL, "transforms"), "100_filebeat.vrl", ".a = 1\n")
        _put(crud, app, doc)
        appconfig.render(crud, settings)

        assert target.stat().st_mtime_ns != before

    def test_a_table_the_app_names_entry_by_entry_gets_a_derived_entry(self, crud, tmp_path):
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(VRL, "enrichment"), "timezones.csv", "a,b\n1,2\n")
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        assert _rendered(settings, VRL, "filebeat")["enrichment_tables"] == [
            {"name": "timezones", "path": f"{MOUNT}/{VRL}/filebeat/enrichment/timezones.csv"}
        ]

    def test_an_entry_the_config_already_names_is_left_alone(self, crud, tmp_path):
        # That one carries the author's key columns; a derived entry would turn
        # every lookup into a full scan.
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        set_path(
            doc,
            "config.enrichment_tables",
            [{"name": "timezones", "path": "/elsewhere.csv", "key_columns": ["zone"]}],
        )
        files.upsert_file(doc, file_set(VRL, "enrichment"), "timezones.csv", "a,b\n1,2\n")
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        entries = _rendered(settings, VRL, "filebeat")["enrichment_tables"]
        assert entries == [{"name": "timezones", "path": "/elsewhere.csv", "key_columns": ["zone"]}]

    @staticmethod
    def _warnings(monkeypatch) -> list[tuple[str, dict]]:
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            appconfig.logger, "warning", lambda message, **fields: seen.append((message, fields))
        )
        return seen

    def test_a_declared_entry_whose_file_was_removed_is_kept_and_named(
        self, crud, tmp_path, monkeypatch
    ):
        # Kept for its key columns; the render rewrote the directory, so its path is dead.
        seen = self._warnings(monkeypatch)
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        orphan = {
            "name": "geo",
            "path": f"{MOUNT}/{VRL}/filebeat/enrichment/geo.csv",
            "key_columns": ["ip"],
        }
        set_path(doc, "config.enrichment_tables", [orphan])
        files.upsert_file(doc, file_set(VRL, "enrichment"), "timezones.csv", "a,b\n1,2\n")
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        entries = _rendered(settings, VRL, "filebeat")["enrichment_tables"]
        assert orphan in entries
        named = [fields for _, fields in seen if fields.get("entry") == "geo"]
        assert named == [
            {
                "file_set": "enrichment",
                "entry": "geo",
                "missing_file": "geo.csv",
                "path": orphan["path"],
            }
        ]

    def test_an_entry_pointing_outside_the_set_is_not_called_an_orphan(
        self, crud, tmp_path, monkeypatch
    ):
        # A file baked into the image or mounted by the chart never passes through the set.
        seen = self._warnings(monkeypatch)
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        set_path(doc, "config.enrichment_tables", [{"name": "geo", "path": "/opt/baked/geo.csv"}])
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        assert [fields for _, fields in seen if fields.get("entry") == "geo"] == []


class TestOneContainerPerInstance:
    """A per-config app runs one container per source, so it renders one config each."""

    def test_two_instances_of_one_app_read_their_own_config(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu", config__sink__topic="crowdstrike-eu_load")
        _deploy(crud, VRL, "crowdstrike-us", config__sink__topic="crowdstrike-us_load")

        appconfig.render(crud, settings)

        assert _rendered(settings, VRL, "crowdstrike-eu")["sink"]["topic"] == "crowdstrike-eu_load"
        assert _rendered(settings, VRL, "crowdstrike-us")["sink"]["topic"] == "crowdstrike-us_load"

    def test_each_instance_is_reported_against_its_own_container(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu")
        _deploy(crud, VRL, "crowdstrike-us")

        rendered = [r for r in appconfig.render(crud, settings) if r.service == VRL]

        assert [r.container for r in rendered] == [
            f"{VRL}-crowdstrike-eu",
            f"{VRL}-crowdstrike-us",
        ]

    def test_the_deployer_is_told_which_containers_to_declare(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu")
        _deploy(crud, VRL, "crowdstrike-us")

        appconfig.render(crud, settings)

        assert _index(settings, VRL).read_text() == "crowdstrike-eu\ncrowdstrike-us\n"

    def test_an_app_with_no_source_declares_no_container(self, crud, tmp_path):
        # A deployment that never adds a fetcher source starts with none running,
        # rather than with one container holding an empty config.
        settings = _settings(tmp_path)

        appconfig.render(crud, settings)

        assert _index(settings, "dfe-fetcher").read_text() == ""

    def test_a_deleted_source_takes_its_container_and_its_config_with_it(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu")
        gone = _deploy(crud, VRL, "crowdstrike-us")
        appconfig.render(crud, settings)

        crud.delete(instances.HELMVARS_CLASS, gone.overlay_name, ACTOR, message="test: rm")
        appconfig.render(crud, settings)

        assert _index(settings, VRL).read_text() == "crowdstrike-eu\n"
        assert not _app_dir(settings, VRL, "crowdstrike-us").exists()

    def test_an_unwritable_env_directory_does_not_cost_the_apps_their_config(self, crud, tmp_path):
        # That directory is the operator's own checkout, so its mode is theirs to
        # fix; the containers still have to get the config they read.
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu")
        env_dir = Path(settings.deployment.app_env_dir)
        env_dir.mkdir(parents=True, exist_ok=True)
        env_dir.chmod(0o500)

        try:
            appconfig.render(crud, settings)
        finally:
            env_dir.chmod(0o700)

        assert _rendered(settings, VRL, "crowdstrike-eu") is not None

    def test_a_stack_wide_app_keeps_its_one_directory(self, crud, tmp_path):
        # The loader is one deployment for the whole stack, so its config stays
        # where its committed container already mounts it.
        settings = _settings(tmp_path)
        _deploy(crud, LOADER)

        appconfig.render(crud, settings)

        assert (_app_dir(settings, LOADER) / "loader.yaml").is_file()
        assert not _index(settings, LOADER).exists()


class TestCustomEnvironment:
    def test_the_overlay_block_becomes_one_file_per_compose_service(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__SECOND="also", extraEnv__DFE_LOADER_HOUSE_KEY="kept")

        appconfig.render(crud, settings)

        # Sorted, so an unordered overlay does not rewrite the file every render.
        assert _env_file(settings, LOADER).read_text() == (
            "DFE_LOADER_HOUSE_KEY=kept\nSECOND=also\n"
        )

    def test_each_instance_writes_the_file_its_own_container_reads(self, crud, tmp_path):
        # One shared file per app would hand every source the last one written.
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "crowdstrike-eu", extraEnv__DFE_VRL_REGION="eu")
        _deploy(crud, VRL, "crowdstrike-us", extraEnv__DFE_VRL_REGION="us")

        rendered = {r.container: r for r in appconfig.render(crud, settings)}

        assert _env_file(settings, f"{VRL}-crowdstrike-eu").read_text() == "DFE_VRL_REGION=eu\n"
        assert _env_file(settings, f"{VRL}-crowdstrike-us").read_text() == "DFE_VRL_REGION=us\n"
        assert not _env_file(settings, VRL).exists()
        assert rendered[f"{VRL}-crowdstrike-eu"].custom_env_changed

    def test_a_leftover_app_level_file_from_before_per_instance_env_is_emptied(
        self, crud, tmp_path
    ):
        # v1.20.6-v1.22.0 wrote every instance's keys into this name, and a
        # generated Compose instance still extends the base service that reads it.
        settings = _settings(tmp_path)
        leftover = _env_file(settings, VRL)
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("OLD_INSTANCE_KEY=leaked\n", encoding="utf-8")
        _deploy(crud, VRL, "crowdstrike-eu", extraEnv__DFE_VRL_REGION="eu")

        appconfig.render(crud, settings)

        assert leftover.read_text() == ""

    def test_the_file_is_readable_by_its_group_and_nobody_else(self, crud, tmp_path):
        # Compose reads env_file as the operator who runs it, not as the engine.
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_TOKEN="hunter2")

        appconfig.render(crud, settings)

        assert _env_file(settings, LOADER).stat().st_mode & 0o777 == 0o640

    def test_a_restrictive_umask_does_not_take_the_group_read_away(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_TOKEN="hunter2")

        previous = os.umask(0o077)
        try:
            appconfig.render(crud, settings)
        finally:
            os.umask(previous)

        assert _env_file(settings, LOADER).stat().st_mode & 0o777 == 0o640

    def test_the_file_takes_the_group_of_the_directory_it_sits_in(self, crud, tmp_path):
        settings = _settings(tmp_path)
        env_dir = Path(settings.deployment.app_env_dir)
        env_dir.mkdir(parents=True)
        other = next((g for g in os.getgroups() if g != env_dir.stat().st_gid), None)
        if other is None:
            pytest.skip("this process holds no second group to give the directory")
        os.chown(env_dir, -1, other)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_TOKEN="hunter2")

        appconfig.render(crud, settings)

        assert _env_file(settings, LOADER).stat().st_gid == other

    def test_a_file_written_owner_only_is_widened_and_recreated(self, crud, tmp_path):
        # An earlier engine wrote this file 0600, which Compose run by anyone else cannot read.
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_HOUSE_KEY="kept")
        appconfig.render(crud, settings)
        env_file = _env_file(settings, LOADER)
        env_file.chmod(0o600)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert env_file.stat().st_mode & 0o777 == 0o640
        assert rendered[LOADER].restart_hint == f"recreate required: make apply SERVICES={LOADER}"

    def test_a_directory_group_the_engine_does_not_hold_is_named_with_the_fix(
        self, tmp_path, captured_logs
    ):
        # Only root can make a directory with a foreign group, so borrow a writable ancestor.
        mine = {os.getegid(), *os.getgroups()}
        foreign = next(
            (
                p
                for p in tmp_path.parents
                if p.stat().st_gid not in mine and os.access(p, os.W_OK | os.X_OK)
            ),
            None,
        )
        if os.geteuid() == 0 or foreign is None:
            pytest.skip("no writable directory here has a group this process lacks")
        settings = _settings(tmp_path, app_env_dir=str(foreign))
        service = f"dfe-test-{uuid.uuid4().hex}"
        written = foreign / f"{service}{appconfig.CUSTOM_ENV_SUFFIX}"

        try:
            changed = appconfig.write_custom_env(settings, service, {"KEY": "value"})
        finally:
            written.unlink(missing_ok=True)
            written.with_name(f".{written.name}.tmp").unlink(missing_ok=True)

        assert changed
        warning = [line for line in captured_logs if line.startswith("WARNING ")]
        assert any("cannot take its directory's group" in line for line in warning)
        assert any("group_add" in line for line in warning)

    def test_a_bool_is_spelled_the_way_an_app_parses_one(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_VERBOSE=True)

        appconfig.render(crud, settings)

        assert _env_file(settings, LOADER).read_text() == "DFE_LOADER_VERBOSE=true\n"

    def test_a_deployment_that_names_no_directory_writes_nothing(self, crud, tmp_path):
        settings = _settings(tmp_path, app_env_dir="")
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_HOUSE_KEY="kept")

        appconfig.render(crud, settings)

        assert list(tmp_path.glob(f"**/*{appconfig.CUSTOM_ENV_SUFFIX}")) == []

    def test_an_env_change_needs_a_recreate_rather_than_a_restart(self, crud, tmp_path):
        # Compose reads env_file at up time, so `restart` would keep the old set.
        settings = _settings(tmp_path)
        appconfig.render(crud, settings)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_HOUSE_KEY="kept")

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[LOADER].custom_env_changed
        assert rendered[LOADER].restart_hint == f"recreate required: make apply SERVICES={LOADER}"

    def test_an_unchanged_block_asks_for_nothing(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_HOUSE_KEY="kept")
        appconfig.render(crud, settings)

        again = {r.service: r for r in appconfig.render(crud, settings)}

        assert not again[LOADER].custom_env_changed
        assert again[LOADER].restart_hint == ""


class TestWhatTakingTheChangeCosts:
    def test_an_unchanged_render_reports_no_change_and_no_restart(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, RECEIVER)
        appconfig.render(crud, settings)

        again = {r.service: r for r in appconfig.render(crud, settings)}

        assert not again[RECEIVER].changed
        assert not again[RECEIVER].restart_required

    def test_a_hot_app_takes_its_config_change_where_it_stands(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, RECEIVER, config__routing__default_source="main")

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].changed
        assert not rendered[RECEIVER].restart_required
        assert rendered[RECEIVER].restart_hint == ""

    def test_a_new_destination_restarts_a_receiver_that_reloads_its_routing(self, crud, tmp_path):
        # The receiver opens one sink per destination at startup, so a destination
        # added later has none until the container restarts, reload or not.
        settings = _settings(tmp_path)
        app = _deploy(crud, RECEIVER, config__routing__default_source="main")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(
            doc,
            "config.destinations.named.transform",
            {"grpc": {"endpoint": "http://dfe-transform-vrl-auth:6000"}},
        )
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].restart_required
        assert rendered[RECEIVER].restart_hint == (
            f"restart required: make apply SERVICES={RECEIVER}"
        )

    def test_a_restart_path_deep_in_a_block_the_receiver_reloads(self, crud, tmp_path):
        # The DLQ sits under the routing block the app rebuilds, and is still
        # built once at startup.
        settings = _settings(tmp_path)
        app = _deploy(crud, RECEIVER, config__routing__default_source="main")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(doc, "config.routing.dlq.mode", "file")
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].restart_required

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("config.server.ip_filter.mode", "allowlist"),
            ("config.server.rate_limit.enabled", True),
            ("config.server.auth.mode", "bearer"),
            ("config.server.max_body_size", 1024),
        ],
    )
    def test_a_server_setting_the_http_listener_binds_restarts_the_receiver(
        self, crud, tmp_path, path, value
    ):
        # The HTTP listener reads these once when it starts serving; only the
        # bearer tokens reload.
        settings = _settings(tmp_path)
        app = _deploy(crud, RECEIVER, config__routing__default_source="main")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(doc, path, value)
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].restart_required

    def test_a_routing_change_is_still_taken_where_it_stands(self, crud, tmp_path):
        # The router is rebuilt in place, so the restart paths leave it hot.
        settings = _settings(tmp_path)
        app = _deploy(crud, RECEIVER, config__routing__default_source="main")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(doc, "config.routing.default_source", "elsewhere")
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].changed
        assert not rendered[RECEIVER].restart_required
        assert rendered[RECEIVER].restart_hint == ""

    def test_a_previous_render_that_no_longer_parses_counts_as_a_change(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, RECEIVER, config__routing__default_source="main")
        appconfig.render(crud, settings)
        target = _app_dir(settings, RECEIVER) / descriptor(RECEIVER).config_file
        target.write_text("destinations: [unclosed\n", encoding="utf-8")

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[RECEIVER].restart_required

    def test_the_loader_is_told_to_watch_the_file_rendered_for_it(self, crud, tmp_path):
        # dfe-loader's watcher ships off; without this key a change reported as
        # needing no restart is never read.
        settings = _settings(tmp_path)
        _deploy(crud, LOADER)

        appconfig.render(crud, settings)

        assert _rendered(settings, LOADER)["hot_reload"] == {"enabled": True}

    def test_a_loader_the_deployment_pins_is_restarted_for_a_change(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _base(settings, LOADER, "hot_reload:\n  enabled: false\n")
        app = _deploy(crud, LOADER)
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(doc, "config.routing.default_table", "elsewhere")
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert _rendered(settings, LOADER)["hot_reload"] == {"enabled": False}
        assert rendered[LOADER].restart_hint == f"restart required: make apply SERVICES={LOADER}"

    def test_a_startup_bound_app_names_the_command_that_applies_it(self, crud, tmp_path):
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        set_path(doc, "config.sink.topic", "elsewhere")
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[VRL].restart_required
        # The container carrying this instance, not the app: a per-config app has
        # one per source, so the app's own name would restart the wrong one.
        assert rendered[VRL].restart_hint == (
            f"restart required: make apply SERVICES={VRL}-filebeat"
        )
        assert appconfig.restart_hints(list(rendered.values())) == [rendered[VRL].restart_hint]

    def test_the_first_render_of_a_stack_asks_for_no_restart(self, crud, tmp_path):
        # Nothing is reading a directory this render created, so a fresh stack
        # must not hand its operator five restart commands before it has started.
        settings = _settings(tmp_path)

        rendered = appconfig.render(crud, settings)

        assert [r.service for r in rendered if r.changed]
        assert appconfig.restart_hints(rendered) == []

    def test_a_new_instance_names_the_container_to_bring_up(self, crud, tmp_path):
        # There is no container for it yet, so a restart would apply to nothing.
        settings = _settings(tmp_path)
        appconfig.render(crud, settings)
        _deploy(crud, VRL, "filebeat")

        rendered = {r.container: r for r in appconfig.render(crud, settings)}

        assert rendered[f"{VRL}-filebeat"].restart_hint == (
            f"recreate required: make apply SERVICES={VRL}-filebeat"
        )

    def test_a_rolled_file_set_needs_a_restart_even_on_a_hot_app(self, crud, tmp_path):
        # dfe-transform-vector reloads its config in place and still compiles its
        # programs once, which is why the two reload facts are separate.
        settings = _settings(tmp_path)
        service = "dfe-transform-vector"
        app = _deploy(crud, service, "filebeat")
        appconfig.render(crud, settings)
        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(service, "enrichment"), "t.csv", "a\n1\n")
        _put(crud, app, doc)

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[service].restart_required
