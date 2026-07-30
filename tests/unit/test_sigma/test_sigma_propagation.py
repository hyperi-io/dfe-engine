#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_propagation.py
#  Purpose:      Propagate selected sigma rules -> sigma-bound DFE rules + hunts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SigmaPropagator: selected sigma rule -> DFE rule over {source}_sigma view -> hunt.

No mocks and no live ClickHouse: a real (no-remote) gitcrud sigma catalogue, a real
SourceRegistry / RuleRegistry / HuntConfigRegistry over tmp dirs, and the real
SqlBackend conversion. The generated WHERE clause is asserted as a string only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.sigma.catalog import (
    RULES_CLASS,
    SigmaCatalogStore,
    SigmaSelectionStore,
    sigma_registry,
)
from dfe_engine.sigma.propagation import (
    SigmaPropagator,
    binding_rule_id,
    convert_detection_to_where,
    sigma_hunt_name,
    sigma_level,
)
from dfe_engine.sigma.providers import parse_sigma_yaml
from dfe_engine.sigma.source_mapper import SigmaSourceMapper
from dfe_engine.source.registry import SourceRegistry

_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _rule_yaml(rule_id: str = _ID, level: str = "high", modified: str = "2023-05-01") -> str:
    return f"""
title: Suspicious Certutil
id: {rule_id}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Image|endswith: \\certutil.exe
    condition: selection
level: {level}
date: 2022-01-01
modified: {modified}
"""


