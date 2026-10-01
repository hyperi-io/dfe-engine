"""Tests for SigmaSourceMapper — Source-based Sigma field mapping."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapRegistry
from dfe_engine.source.models import Source, SourceView
from dfe_engine.source.registry import SourceNotFoundError
from dfe_engine.source.type_registry import TypeRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    name: str = "windows-audit",
    *,
    sigma: SourceView | None = None,
    enabled: bool = True,
    engine: str = "MergeTree",
    meta_schema: str | None = None,
) -> Source:
    """Create a minimal Source for testing."""
    data: dict = {
        "source": name,
        "enabled": enabled,
        "match": {"field": "tags.collector.type", "value": name},
        "schema": {
            "engine": engine,
        },
    }
    if meta_schema:
        data["schema"]["meta_schema"] = meta_schema
    if sigma is not None:
        data["views"] = [sigma.model_dump(mode="json")]
    return Source.model_validate(data)


def _make_sigma(
    taxonomy: str | None = "windows",
    mappings: dict[str, str] | None = None,
) -> SourceView:
    """Create a sigma SourceView entry."""
    return SourceView(
        standard="sigma",
        taxonomy=taxonomy,
        custom_mappings=mappings or {},
    )


class FakeSourceRegistry:
    """Minimal mock of SourceRegistry for unit tests."""

    def __init__(self, sources: list[Source] | None = None) -> None:
        self._sources = {s.source: s for s in (sources or [])}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source '{source_name}' not found")
        return self._sources[source_name]

    def get_all_sources(self, enabled_only: bool = False) -> list[Source]:
        sources = list(self._sources.values())
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        return sources


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def type_registry():
    return TypeRegistry.default()


@pytest.fixture
def windows_sigma():
    return _make_sigma(
        taxonomy="windows",
        mappings={
            "EventID": "event_id",
            "CommandLine": "command_line",
            "Image": "process_name",
            "ParentImage": "parent_process_name",
        },
    )


@pytest.fixture
def linux_sigma():
    return _make_sigma(
        taxonomy="linux",
        mappings={
            "exe": "process_name",
            "cmdline": "command_line",
        },
    )


@pytest.fixture
def empty_sigma():
    return _make_sigma(taxonomy=None, mappings={})


@pytest.fixture
def source_registry(windows_sigma, linux_sigma, empty_sigma):
    sources = [
        _make_source("windows-audit", sigma=windows_sigma),
        _make_source("linux-syslog", sigma=linux_sigma),
        _make_source("network-flow", sigma=empty_sigma),
        _make_source("raw-passthrough"),  # no sigma at all
        _make_source("disabled-source", sigma=windows_sigma, enabled=False),
        # A meta_schema that resolves to nothing, so the build really does raise.
        _make_source("broken-schema", meta_schema="does/not/exist.yaml"),
    ]
    return FakeSourceRegistry(sources)


@pytest.fixture
def mapper(source_registry, type_registry):
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    return SigmaSourceMapper(source_registry, registry=type_registry)


# ---------------------------------------------------------------------------
# Tests: get_source
# ---------------------------------------------------------------------------


class TestGetSource:
    def test_get_existing_source(self, mapper):
        source = mapper.get_source("windows-audit")
        assert source.source == "windows-audit"

    def test_get_missing_source_raises(self, mapper):
        with pytest.raises(SourceNotFoundError):
            mapper.get_source("nonexistent")


# ---------------------------------------------------------------------------
# Tests: get_field_mappings
# ---------------------------------------------------------------------------


class TestGetFieldMappings:
    def test_returns_sigma_mappings(self, mapper):
        mappings = mapper.get_field_mappings("windows-audit")
        assert mappings == {
            "EventID": "event_id",
            "CommandLine": "command_line",
            "Image": "process_name",
            "ParentImage": "parent_process_name",
        }

    def test_returns_empty_for_no_sigma_config(self, mapper):
        mappings = mapper.get_field_mappings("raw-passthrough")
        assert mappings == {}

    def test_returns_empty_for_empty_mappings(self, mapper):
        mappings = mapper.get_field_mappings("network-flow")
        assert mappings == {}

    def test_returns_linux_mappings(self, mapper):
        mappings = mapper.get_field_mappings("linux-syslog")
        assert mappings == {
            "exe": "process_name",
            "cmdline": "command_line",
        }

    def test_missing_source_raises(self, mapper):
        with pytest.raises(SourceNotFoundError):
            mapper.get_field_mappings("nonexistent")


# ---------------------------------------------------------------------------
# Tests: get_schema_metadata
# ---------------------------------------------------------------------------


class TestGetSchemaMetadata:
    def test_returns_empty_when_build_fails(self, mapper):
        """When SchemaBuilderV2.build raises, returns empty dict."""
        metadata = mapper.get_schema_metadata("broken-schema")
        assert metadata == {}

    def test_a_source_with_no_schema_files_still_reports_its_header_columns(self, mapper):
        """A source that authors neither a header nor a meta_schema takes the
        default profile, so its metadata is that profile's columns."""
        metadata = mapper.get_schema_metadata("raw-passthrough")

        assert set(metadata) == {
            "_timestamp_load",
            "_timestamp",
            "_timestamp_received",
            "_uuid",
            "_org_id",
            "_source",
            "_raw",
            "_json",
            "_tags",
        }

    def test_missing_source_raises(self, mapper):
        with pytest.raises(SourceNotFoundError):
            mapper.get_schema_metadata("nonexistent")


