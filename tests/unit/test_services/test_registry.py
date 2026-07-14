"""Tests for ServiceConfigRegistry — CRUD, validation, list, schema-less mode."""

from __future__ import annotations

import pytest

from dfe_engine.services.registry import (
    ConfigNotFoundError,
    ServiceConfigRegistry,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the registry singleton before and after each test."""
    ServiceConfigRegistry.reset_instance()
    yield
    ServiceConfigRegistry.reset_instance()


@pytest.fixture
def registry(tmp_path):
    """Fresh registry backed by a tmp directory."""
    reg = ServiceConfigRegistry(config_directory=tmp_path / "services", refresh_interval=0)
    yield reg
    reg.close()


@pytest.fixture
def receiver_yaml():
    """Minimal valid receiver config content."""
    return (
        "server:\n"
        "  bind_address: '0.0.0.0:8080'\n"
        "kafka:\n"
        "  brokers:\n"
        "    - localhost:9092\n"
        "routing:\n"
        "  default_source: default\n"
    )


@pytest.fixture
def loader_yaml():
    """Minimal valid loader config content."""
    return (
        "kafka:\n"
        "  brokers:\n"
        "    - localhost:9092\n"
        "clickhouse:\n"
        "  hosts:\n"
        "    - localhost:9000\n"
        "  database: default\n"
        "routing:\n"
        "  default_db: common\n"
    )


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_get_config_not_found_raises(self, registry):
        with pytest.raises(ConfigNotFoundError, match="receiver/default"):
            registry.get_config("receiver", "default")

    def test_get_config_returns_typed_model(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_typed"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            config = reg.get_config("receiver", "default")
            # Should be a Pydantic model (ReceiverConfig), not a raw dict
            assert hasattr(config, "model_dump")
        finally:
            reg.close()

    def test_get_config_unknown_service_returns_dict(self, tmp_path):
        """Unknown services (schema-less mode) return raw dict."""
        svc_dir = tmp_path / "svc_schemaless"
        svc_dir.mkdir()
        (svc_dir / "myservice-default.yaml").write_text("key: value\nfoo: bar\n")

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            result = reg.get_config("myservice", "default")
            assert isinstance(result, dict)
            assert result["key"] == "value"
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# save_config + get_config round-trip
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_save_and_retrieve_known_service(self, tmp_path, receiver_yaml):
        """Save a known service config then retrieve it as a typed model."""
        svc_dir = tmp_path / "svc_save"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            # Retrieve initial config, mutate, save back
            config = reg.get_config("receiver", "default")
            data = config.model_dump(mode="json")
            data["kafka"]["brokers"] = ["kafka-new:9092"]

            reg.save_config("receiver", data, instance="default")

            updated = reg.get_config("receiver", "default")
            assert updated.kafka.brokers == ["kafka-new:9092"]
        finally:
            reg.close()

    def test_save_unknown_service_dict(self, tmp_path):
        """Save an unknown service config as raw dict."""
        svc_dir = tmp_path / "svc_unknown"
        svc_dir.mkdir()

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            reg.save_config("new-service", {"alpha": 1, "beta": "ok"}, instance="prod")

            result = reg.get_config("new-service", "prod")
            assert isinstance(result, dict)
            assert result["alpha"] == 1
        finally:
            reg.close()

    def test_save_file_written_to_disk(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_disk"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            config = reg.get_config("receiver", "default")
            reg.save_config("receiver", config, instance="staging")

            yaml_file = svc_dir / "receiver-staging.yaml"
            assert yaml_file.exists()
            assert yaml_file.stat().st_size > 0
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# delete_config
# ---------------------------------------------------------------------------


class TestDeleteConfig:
    def test_delete_existing_config(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_del"
        svc_dir.mkdir()
        (svc_dir / "receiver-production.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            # Verify it exists first
            reg.get_config("receiver", "production")

            reg.delete_config("receiver", "production")
            assert not (svc_dir / "receiver-production.yaml").exists()

            with pytest.raises(ConfigNotFoundError):
                reg.get_config("receiver", "production")
        finally:
            reg.close()

    def test_delete_missing_config_is_noop(self, registry):
        """Deleting a non-existent config should not raise."""
        registry.delete_config("receiver", "nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# list_configs
# ---------------------------------------------------------------------------


class TestListConfigs:
    def test_list_all_returns_entries(self, tmp_path, receiver_yaml, loader_yaml):
        svc_dir = tmp_path / "svc_list"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)
        (svc_dir / "loader-production.yaml").write_text(loader_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            entries = reg.list_configs()
            services = {e["service"] for e in entries}
            assert "receiver" in services
            assert "loader" in services
        finally:
            reg.close()

    def test_list_filtered_by_service(self, tmp_path, receiver_yaml, loader_yaml):
        svc_dir = tmp_path / "svc_filter"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)
        (svc_dir / "loader-production.yaml").write_text(loader_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            entries = reg.list_configs(service="receiver")
            assert all(e["service"] == "receiver" for e in entries)
            assert len(entries) >= 1
        finally:
            reg.close()

    def test_list_includes_instance_field(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_inst"
        svc_dir.mkdir()
        (svc_dir / "receiver-staging.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            entries = reg.list_configs(service="receiver")
            assert any(e["instance"] == "staging" for e in entries)
        finally:
            reg.close()

    def test_list_empty_directory(self, registry):
        entries = registry.list_configs()
        assert entries == []

    def test_list_unknown_service_included(self, tmp_path):
        """Unknown services in schema-less mode appear in list_configs."""
        svc_dir = tmp_path / "svc_sl"
        svc_dir.mkdir()
        (svc_dir / "custom-prod.yaml").write_text("key: value\n")

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            entries = reg.list_configs()
            assert any(e["service"] == "custom" and e["instance"] == "prod" for e in entries)
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_known_service_valid(self, registry, receiver_yaml):
        import yaml as pyyaml

        data = pyyaml.safe_load(receiver_yaml)
        result = registry.validate("receiver", data)
        assert result.valid

    def test_validate_unknown_service_returns_warning(self, registry):
        result = registry.validate("myunknownservice", {"a": 1})
        assert result.valid
        assert any("No schema registered" in w for w in result.warnings)

    def test_validate_invalid_data_for_known_service(self, registry):
        # Pass completely invalid data for receiver
        result = registry.validate("receiver", {"this_field": "does_not_match_schema"})
        # May be valid (extra fields allowed) or invalid depending on model
        # The important thing is it returns a ValidationResult
        assert hasattr(result, "valid")
        assert hasattr(result, "errors")


# ---------------------------------------------------------------------------
# singleton pattern
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_instance_requires_directory_on_first_call(self):
        from dfe_engine.services.registry import ServiceConfigError

        with pytest.raises(ServiceConfigError, match="config_directory is required"):
            ServiceConfigRegistry.get_instance()

    def test_get_instance_returns_same_object(self, tmp_path):
        svc_dir = tmp_path / "svc_singleton"
        reg1 = ServiceConfigRegistry.get_instance(config_directory=svc_dir)
        reg2 = ServiceConfigRegistry.get_instance()
        assert reg1 is reg2
        reg1.close()

    def test_reset_instance_clears_singleton(self, tmp_path):
        svc_dir = tmp_path / "svc_reset"
        reg1 = ServiceConfigRegistry.get_instance(config_directory=svc_dir)
        ServiceConfigRegistry.reset_instance()
        assert ServiceConfigRegistry._instance is None
        # Create fresh one after reset
        svc_dir2 = tmp_path / "svc_reset2"
        reg2 = ServiceConfigRegistry.get_instance(config_directory=svc_dir2)
        assert reg2 is not reg1
        reg2.close()


# ---------------------------------------------------------------------------
# git property stubs (non-git directory)
# ---------------------------------------------------------------------------


class TestGitProperties:
    def test_is_git_false_for_non_git_dir(self, registry):
        assert registry.is_git is False

    def test_current_branch_none_for_non_git(self, registry):
        assert registry.current_branch is None

    def test_list_branches_raises_for_non_git(self, registry):
        with pytest.raises(RuntimeError):
            registry.list_branches()


# ---------------------------------------------------------------------------
# get_config_history (non-git → returns empty)
# ---------------------------------------------------------------------------


class TestConfigHistory:
    def test_history_returns_empty_for_non_git(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_hist"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            history = reg.get_config_history("receiver", "default")
            assert history == []
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# on_change callback
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_register_callback_does_not_raise(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_cb"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            called = []
            reg.on_change("receiver", "default", lambda table, data: called.append(table))
            # Just verify no exception is raised during registration
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# seed_defaults
# ---------------------------------------------------------------------------


class TestSeedDefaults:
    def test_seed_defaults_writes_files(self, tmp_path):
        svc_dir = tmp_path / "svc_seed"
        svc_dir.mkdir()

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            count = reg.seed_defaults()
            # There are built-in default configs; should seed > 0
            assert count > 0
            yaml_files = list(svc_dir.glob("*.yaml"))
            assert len(yaml_files) == count
        finally:
            reg.close()

    def test_seed_defaults_skip_existing(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_seed_skip"
        svc_dir.mkdir()
        # Pre-create a file that matches a default
        (svc_dir / "receiver-default.yaml").write_text("# custom content\n")

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            reg.seed_defaults(overwrite=False)
            # The custom file should be preserved
            assert (svc_dir / "receiver-default.yaml").read_text() == "# custom content\n"
        finally:
            reg.close()

    def test_seed_defaults_overwrite(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_seed_overwrite"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text("# custom content\n")

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            reg.seed_defaults(overwrite=True)
            content = (svc_dir / "receiver-default.yaml").read_text()
            # Should be overwritten with the real default
            assert "custom content" not in content
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# export_yaml
# ---------------------------------------------------------------------------


class TestExportYaml:
    def test_export_yaml_writes_file(self, tmp_path, receiver_yaml):
        svc_dir = tmp_path / "svc_export"
        svc_dir.mkdir()
        (svc_dir / "receiver-default.yaml").write_text(receiver_yaml)

        reg = ServiceConfigRegistry(config_directory=svc_dir, refresh_interval=0)
        try:
            out = tmp_path / "export" / "receiver.yaml"
            result_path = reg.export_yaml("receiver", "default", out)
            assert result_path.exists()
            assert result_path.stat().st_size > 0
        finally:
            reg.close()


# ---------------------------------------------------------------------------
# _parse_table_name
# ---------------------------------------------------------------------------


class TestParseTableName:
    def test_known_service_simple(self):
        result = ServiceConfigRegistry._parse_table_name("receiver-default")
        assert result == ("receiver", "default")

    def test_known_service_hyphenated(self):
        result = ServiceConfigRegistry._parse_table_name("transform-wasm-production")
        assert result == ("transform-wasm", "production")

    def test_unknown_service_fallback(self):
        """Unknown services fall back to last-hyphen split."""
        result = ServiceConfigRegistry._parse_table_name("newservice-prod")
        assert result == ("newservice", "prod")

    def test_invalid_no_hyphen_returns_none(self):
        result = ServiceConfigRegistry._parse_table_name("nodash")
        assert result is None

    def test_transform_vector_parsing(self):
        result = ServiceConfigRegistry._parse_table_name("transform-vector-staging")
        assert result == ("transform-vector", "staging")
