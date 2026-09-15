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

from __future__ import annotations

from pathlib import Path

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


def _rendered(settings, service: str):
    app = descriptor(service)
    return yaml_load(Path(settings.deployment.app_config_dir) / service / app.config_file)


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
    def test_a_program_reaches_the_directory_the_config_names(self, crud, tmp_path):
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        doc = instances.read_overlay(crud, app)
        files.upsert_file(doc, file_set(VRL, "transforms"), "100_filebeat.vrl", ".marked = true\n")
        _put(crud, app, doc)

        appconfig.render(crud, settings)

        config = _rendered(settings, VRL)
        assert config["transforms"]["dir"] == f"{MOUNT}/{VRL}/transforms"
        written = Path(settings.deployment.app_config_dir, VRL, "transforms", "100_filebeat.vrl")
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

        directory = Path(settings.deployment.app_config_dir, VRL, "transforms")
        assert list(directory.iterdir()) == []

    def test_a_new_program_rewrites_the_config_the_app_watches(self, crud, tmp_path):
        # The app polls its config file, and a program appearing in a directory
        # that file already names moves nothing it can see.
        settings = _settings(tmp_path)
        app = _deploy(crud, VRL, "filebeat")
        appconfig.render(crud, settings)
        target = Path(settings.deployment.app_config_dir, VRL, "config.yaml")
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

        assert _rendered(settings, VRL)["enrichment_tables"] == [
            {"name": "timezones", "path": f"{MOUNT}/{VRL}/enrichment/timezones.csv"}
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

        entries = _rendered(settings, VRL)["enrichment_tables"]
        assert entries == [{"name": "timezones", "path": "/elsewhere.csv", "key_columns": ["zone"]}]


class TestCustomEnvironment:
    def test_the_overlay_block_becomes_one_file_per_app(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__SECOND="also", extraEnv__DFE_LOADER_HOUSE_KEY="kept")

        appconfig.render(crud, settings)

        # Sorted, so an unordered overlay does not rewrite the file every render.
        assert _env_file(settings, LOADER).read_text() == (
            "DFE_LOADER_HOUSE_KEY=kept\nSECOND=also\n"
        )

    def test_the_file_is_readable_only_by_its_owner(self, crud, tmp_path):
        settings = _settings(tmp_path)
        _deploy(crud, LOADER, extraEnv__DFE_LOADER_TOKEN="hunter2")

        appconfig.render(crud, settings)

        assert _env_file(settings, LOADER).stat().st_mode & 0o777 == 0o600

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
        assert rendered[LOADER].restart_hint == f"recreate required: docker compose up -d {LOADER}"

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

    def test_a_startup_bound_app_names_the_command_that_applies_it(self, crud, tmp_path):
        settings = _settings(tmp_path)
        appconfig.render(crud, settings)
        _deploy(crud, VRL, "filebeat")

        rendered = {r.service: r for r in appconfig.render(crud, settings)}

        assert rendered[VRL].restart_required
        assert rendered[VRL].restart_hint == f"restart required: docker compose restart {VRL}"
        assert appconfig.restart_hints(list(rendered.values())) == [rendered[VRL].restart_hint]

    def test_the_first_render_of_a_stack_asks_for_no_restart(self, crud, tmp_path):
        # Nothing is reading a directory this render created, so a fresh stack
        # must not hand its operator five restart commands before it has started.
        settings = _settings(tmp_path)
        _deploy(crud, VRL, "filebeat")

        rendered = appconfig.render(crud, settings)

        assert [r.service for r in rendered if r.changed]
        assert appconfig.restart_hints(rendered) == []

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
