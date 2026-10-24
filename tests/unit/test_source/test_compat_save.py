#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_compat_save.py
#  Purpose:      Compound conditions, aliases and profile bindings at the source save gate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a source save refuses about its match, its ``when`` and the profiles it binds.

The registry writes to a real local deploy repo seeded with the profiles the pinned
dfe-schemas wheel ships, as the engine seeds them at startup.
"""

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.ingest.models import CompatSetting
from dfe_engine.ingest.store import IngestStore
from dfe_engine.source.models import CompatBinding, Source, SourceMatch
from dfe_engine.source.registry import (
    SourceAliasConflictError,
    SourceMatchConflictError,
    SourceRegistry,
    SourceRuleError,
)
from dfe_engine.yaml_utils import yaml_load

LEGACY = "dfe-legacy-2x"
ELASTIC = "elastic-agent"


@pytest.fixture
def crud(tmp_path):
    return GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())


@pytest.fixture
def store(crud) -> IngestStore:
    store = IngestStore(crud)
    assert store.seed_core_profiles() == [LEGACY, ELASTIC]
    return store


@pytest.fixture
def registry(crud, store) -> SourceRegistry:
    return SourceRegistry(crud=crud)


def _source(name: str, **body) -> Source:
    return Source.model_validate({"source": name, **body})


def _dataset(value: str) -> dict:
    return {"field": "data_stream.dataset", "value": value}


def _problems(caught: pytest.ExceptionInfo[SourceRuleError]) -> list[tuple[str, str]]:
    return [(problem.field, problem.detail) for problem in caught.value.problems]


class TestSeeding:
    def test_a_second_seed_writes_nothing(self, store, crud):
        head = crud.head_revision()
        assert store.seed_core_profiles() == []
        assert crud.head_revision() == head

    def test_a_custom_profile_holding_a_shipped_id_is_left_alone(self, store, crud):
        doc = crud.get("compat_profiles", LEGACY)
        doc["resource_type"] = "custom"
        crud.put("compat_profiles", LEGACY, doc, "test", message="test: take the id")

        assert store.seed_core_profiles() == []
        assert crud.get("compat_profiles", LEGACY)["resource_type"] == "custom"


class TestCompoundConditionsSave:
    def test_a_compound_match_round_trips_through_the_stored_yaml(self, registry, crud):
        match = {
            "any": [
                {"all": [_dataset("cisco_ios.log"), {"field": "agent.type", "value": "filebeat"}]},
                {"field": "data_stream.dataset", "value": "cisco_ios.debug", "negate": True},
            ]
        }
        registry.save_source(_source("cisco-ios", match=match))

        stored = yaml_load(crud.repo_path / "config/sources/cisco-ios.yaml")
        assert stored["versions"]["1.0.0"]["match"] == {
            "any": [
                {
                    "all": [
                        {
                            "field": "data_stream.dataset",
                            "operator": "equals",
                            "value": "cisco_ios.log",
                        },
                        {"field": "agent.type", "operator": "equals", "value": "filebeat"},
                    ]
                },
                {
                    "field": "data_stream.dataset",
                    "operator": "equals",
                    "value": "cisco_ios.debug",
                    "negate": True,
                },
            ]
        }
        assert registry.get_source("cisco-ios").match == SourceMatch.model_validate(match)

    def test_a_single_condition_stores_as_it_always_has(self, registry, crud):
        registry.save_source(_source("cisco-ios", match=_dataset("cisco_ios.log")))

        stored = yaml_load(crud.repo_path / "config/sources/cisco-ios.yaml")
        assert stored["versions"]["1.0.0"]["match"] == {
            "field": "data_stream.dataset",
            "operator": "equals",
            "value": "cisco_ios.log",
        }

    def test_a_when_saves_and_compiles_to_its_row(self, registry):
        when = "data_stream.dataset == 'okta.system'"
        registry.save_source(_source("okta", when=when))
        assert registry.get_source("okta").when == when

    def test_a_when_shared_with_an_enabled_source_is_a_match_conflict(self, registry):
        registry.save_source(_source("okta", when="data_stream.dataset == 'okta.system'"))
        with pytest.raises(SourceMatchConflictError) as caught:
            registry.save_source(_source("okta-2", when=" data_stream.dataset == 'okta.system' "))
        assert caught.value.conflicting_source == "okta"
        assert caught.value.field == ""

    def test_an_identical_group_is_a_match_conflict(self, registry):
        group = {"all": [_dataset("a.log"), _dataset("b.log")]}
        registry.save_source(_source("a", match=group))
        with pytest.raises(
            SourceMatchConflictError, match=r"\(data_stream.dataset equals a.log and"
        ):
            registry.save_source(_source("b", match=group))


class TestProfileBindingsSave:
    def test_a_source_binding_a_shipped_profile_with_aliases_saves(self, registry):
        registry.save_source(
            _source(
                "auth",
                match=_dataset("system.auth"),
                aliases={LEGACY: ["authentication"]},
                compat_profiles=[{"profile": LEGACY}],
            )
        )
        saved = registry.get_source("auth")
        assert saved.aliases == {LEGACY: ["authentication"]}
        assert saved.compat_profiles == [CompatBinding(profile=LEGACY)]

    @pytest.mark.parametrize(
        ("binding", "detail"),
        [
            ({"profile": "splunk-hec"}, "no compatibility profile 'splunk-hec'"),
            ({"profile": LEGACY, "version": "9.0.0"}, "dfe-legacy-2x publishes no version 9.0.0"),
            (
                {"profile": ELASTIC, "opt_in": ["org-tags"]},
                "elastic-agent offers no opt-in row org-tags, only org-namespace",
            ),
        ],
    )
    def test_a_binding_naming_what_is_not_stored_is_refused(self, registry, binding, detail):
        with pytest.raises(SourceRuleError) as caught:
            registry.save_source(
                _source("auth", match=_dataset("system.auth"), compat_profiles=[binding])
            )
        assert _problems(caught) == [("compat_profiles.0", detail)]

    def test_aliases_under_a_profile_that_is_not_stored_are_refused(self, registry):
        with pytest.raises(SourceRuleError) as caught:
            registry.save_source(
                _source("auth", match=_dataset("system.auth"), aliases={"splunk-hec": ["auth"]})
            )
        assert _problems(caught) == [
            ("aliases.splunk-hec", "no compatibility profile 'splunk-hec'")
        ]

    def test_aliases_under_an_unbound_profile_are_kept(self, registry):
        registry.save_source(
            _source("auth", match=_dataset("system.auth"), aliases={LEGACY: ["authentication"]})
        )
        assert registry.get_source("auth").aliases == {LEGACY: ["authentication"]}

    def test_an_alias_another_enabled_source_holds_is_refused(self, registry):
        registry.save_source(
            _source("auth", match=_dataset("system.auth"), aliases={LEGACY: ["authentication"]})
        )
        with pytest.raises(SourceAliasConflictError) as caught:
            registry.save_source(
                _source(
                    "auth-2", match=_dataset("other.auth"), aliases={LEGACY: ["authentication"]}
                )
            )
        assert (caught.value.conflicting_source, caught.value.profile, caught.value.alias) == (
            "auth",
            LEGACY,
            "authentication",
        )

    def test_a_disabled_source_releases_its_aliases(self, registry):
        registry.save_source(
            _source(
                "auth",
                state="disabled",
                match=_dataset("system.auth"),
                aliases={LEGACY: ["authentication"]},
            )
        )
        registry.save_source(
            _source("auth-2", match=_dataset("other.auth"), aliases={LEGACY: ["authentication"]})
        )
        assert registry.source_exists("auth-2")

    def test_a_second_version_of_one_profile_is_refused(self, registry, crud):
        doc = crud.get("compat_profiles", LEGACY)
        doc["versions"]["1.1.0"] = {**doc["versions"]["1.0.0"], "type": "addition"}
        crud.put("compat_profiles", LEGACY, doc, "test", message="test: publish 1.1.0")
        registry.save_source(
            _source(
                "a",
                match=_dataset("a.log"),
                compat_profiles=[{"profile": LEGACY, "version": "1.0.0"}],
            )
        )
        with pytest.raises(SourceRuleError) as caught:
            registry.save_source(
                _source(
                    "b",
                    match=_dataset("b.log"),
                    compat_profiles=[{"profile": LEGACY, "version": "1.1.0"}],
                )
            )
        [(field, detail)] = _problems(caught)
        assert field == "compat_profiles"
        assert "a binds 1.0.0" in detail

    def test_an_update_that_omits_the_bindings_keeps_them_and_null_inherits(self, registry):
        registry.create_source_from_write(
            {
                "source": "auth",
                "match": _dataset("system.auth"),
                "aliases": {LEGACY: ["authentication"]},
                "compat_profiles": [{"profile": LEGACY}],
            }
        )
        body = {"source": "auth", "match": _dataset("system.auth")}
        registry.update_source_from_write("auth", {**body, "description": "renamed"})
        kept = registry.get_source("auth")
        assert kept.compat_profiles == [CompatBinding(profile=LEGACY)]
        assert kept.aliases == {LEGACY: ["authentication"]}

        registry.update_source_from_write("auth", {**body, "compat_profiles": []})
        assert registry.get_source("auth").compat_profiles == []

        registry.update_source_from_write("auth", {**body, "compat_profiles": None})
        assert registry.get_source("auth").compat_profiles is None

    def test_the_deployment_bindings_apply_to_a_source_with_no_list(self, registry, store):
        store.put_compat(
            CompatSetting(profiles=[CompatBinding(profile=LEGACY)]), "test", base_revision=None
        )
        registry.save_source(
            _source("auth", match=_dataset("system.auth"), aliases={LEGACY: ["authentication"]})
        )
        assert registry.ingest_rules().defaults == (CompatBinding(profile=LEGACY),)