# ---------------------------------------------------------------------------
# Tests: get_sigma_field_metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def view_store(tmp_path):
    """A real SigmaViewStore over a local (no-remote) deploy repo."""
    from dfe_engine.gitcrud import GitCrud, default_registry
    from dfe_engine.gitops.repo import GitopsRepo
    from dfe_engine.sigma.views import SigmaViewStore

    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return SigmaViewStore(GitCrud(repo, default_registry()))


def _field_mapper(type_registry, *sources: Source, view_store=None, field_map_registry=None):
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    return SigmaSourceMapper(
        FakeSourceRegistry(list(sources)),
        registry=type_registry,
        field_map_registry=field_map_registry,
        view_store=view_store,
    )


class TestGetSigmaFieldMetadata:
    """The view's columns carry Sigma field names, so the metadata is keyed by them.

    ``_raw`` is the default profile's ``substring_search`` column (a text index) and
    ``_org_id`` its ``dimension`` column, so both are real schema columns here.
    """

    def test_a_field_aliased_to_a_text_indexed_column_takes_its_metadata(self, type_registry):
        from dfe_engine.sigma.sigma_backend_clickhouse import declares_text_index

        source = _make_source("win", sigma=_make_sigma(mappings={"Message": "_raw"}))
        m = _field_mapper(type_registry, source)

        metadata = m.get_sigma_field_metadata("win")

        assert metadata == {"Message": m.get_schema_metadata("win")["_raw"]}
        assert declares_text_index(metadata["Message"])

    def test_the_metadata_is_keyed_by_the_sigma_field_not_the_column(self, type_registry):
        source = _make_source("win", sigma=_make_sigma(mappings={"Message": "_raw"}))

        metadata = _field_mapper(type_registry, source).get_sigma_field_metadata("win")

        assert "_raw" not in metadata

    def test_a_field_aliased_to_an_unindexed_column_carries_that_columns_use_case(
        self, type_registry
    ):
        from dfe_engine.sigma.sigma_backend_clickhouse import declares_text_index

        source = _make_source("win", sigma=_make_sigma(mappings={"Org": "_org_id"}))

        metadata = _field_mapper(type_registry, source).get_sigma_field_metadata("win")

        assert metadata["Org"]["use_case"] == "dimension"
        assert not declares_text_index(metadata["Org"])

    def test_a_field_aliased_to_a_column_the_schema_lacks_gets_none(self, type_registry):
        source = _make_source(
            "win", sigma=_make_sigma(mappings={"Image": "process_name", "Message": "_raw"})
        )

        metadata = _field_mapper(type_registry, source).get_sigma_field_metadata("win")

        assert set(metadata) == {"Message"}

    def test_a_source_with_no_sigma_aliases_gets_none(self, mapper):
        assert mapper.get_sigma_field_metadata("raw-passthrough") == {}
        assert mapper.get_sigma_field_metadata("network-flow") == {}

    def test_a_schema_that_fails_to_build_degrades_to_no_metadata(self, type_registry):
        source = _make_source(
            "broken",
            sigma=_make_sigma(mappings={"Message": "_raw"}),
            meta_schema="does/not/exist.yaml",
        )

        assert _field_mapper(type_registry, source).get_sigma_field_metadata("broken") == {}

    def test_missing_source_raises(self, mapper):
        with pytest.raises(SourceNotFoundError):
            mapper.get_sigma_field_metadata("nonexistent")

    def test_a_field_map_pinned_to_another_standard_raises(self, type_registry, fm_registry):
        from dfe_engine.fieldmap.registry import FieldMapError

        source = _make_source(
            "win",
            sigma=SourceView(
                standard="sigma",
                taxonomy="windows",
                field_map="ecs/corp-pin",
                custom_mappings={"Message": "_raw"},
            ),
        )
        m = _field_mapper(type_registry, source, field_map_registry=fm_registry)

        with pytest.raises(FieldMapError, match="ecs"):
            m.get_sigma_field_metadata("win")

    def test_a_stored_definition_maps_its_plain_source_columns_only(
        self, type_registry, view_store
    ):
        """A JSON extraction and a CAST are not the expression the index covers."""
        from dfe_engine.sigma.views import SigmaViewDefinition

        view_store.save(
            SigmaViewDefinition.model_validate(
                {
                    "source_name": "win",
                    "columns": [
                        {"sigma_field": "Message", "source_column": "_raw"},
                        {"sigma_field": "RawText", "source_column": "_raw", "type": "String"},
                        {"sigma_field": "CommandLine", "json_path": "process.command_line"},
                    ],
                }
            ),
            actor="tester",
        )
        source = _make_source("win", sigma=_make_sigma(mappings={}))
        m = _field_mapper(type_registry, source, view_store=view_store)

        metadata = m.get_sigma_field_metadata("win")

        assert metadata == {"Message": m.get_schema_metadata("win")["_raw"]}

    def test_a_stored_definition_wins_over_the_field_maps(self, type_registry, view_store):
        """The view renders the stored definition, so the field maps no longer apply."""
        from dfe_engine.sigma.views import SigmaViewDefinition

        view_store.save(
            SigmaViewDefinition.model_validate(
                {
                    "source_name": "win",
                    "columns": [{"sigma_field": "Message", "json_path": "message"}],
                }
            ),
            actor="tester",
        )
        source = _make_source("win", sigma=_make_sigma(mappings={"Message": "_raw"}))

        metadata = _field_mapper(
            type_registry, source, view_store=view_store
        ).get_sigma_field_metadata("win")

        assert metadata == {}

    def test_an_empty_view_store_falls_back_to_the_field_maps(self, type_registry, view_store):
        source = _make_source("win", sigma=_make_sigma(mappings={"Message": "_raw"}))

        metadata = _field_mapper(
            type_registry, source, view_store=view_store
        ).get_sigma_field_metadata("win")

        assert set(metadata) == {"Message"}


