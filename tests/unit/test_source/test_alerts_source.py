"""Tests for the built-in dfe-alerts Source definition."""

import importlib.resources as resources

from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry


class TestBuiltinAlertsSource:
    """Test that dfe-alerts.yaml is a valid Source definition."""

    def test_yaml_exists_as_package_resource(self):
        """The dfe-alerts.yaml file exists in builtin_sources."""
        builtins = resources.files("dfe_engine.source") / "builtin_sources"
        alerts_file = builtins / "dfe-alerts.yaml"
        assert alerts_file.is_file()

    def test_parses_as_valid_source(self):
        """dfe-alerts.yaml parses into a valid Source model."""
        builtins = resources.files("dfe_engine.source") / "builtin_sources"
        alerts_file = builtins / "dfe-alerts.yaml"
        content = alerts_file.read_text()

        from dfe_engine.yaml_utils import yaml_load_string

        data = yaml_load_string(content)
        source = Source.model_validate(data)

        assert source.source == "dfe-alerts"
        assert source.display_name == "DFE Alerts"
        assert source.enabled is True
        assert source.header.type == "timeseries"
        assert source.current == "1.0.0"
        assert source.deployed_version == "1.0.0"
        assert "1.0.0" in source.versions

    def test_every_builtin_header_profile_resolves_to_a_real_file(self):
        """A header type is a filename in dfe-schemas, and nothing normalises it.

        Asserting the string round-trips is not enough: `time_series` survived
        every such assertion for as long as it named a profile that does not
        exist, because no test ever tried to load it.
        """
        from dfe_engine.schema.schema_loader import SchemaLoader
        from dfe_engine.yaml_utils import yaml_load_string

        builtins = resources.files("dfe_engine.source") / "builtin_sources"
        seen = 0
        for entry in builtins.iterdir():
            if not entry.name.endswith(".yaml"):
                continue
            source = Source.model_validate(yaml_load_string(entry.read_text()))
            for snap in source.versions.values():
                if snap.header is None:
                    continue
                seen += 1
                # Raises SchemaLoadError if the profile file is not there.
                SchemaLoader.load_profile(snap.header.type, profile_version=snap.header.version)

        assert seen, "no builtin source declared a header, so this proved nothing"

    def test_the_default_header_profile_resolves(self):
        from dfe_engine.schema.schema_loader import SchemaLoader
        from dfe_engine.source.models import SourceHeader

        SchemaLoader.load_profile(SourceHeader().type)

    def test_builtin_yaml_uses_version_tree(self):
        data = _load_builtin("dfe-alerts")
        assert data["current"] == "1.0.0"
        assert data["deployed_version"] == "1.0.0"
        assert "1.0.0" in data["versions"]
        assert "header" not in data
        assert "schema" not in data
        assert data["versions"]["1.0.0"]["views"] == [{"standard": "sigma"}]

    def test_has_sigma_view(self):
        """dfe-alerts declares a sigma naming-standard view."""
        data = _load_builtin("dfe-alerts")
        source = Source.model_validate(data)
        assert source.view_for("sigma") is not None

    def test_references_detection_columns(self):
        """dfe-alerts references the hunt-results/detection.yaml additional fields."""
        data = _load_builtin("dfe-alerts")
        source = Source.model_validate(data)
        assert source.schema_config.additional_fields == "hunt-results/detection.yaml"


class TestSeedBuiltinSources:
    """Test the seed_builtin_sources() method."""

    def test_seeds_into_empty_directory(self, tmp_path):
        """Seeding into an empty directory creates the source file."""
        registry = SourceRegistry(sources_directory=str(tmp_path))
        count = registry.seed_builtin_sources()
        assert count >= 1
        assert (tmp_path / "dfe-alerts.yaml").exists()

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
        source = registry.get_source("dfe-alerts")
        assert source.source == "dfe-alerts"
        assert source.enabled is True


def _load_builtin(name: str) -> dict:
    """Load a built-in source YAML as a dict."""
    from dfe_engine.yaml_utils import yaml_load_string

    builtins = resources.files("dfe_engine.source") / "builtin_sources"
    content = (builtins / f"{name}.yaml").read_text()
    return yaml_load_string(content)
