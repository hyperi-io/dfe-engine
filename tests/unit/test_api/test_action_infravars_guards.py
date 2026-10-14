#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_action_infravars_guards.py
#  Purpose:      A defined action meets the infra overlay's guards, as a direct write does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An action that writes the infra overlay is refused where a direct write is.

Actions write the same overlay files as the backing-services router, so an
operator holding only ``action:invoke:<name>`` could otherwise lower a broker
count the router refuses to lower for anyone, or size a service a provider runs.
The guards judge what the whole action leaves, so two changes to one value are
judged on the last. Validate reports each of those refusals as an error, on every
enum map branch, so a wizard that validates first is not refused at invoke.

Everything runs against a real dulwich deploy repo.
"""

import pytest

from dfe_engine.settings import BackingFactSettings, BackingSettings
from tests.unit.test_api.test_governed_ops import _scoped_headers, _wire_gitcrud

_ACTIONS = "/api/v1/governance/admin/actions"

_CONFLUENT = BackingFactSettings(provider="confluent-cloud", tier="basic")


def _action(name: str, changes: list[dict], params: dict | None) -> dict:
    return {
        "name": name,
        "description": name,
        "required_action": f"action:invoke:{name}",
        "params": params or {},
        "changes": changes,
    }


def _define(client, headers, name: str, changes: list[dict], params: dict | None = None) -> None:
    resp = client.post(_ACTIONS, json=_action(name, changes, params), headers=headers)
    assert resp.status_code == 201, resp.text


def _validate(client, headers, name: str, changes: list[dict], params: dict | None = None):
    resp = client.post(f"{_ACTIONS}/validate", json=_action(name, changes, params), headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _invoke(client, headers, name: str, *, dry_run: bool = False, params: dict | None = None):
    return client.post(
        f"/api/v1/governance/actions/{name}/invoke",
        params={"dry_run": dry_run},
        json={"params": params} if params else None,
        headers=headers,
    )


def _change(name: str, path: str, value: object) -> dict:
    return {"cls": "infravars", "name": name, "path": path, "value": value}


class TestAnActionCannotSizeAManagedService:
    """The 409 the router answers for a value DFE does not set is the action's answer too."""

    @pytest.mark.parametrize("dry_run", [False, True], ids=["invoke", "dry-run"])
    def test_a_read_only_value_is_refused_whatever_the_caller_holds(
        self, client, app, admin_headers, tmp_path, dry_run
    ):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.deployment.backing = BackingSettings(kafka=_CONFLUENT)
        _define(client, admin_headers, "size-kafka", [_change("kafka", "kafka.replicas", 9)])

        resp = _invoke(client, admin_headers, "size-kafka", dry_run=dry_run)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "managed_by_provider"
        assert resp.json()["context"]["provider"] == "confluent-cloud"
        assert resp.json()["context"]["path"] == "kafka.replicas"
        assert gc.list("infravars") == []

    def test_an_invoker_holding_only_the_action_is_refused_too(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.deployment.backing = BackingSettings(
            clickhouse=BackingFactSettings(provider="clickhouse-cloud")
        )
        _define(
            client,
            admin_headers,
            "size-clickhouse",
            [_change("clickhouse-cluster", "clickhouse.storage.size", "2Ti")],
        )
        invoker = _scoped_headers(
            app,
            api_settings,
            username="ch-invoker",
            role="ch-invoker",
            permissions=["governance:read", "action:invoke:size-clickhouse"],
        )

        resp = _invoke(client, invoker, "size-clickhouse")

        assert resp.status_code == 409, resp.text
        assert resp.json()["context"]["set_in"] == "provider"
        assert gc.list("infravars") == []

    def test_it_answers_before_the_count_guard(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        app.state.settings.deployment.backing = BackingSettings(kafka=_CONFLUENT)
        _define(client, admin_headers, "shrink-managed", [_change("kafka", "kafka.replicas", 2)])

        resp = _invoke(client, admin_headers, "shrink-managed")

        assert resp.status_code == 409, resp.text
        assert gc.get("infravars", "kafka") == {"kafka": {"replicas": 5}}

    def test_a_value_outside_the_read_only_set_is_applied(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.deployment.backing = BackingSettings(kafka=_CONFLUENT)
        _define(
            client, admin_headers, "rename-kafka", [_change("kafka", "kafka.name", "dfe-kafka")]
        )

        resp = _invoke(client, admin_headers, "rename-kafka")

        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "kafka") == {"kafka": {"name": "dfe-kafka"}}

    def test_an_action_can_hand_an_external_service_back_by_its_mode(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        app.state.settings.deployment.backing = BackingSettings(
            kafka=BackingFactSettings(provider="strimzi")
        )
        _define(client, admin_headers, "back-to-dfe", [_change("common", "kafka.mode", "cluster")])

        resp = _invoke(client, admin_headers, "back-to-dfe")

        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "common") == {"kafka": {"mode": "cluster"}}


class TestAnActionCannotShrinkAMemberCount:
    @pytest.mark.parametrize("dry_run", [False, True], ids=["invoke", "dry-run"])
    def test_lowering_kafka_brokers_is_refused_with_the_routers_reason(
        self, client, app, admin_headers, tmp_path, dry_run
    ):
        """admin holds helmvars:override and is still refused: the hazard is data loss."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _define(client, admin_headers, "shrink-kafka", [_change("kafka", "kafka.replicas", 2)])

        resp = _invoke(client, admin_headers, "shrink-kafka", dry_run=dry_run)

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "scale_down_refused"
        assert "5 -> 2 would remove a member" in resp.json()["message"]
        assert "reassigned off it" in resp.json()["message"]
        assert gc.get("infravars", "kafka") == {"kafka": {"replicas": 5}}

    def test_an_invoker_holding_only_the_action_is_refused_too(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "clickhouse-cluster", {"clickhouse": {"keeper": {"replicas": 3}}}, "t")
        _define(
            client,
            admin_headers,
            "shrink-keeper",
            [_change("clickhouse-cluster", "clickhouse.keeper.replicas", 1)],
        )
        invoker = _scoped_headers(
            app,
            api_settings,
            username="keeper-invoker",
            role="keeper-invoker",
            permissions=["governance:read", "action:invoke:shrink-keeper"],
        )

        resp = _invoke(client, invoker, "shrink-keeper")

        assert resp.status_code == 400, resp.text
        assert "clickhouse.keeper.replicas" in resp.json()["message"]
        assert gc.get("infravars", "clickhouse-cluster")["clickhouse"]["keeper"]["replicas"] == 3

    @pytest.mark.parametrize(
        ("path", "value", "said"),
        [
            ("kafka", {"x": 1}, "this action leaves it undeclared"),
            ("kafka.replicas", None, "this action leaves it undeclared"),
            ("kafka.replicas", "7", "replaces a member count with something else"),
        ],
    )
    def test_a_declared_count_cannot_be_written_away(
        self, client, app, admin_headers, tmp_path, path, value, said
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _define(client, admin_headers, "away", [_change("kafka", path, value)])

        resp = _invoke(client, admin_headers, "away")

        assert resp.status_code == 400, resp.text
        assert said in resp.json()["message"]
        assert gc.get("infravars", "kafka") == {"kafka": {"replicas": 5}}


class TestWhatTheActionLeavesIsWhatIsJudged:
    def test_raising_a_count_is_applied(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 3}}, "tester")
        _define(client, admin_headers, "grow-kafka", [_change("kafka", "kafka.replicas", 6)])

        resp = _invoke(client, admin_headers, "grow-kafka")

        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "kafka")["kafka"]["replicas"] == 6

    def test_two_changes_to_one_count_are_judged_on_the_last(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _define(
            client,
            admin_headers,
            "dip-then-grow",
            [_change("kafka", "kafka.replicas", 2), _change("kafka", "kafka.replicas", 7)],
        )
        _define(
            client,
            admin_headers,
            "grow-then-dip",
            [_change("kafka", "kafka.replicas", 7), _change("kafka", "kafka.replicas", 2)],
        )

        assert _invoke(client, admin_headers, "grow-then-dip").status_code == 400
        assert gc.get("infravars", "kafka")["kafka"]["replicas"] == 5
        assert _invoke(client, admin_headers, "dip-then-grow").status_code == 200
        assert gc.get("infravars", "kafka")["kafka"]["replicas"] == 7

    def test_lowering_the_shared_file_below_the_winning_one_is_allowed(
        self, client, app, admin_headers, tmp_path
    ):
        """The per-chart file wins, so the shared one going down removes no member."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"replicas": 5}}, "tester")
        gc.put("infravars", "kafka", {"kafka": {"replicas": 9}}, "tester")
        _define(client, admin_headers, "shared-down", [_change("common", "kafka.replicas", 2)])

        resp = _invoke(client, admin_headers, "shared-down")

        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "common")["kafka"]["replicas"] == 2

    def test_a_refused_change_fails_the_whole_action(self, client, app, admin_headers, tmp_path):
        """Atomic, as a protected var is: the allowed change does not land either."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _define(
            client,
            admin_headers,
            "mixed",
            [
                _change("kafka", "kafka.resources.requests.cpu", "2"),
                _change("kafka", "kafka.replicas", 1),
            ],
        )

        resp = _invoke(client, admin_headers, "mixed")

        assert resp.status_code == 400, resp.text
        assert gc.get("infravars", "kafka") == {"kafka": {"replicas": 5}}

    def test_another_class_is_not_the_overlays_business(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("helmvars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _define(
            client,
            admin_headers,
            "helm-change",
            [{"cls": "helmvars", "name": "kafka", "path": "kafka.replicas", "value": 2}],
        )

        resp = _invoke(client, admin_headers, "helm-change")

        assert resp.status_code == 200, resp.text
        assert gc.get("helmvars", "kafka")["kafka"]["replicas"] == 2


_KAFKA_FIVE = [("infravars", "kafka", {"kafka": {"replicas": 5}})]

_PARITY = [
    pytest.param(
        [],
        BackingSettings(kafka=_CONFLUENT),
        [_change("kafka", "kafka.replicas", 9)],
        409,
        id="read-only-value",
    ),
    pytest.param(
        [],
        BackingSettings(clickhouse=BackingFactSettings(provider="clickhouse-cloud")),
        [_change("clickhouse-cluster", "clickhouse.storage.size", "2Ti")],
        409,
        id="provider-sized-disk",
    ),
    pytest.param(
        _KAFKA_FIVE,
        BackingSettings(kafka=_CONFLUENT),
        [_change("kafka", "kafka.replicas", 2)],
        409,
        id="managed-before-count",
    ),
    pytest.param(
        [],
        BackingSettings(kafka=_CONFLUENT),
        [_change("kafka", "kafka.name", "dfe-kafka")],
        200,
        id="outside-read-only",
    ),
    pytest.param(
        [("infravars", "common", {"kafka": {"mode": "external"}})],
        BackingSettings(kafka=BackingFactSettings(provider="strimzi")),
        [_change("common", "kafka.mode", "cluster")],
        200,
        id="mode-handback",
    ),
    pytest.param(
        _KAFKA_FIVE, None, [_change("kafka", "kafka.replicas", 2)], 400, id="fewer-brokers"
    ),
    pytest.param(
        [("infravars", "clickhouse-cluster", {"clickhouse": {"keeper": {"replicas": 3}}})],
        None,
        [_change("clickhouse-cluster", "clickhouse.keeper.replicas", 1)],
        400,
        id="fewer-keepers",
    ),
    pytest.param(_KAFKA_FIVE, None, [_change("kafka", "kafka", {"x": 1})], 400, id="parent-drop"),
    pytest.param(
        _KAFKA_FIVE, None, [_change("kafka", "kafka.replicas", None)], 400, id="undeclared"
    ),
    pytest.param(
        _KAFKA_FIVE, None, [_change("kafka", "kafka.replicas", "7")], 400, id="not-a-count"
    ),
    pytest.param(
        [("infravars", "kafka", {"kafka": {"replicas": 3}})],
        None,
        [_change("kafka", "kafka.replicas", 6)],
        200,
        id="more-brokers",
    ),
    pytest.param(
        _KAFKA_FIVE,
        None,
        [_change("kafka", "kafka.replicas", 7), _change("kafka", "kafka.replicas", 2)],
        400,
        id="grow-then-dip",
    ),
    pytest.param(
        _KAFKA_FIVE,
        None,
        [_change("kafka", "kafka.replicas", 2), _change("kafka", "kafka.replicas", 7)],
        200,
        id="dip-then-grow",
    ),
    pytest.param(
        [
            ("infravars", "common", {"kafka": {"replicas": 5}}),
            ("infravars", "kafka", {"kafka": {"replicas": 9}}),
        ],
        None,
        [_change("common", "kafka.replicas", 2)],
        200,
        id="shared-below-winning",
    ),
    pytest.param(
        _KAFKA_FIVE,
        None,
        [
            _change("kafka", "kafka.resources.requests.cpu", "2"),
            _change("kafka", "kafka.replicas", 1),
        ],
        400,
        id="mixed",
    ),
    pytest.param(
        [("helmvars", "kafka", {"kafka": {"replicas": 5}})],
        None,
        [{"cls": "helmvars", "name": "kafka", "path": "kafka.replicas", "value": 2}],
        200,
        id="other-class",
    ),
]


class TestValidateAnswersAsInvokeDoes:
    """Validate reports, as an error, each refusal invoke answers for the same definition."""

    @pytest.mark.parametrize(("seeds", "backing", "changes", "status"), _PARITY)
    def test_each_refusal_is_one_validate_error(
        self, client, app, admin_headers, tmp_path, seeds, backing, changes, status
    ):
        gc = _wire_gitcrud(app, tmp_path)
        for cls, name, doc in seeds:
            gc.put(cls, name, doc, "tester")
        if backing is not None:
            app.state.settings.deployment.backing = backing
        head = gc.head_revision()

        validated = _validate(client, admin_headers, "parity", changes)

        assert gc.head_revision() == head
        _define(client, admin_headers, "parity", changes)
        invoked = _invoke(client, admin_headers, "parity", dry_run=True)
        assert invoked.status_code == status, invoked.text
        if status == 200:
            assert validated["valid"] is True
            assert validated["errors"] == []
        else:
            assert validated["valid"] is False
            (error,) = validated["errors"]
            assert error.endswith(f": {invoked.json()['message']}")


_SIZE = "size"


def _sized(values: list[str], default: str, mapping: dict) -> tuple[list[dict], dict]:
    """A change setting kafka's broker count from an enum param, and that param."""
    params = {_SIZE: {"type": "enum", "values": values, "default": default}}
    changes = [_change("kafka", "kafka.replicas", {"$param": _SIZE, "map": mapping})]
    return changes, params


class TestValidateChecksEveryDetent:
    """An enum map branch invoke refuses is reported, even where the default is accepted."""

    @pytest.mark.parametrize(
        ("lowered", "said"),
        [
            (2, "5 -> 2 would remove a member"),
            (None, "this action leaves it undeclared"),
            ("7", "replaces a member count with something else"),
        ],
    )
    def test_a_detent_that_lowers_the_count_is_reported_by_its_branch(
        self, client, app, admin_headers, tmp_path, lowered, said
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        changes, params = _sized(
            ["keep", "grow", "shrink"], "keep", {"keep": 5, "grow": 9, "shrink": lowered}
        )

        validated = _validate(client, admin_headers, "resize", changes, params)

        assert validated["valid"] is False
        (error,) = validated["errors"]
        assert error.startswith("infravars/kafka:kafka.replicas: map branch 'shrink': ")
        assert said in error
        _define(client, admin_headers, "resize", changes, params)
        for size in ("keep", "grow"):
            accepted = _invoke(client, admin_headers, "resize", dry_run=True, params={_SIZE: size})
            assert accepted.status_code == 200, accepted.text
        refused = _invoke(client, admin_headers, "resize", dry_run=True, params={_SIZE: "shrink"})
        assert refused.status_code == 400, refused.text
        assert error.endswith(f": {refused.json()['message']}")

    def test_every_refusing_detent_is_reported_in_one_pass(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        changes, params = _sized(
            ["keep", "halve", "single"], "keep", {"keep": 5, "halve": 3, "single": 1}
        )

        validated = _validate(client, admin_headers, "resize", changes, params)

        assert len(validated["errors"]) == 2, validated["errors"]
        halve, single = validated["errors"]
        assert "map branch 'halve'" in halve
        assert "5 -> 3 would remove a member" in halve
        assert "map branch 'single'" in single
        assert "5 -> 1 would remove a member" in single

    def test_a_refused_default_is_reported_once(self, client, app, admin_headers, tmp_path):
        """The default branch IS the landed document, so its refusal is not repeated."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        changes, params = _sized(["keep", "shrink"], "shrink", {"keep": 5, "shrink": 2})

        validated = _validate(client, admin_headers, "resize", changes, params)

        (error,) = validated["errors"]
        assert "map branch" not in error
        _define(client, admin_headers, "resize", changes, params)
        refused = _invoke(client, admin_headers, "resize", dry_run=True)
        assert refused.status_code == 400, refused.text
        assert error == f"infravars/kafka:kafka.replicas: {refused.json()['message']}"

    def test_a_detent_on_a_read_only_value_is_reported_once(
        self, client, app, admin_headers, tmp_path
    ):
        """Every detent writes the same read-only path, so the refusal is the same for each."""
        _wire_gitcrud(app, tmp_path)
        app.state.settings.deployment.backing = BackingSettings(kafka=_CONFLUENT)
        changes, params = _sized(["small", "large"], "small", {"small": 3, "large": 9})

        validated = _validate(client, admin_headers, "resize", changes, params)

        (error,) = validated["errors"]
        _define(client, admin_headers, "resize", changes, params)
        for size in ("small", "large"):
            refused = _invoke(client, admin_headers, "resize", dry_run=True, params={_SIZE: size})
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "managed_by_provider"
            assert error == f"infravars/kafka:kafka.replicas: {refused.json()['message']}"
