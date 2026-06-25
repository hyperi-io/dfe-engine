"""Tests for hunt config registry helpers."""

from dfe_engine.hunts.hunt_config_registry import (
    default_display_name,
    resolve_display_name,
    strip_identity_fields_from_yaml,
)


class TestHuntNaming:
    def test_default_display_name(self):
        assert default_display_name("api_test_hunt") == "Api test hunt"
        assert default_display_name("my-hunt") == "My hunt"
        assert default_display_name("Test") == "Test"

    def test_resolve_display_name_prefers_display_name(self):
        assert resolve_display_name({"display_name": "Custom"}, "file") == "Custom"

    def test_resolve_display_name_legacy_name_field(self):
        assert resolve_display_name({"name": "Legacy"}, "file") == "Legacy"

    def test_strip_identity_fields(self):
        out = strip_identity_fields_from_yaml(
            {"name": "id", "hunt_id": "x", "display_name": "D", "cron": "*"}
        )
        assert "name" not in out
        assert "hunt_id" not in out
        assert out["display_name"] == "D"
