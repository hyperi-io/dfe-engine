"""Tests for hunt alert dispatcher (Apprise-based notifications)."""

from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.hunts.alert import (
    AlertConfig,
    AlertDestination,
    AlertDestinationRegistry,
    AlertDispatcher,
    AlertTrigger,
    build_alert_config,
)


# ── AlertTrigger ──────────────────────────────────────────────────


class TestAlertTrigger:
    def test_any_match_trigger(self):
        t = AlertTrigger(type="any_match")
        assert t.type == "any_match"
        assert t.operator == ">="

    def test_result_count_trigger(self):
        t = AlertTrigger(type="result_count", operator=">=", value=10)
        assert t.value == 10

    def test_field_value_trigger(self):
        t = AlertTrigger(type="field_value", field="severity", operator="==", value="critical")
        assert t.field == "severity"
        assert t.value == "critical"


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

    def test_load_from_dict(self):
        r = AlertDestinationRegistry()
        r.load_from_dict({
            "slack-dfe": "slack://t/b/c/",
            "email-ops": "mailto://user:pass@smtp.example.com",
        })
        assert len(r) == 2
        assert r.resolve("slack-dfe") == "slack://t/b/c/"
        assert r.resolve("email-ops") == "mailto://user:pass@smtp.example.com"

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

    def test_any_match_fires_on_results(self):
        d = self._dispatcher([AlertTrigger(type="any_match")])
        assert d._should_fire(5, None) is True

    def test_any_match_does_not_fire_on_zero(self):
        d = self._dispatcher([AlertTrigger(type="any_match")])
        assert d._should_fire(0, None) is False

    def test_result_count_ge(self):
        d = self._dispatcher([AlertTrigger(type="result_count", operator=">=", value=10)])
        assert d._should_fire(10, None) is True
        assert d._should_fire(9, None) is False

    def test_result_count_gt(self):
        d = self._dispatcher([AlertTrigger(type="result_count", operator=">", value=5)])
        assert d._should_fire(6, None) is True
        assert d._should_fire(5, None) is False

    def test_result_count_eq(self):
        d = self._dispatcher([AlertTrigger(type="result_count", operator="==", value=3)])
        assert d._should_fire(3, None) is True
        assert d._should_fire(4, None) is False

    def test_result_count_lt(self):
        d = self._dispatcher([AlertTrigger(type="result_count", operator="<", value=5)])
        assert d._should_fire(4, None) is True
        assert d._should_fire(5, None) is False

    def test_field_value_match(self):
        d = self._dispatcher([
            AlertTrigger(type="field_value", field="severity", operator="==", value="critical")
        ])
        results = [{"severity": "low"}, {"severity": "critical"}]
        assert d._should_fire(2, results) is True

    def test_field_value_no_match(self):
        d = self._dispatcher([
            AlertTrigger(type="field_value", field="severity", operator="==", value="critical")
        ])
        results = [{"severity": "low"}, {"severity": "medium"}]
        assert d._should_fire(2, results) is False

    def test_field_value_missing_field(self):
        d = self._dispatcher([
            AlertTrigger(type="field_value", field="severity", operator="==", value="critical")
        ])
        results = [{"other": "value"}]
        assert d._should_fire(1, results) is False

    def test_field_value_no_results(self):
        d = self._dispatcher([
            AlertTrigger(type="field_value", field="severity", operator="==", value="critical")
        ])
        assert d._should_fire(0, None) is False

    def test_multiple_triggers_any_fires(self):
        """Multiple triggers: first match wins (OR logic)."""
        d = self._dispatcher([
            AlertTrigger(type="result_count", operator=">=", value=100),
            AlertTrigger(type="any_match"),
        ])
        assert d._should_fire(1, None) is True

    def test_no_triggers_does_not_fire(self):
        config = AlertConfig(channels=["slack://t"], triggers=[])
        d = AlertDispatcher(config)
        assert d._should_fire(5, None) is False

    def test_invalid_result_count_value(self):
        d = self._dispatcher([AlertTrigger(type="result_count", operator=">=", value="abc")])
        assert d._should_fire(5, None) is False


# ── AlertDispatcher.evaluate_and_send ─────────────────────────────


