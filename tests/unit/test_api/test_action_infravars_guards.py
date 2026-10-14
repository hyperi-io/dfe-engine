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
judged on the last.

Everything runs against a real dulwich deploy repo.
"""

import pytest

from dfe_engine.settings import BackingFactSettings, BackingSettings
from tests.unit.test_api.test_governed_ops import _scoped_headers, _wire_gitcrud

_ACTIONS = "/api/v1/governance/admin/actions"

_CONFLUENT = BackingFactSettings(provider="confluent-cloud", tier="basic")


def _define(client, headers, name: str, changes: list[dict]) -> None:
    resp = client.post(
        _ACTIONS,
        json={
            "name": name,
            "description": name,
            "required_action": f"action:invoke:{name}",
            "changes": changes,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


def _invoke(client, headers, name: str, *, dry_run: bool = False):
    return client.post(
        f"/api/v1/governance/actions/{name}/invoke",
        params={"dry_run": dry_run},
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
