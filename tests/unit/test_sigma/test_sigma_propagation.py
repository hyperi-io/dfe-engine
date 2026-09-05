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
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.deploy_repo import DeployRepoStore
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


def _add_windows_source(env, name: str = "windows-audit") -> None:
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
    a = binding_rule_id(_ID, "windows-audit")
    assert a == binding_rule_id(_ID, "windows-audit")  # deterministic
    assert a != binding_rule_id(_ID, "linux-syslog")  # source-scoped
    assert "-" not in a.removeprefix("sigma_windows-audit_")  # uuid hyphens stripped


def test_sigma_level_maps_informational_to_low():
    assert sigma_level({"level": "informational"}) == "low"
    assert sigma_level({"level": "critical"}) == "critical"
    assert sigma_level({}) == "medium"


# -- Task A: rule generation over the {source}_sigma view ----


def test_propagate_creates_binding_over_sigma_view(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()
    rid = binding_rule_id(_ID, "windows-audit")
    assert report.created == [rid]
    assert report.total_selected == 1

    rule = env.rules.get(rid)
    # targets the sigma VIEW, references Sigma field names, carries the back-ref
    assert rule.source_table == "windows-audit_sigma"
    assert rule.source == "windows-audit"
    assert "Image ILIKE" in rule.where_clause
    assert rule.sigma_rule_id == _ID
    assert rule.severity == "high"
    assert rule.sigma_provenance["source"] == "windows-audit"
    assert rule.sigma_provenance["upstream_modified"] == "2023-05-01"
    assert rule.sigma_provenance["generated_hash"]


def test_propagate_binds_one_rule_to_every_matching_source(env):
    _add_windows_source(env, "windows-audit")
    _add_windows_source(env, "windows-sysmon")
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()
    assert set(report.created) == {
        binding_rule_id(_ID, "windows-audit"),
        binding_rule_id(_ID, "windows-sysmon"),
    }
    assert set(report.hunts_touched) == {
        sigma_hunt_name("windows-audit"),
        sigma_hunt_name("windows-sysmon"),
    }


def test_deselect_then_repropagate_reports_stale_binding(env):
    # Deselecting a rule then re-propagating leaves its binding behind - the
    # propagate report must SURFACE it as stale (and list_bindings flag it) so the
    # operator knows to delete it, instead of it silently firing forever.
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    rid = binding_rule_id(_ID, "windows-audit")

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
            "source": "linux-syslog",
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": "linux-syslog"},
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
    rid = binding_rule_id(_ID, "windows-audit")
    assert report.updated == []
    assert [s["rule_id"] for s in report.skipped_drifted] == [rid]
    # the binding still runs in the hunt (not deleted)
    assert rid in [r["rule_name"] for r in env.hunts.get(sigma_hunt_name("windows-audit"))["rules"]]


def test_force_regenerates_a_drifted_rule(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    env.catalog.adopt_rule(_ID, actor="op")

    report = prop.propagate(force=True)
    assert report.updated == [binding_rule_id(_ID, "windows-audit")]
    assert report.skipped_drifted == []


def test_propagate_skips_hand_edited_binding(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()

    # operator hand-edits the generated binding's WHERE clause
    rid = binding_rule_id(_ID, "windows-audit")
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
    assert report.created == [binding_rule_id(_ID, "windows-audit")]
    assert report.skipped_drifted == []


def test_clean_repropagate_is_idempotent_content(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    prop = _propagator(env)
    prop.propagate()
    rid = binding_rule_id(_ID, "windows-audit")
    created_at = env.rules.get(rid).created_at

    prop.propagate()  # clean re-run regenerates but preserves created_at
    assert env.rules.get(rid).created_at == created_at


# -- Task B: rule -> hunt binding ----------------------------


def test_propagate_creates_per_source_hunt_including_binding(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    _propagator(env).propagate(hunt_cron="*/5 * * * *", hunt_customers=["acme"])
    hunt = env.hunts.get(sigma_hunt_name("windows-audit"))
    rid = binding_rule_id(_ID, "windows-audit")
    assert [r["rule_name"] for r in hunt["rules"]] == [rid]
    assert hunt["cron"] == "*/5 * * * *"
    assert hunt["customers"] == ["acme"]
    assert hunt["global_source_table_name"] == "windows-audit_sigma"


def test_propagate_preserves_existing_hunt_rules_and_overrides(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    # a pre-existing per-source hunt with an operator rule + a per-rule override
    env.hunts.save(
        sigma_hunt_name("windows-audit"),
        {
            "display_name": "Windows",
            "cron": "*/10 * * * *",
            "global_target_table_name": "detection",
            "customers": ["acme"],
            "rules": [{"rule_name": "operator_rule", "target_table_name": "custom"}],
        },
    )

    _propagator(env).propagate()
    hunt = env.hunts.get(sigma_hunt_name("windows-audit"))
    by_name = {r["rule_name"]: r for r in hunt["rules"]}
    # the operator rule + its override survive; the binding is appended
    assert by_name["operator_rule"]["target_table_name"] == "custom"
    assert binding_rule_id(_ID, "windows-audit") in by_name
    assert hunt["cron"] == "*/10 * * * *"  # existing hunt fields untouched


def test_propagate_without_hunts_flag_creates_no_hunt(env):
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate(create_hunts=False)
    assert report.created == [binding_rule_id(_ID, "windows-audit")]
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
    rid = binding_rule_id(_ID, "windows-audit")

    routing = prop.delete_binding(rid)
    assert routing is not None
    assert routing.review_required is False  # directory backend: nothing to review
    assert env.rules.exists(rid) is False
    # the per-source hunt held only this binding -> removed when emptied
    assert sigma_hunt_name("windows-audit") not in [h["name"] for h in env.hunts.list_hunts()]


def test_delete_binding_none_for_non_sigma_rule(env):
    from dfe_engine.hunts.rule_model import Rule

    env.rules.save(Rule(rule_id="plain", name="Plain", where_clause="x = 1"))
    prop = _propagator(env)
    assert prop.delete_binding("plain") is None
    assert env.rules.exists("plain") is True  # a hand-authored rule is not touched


# -- Review routing (production+team) ------------------------


class _RecordingForge:
    """ForgeProvider seam double: one PR per review branch, numbered in order."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        number = len(self.calls)
        return PullRequest(number=number, url=f"http://forge/pr/{number}", branch=head)


def _rebind(env, *, environment: str, mode: str, forge=None) -> None:
    """Point the rule + hunt registries at the deploy repo, at one write posture."""

    def _store(cls_name: str) -> DeployRepoStore:
        return DeployRepoStore(env.gc, cls_name, environment=environment, mode=mode, forge=forge)

    env.rules.close()
    env.hunts.close()
    env.rules = RuleRegistry(deploy_repo=_store("rules"))
    env.hunts = HuntConfigRegistry(deploy_repo=_store("hunts"))


@pytest.fixture
def review_env(env):
    """The same stores, with rules + hunts written at a production+team posture.

    Rebinding the two registries onto the deploy repo is what makes route_write
    refuse main, so a propagate here lands every write on a review branch.
    """
    env.forge = _RecordingForge()
    _rebind(env, environment="production", mode="team", forge=env.forge)
    return env


def test_propagate_says_review_required_and_names_every_branch(review_env):
    """The report cannot read created/updated while every write sits unmerged."""
    _add_windows_source(review_env)
    _import_and_select(review_env, _rule_yaml(), _ID)
    rid = binding_rule_id(_ID, "windows-audit")

    report = _propagator(review_env).propagate()

    assert report.created == [rid]
    assert report.review_required is True
    # one branch and one PR per write: the binding rule, then the per-source hunt
    assert len(report.review_branches) == 2
    assert report.pr_urls == ["http://forge/pr/1", "http://forge/pr/2"]
    assert all(b.startswith("dfe/") for b in report.review_branches)
    assert report.as_dict()["review_required"] is True
    # main never got either file, so the hunt runner's git-sync does not serve them
    assert not (review_env.gc.repo_path / "config" / "rules" / f"{rid}.yaml").exists()
    assert not (
        review_env.gc.repo_path / "config" / "hunts" / f"{sigma_hunt_name('windows-audit')}.yaml"
    ).exists()


def test_delete_binding_says_review_required(env):
    """A delete the runner will keep running until the branch merges says so."""
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)
    _rebind(env, environment="dev", mode="solo")
    _propagator(env).propagate()
    rid = binding_rule_id(_ID, "windows-audit")

    forge = _RecordingForge()
    _rebind(env, environment="production", mode="team", forge=forge)
    routing = _propagator(env).delete_binding(rid)

    assert routing is not None
    assert routing.review_required is True
    assert routing.first_pr_url == routing.pr_urls[0]
    # the rule is still on main, so the hunt runner still compiles it
    assert (env.gc.repo_path / "config" / "rules" / f"{rid}.yaml").is_file()


def test_a_dev_posture_propagate_reports_no_review(env):
    """The directory-backed registries report nothing, so the report stays quiet."""
    _add_windows_source(env)
    _import_and_select(env, _rule_yaml(), _ID)

    report = _propagator(env).propagate()

    assert report.review_required is False
    assert report.review_branches == []
    assert report.pr_urls == []
