"""Tests for the built-in dfe_alerts Source definition."""

import importlib.resources as resources


from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry


class TestBuiltinAlertsSource:
    """Test that dfe_alerts.yaml is a valid Source definition."""

    def test_yaml_exists_as_package_resource(self):
        """The dfe_alerts.yaml file exists in builtin_sources."""
        builtins = resources.files("dfe_engine.source") / "builtin_sources"
        alerts_file = builtins / "dfe_alerts.yaml"
        assert alerts_file.is_file()

    def test_parses_as_valid_source(self):
        """dfe_alerts.yaml parses into a valid Source model."""
        builtins = resources.files("dfe_engine.source") / "builtin_sources"
        alerts_file = builtins / "dfe_alerts.yaml"
        content = alerts_file.read_text()

        from dfe_engine.yaml_utils import yaml_load_string

        data = yaml_load_string(content)
        source = Source.model_validate(data)

        assert source.source == "dfe_alerts"
        assert source.display_name == "DFE Alerts"
        assert source.enabled is True
        assert source.header.type == "time_series"

    def test_has_sigma_mapping_standard(self):
        """dfe_alerts declares sigma mapping standard."""
        data = _load_builtin("dfe_alerts")
        source = Source.model_validate(data)
        assert "sigma" in source.mapping_standards

    def test_references_detection_columns(self):
        """dfe_alerts references the hunt-results/detection.yaml additional fields."""
        data = _load_builtin("dfe_alerts")
        source = Source.model_validate(data)
        assert source.schema_config.additional_fields == "hunt-results/detection.yaml"


class TestSeedBuiltinSources:
    """Test the seed_builtin_sources() method."""

    def test_seeds_into_empty_directory(self, tmp_path):
        """Seeding into an empty directory creates the source file."""
        registry = SourceRegistry(sources_directory=str(tmp_path))
        count = registry.seed_builtin_sources()
        assert count >= 1
        assert (tmp_path / "dfe_alerts.yaml").exists()

    def test_seed_is_idempotent(self, tmp_path):
        """Second seed with overwrite=False skips existing files."""
        registry = SourceRegistry(sources_directory=str(tmp_path))
        first = registry.seed_builtin_sources()
        second = registry.seed_builtin_sources(overwrite=False)
        assert first >= 1
        assert second == 0

    def test_seed_with_overwrite(self, tmp_path):
        """overwrite=True replaces existing files."""
        registry = SourceRegistry(sources_directory=str(tmp_path))
        registry.seed_builtin_sources()
        count = registry.seed_builtin_sources(overwrite=True)
        assert count >= 1

    def test_seeded_source_is_queryable(self, tmp_path):
        """After seeding, the source can be retrieved from the registry."""
        registry = SourceRegistry(sources_directory=str(tmp_path))
        registry.seed_builtin_sources()
        source = registry.get_source("dfe_alerts")
        assert source.source == "dfe_alerts"
        assert source.enabled is True


def _load_builtin(name: str) -> dict:
    """Load a built-in source YAML as a dict."""
    from dfe_engine.yaml_utils import yaml_load_string

    builtins = resources.files("dfe_engine.source") / "builtin_sources"
    content = (builtins / f"{name}.yaml").read_text()
    return yaml_load_string(content)
