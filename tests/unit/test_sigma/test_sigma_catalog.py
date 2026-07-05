#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_catalog.py
#  Purpose:      Tests for the id-keyed sigma catalogue: upsert/merge, selection, providers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SigmaCatalogStore / SigmaSelectionStore / SigmaProviderStore over a real GitCrud.

No mocks: a real local (no-remote) dulwich repo, the real gitcrud engine, and the
real deep_merge existing-wins merge - so the idempotent upsert + local-edit-wins
behaviour is exercised end to end.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.sigma.catalog import (
    SigmaCatalogStore,
    SigmaProviderStore,
    SigmaSelectionStore,
)
from dfe_engine.sigma.providers import ProviderKind, SigmaRuleDoc

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _doc(
    rule_id: str = _UUID_A,
    modified: str = "2023-05-01",
    origin: str = "provider:test",
    title: str = "Sample",
    level: str = "high",
) -> SigmaRuleDoc:
    return SigmaRuleDoc(
        id=rule_id,
        title=title,
        rule={
            "title": title,
            "id": rule_id,
            "level": level,
            "logsource": {"product": "windows", "category": "process_creation"},
            "detection": {"selection": {"Image|endswith": "\\evil.exe"}, "condition": "selection"},
            "modified": modified,
        },
        modified=modified,
        date="2022-01-01",
        origin=origin,
        source_ref="rules/a.yml",
    )


# ── Upsert: add / skip / update ─────────────────────────────


def test_import_new_rule_adds_with_provenance(crud):
    store = SigmaCatalogStore(crud)
    report = store.import_docs([_doc()], actor="alice", source="test")
    assert report.added == 1
    assert report.skipped == 0
    assert report.committed is True
    assert report.commit_sha

    stored = store.get_rule(_UUID_A)
    assert stored["id"] == _UUID_A
    assert stored["provenance"]["origin"] == "provider:test"
    assert stored["provenance"]["local_edited"] is False
    # upstream_modified round-trips (stored as YAML date, read back as text)
    assert str(stored["provenance"]["upstream_modified"]) == "2023-05-01"


def test_reimport_unchanged_is_noop(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc()], actor="alice", source="test")
    head = crud.head_revision()

    report = store.import_docs([_doc()], actor="alice", source="test")
    assert report.added == 0
    assert report.skipped == 1
    assert report.committed is False
    assert report.commit_sha is None
    # no new commit was made (idempotent - survives the YAML date round-trip)
    assert crud.head_revision() == head


def test_reimport_changed_upstream_updates(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc(modified="2023-05-01", level="high")], actor="a", source="test")
    report = store.import_docs(
        [_doc(modified="2023-09-09", level="critical")], actor="a", source="test"
    )
    assert report.updated == 1
    assert report.skipped == 0
    stored = store.get_rule(_UUID_A)
    assert stored["rule"]["level"] == "critical"
    assert str(stored["provenance"]["upstream_modified"]) == "2023-09-09"
    assert stored["provenance"]["local_edited"] is False


# ── Local-edit-wins merge (the core Task B contract) ────────


def test_local_edit_survives_reimport_with_upstream_change(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc(modified="2023-05-01", level="high")], actor="a", source="test")

    # operator edits the rule locally (bumps level to informational, adds a tag)
    edited_rule = store.get_rule(_UUID_A)["rule"]
    edited_rule["level"] = "informational"
    edited_rule["tags"] = ["local.only"]
    store.edit_rule(_UUID_A, edited_rule, actor="op", title="Renamed")

    # upstream ships a change (level critical, new modified date)
    report = store.import_docs(
        [_doc(modified="2024-01-01", level="critical", title="Upstream")],
        actor="a",
        source="test",
    )
    assert report.merged == 1
    assert report.updated == 0

    stored = store.get_rule(_UUID_A)
    # local edit wins - level stays the operator's value, not upstream 'critical'
    assert stored["rule"]["level"] == "informational"
    assert stored["rule"]["tags"] == ["local.only"]
    # local title preserved
    assert stored["title"] == "Renamed"
    # provenance: still local, drift recorded, upstream_modified advanced
    prov = stored["provenance"]
    assert prov["local_edited"] is True
    assert prov["drift"] is True
    assert str(prov["upstream_modified"]) == "2024-01-01"