class TestEvaluateAndSend:
    def test_disabled_config_returns_false(self):
        config = AlertConfig(
            channels=["slack://t"],
            triggers=[AlertTrigger(type="any_match")],
            enabled=False,
        )
        d = AlertDispatcher(config)
        assert d.evaluate_and_send("hunt1", "org_a", "rule1", 5) is False

    def test_no_channels_returns_false(self):
        config = AlertConfig(
            channels=[],
            triggers=[AlertTrigger(type="any_match")],
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
            triggers=[AlertTrigger(type="any_match")],
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
            triggers=[AlertTrigger(type="any_match")],
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
    def test_send_success(self, MockApprise):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = True
        MockApprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is True
        mock_ap.add.assert_called_once_with("slack://t")
        mock_ap.notify.assert_called_once_with(title="title", body="body")

    @patch("dfe_engine.hunts.alert.apprise.Apprise")
    def test_send_failure(self, MockApprise):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = False
        MockApprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is False

    @patch("dfe_engine.hunts.alert.apprise.Apprise")
    def test_send_exception(self, MockApprise):
        mock_ap = MagicMock()
        mock_ap.notify.side_effect = RuntimeError("network error")
        MockApprise.return_value = mock_ap

        config = AlertConfig(channels=["slack://t"])
        d = AlertDispatcher(config)
        assert d._send("title", "body") is False


# ── build_alert_config ────────────────────────────────────────────


class TestBuildAlertConfig:
    def test_no_alerts_section_no_global(self):
        """No alert config at all → None."""
        assert build_alert_config({}) is None

    def test_global_channels_only(self):
        """Global channels with no hunt-level alerts → config with any_match default."""
        config = build_alert_config({}, global_channels=["slack://global"])
        assert config is not None
        assert config.channels == ["slack://global"]
        assert len(config.triggers) == 1
        assert config.triggers[0].type == "any_match"

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

    def test_custom_triggers(self):
        data = {
            "alerts": {
                "channels": ["slack://t"],
                "triggers": [
                    {"type": "result_count", "operator": ">=", "value": 10},
                ],
            }
        }
        config = build_alert_config(data)
        assert len(config.triggers) == 1
        assert config.triggers[0].type == "result_count"
        assert config.triggers[0].value == 10

    def test_default_trigger_when_no_triggers_specified(self):
        """Channels but no triggers → defaults to any_match."""
        data = {"alerts": {"channels": ["slack://t"]}}
        config = build_alert_config(data)
        assert len(config.triggers) == 1
        assert config.triggers[0].type == "any_match"

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
        assert config.channels == ["slack://t/b/c/", "mailto://user:pass@smtp.example.com"]

    def test_destinations_plus_global_channels(self, registry):
        """Named destinations + global channels merged and deduplicated."""
        data = {"alerts": {"destinations": ["slack-dfe"]}}
        config = build_alert_config(
            data, global_channels=["pagerduty://key"], destination_registry=registry
        )
        assert config.channels == ["slack://t/b/c/", "pagerduty://key"]

    def test_destinations_deduped_with_global(self, registry):
        """Destination URL same as global → not duplicated."""
        data = {"alerts": {"destinations": ["pd-oncall"]}}
        config = build_alert_config(
            data, global_channels=["pagerduty://key"], destination_registry=registry
        )
        assert config.channels == ["pagerduty://key"]

    def test_no_registry_warns(self):
        """Destinations referenced but no registry → warning, no channels."""
        data = {"alerts": {"destinations": ["slack-dfe"]}}
        config = build_alert_config(data)
        assert config is None

    def test_empty_destinations_list(self, registry):
        """Empty destinations list → falls through to channels/global."""
        data = {"alerts": {"destinations": []}}
        config = build_alert_config(data, global_channels=["slack://g"], destination_registry=registry)
        assert config.channels == ["slack://g"]


# ── AlertDestinationRegistry with DirectoryConfigStore ───────────


class TestAlertDestinationRegistryFileBacked:
    """Tests for file-backed registry using DirectoryConfigStore."""

    def test_add_and_get_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="slack-test", url="slack://t/b/c/"))
        dest = r.get("slack-test")
        assert dest.url == "slack://t/b/c/"
        assert dest.name == "slack-test"

    def test_persists_to_yaml(self, tmp_path):
        dest_dir = tmp_path / "dests"
        r = AlertDestinationRegistry(directory=str(dest_dir))
        r.add(AlertDestination(name="slack-prod", url="slack://a/b/c/", description="Production"))
        yaml_file = dest_dir / "slack-prod.yaml"
        assert yaml_file.exists()

    def test_list_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="slack-a", url="slack://a"))
        r.add(AlertDestination(name="slack-b", url="slack://b"))
        destinations = r.list()
        names = {d.name for d in destinations}
        assert names == {"slack-a", "slack-b"}

    def test_remove_file_backed(self, tmp_path):
        dest_dir = tmp_path / "dests"
        r = AlertDestinationRegistry(directory=str(dest_dir))
        r.add(AlertDestination(name="slack-rm", url="slack://t"))
        assert r.remove("slack-rm") is True
        assert not (dest_dir / "slack-rm.yaml").exists()
        assert r.resolve("slack-rm") is None

    def test_remove_nonexistent_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        assert r.remove("nope") is False

    def test_resolve_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="pd", url="pagerduty://key"))
        assert r.resolve("pd") == "pagerduty://key"

    def test_resolve_disabled_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="off", url="slack://t", enabled=False))
        assert r.resolve("off") is None

    def test_len_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="a", url="slack://a"))
        r.add(AlertDestination(name="b", url="slack://b"))
        assert len(r) == 2

    def test_contains_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="x", url="slack://x"))
        assert "x" in r
        assert "y" not in r

    def test_load_from_dict_file_backed(self, tmp_path):
        dest_dir = tmp_path / "dests"
        r = AlertDestinationRegistry(directory=str(dest_dir))
        r.load_from_dict({"slack-a": "slack://a", "slack-b": "slack://b"})
        assert len(r) == 2
        assert (dest_dir / "slack-a.yaml").exists()
        assert (dest_dir / "slack-b.yaml").exists()

    def test_overwrite_file_backed(self, tmp_path):
        r = AlertDestinationRegistry(directory=str(tmp_path / "dests"))
        r.add(AlertDestination(name="s", url="slack://old"))
        r.add(AlertDestination(name="s", url="slack://new"))
        assert r.resolve("s") == "slack://new"
        assert len(r) == 1
