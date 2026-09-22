#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_regressions.py
#  Purpose:      Regression cover for the app-management defects found by review
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Each test here pins one defect that reached main and was fixed.

Every case runs against a real dulwich deploy repo rather than a stubbed store,
because the corruption these guard against only appears once the document has
been emitted to YAML and parsed back.
"""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue, files, instances
from dfe_engine.appmgmt.files import InvalidContentError
from dfe_engine.appmgmt.instances import HELMVARS_CLASS

VRL = "dfe-transform-vrl"
LOADER = "dfe-loader"
FETCHER = "dfe-fetcher"

# A lone carriage return terminates a line for the YAML parser but is not the
# newline the block-scalar indentation is measured against, so unguarded block
# emission lets this body close its own scalar and forge sibling keys.
CR_INJECTION = ".a = 1\rreplicaCount: 99\rimage:\r  tag: latest\r"

# The first line is indented further than a later one, so a block scalar takes its
# indentation from the first line and the rest of the body parses as sibling YAML.
INDENT_INJECTION = "  .j = 7\n.k = 8\n"


@pytest.fixture
def vrl_set():
    return catalogue.file_set(VRL, "transforms")


def _committed(crud, doc: dict, app: instances.AppInstance) -> dict:
    """Write the overlay and read it back through git, as the API path does."""
    crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")
    return crud.get(HELMVARS_CLASS, app.overlay_name)


class TestContentCannotForgeOverlayKeys:
    def test_carriage_returns_do_not_inject_sibling_keys(self, crud, vrl_set):
        app = instances.instance_of(VRL, "edge")
        doc = instances.initial_overlay(app)
        files.upsert_file(doc, vrl_set, "000_parse.vrl", CR_INJECTION)

        reloaded = _committed(crud, doc, app)

        # The forged keys are the payload: replicaCount is controller-owned and a
        # floating image tag defeats the pinning policy, so neither may appear.
        assert "replicaCount" not in reloaded
        assert "image" not in reloaded
        # Writing a file may add its own key and nothing else, whatever the app's
        # own overlay carries.
        baseline = set(instances.initial_overlay(app))
        assert set(reloaded) == baseline | {vrl_set.values_path}
        assert files.read_file(reloaded, vrl_set, "000_parse.vrl").content == CR_INJECTION

    def test_a_more_indented_first_line_round_trips(self, crud, vrl_set):
        app = instances.instance_of(VRL, "edge")
        doc = instances.initial_overlay(app)
        files.upsert_file(doc, vrl_set, "010_indent.vrl", INDENT_INJECTION)

        reloaded = _committed(crud, doc, app)

        baseline = set(instances.initial_overlay(app))
        assert set(reloaded) == baseline | {vrl_set.values_path}
        assert files.read_file(reloaded, vrl_set, "010_indent.vrl").content == INDENT_INJECTION

    def test_on_disk_yaml_carries_no_forged_top_level_key(self, crud, vrl_set):
        app = instances.instance_of(VRL, "edge")
        doc = instances.initial_overlay(app)
        files.upsert_file(doc, vrl_set, "000_parse.vrl", CR_INJECTION)
        crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")

        on_disk = (crud.repo_path / "values" / f"{app.overlay_name}.yaml").read_text(
            encoding="utf-8"
        )
        # A forged key would sit at column zero; the real content is indented under
        # transformFiles no matter which scalar style the emitter chose.
        assert "\nreplicaCount:" not in on_disk
        assert "\nimage:" not in on_disk


class TestControlCharactersAreRefused:
    @pytest.mark.parametrize("bad", ["\x00", "\x0c", "\x1b", "\x07"])
    def test_content_with_a_control_character_raises(self, vrl_set, bad):
        # YAML carries no C0 control character but tab, newline and carriage
        # return, so storing one leaves an overlay nothing can parse or repair.
        with pytest.raises(InvalidContentError):
            files.upsert_file({}, vrl_set, "000_parse.vrl", f".a = 1{bad}\n")

    @pytest.mark.parametrize("good", ["\t", "\n", "\r"])
    def test_the_characters_yaml_can_carry_are_still_accepted(self, vrl_set, good):
        doc: dict = {}
        files.upsert_file(doc, vrl_set, "000_parse.vrl", f".a = 1{good}.b = 2\n")
        assert files.read_file(doc, vrl_set, "000_parse.vrl").content.endswith(".b = 2\n")


class TestOtelServiceNameIsTheChartsOwnDial:
    def test_overlay_sets_the_top_level_dial_and_never_env(self):
        # `env` is a STRING in the dfe-infra charts (the deployment environment)
        # feeding the dfe.hyperi.io/env label and the namespace, so writing a map
        # there renders an invalid label value and every object is rejected.
        app = instances.instance_of(VRL, "edge")
        doc = instances.initial_overlay(app)

        assert doc[catalogue.OTEL_SERVICE_NAME_PATH] == app.telemetry_name
        assert doc["otelServiceName"] == "dfe-transform-vrl-edge"
        assert "env" not in doc

    def test_the_dial_survives_a_commit(self, crud):
        app = instances.instance_of("dfe-transform-vector", "edge")
        reloaded = _committed(crud, instances.initial_overlay(app), app)
        assert reloaded["otelServiceName"] == "dfe-transform-vector-edge"
        assert "env" not in reloaded


class TestMalformedFileSetsAreRefused:
    # Both mutators write the whole list back over the key, so an entry quietly
    # filtered out on the way in is a hand-authored file deleted on the way out.
    NAMELESS = {"transformFiles": [{"content": "x"}]}

    def test_list_refuses_an_entry_without_a_name(self, vrl_set):
        with pytest.raises(ValueError, match="without a 'name' key"):
            files.list_files(dict(self.NAMELESS), vrl_set)

    def test_upsert_refuses_an_entry_without_a_name(self, vrl_set):
        with pytest.raises(ValueError, match="without a 'name' key"):
            files.upsert_file(dict(self.NAMELESS), vrl_set, "000_parse.vrl", ".a = 1\n")

    def test_delete_refuses_an_entry_without_a_name(self, vrl_set):
        with pytest.raises(ValueError, match="without a 'name' key"):
            files.delete_file(dict(self.NAMELESS), vrl_set, "000_parse.vrl")

    def test_a_non_mapping_entry_is_refused(self, vrl_set):
        with pytest.raises(ValueError, match="without a 'name' key"):
            files.list_files({"transformFiles": ["000_parse.vrl"]}, vrl_set)

    def test_a_well_formed_set_still_loads(self, vrl_set):
        doc = {"transformFiles": [{"name": "000_parse.vrl", "content": ".a = 1\n"}]}
        assert [f.name for f in files.list_files(doc, vrl_set)] == ["000_parse.vrl"]


class TestInstanceCountFollowsMultiplicity:
    def _deploy(self, crud, service: str, instance: str) -> instances.AppInstance:
        app = instances.instance_of(service, instance)
        crud.put(HELMVARS_CLASS, app.overlay_name, instances.initial_overlay(app), actor="alice")
        return app

    def test_a_single_deployment_app_refuses_a_second_instance(self, crud):
        # dfe-loader's chart names its objects from the component alone, so two
        # configs render identical names and the Applications fight under self-heal.
        self._deploy(crud, LOADER, "default")
        allowed, reason = instances.additional_instance_allowed(
            crud, instances.instance_of(LOADER, "spare")
        )
        assert allowed is False
        assert "one deployment" in reason
        assert "default" in reason

    def test_a_single_deployment_app_allows_its_first_instance(self, crud):
        allowed, _ = instances.additional_instance_allowed(
            crud, instances.instance_of(LOADER, "default")
        )
        assert allowed is True

    def test_a_per_config_app_allows_a_second_instance(self, crud):
        self._deploy(crud, FETCHER, "alpha")
        allowed, reason = instances.additional_instance_allowed(
            crud, instances.instance_of(FETCHER, "beta")
        )
        assert allowed is True
        assert reason == ""

    def test_a_target_that_runs_none_refuses_the_first(self, crud):
        # A Compose deployment whose engine renders no app config runs only the
        # containers its committed file declares, so an instance written here
        # would be stored and never run.
        allowed, reason = instances.additional_instance_allowed(
            crud, instances.instance_of(FETCHER, "alpha"), 0
        )

        assert allowed is False
        assert "runs no dfe-fetcher at all" in reason

    def test_rewriting_the_bound_instance_is_not_a_second_one(self, crud):
        self._deploy(crud, FETCHER, "alpha")
        allowed, _ = instances.additional_instance_allowed(
            crud, instances.instance_of(FETCHER, "alpha"), 1
        )
        assert allowed is True

    def test_a_per_config_overlay_carries_the_instance_in_its_component(self):
        # dfe-common.fullname is {project}-{component} with no instance of its own,
        # so the component is the only thing keeping two deployments' names apart.
        alpha = instances.instance_of(FETCHER, "alpha")
        beta = instances.instance_of(FETCHER, "beta")
        alpha_doc = instances.initial_overlay(alpha)
        beta_doc = instances.initial_overlay(beta)

        assert alpha_doc[catalogue.COMPONENT_PATH] == "fetcher-alpha"
        assert beta_doc[catalogue.COMPONENT_PATH] == "fetcher-beta"
        assert alpha_doc["component"] != beta_doc["component"]

    def test_a_single_deployment_overlay_does_not_set_the_component(self):
        # One deployment per app, so overriding the component would only rename the
        # objects the chart already names consistently.
        assert catalogue.COMPONENT_PATH not in instances.initial_overlay(
            instances.instance_of(LOADER, "default")
        )