def test_locally_edited_reimport_without_upstream_change_is_noop(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc(modified="2023-05-01")], actor="a", source="test")
    store.adopt_rule(_UUID_A, actor="op")  # mark local_edited without content change
    head = crud.head_revision()
    report = store.import_docs([_doc(modified="2023-05-01")], actor="a", source="test")
    assert report.skipped == 1
    assert report.merged == 0
    assert crud.head_revision() == head


# ── Edit / adopt / delete ───────────────────────────────────


def test_edit_marks_local_edited(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc()], actor="a", source="test")
    rule = store.get_rule(_UUID_A)["rule"]
    rule["level"] = "low"
    store.edit_rule(_UUID_A, rule, actor="op")
    assert store.get_rule(_UUID_A)["provenance"]["local_edited"] is True
    assert store.get_rule(_UUID_A)["rule"]["level"] == "low"


def test_adopt_pins_rule(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc()], actor="a", source="test")
    store.adopt_rule(_UUID_A, actor="op")
    assert store.get_rule(_UUID_A)["provenance"]["local_edited"] is True


def test_delete_rule(crud):
    store = SigmaCatalogStore(crud)
    store.import_docs([_doc()], actor="a", source="test")
    assert store.exists(_UUID_A)
    store.delete_rule(_UUID_A, actor="op")
    assert not store.exists(_UUID_A)


def test_summaries_and_multi_rule_import(crud):
    store = SigmaCatalogStore(crud)
    report = store.import_docs([_doc(_UUID_A), _doc(_UUID_B, title="Two")], actor="a", source="hq")
    assert report.added == 2
    ids = {s["id"] for s in store.summaries()}
    assert ids == {_UUID_A, _UUID_B}


# ── Selection CRUD (Task C) ─────────────────────────────────


def test_select_deselect_list(crud):
    sel = SigmaSelectionStore(crud)
    assert sel.list_selected() == []
    assert sel.select(_UUID_A, actor="op") is True
    assert sel.select(_UUID_A, actor="op") is False  # already selected
    assert sel.select(_UUID_B, actor="op") is True
    assert set(sel.list_selected()) == {_UUID_A, _UUID_B}
    assert sel.is_selected(_UUID_A) is True

    assert sel.deselect(_UUID_A, actor="op") is True
    assert sel.deselect(_UUID_A, actor="op") is False  # not selected now
    assert sel.list_selected() == [_UUID_B]


# ── Provider config store ───────────────────────────────────


def test_provider_store_lists_builtin_default(crud):
    store = SigmaProviderStore(crud)
    names = {c.name for c in store.list_configs()}
    assert "sigmahq" in names
    default = store.get_config("sigmahq")
    assert default.kind == ProviderKind.GIT_REPO
    assert "SigmaHQ/sigma" in default.options["url"]


def test_provider_store_save_and_disable(crud):
    from dfe_engine.sigma.providers import ProviderConfig

    store = SigmaProviderStore(crud)
    cfg = ProviderConfig(
        name="myrepo",
        kind=ProviderKind.GIT_REPO,
        options={"url": "https://example.com/r.git", "subdir": "rules"},
    )
    store.save_config(cfg, actor="op")
    assert store.get_config("myrepo").options["url"] == "https://example.com/r.git"

    # disabling the built-in default materialises it into the store
    updated = store.set_enabled("sigmahq", False, actor="op")
    assert updated.enabled is False
    assert store.get_config("sigmahq").enabled is False


def test_provider_store_delete_reverts_to_builtin(crud):
    store = SigmaProviderStore(crud)
    store.set_enabled("sigmahq", False, actor="op")  # materialise a disabled override
    assert store.get_config("sigmahq").enabled is False
    store.delete_config("sigmahq", actor="op")
    # reverts to the built-in default (enabled)
    assert store.get_config("sigmahq").enabled is True