@pytest.fixture
def env(tmp_path):
    """Real stores over tmp dirs (gitcrud catalogue + directory-backed registries)."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    catalog = SigmaCatalogStore(gc)
    selection = SigmaSelectionStore(gc)
    sources = SourceRegistry(sources_directory=str(tmp_path / "sources"))
    rules = RuleRegistry(rules_directory=str(tmp_path / "rules"))
    hunts = HuntConfigRegistry(hunts_directory=str(tmp_path / "hunts"))
    return SimpleNamespace(
        gc=gc, catalog=catalog, selection=selection, sources=sources, rules=rules, hunts=hunts
    )


def _add_windows_source(env, name: str = "windows_audit") -> None:
    env.sources.save_source(
        {
            "source": name,
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"engine": "MergeTree"},
            "views": [{"standard": "sigma", "taxonomy": "windows"}],
        }
    )


def _import_and_select(env, yaml_text: str, rule_id: str) -> None:
    docs, _ = parse_sigma_yaml(yaml_text, origin="file")
    env.catalog.import_docs(docs, actor="tester", source="file")
    env.selection.select(rule_id, actor="tester")


def _propagator(env) -> SigmaPropagator:
    return SigmaPropagator(
        catalog=env.catalog,
        selection=env.selection,
        source_mapper=SigmaSourceMapper(env.sources),
        rule_registry=env.rules,
        hunt_registry=env.hunts,
        actor="tester",
    )


# -- Conversion helpers --------------------------------------


def test_convert_uses_sigma_field_names_verbatim():
    """No field map: the view aliases columns, so field names emit verbatim."""
    from sigma.collection import SigmaCollection

    rule_dict = SigmaCollection.from_yaml(_rule_yaml()).rules[0].to_dict()
    where = convert_detection_to_where(rule_dict)
    assert "Image ILIKE '%\\\\certutil.exe'" in where  # backslash escaped for CH literal


def test_convert_raises_on_unconvertible_detection():
    # deprecated aggregation-pipe syntax fails to parse -> raises (caller records it)
    bad = {
        "title": "Agg",
        "id": _ID,
        "logsource": {"category": "test", "product": "windows"},
        "detection": {"sel": {"EventID": 1}, "condition": "sel | count() > 5"},
    }
    with pytest.raises(Exception):
        convert_detection_to_where(bad)


def test_binding_rule_id_is_deterministic_and_source_scoped():
    a = binding_rule_id(_ID, "windows_audit")
    assert a == binding_rule_id(_ID, "windows_audit")  # deterministic
    assert a != binding_rule_id(_ID, "linux_syslog")  # source-scoped
    assert "-" not in a.removeprefix("sigma_windows_audit_")  # uuid hyphens stripped


def test_sigma_level_maps_informational_to_low():
    assert sigma_level({"level": "informational"}) == "low"
    assert sigma_level({"level": "critical"}) == "critical"
    assert sigma_level({}) == "medium"


# -- Task A: rule generation over the {source}_sigma view ----


def test_propagate_creates_binding_over_sigma_view(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()
    rid = binding_rule_id(_ID, "windows_audit")
    assert report.created == [rid]
    assert report.total_selected == 1

    rule = env.rules.get(rid)
    # targets the sigma VIEW, references Sigma field names, carries the back-ref
    assert rule.source_table == "windows_audit_sigma"
    assert rule.source == "windows_audit"
    assert "Image ILIKE" in rule.where_clause
    assert rule.sigma_rule_id == _ID
    assert rule.severity == "high"
    assert rule.sigma_provenance["source"] == "windows_audit"
    assert rule.sigma_provenance["upstream_modified"] == "2023-05-01"
    assert rule.sigma_provenance["generated_hash"]


def test_propagate_binds_one_rule_to_every_matching_source(env):
    _add_windows_source(env, "windows_audit")
    _add_windows_source(env, "windows_sysmon")
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()
    assert set(report.created) == {
        binding_rule_id(_ID, "windows_audit"),
        binding_rule_id(_ID, "windows_sysmon"),
    }
    assert set(report.hunts_touched) == {
        sigma_hunt_name("windows_audit"),
        sigma_hunt_name("windows_sysmon"),
    }


def test_deselect_then_repropagate_reports_stale_binding(env):
    # Deselecting a rule then re-propagating leaves its binding behind - the
    # propagate report must SURFACE it as stale (and list_bindings flag it) so the
    # operator knows to delete it, instead of it silently firing forever.
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    rid = binding_rule_id(_ID, "windows_audit")

    env.selection.deselect(_ID, actor="tester")
    report = prop.propagate()

    assert any(b["rule_id"] == rid for b in report.stale_bindings)
    summary = prop.get_binding(rid)
    assert summary["stale"] is True
    assert summary["selected"] is False
    assert summary["drift"] is True  # stale folds into drift for the review surface


def test_propagate_skips_rule_with_no_matching_source(env):
    # source taxonomy 'linux' does not match the rule's product 'windows'
    env.sources.save_source(
        {
            "source": "linux_syslog",
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": "linux_syslog"},
            "schema": {"engine": "MergeTree"},
            "views": [{"standard": "sigma", "taxonomy": "linux"}],
        }
    )
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()
    assert report.created == []
    assert report.skipped_no_source == [_ID]


def test_propagate_records_failed_for_unconvertible_rule(env):
    _add_windows_source(env)
    # seed a catalogue doc with an unconvertible detection directly (bypasses the
    # import-time pySigma validation), then select it.
    raw = GitCrud(env.gc.repo, sigma_registry())
    raw.put(
        RULES_CLASS,
        _ID,
        {
            "id": _ID,
            "title": "Broken",
            "rule": {
                "title": "Broken",
                "id": _ID,
                "logsource": {"category": "process_creation", "product": "windows"},
                "detection": {"sel": {"EventID": 1}, "condition": "sel | count() > 5"},
            },
            "provenance": {"origin": "file", "upstream_modified": "2023-05-01"},
        },
        "tester",
        message="seed broken",
    )
    env.selection.select(_ID, actor="tester")

    report = _propagator(env).propagate()
    assert report.created == []
    assert len(report.failed) == 1
    assert report.failed[0]["sigma_rule_id"] == _ID


# -- Task A: drift is honoured -------------------------------


def test_propagate_skips_drifted_sigma_rule_not_clobbering(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()  # first generation

    # operator adopts the sigma rule (local_edited=True) -> a re-propagate must skip
    env.catalog.adopt_rule(_ID, actor="op")
    report = prop.propagate()
    rid = binding_rule_id(_ID, "windows_audit")
    assert report.updated == []
    assert [s["rule_id"] for s in report.skipped_drifted] == [rid]
    # the binding still runs in the hunt (not deleted)
    assert rid in [r["rule_name"] for r in env.hunts.get(sigma_hunt_name("windows_audit"))["rules"]]


def test_force_regenerates_a_drifted_rule(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    env.catalog.adopt_rule(_ID, actor="op")

    report = prop.propagate(force=True)
    assert report.updated == [binding_rule_id(_ID, "windows_audit")]
    assert report.skipped_drifted == []


def test_propagate_skips_hand_edited_binding(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()

    # operator hand-edits the generated binding's WHERE clause
    rid = binding_rule_id(_ID, "windows_audit")
    edited = env.rules.get(rid).model_copy(update={"where_clause": "hacked = 1"})
    env.rules.save(edited)

    report = prop.propagate()  # sigma rule is clean, but the binding drifted
    assert [s["rule_id"] for s in report.skipped_drifted] == [rid]
    assert env.rules.get(rid).where_clause == "hacked = 1"  # not clobbered


def test_first_generation_of_a_drifted_rule_still_generates(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    env.catalog.adopt_rule(_ID, actor="op")  # drifted BEFORE any binding exists

    report = _propagator(env).propagate()
    # nothing to clobber on first generation -> created, not skipped
    assert report.created == [binding_rule_id(_ID, "windows_audit")]
    assert report.skipped_drifted == []


def test_clean_repropagate_is_idempotent_content(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    rid = binding_rule_id(_ID, "windows_audit")
    created_at = env.rules.get(rid).created_at

    prop.propagate()  # clean re-run regenerates but preserves created_at
    assert env.rules.get(rid).created_at == created_at


# -- Task B: rule -> hunt binding ----------------------------


def test_propagate_creates_per_source_hunt_including_binding(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    _propagator(env).propagate(hunt_cron="*/5 * * * *", hunt_customers=["acme"])
    hunt = env.hunts.get(sigma_hunt_name("windows_audit"))
    rid = binding_rule_id(_ID, "windows_audit")
    assert [r["rule_name"] for r in hunt["rules"]] == [rid]
    assert hunt["cron"] == "*/5 * * * *"
    assert hunt["customers"] == ["acme"]
    assert hunt["global_source_table_name"] == "windows_audit_sigma"


def test_propagate_preserves_existing_hunt_rules_and_overrides(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    # a pre-existing per-source hunt with an operator rule + a per-rule override
    env.hunts.save(
        sigma_hunt_name("windows_audit"),
        {
            "display_name": "Windows",
            "cron": "*/10 * * * *",
            "global_target_table_name": "hunt_results",
            "customers": ["acme"],
            "rules": [{"rule_name": "operator_rule", "target_table_name": "custom"}],
        },
    )

    _propagator(env).propagate()
    hunt = env.hunts.get(sigma_hunt_name("windows_audit"))
    by_name = {r["rule_name"]: r for r in hunt["rules"]}
    # the operator rule + its override survive; the binding is appended
    assert by_name["operator_rule"]["target_table_name"] == "custom"
    assert binding_rule_id(_ID, "windows_audit") in by_name
    assert hunt["cron"] == "*/10 * * * *"  # existing hunt fields untouched


def test_propagate_without_hunts_flag_creates_no_hunt(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate(create_hunts=False)
    assert report.created == [binding_rule_id(_ID, "windows_audit")]
    assert report.hunts_touched == []
    assert env.hunts.list_hunts() == []


# -- Bindings CRUD -------------------------------------------


def test_list_bindings_reports_live_drift(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()

    bindings = prop.list_bindings()
    assert len(bindings) == 1
    assert bindings[0]["drift"] is False

    env.catalog.adopt_rule(_ID, actor="op")  # catalogue now local_edited
    assert prop.list_bindings()[0]["drift"] is True


def test_get_binding_none_for_non_sigma_rule(env):
    from dfe_engine.hunts.rule_model import Rule

    env.rules.save(Rule(rule_id="plain", name="Plain", where_clause="x = 1"))
    prop = _propagator(env)
    assert prop.get_binding("plain") is None
    assert prop.get_binding("does_not_exist") is None


def test_delete_binding_removes_rule_and_unlinks_hunt(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    rid = binding_rule_id(_ID, "windows_audit")

    assert prop.delete_binding(rid) is True
    assert env.rules.exists(rid) is False
    # the per-source hunt held only this binding -> removed when emptied
    assert sigma_hunt_name("windows_audit") not in [h["name"] for h in env.hunts.list_hunts()]


def test_delete_binding_false_for_non_sigma_rule(env):
    from dfe_engine.hunts.rule_model import Rule

    env.rules.save(Rule(rule_id="plain", name="Plain", where_clause="x = 1"))
    prop = _propagator(env)
    assert prop.delete_binding("plain") is False
    assert env.rules.exists("plain") is True  # a hand-authored rule is not touched