# ---------------------------------------------------------------------------
# Tests: generate_sigma_view
# ---------------------------------------------------------------------------


class TestGenerateSigmaView:
    def test_generates_view_ddl(self, mapper):
        ddl = mapper.generate_sigma_view("windows-audit")
        assert ddl is not None
        assert "CREATE OR REPLACE VIEW" in ddl
        assert "windows-audit_sigma" in ddl
        # Mappings should appear as aliases
        assert "event_id" in ddl
        assert "EventID" in ddl
        assert "command_line" in ddl
        assert "CommandLine" in ddl

    def test_uses_custom_db(self, mapper):
        ddl = mapper.generate_sigma_view("windows-audit", db="my_db")
        assert ddl is not None
        assert "my_db" in ddl

    def test_returns_none_for_no_sigma(self, mapper):
        ddl = mapper.generate_sigma_view("raw-passthrough")
        assert ddl is None

    def test_returns_none_for_empty_mappings(self, mapper):
        ddl = mapper.generate_sigma_view("network-flow")
        assert ddl is None

    def test_missing_source_raises(self, mapper):
        with pytest.raises(SourceNotFoundError):
            mapper.generate_sigma_view("nonexistent")


# ---------------------------------------------------------------------------
# Tests: generate_all_sigma_views
# ---------------------------------------------------------------------------


class TestGenerateAllSigmaViews:
    def test_generates_views_for_sources_with_mappings(self, mapper):
        views = mapper.generate_all_sigma_views()
        # Should include windows-audit and linux-syslog (have non-empty mappings)
        assert "windows-audit" in views
        assert "linux-syslog" in views
        # Should NOT include network-flow (empty mappings) or raw-passthrough (no sigma)
        assert "network-flow" not in views
        assert "raw-passthrough" not in views

    def test_enabled_only_excludes_disabled(self, mapper):
        views = mapper.generate_all_sigma_views(enabled_only=True)
        assert "disabled-source" not in views
        assert "windows-audit" in views

    def test_enabled_only_false_includes_disabled(self, mapper):
        views = mapper.generate_all_sigma_views(enabled_only=False)
        assert "disabled-source" in views

    def test_views_contain_valid_ddl(self, mapper):
        views = mapper.generate_all_sigma_views()
        for source_name, ddl in views.items():
            assert "CREATE OR REPLACE VIEW" in ddl
            assert f"{source_name}_sigma" in ddl


