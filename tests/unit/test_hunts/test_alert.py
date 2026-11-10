"""Tests for hunt alert dispatcher (Apprise-based notifications)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.hunts.alert import (
    AlertConfig,
    AlertDestination,
    AlertDestinationNameError,
    AlertDestinationRegistry,
    AlertDispatcher,
    AlertTrigger,
    build_alert_config,
)
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings

# ── AlertTrigger ──────────────────────────────────────────────────


class TestAlertTrigger:
    def test_basic_condition(self):
        t = AlertTrigger(when="result_count > 0")
        assert t.when == "result_count > 0"

    def test_threshold_condition(self):
        t = AlertTrigger(when="result_count >= 10")
        assert t.when == "result_count >= 10"

    def test_field_condition(self):
        t = AlertTrigger(when='severity == "critical"')
        assert t.when == 'severity == "critical"'

    def test_compound_condition(self):
        t = AlertTrigger(when='severity == "critical" && result_count > 0')
        assert t.when == 'severity == "critical" && result_count > 0'

    def test_invalid_expression_rejected(self):
        with pytest.raises(ValueError, match="Invalid alert trigger"):
            AlertTrigger(when="== broken")

    def test_disallowed_function_rejected(self):
        with pytest.raises(ValueError, match="Invalid alert trigger"):
            AlertTrigger(when="[1,2,3].map(x, x * 2)")


# ── AlertConfig ───────────────────────────────────────────────────


class TestAlertConfig:
    def test_defaults(self):
        cfg = AlertConfig(channels=["slack://token/#ch"])
        assert cfg.enabled is True
        assert "DFE Hunt Alert" in cfg.title_template
        assert cfg.triggers == []

    def test_custom_templates(self):
        cfg = AlertConfig(
            channels=["slack://t"],
            title_template="Alert: {hunt_name}",
            body_template="{result_count} hits",
        )
        assert cfg.title_template == "Alert: {hunt_name}"


# ── AlertDestination ─────────────────────────────────────────────


class TestAlertDestination:
    def test_create(self):
        d = AlertDestination(name="slack-alerts", url="slack://t/b/c/")
        assert d.name == "slack-alerts"
        assert d.url == "slack://t/b/c/"
        assert d.enabled is True
        assert d.description == ""

    def test_with_description(self):
        d = AlertDestination(
            name="pd-oncall",
            url="pagerduty://key",
            description="PagerDuty on-call team",
        )
        assert d.description == "PagerDuty on-call team"

    def test_disabled(self):
        d = AlertDestination(name="slack-dev", url="slack://t", enabled=False)
        assert d.enabled is False


# ── AlertDestinationRegistry ─────────────────────────────────────


class TestAlertDestinationRegistry:
    def _registry(self):
        r = AlertDestinationRegistry()
        r.add(AlertDestination(name="slack-alerts", url="slack://t/b/c/"))
        r.add(AlertDestination(name="pd-oncall", url="pagerduty://key"))
        return r

    def test_add_and_get(self):
        r = self._registry()
        d = r.get("slack-alerts")
        assert d.url == "slack://t/b/c/"

    def test_get_unknown_raises(self):
        r = self._registry()
        with pytest.raises(KeyError):
            r.get("nonexistent")

    def test_resolve(self):
        r = self._registry()
        assert r.resolve("slack-alerts") == "slack://t/b/c/"

    def test_resolve_unknown_returns_none(self):
        r = self._registry()
        assert r.resolve("nonexistent") is None

    def test_resolve_disabled_returns_none(self):
        r = AlertDestinationRegistry()
        r.add(AlertDestination(name="slack-dev", url="slack://t", enabled=False))
        assert r.resolve("slack-dev") is None

    def test_resolve_many(self):
        r = self._registry()
        urls = r.resolve_many(["slack-alerts", "pd-oncall"])
        assert urls == ["slack://t/b/c/", "pagerduty://key"]

    def test_resolve_many_skips_unknown(self):
        r = self._registry()
        urls = r.resolve_many(["slack-alerts", "nonexistent", "pd-oncall"])
        assert urls == ["slack://t/b/c/", "pagerduty://key"]

    def test_remove(self):
        r = self._registry()
        assert r.remove("slack-alerts") is True
        assert r.resolve("slack-alerts") is None

    def test_remove_nonexistent(self):
        r = self._registry()
        assert r.remove("nope") is False

    def test_list(self):
        r = self._registry()
        destinations = r.list()
        assert len(destinations) == 2
        names = {d.name for d in destinations}
        assert names == {"slack-alerts", "pd-oncall"}

    def test_overwrite(self):
        r = self._registry()
        r.add(AlertDestination(name="slack-alerts", url="slack://new/url/"))
        assert r.resolve("slack-alerts") == "slack://new/url/"
        assert len(r) == 2

    def test_contains(self):
        r = self._registry()
        assert "slack-alerts" in r
        assert "nope" not in r

    def test_len(self):
        r = self._registry()
        assert len(r) == 2


# ── AlertDispatcher._should_fire ──────────────────────────────────


class TestShouldFire:
    def _dispatcher(self, triggers):
        config = AlertConfig(channels=["slack://t"], triggers=triggers)
        return AlertDispatcher(config)

    def test_fires_on_results(self):
        d = self._dispatcher([AlertTrigger(when="result_count > 0")])
        assert d._should_fire(5, None) is True

    def test_does_not_fire_on_zero(self):
        d = self._dispatcher([AlertTrigger(when="result_count > 0")])
        assert d._should_fire(0, None) is False

    def test_result_count_ge(self):
        d = self._dispatcher([AlertTrigger(when="result_count >= 10")])
        assert d._should_fire(10, None) is True
        assert d._should_fire(9, None) is False

    def test_result_count_gt(self):
        d = self._dispatcher([AlertTrigger(when="result_count > 5")])
        assert d._should_fire(6, None) is True
        assert d._should_fire(5, None) is False

    def test_result_count_eq(self):
        d = self._dispatcher([AlertTrigger(when="result_count == 3")])
        assert d._should_fire(3, None) is True
        assert d._should_fire(4, None) is False

    def test_result_count_lt(self):
        d = self._dispatcher([AlertTrigger(when="result_count < 5")])
        assert d._should_fire(4, None) is True
        assert d._should_fire(5, None) is False

    def test_field_value_match(self):
        d = self._dispatcher([AlertTrigger(when='severity == "critical"')])
        results = [{"severity": "low"}, {"severity": "critical"}]
        assert d._should_fire(2, results) is True

    def test_field_value_no_match(self):
        d = self._dispatcher([AlertTrigger(when='severity == "critical"')])
        results = [{"severity": "low"}, {"severity": "medium"}]
        assert d._should_fire(2, results) is False

    def test_field_value_missing_field(self):
        """Missing field in CEL → evaluate_condition returns False."""
        d = self._dispatcher([AlertTrigger(when='severity == "critical"')])
        results = [{"other": "value"}]
        assert d._should_fire(1, results) is False

    def test_field_value_no_results(self):
        """Field condition with no results → aggregate-only eval, field missing → False."""
        d = self._dispatcher([AlertTrigger(when='severity == "critical"')])
        assert d._should_fire(0, None) is False

    def test_compound_field_and_count(self):
        """Compound condition: field + result_count."""
        d = self._dispatcher([AlertTrigger(when='severity == "critical" && result_count > 0')])
        results = [{"severity": "critical"}]
        assert d._should_fire(1, results) is True

    def test_compound_field_and_count_no_match(self):
        d = self._dispatcher([AlertTrigger(when='severity == "critical" && result_count > 10')])
        results = [{"severity": "critical"}]
        assert d._should_fire(1, results) is False

    def test_multiple_triggers_any_fires(self):
        """Multiple triggers: first match wins (OR logic)."""
        d = self._dispatcher(
            [
                AlertTrigger(when="result_count >= 100"),
                AlertTrigger(when="result_count > 0"),
            ]
        )
        assert d._should_fire(1, None) is True

    def test_no_triggers_does_not_fire(self):
        config = AlertConfig(channels=["slack://t"], triggers=[])
        d = AlertDispatcher(config)
        assert d._should_fire(5, None) is False

    def test_string_contains(self):
        d = self._dispatcher([AlertTrigger(when='message.contains("error")')])
        results = [{"message": "an error occurred"}]
        assert d._should_fire(1, results) is True

    def test_membership_in(self):
        d = self._dispatcher([AlertTrigger(when='severity in ["critical", "high"]')])
        results = [{"severity": "high"}]
        assert d._should_fire(1, results) is True

    def test_membership_in_no_match(self):
        d = self._dispatcher([AlertTrigger(when='severity in ["critical", "high"]')])
        results = [{"severity": "low"}]
        assert d._should_fire(1, results) is False

    def test_logical_or(self):
        d = self._dispatcher([AlertTrigger(when='severity == "critical" || result_count >= 100')])
        results = [{"severity": "low"}]
        assert d._should_fire(100, results) is True

    def test_negation(self):
        d = self._dispatcher([AlertTrigger(when='!(severity == "low")')])
        results = [{"severity": "critical"}]
        assert d._should_fire(1, results) is True

    def test_numeric_field_comparison(self):
        d = self._dispatcher([AlertTrigger(when="score > 80")])
        results = [{"score": 95}]
        assert d._should_fire(1, results) is True


# ── AlertDispatcher.evaluate_and_send ─────────────────────────────


class TestEvaluateAndSend:
    def test_disabled_config_returns_false(self):
        config = AlertConfig(
            channels=["slack://t"],
            triggers=[AlertTrigger(when="result_count > 0")],
            enabled=False,
        )
        d = AlertDispatcher(config)
        assert d.evaluate_and_send("hunt1", "org_a", "rule1", 5) is False

    def test_no_channels_returns_false(self):
        config = AlertConfig(
            channels=[],
            triggers=[AlertTrigger(when="result_count > 0")],
        )
        d = AlertDispatcher(config)
        assert d.evaluate_and_send("hunt1", "org_a", "rule1", 5) is False

    def test_no_triggers_returns_false(self):
        config = AlertConfig(channels=["slack://t"], triggers=[])
        d = AlertDispatcher(config)
        assert d.evaluate_and_send("hunt1", "org_a", "rule1", 5) is False

    @patch.object(AlertDispatcher, "_send", return_value=True)
    def test_sends_when_trigger_fires(self, mock_send):
        config = AlertConfig(
            channels=["slack://t"],
            triggers=[AlertTrigger(when="result_count > 0")],
        )
        d = AlertDispatcher(config)
        result = d.evaluate_and_send("hunt1", "org_a", "rule1", 5)
        assert result is True
        mock_send.assert_called_once()
        title, body = mock_send.call_args[0]
        assert "hunt1" in title
        assert "5" in body
        assert "org_a" in body

    @patch.object(AlertDispatcher, "_send", return_value=True)
    def test_custom_templates(self, mock_send):
        config = AlertConfig(
            channels=["slack://t"],
            triggers=[AlertTrigger(when="result_count > 0")],
            title_template="ALERT: {hunt_name}",
            body_template="{result_count} hits for {customer}",
        )
        d = AlertDispatcher(config)
        d.evaluate_and_send("my_hunt", "acme", "r1", 42)
        title, body = mock_send.call_args[0]
        assert title == "ALERT: my_hunt"
        assert body == "42 hits for acme"


# ── AlertDispatcher._send ─────────────────────────────────────────


class TestSend:
    @patch("dfe_engine.hunts.alert.apprise.Apprise")
    def test_send_success(self, mock_apprise):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = True
        mock_apprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is True
        mock_ap.add.assert_called_once_with("slack://t")
        mock_ap.notify.assert_called_once_with(title="title", body="body")

    @patch("dfe_engine.hunts.alert.apprise.Apprise")
    def test_send_failure(self, mock_apprise):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = False
        mock_apprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is False

    @patch("dfe_engine.hunts.alert.apprise.Apprise")
    def test_send_exception(self, mock_apprise):
        mock_ap = MagicMock()
        mock_ap.notify.side_effect = RuntimeError("network error")
        mock_apprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is False


# ── build_alert_config ────────────────────────────────────────────


class TestBuildAlertConfig:
    def test_no_alerts_section_no_global(self):
        """No alert config at all → None."""
        assert build_alert_config({}) is None

    def test_global_channels_only(self):
        """Global channels with no hunt-level alerts → config with default trigger."""
        config = build_alert_config({}, global_channels=["slack://global"])
        assert config is not None
        assert config.channels == ["slack://global"]
        assert len(config.triggers) == 1
        assert config.triggers[0].when == "result_count > 0"

    def test_hunt_channels_only(self):
        """Hunt-level channels with no global → config with those channels."""
        data = {"alerts": {"channels": ["pagerduty://key"]}}
        config = build_alert_config(data)
        assert config is not None
        assert config.channels == ["pagerduty://key"]

    def test_merge_deduplicated(self):
        """Hunt + global channels merged, duplicates removed."""
        data = {"alerts": {"channels": ["slack://a", "slack://b"]}}
        config = build_alert_config(data, global_channels=["slack://b", "slack://c"])
        assert config is not None
        assert config.channels == ["slack://a", "slack://b", "slack://c"]

    def test_cel_triggers(self):
        data = {
            "alerts": {
                "channels": ["slack://t"],
                "triggers": [
                    {"when": "result_count >= 10"},
                ],
            }
        }
        config = build_alert_config(data)
        assert config is not None
        assert len(config.triggers) == 1
        assert config.triggers[0].when == "result_count >= 10"

    def test_multiple_cel_triggers(self):
        data = {
            "alerts": {
                "channels": ["slack://t"],
                "triggers": [
                    {"when": "result_count > 0"},
                    {"when": 'severity == "critical"'},
                ],
            }
        }
        config = build_alert_config(data)
        assert config is not None
        assert len(config.triggers) == 2

    def test_default_trigger_when_no_triggers_specified(self):
        """Channels but no triggers → defaults to result_count > 0."""
        data = {"alerts": {"channels": ["slack://t"]}}
        config = build_alert_config(data)
        assert config is not None
        assert len(config.triggers) == 1
        assert config.triggers[0].when == "result_count > 0"

    def test_disabled_alerts(self):
        data = {"alerts": {"channels": ["slack://t"], "enabled": False}}
        config = build_alert_config(data)
        assert config is not None
        assert config.enabled is False

    def test_custom_templates(self):
        data = {
            "alerts": {
                "channels": ["slack://t"],
                "title_template": "Custom: {hunt_name}",
                "body_template": "Found {result_count}",
            }
        }
        config = build_alert_config(data)
        assert config is not None
        assert config.title_template == "Custom: {hunt_name}"
        assert config.body_template == "Found {result_count}"

    def test_invalid_alerts_section(self):
        """alerts: not a dict → None."""
        assert build_alert_config({"alerts": "bad"}) is None


# ── build_alert_config with destination registry ─────────────────


class TestBuildAlertConfigWithRegistry:
    @pytest.fixture
    def registry(self):
        r = AlertDestinationRegistry()
        r.add(AlertDestination(name="slack-dfe", url="slack://t/b/c/"))
        r.add(AlertDestination(name="pd-oncall", url="pagerduty://key"))
        r.add(AlertDestination(name="disabled-dest", url="slack://off", enabled=False))
        return r

    def test_resolve_named_destinations(self, registry):
        data = {"alerts": {"destinations": ["slack-dfe", "pd-oncall"]}}
        config = build_alert_config(data, destination_registry=registry)
        assert config is not None
        assert config.channels == ["slack://t/b/c/", "pagerduty://key"]

    def test_unknown_destination_skipped(self, registry):
        data = {"alerts": {"destinations": ["slack-dfe", "nonexistent"]}}
        config = build_alert_config(data, destination_registry=registry)
        assert config is not None
        assert config.channels == ["slack://t/b/c/"]

    def test_disabled_destination_skipped(self, registry):
        data = {"alerts": {"destinations": ["disabled-dest"]}}
        config = build_alert_config(data, destination_registry=registry)
        assert config is None  # no channels resolved

    def test_destinations_plus_raw_channels(self, registry):
        """Named destinations + raw channel URLs coexist."""
        data = {
            "alerts": {
                "destinations": ["slack-dfe"],
                "channels": ["mailto://user:pass@smtp.example.com"],
            }
        }
        config = build_alert_config(data, destination_registry=registry)
        assert config is not None
        assert config.channels == ["slack://t/b/c/", "mailto://user:pass@smtp.example.com"]

    def test_destinations_plus_global_channels(self, registry):
        """Named destinations + global channels merged and deduplicated."""
        data = {"alerts": {"destinations": ["slack-dfe"]}}
        config = build_alert_config(
            data, global_channels=["pagerduty://key"], destination_registry=registry
        )
        assert config is not None
        assert config.channels == ["slack://t/b/c/", "pagerduty://key"]

    def test_destinations_deduped_with_global(self, registry):
        """Destination URL same as global → not duplicated."""
        data = {"alerts": {"destinations": ["pd-oncall"]}}
        config = build_alert_config(
            data, global_channels=["pagerduty://key"], destination_registry=registry
        )
        assert config is not None
        assert config.channels == ["pagerduty://key"]

    def test_no_registry_warns(self):
        """Destinations referenced but no registry → warning, no channels."""
        data = {"alerts": {"destinations": ["slack-dfe"]}}
        config = build_alert_config(data)
        assert config is None

    def test_empty_destinations_list(self, registry):
        """Empty destinations list → falls through to channels/global."""
        data = {"alerts": {"destinations": []}}
        config = build_alert_config(
            data, global_channels=["slack://g"], destination_registry=registry
        )
        assert config is not None
        assert config.channels == ["slack://g"]


# ── AlertDestinationRegistry over the secrets backend ────────────


def _secrets(tmp_path):
    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


def _engine_registry(tmp_path) -> AlertDestinationRegistry:
    """The registry the engine API builds: the secrets backend plus its metadata directory."""
    return AlertDestinationRegistry(secrets=_secrets(tmp_path), directory=str(tmp_path / "dests"))


def _files(root) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


class TestAlertDestinationRegistrySecretsBacked:
    """The URL lives in the secrets backend, the description and owner in the directory."""

    def test_add_get_list_and_overwrite(self, tmp_path):
        r = _engine_registry(tmp_path)
        r.add(AlertDestination(name="slack-a", url="slack://a", description="A", hunt_name="h1"))
        r.add(AlertDestination(name="slack-b", url="slack://b"))
        r.add(AlertDestination(name="slack-b", url="slack://b2", enabled=False))
        try:
            assert r.get("slack-a") == AlertDestination(
                name="slack-a", url="slack://a", description="A", hunt_name="h1"
            )
            assert [d.name for d in r.list()] == ["slack-a", "slack-b"]
            assert len(r) == 2
            assert "slack-a" in r
            assert "slack-c" not in r
            assert r.resolve("slack-a") == "slack://a"
            assert r.resolve("slack-b") is None
        finally:
            r.close()

    def test_the_secret_holds_the_url_and_the_directory_never_does(self, tmp_path):
        r = _engine_registry(tmp_path)
        r.add(AlertDestination(name="pd", url="pagerduty://key", description="On call"))
        r.close()

        secret = tmp_path / "secrets" / "alert-destinations" / "pd"
        assert json.loads(secret.read_text(encoding="utf-8")) == {
            "url": "pagerduty://key",
            "enabled": True,
        }
        assert _files(tmp_path / "dests") == ["pd.yaml"]
        assert "pagerduty://key" not in (tmp_path / "dests" / "pd.yaml").read_text(encoding="utf-8")

    def test_the_runner_reads_by_name_and_lists_nothing(self, tmp_path):
        engine = _engine_registry(tmp_path)
        engine.add(AlertDestination(name="ops", url="json://ops", hunt_name="h1"))
        engine.add(AlertDestination(name="off", url="json://off", enabled=False))
        engine.close()

        runner = AlertDestinationRegistry(secrets=_secrets(tmp_path))

        assert runner.resolve_many(["ops", "off", "absent"]) == ["json://ops"]
        assert runner.get("ops").hunt_name is None
        assert runner.list() == []

    def test_a_change_reaches_a_runner_already_reading(self, tmp_path):
        """Each lookup reads the backend, so the runner needs no restart to see an edit."""
        engine = _engine_registry(tmp_path)
        runner = AlertDestinationRegistry(secrets=_secrets(tmp_path))
        engine.add(AlertDestination(name="ops", url="json://first"))
        assert runner.resolve("ops") == "json://first"

        engine.add(AlertDestination(name="ops", url="json://second"))
        assert runner.resolve("ops") == "json://second"
        engine.add(AlertDestination(name="ops", url="json://second", enabled=False))
        assert runner.resolve("ops") is None
        engine.remove("ops")
        engine.close()
        assert "ops" not in runner

    def test_remove_drops_the_secret_and_the_metadata(self, tmp_path):
        r = _engine_registry(tmp_path)
        r.add(AlertDestination(name="slack-rm", url="slack://t"))
        try:
            assert r.remove("slack-rm") is True
            assert r.remove("slack-rm") is False
            assert r.resolve("slack-rm") is None
            assert r.list() == []
        finally:
            r.close()
        assert _files(tmp_path / "secrets") == []
        assert _files(tmp_path / "dests") == []

    def test_metadata_whose_secret_is_gone_reads_as_absent(self, tmp_path):
        r = _engine_registry(tmp_path)
        r.add(AlertDestination(name="kept", url="slack://k"))
        r.add(AlertDestination(name="orphan", url="slack://o"))
        (tmp_path / "secrets" / "alert-destinations" / "orphan").unlink()
        try:
            assert [d.name for d in r.list()] == ["kept"]
            assert "orphan" not in r
            with pytest.raises(KeyError):
                r.get("orphan")
            assert r.remove("orphan") is True
            assert _files(tmp_path / "dests") == ["kept.yaml"]
        finally:
            r.close()

    def test_a_directory_with_no_secrets_backend_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="needs a secrets backend"):
            AlertDestinationRegistry(directory=str(tmp_path / "dests"))


class TestAlertDestinationNames:
    """A name is a secret name on every backend and a file stem, so it takes the store-name rule."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("../escape", id="parent"),
            pytest.param("nested/../../escape", id="nested-parent"),
            pytest.param("team/slack", id="separator"),
            pytest.param("/abs/escape", id="absolute"),
            pytest.param("nul\x00byte", id="nul"),
            pytest.param(".", id="dot"),
            pytest.param("..", id="dotdot"),
            pytest.param(".hidden", id="leading-dot"),
            pytest.param("has space", id="space"),
            pytest.param("", id="empty"),
            pytest.param("ops\n", id="trailing-newline"),
            pytest.param("x" * 129, id="too-long"),
        ],
    )
    def test_add_refuses_the_name_and_writes_nothing(self, tmp_path, name):
        r = _engine_registry(tmp_path)
        try:
            with pytest.raises(AlertDestinationNameError):
                r.add(AlertDestination(name=name, url="slack://x"))
            with pytest.raises(AlertDestinationNameError):
                AlertDestinationRegistry().add(AlertDestination(name=name, url="slack://x"))
        finally:
            r.close()
        assert _files(tmp_path) == []

    @pytest.mark.parametrize("name", ["a", "ops-webhook", "slack.alerts_2", "9" + "x" * 127])
    def test_a_store_name_is_accepted(self, tmp_path, name):
        r = _engine_registry(tmp_path)
        try:
            r.add(AlertDestination(name=name, url="slack://x"))
            assert r.resolve(name) == "slack://x"
        finally:
            r.close()

    @pytest.mark.parametrize("form", ["absolute", "dotted"])
    def test_a_name_outside_the_backend_is_found_nowhere_and_removes_nothing(self, tmp_path, form):
        planted = tmp_path / "secrets" / "planted"
        planted.parent.mkdir(parents=True)
        planted.write_text('{"url": "slack://kept", "enabled": true}', encoding="utf-8")
        r = _engine_registry(tmp_path)
        name = str(planted) if form == "absolute" else "../planted"
        try:
            assert name not in r
            with pytest.raises(KeyError):
                r.get(name)
            assert r.resolve(name) is None
            assert r.remove(name) is False
        finally:
            r.close()
        assert planted.exists()
