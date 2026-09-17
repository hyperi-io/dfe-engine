"""Tests for the built-in dfe-alerts Source definition, shipped in dfe-schemas."""

from dfe_engine.schema.plan import core_schemas_root
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry

_NAME = "dfe-alerts"


class TestBuiltinAlertsSource:
    """The definition dfe-schemas ships is a valid Source once it is named."""

    def test_yaml_exists_in_the_pinned_package(self):
        assert (core_schemas_root() / "sources" / f"{_NAME}.yaml").is_file()

    def test_parses_as_valid_source(self):
        source = Source.model_validate(_load_builtin(_NAME))

        assert source.source == _NAME
        assert source.display_name == "DFE Alerts"
        assert source.enabled is True
        assert source.header.type == "timeseries"
        assert source.current in source.versions
        assert source.deployed_version == source.current

    def test_every_builtin_header_profile_resolves_to_a_real_file(self):
        """A header type is a filename in dfe-schemas, and nothing normalises it.

        Asserting the string round-trips is not enough: `time_series` survived
        every such assertion for as long as it named a profile that does not
        exist, because no test ever tried to load it.
        """
        from dfe_engine.schema.schema_loader import SchemaLoader
        from dfe_engine.yaml_utils import yaml_load_string

        builtins = core_schemas_root() / "sources"
        seen = 0
        for entry in builtins.iterdir():
            if not entry.name.endswith(".yaml"):
                continue
            doc = yaml_load_string(entry.read_text()) or {}
            doc.setdefault("source", entry.name.removesuffix(".yaml"))
            source = Source.model_validate(doc)
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
        data = _load_builtin(_NAME)
        current = data["current"]
        assert data["deployed_version"] == current
        assert current in data["versions"]
        assert "header" not in data
        assert "schema" not in data
        assert data["versions"][current]["views"] == [{"standard": "sigma"}]

    def test_has_sigma_view(self):
        """dfe-alerts declares a sigma naming-standard view."""
        assert Source.model_validate(_load_builtin(_NAME)).view_for("sigma") is not None

    def test_references_a_detection_schema_the_package_ships(self):
        """The referenced additional_fields must resolve, not merely be a string.

        The engine's own copy pointed at ``hunt-results/detection.yaml``, a path
        no dfe-schemas release has ever carried.
        """
        source = Source.model_validate(_load_builtin(_NAME))
        reference = source.schema_config.additional_fields
        assert reference
        assert (core_schemas_root() / reference).is_file()


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
    """A shipped source definition as a dict, named the way the seed names it."""
    from dfe_engine.yaml_utils import yaml_load_string

    path = core_schemas_root() / "sources" / f"{name}.yaml"
    doc = yaml_load_string(path.read_text()) or {}
    doc.setdefault("source", name)
    return doc
