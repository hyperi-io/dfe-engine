#  Project:      dfe-engine
#  File:         tests/governance/test_action_params.py
#  Purpose:      Constrained action params - model validation, resolution, invoke
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Constrained invoke-time params over a real local gitops repo (no mocks).

Every param carries a CLOSED constraint (enum values or numeric bounds) -
free-string params must be impossible to declare. References substitute the
WHOLE VarChange.value, never inside strings, and never into cls/name/path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import (
    ActionDef,
    ActionStore,
    InvalidParamsError,
    ParamSpec,
)


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("actions", "governance/actions", rbac_prefix="governance"),
        ]
    )
    return GitCrud(repo, registry)


@pytest.fixture
def store(crud):
    return ActionStore(crud)


def _surge_action(**overrides):
    doc = {
        "name": "receiver-surge",
        "description": "Raise the receiver ceiling",
        "params": {
            "level": {
                "type": "enum",
                "values": ["2x", "max"],
                "default": "2x",
            }
        },
        "changes": [
            {
                "cls": "helmvars",
                "name": "receiver-default",
                "path": "keda.maxReplicas",
                "value": {"$param": "level", "map": {"2x": 8, "max": 20}},
            }
        ],
    }
    doc.update(overrides)
    return ActionDef.model_validate(doc)


class TestParamSpecConstraints:
    def test_enum_requires_values(self):
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "enum"})

    def test_enum_rejects_empty_values(self):
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "enum", "values": []})

    def test_numeric_requires_both_bounds(self):
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "int", "min": 1})
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "float", "max": 5.0})

    def test_numeric_rejects_inverted_bounds(self):
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "int", "min": 10, "max": 1})

    def test_no_free_string_type(self):
        # the whole point: an unconstrained string param cannot be declared
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "str"})

    def test_default_must_satisfy_the_constraint(self):
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "enum", "values": ["a"], "default": "b"})
        with pytest.raises(ValidationError):
            ParamSpec.model_validate({"type": "int", "min": 1, "max": 5, "default": 9})

    def test_valid_specs(self):
        ParamSpec.model_validate({"type": "enum", "values": ["a", "b"], "default": "a"})
        ParamSpec.model_validate({"type": "int", "min": 0, "max": 100})
        ParamSpec.model_validate({"type": "float", "min": 0.1, "max": 0.9, "default": 0.5})


class TestActionDefParamWiring:
    def test_reference_to_undeclared_param_rejected(self):
        with pytest.raises(ValidationError):
            _surge_action(
                changes=[
                    {
                        "cls": "helmvars",
                        "name": "receiver-default",
                        "path": "keda.maxReplicas",
                        "value": {"$param": "nope"},
                    }
                ]
            )

    def test_map_only_on_enum_params(self):
        with pytest.raises(ValidationError):
            _surge_action(
                params={"n": {"type": "int", "min": 1, "max": 9}},
                changes=[
                    {
                        "cls": "helmvars",
                        "name": "receiver-default",
                        "path": "keda.maxReplicas",
                        "value": {"$param": "n", "map": {"1": 2}},
                    }
                ],
            )

    def test_map_must_cover_every_enum_value(self):
        with pytest.raises(ValidationError):
            _surge_action(
                changes=[
                    {
                        "cls": "helmvars",
                        "name": "receiver-default",
                        "path": "keda.maxReplicas",
                        "value": {"$param": "level", "map": {"2x": 8}},
                    }
                ]
            )

    def test_reference_with_stray_keys_rejected(self):
        with pytest.raises(ValidationError):
            _surge_action(
                changes=[
                    {
                        "cls": "helmvars",
                        "name": "receiver-default",
                        "path": "keda.maxReplicas",
                        "value": {"$param": "level", "map": {"2x": 8, "max": 20}, "x": 1},
                    }
                ]
            )

    def test_plain_dict_value_stays_a_literal(self):
        # a dict WITHOUT $param is a whole-subtree literal, not a reference
        action = _surge_action(
            params={},
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "resources",
                    "value": {"limits": {"cpu": "2"}},
                }
            ],
        )
        assert action.changes[0].value == {"limits": {"cpu": "2"}}