# ---------------------------------------------------------------------------
# Tests: get_sources_for_logsource
# ---------------------------------------------------------------------------


class TestGetSourcesForLogsource:
    def test_match_by_product_windows(self, mapper):
        matches = mapper.get_sources_for_logsource(product="windows")
        names = [s.source for s in matches]
        assert "windows-audit" in names
        assert "linux-syslog" not in names

    def test_match_by_product_linux(self, mapper):
        matches = mapper.get_sources_for_logsource(product="linux")
        names = [s.source for s in matches]
        assert "linux-syslog" in names
        assert "windows-audit" not in names

    def test_case_insensitive_product(self, mapper):
        matches = mapper.get_sources_for_logsource(product="Windows")
        names = [s.source for s in matches]
        assert "windows-audit" in names

    def test_no_match_returns_empty(self, mapper):
        matches = mapper.get_sources_for_logsource(product="macos")
        assert matches == []

    def test_no_product_returns_empty(self, mapper):
        matches = mapper.get_sources_for_logsource()
        assert matches == []

    def test_disabled_sources_excluded(self, mapper):
        """get_sources_for_logsource only queries enabled sources."""
        matches = mapper.get_sources_for_logsource(product="windows")
        names = [s.source for s in matches]
        # disabled-source has windows taxonomy but is disabled
        assert "disabled-source" not in names

    def test_service_narrowing_excludes_wrong_source(self, type_registry):
        # Two same-product sources - a sysmon-service rule must bind ONLY
        # the sysmon source, not the audit source that declares a different service.
        audit = _make_source(
            "windows-audit",
            sigma=SourceView(standard="sigma", taxonomy="windows", service="audit"),
        )
        sysmon = _make_source(
            "windows-sysmon",
            sigma=SourceView(standard="sigma", taxonomy="windows", service="sysmon"),
        )
        bare = _make_source("windows-any", sigma=SourceView(standard="sigma", taxonomy="windows"))
        from dfe_engine.sigma.source_mapper import SigmaSourceMapper

        m = SigmaSourceMapper(FakeSourceRegistry([audit, sysmon, bare]), registry=type_registry)

        names = {s.source for s in m.get_sources_for_logsource(product="windows", service="sysmon")}
        assert "windows-sysmon" in names
        assert "windows-audit" not in names  # wrong service, excluded
        assert "windows-any" in names  # no declared service -> matches any


# ---------------------------------------------------------------------------
# Tests: FieldMapRegistry integration
# ---------------------------------------------------------------------------


@pytest.fixture
def field_maps_dir(tmp_path):
    d = tmp_path / "field-maps"
    d.mkdir()
    return d


@pytest.fixture
def fm_registry(field_maps_dir):
    FieldMapRegistry.reset_instance()
    reg = FieldMapRegistry(
        field_maps_directory=field_maps_dir,
        writable=True,
        refresh_interval=0,
    )
    yield reg
    reg.close()
    FieldMapRegistry.reset_instance()


@pytest.fixture
def mapper_with_registry(source_registry, type_registry, fm_registry):
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    return SigmaSourceMapper(
        source_registry, registry=type_registry, field_map_registry=fm_registry
    )