class TestInvokeWithParams:
    def test_mapped_enum_param_applies(self, store, crud):
        store.save(_surge_action(), actor="admin")
        res = store.invoke("receiver-surge", "op", params={"level": "max"})
        assert res.changed is True
        assert crud.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 20
        assert res.diff[0]["new"] == 20

    def test_default_applies_when_param_omitted(self, store, crud):
        store.save(_surge_action(), actor="admin")
        res = store.invoke("receiver-surge", "op")
        assert crud.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 8
        assert res.diff[0]["new"] == 8

    def test_unmapped_param_substitutes_whole_value(self, store, crud):
        action = _surge_action(
            params={"ceiling": {"type": "int", "min": 1, "max": 50}},
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": {"$param": "ceiling"},
                }
            ],
        )
        store.save(action, actor="admin")
        store.invoke("receiver-surge", "op", params={"ceiling": 17})
        assert crud.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 17

    def test_out_of_enum_value_rejected(self, store):
        store.save(_surge_action(), actor="admin")
        with pytest.raises(InvalidParamsError):
            store.invoke("receiver-surge", "op", params={"level": "11x"})

    def test_out_of_bounds_rejected(self, store):
        action = _surge_action(
            params={"ceiling": {"type": "int", "min": 1, "max": 50}},
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": {"$param": "ceiling"},
                }
            ],
        )
        store.save(action, actor="admin")
        with pytest.raises(InvalidParamsError):
            store.invoke("receiver-surge", "op", params={"ceiling": 51})

    def test_bool_is_not_an_int(self, store):
        action = _surge_action(
            params={"ceiling": {"type": "int", "min": 0, "max": 50}},
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": {"$param": "ceiling"},
                }
            ],
        )
        store.save(action, actor="admin")
        with pytest.raises(InvalidParamsError):
            store.invoke("receiver-surge", "op", params={"ceiling": True})

    def test_missing_required_param_rejected(self, store):
        action = _surge_action(
            params={"level": {"type": "enum", "values": ["2x", "max"]}},  # no default
        )
        store.save(action, actor="admin")
        with pytest.raises(InvalidParamsError):
            store.invoke("receiver-surge", "op")

    def test_unknown_param_rejected(self, store):
        store.save(_surge_action(), actor="admin")
        with pytest.raises(InvalidParamsError):
            store.invoke("receiver-surge", "op", params={"level": "max", "extra": 1})

    def test_dry_run_resolves_but_does_not_commit(self, store, crud):
        store.save(_surge_action(), actor="admin")
        head = crud.head_revision()
        res = store.invoke("receiver-surge", "op", params={"level": "max"}, dry_run=True)
        assert res.dry_run is True
        assert res.diff[0]["new"] == 20
        assert crud.head_revision() == head


class TestPreviewWithParams:
    def test_preview_uses_defaults_and_representatives(self, store):
        # level has a default (2x); ceiling has none -> representative = min bound
        action = _surge_action(
            params={
                "level": {"type": "enum", "values": ["2x", "max"], "default": "2x"},
                "ceiling": {"type": "int", "min": 3, "max": 50},
            },
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": {"$param": "level", "map": {"2x": 8, "max": 20}},
                },
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.minReplicas",
                    "value": {"$param": "ceiling"},
                },
            ],
        )
        diff, errors = store.preview(action)
        assert errors == []
        by_path = {d["path"]: d for d in diff}
        assert by_path["keda.maxReplicas"]["new"] == 8
        assert by_path["keda.minReplicas"]["new"] == 3

    def test_preview_checks_every_map_branch(self, store):
        # the NON-default branch carries a banned floating tag - preview must
        # catch it, not leave it to explode at invoke time
        action = _surge_action(
            params={"ver": {"type": "enum", "values": ["ok", "bad"], "default": "ok"}},
            changes=[
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "image.tag",
                    "value": {"$param": "ver", "map": {"ok": "1.2.3", "bad": "latest"}},
                }
            ],
        )
        diff, errors = store.preview(action)
        assert any("map branch 'bad'" in e for e in errors)
        assert any("latest" in e for e in errors)