class TestFieldMapRegistryIntegration:
    def test_inline_mappings_win_over_registry(self, mapper_with_registry, fm_registry):
        """LANDMINE 5 guard: inline custom_mappings WIN per-key over the registry (locked)."""
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"EventID": "registry_event_id", "RegistryOnly": "registry_col"},
            )
        )
        mappings = mapper_with_registry.get_field_mappings("windows-audit")
        # The source's inline custom_mappings override the registry per-key...
        assert mappings["EventID"] == "event_id"
        # ...while registry-only keys survive in the merge.
        assert mappings["RegistryOnly"] == "registry_col"

    def test_inline_only_when_no_registry_maps(self, mapper_with_registry):
        """When the registry has no sigma maps, the inline custom_mappings stand alone."""
        mappings = mapper_with_registry.get_field_mappings("windows-audit")
        assert mappings["EventID"] == "event_id"

    def test_source_specific_override_from_registry(self, mapper_with_registry, fm_registry):
        """Source-specific registry map overrides default registry map.

        Uses keys the source's inline custom_mappings do NOT declare, so the
        two-tier registry resolution is what decides them.
        """
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"User": "default_user", "Extra": "extra_col"},
            )
        )
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                source="windows-audit",
                mappings={"User": "win_user"},
            )
        )
        mappings = mapper_with_registry.get_field_mappings("windows-audit")
        assert mappings["User"] == "win_user"
        assert mappings["Extra"] == "extra_col"

    def test_generate_view_merges_registry_and_inline(self, mapper_with_registry, fm_registry):
        """generate_sigma_view renders the merged registry + inline mappings."""
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"RegistryField": "registry_col"},
            )
        )
        ddl = mapper_with_registry.generate_sigma_view("windows-audit")
        assert ddl is not None
        assert "`registry_col` AS `RegistryField`" in ddl
        assert "`event_id` AS `EventID`" in ddl  # inline mapping in the same view

    def test_generate_all_views_uses_registry(self, mapper_with_registry, fm_registry):
        """generate_all_sigma_views picks up registry mappings for all sources."""
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"CommonField": "common_col"},
            )
        )
        views = mapper_with_registry.generate_all_sigma_views()
        # All sources with sigma config OR registry maps should have views
        for _source_name, ddl in views.items():
            assert "`common_col` AS `CommonField`" in ddl

    def test_field_map_pin_honoured(self, type_registry, fm_registry):
        """A sigma view's field_map pins the registry override layer end-to-end."""
        from dfe_engine.sigma.source_mapper import SigmaSourceMapper

        fm_registry.save_map(
            FieldMap(standard="sigma", mappings={"User": "default_user", "EventID": "event_id"})
        )
        # The source-name convention map: bypassed when the view pins another.
        fm_registry.save_map(
            FieldMap(standard="sigma", source="windows-audit", mappings={"User": "convention_user"})
        )
        fm_registry.save_map(
            FieldMap(standard="sigma", source="corp-pin", mappings={"User": "pinned_user"})
        )
        source = _make_source(
            "windows-audit",
            sigma=SourceView(
                standard="sigma",
                taxonomy="windows",
                field_map="corp-pin",
                custom_mappings={"EventID": "inline_event_id"},
            ),
        )
        m = SigmaSourceMapper(
            FakeSourceRegistry([source]),
            registry=type_registry,
            field_map_registry=fm_registry,
        )

        mappings = m.get_field_mappings("windows-audit")
        assert mappings["User"] == "pinned_user"  # pin, not the convention map
        assert mappings["EventID"] == "inline_event_id"  # inline still wins per-key

        ddl = m.generate_sigma_view("windows-audit")
        assert ddl is not None
        assert "`pinned_user` AS `User`" in ddl
        assert "convention_user" not in ddl

    def test_field_map_pin_standard_slash_form(self, type_registry, fm_registry):
        """The 'standard/name' pin form resolves; a mismatched standard raises."""
        from dfe_engine.fieldmap.registry import FieldMapError
        from dfe_engine.sigma.source_mapper import SigmaSourceMapper

        fm_registry.save_map(
            FieldMap(standard="sigma", source="corp-pin", mappings={"User": "pinned_user"})
        )
        good = _make_source(
            "windows-audit",
            sigma=SourceView(standard="sigma", taxonomy="windows", field_map="sigma/corp-pin"),
        )
        bad = _make_source(
            "linux-syslog",
            sigma=SourceView(standard="sigma", taxonomy="linux", field_map="ecs/corp-pin"),
        )
        m = SigmaSourceMapper(
            FakeSourceRegistry([good, bad]),
            registry=type_registry,
            field_map_registry=fm_registry,
        )

        assert m.get_field_mappings("windows-audit")["User"] == "pinned_user"
        with pytest.raises(FieldMapError, match="ecs"):
            m.get_field_mappings("linux-syslog")

    def test_no_registry_behaves_like_legacy(self, mapper):
        """Without field_map_registry, mapper behaves identically to legacy."""
        mappings = mapper.get_field_mappings("windows-audit")
        assert mappings == {
            "EventID": "event_id",
            "CommandLine": "command_line",
            "Image": "process_name",
            "ParentImage": "parent_process_name",
        }
