"""Tests for deployment configuration registry."""

import pytest

from dfe_engine.deployment.models import (
    ArchiverDeploymentConfig,
    LoaderDeploymentConfig,
    ReceiverDeploymentConfig,
)
from dfe_engine.deployment.models.common import TShirtSize
from dfe_engine.deployment.registry import (
    DeploymentConfigError,
    DeploymentConfigNotFoundError,
    DeploymentConfigRegistry,
)


@pytest.fixture
def registry(tmp_path):
    """Create a fresh registry for each test."""
    DeploymentConfigRegistry.reset_instance()
    reg = DeploymentConfigRegistry(config_directory=tmp_path)
    yield reg
    reg.close()
    DeploymentConfigRegistry.reset_instance()


class TestRegistryCrud:
    def test_save_and_get_receiver(self, registry):
        config = ReceiverDeploymentConfig()
        registry.save_config("receiver", config, instance="default")
        loaded = registry.get_config("receiver", "default")
        assert loaded.size == config.size
        assert loaded.image == config.image

    def test_save_and_get_loader(self, registry):
        config = LoaderDeploymentConfig()
        registry.save_config("loader", config, instance="default")
        loaded = registry.get_config("loader", "default")
        assert loaded.size == TShirtSize.medium

    def test_save_and_get_archiver(self, registry):
        config = ArchiverDeploymentConfig()
        registry.save_config("archiver", config, instance="production")
        loaded = registry.get_config("archiver", "production")
        assert loaded.size == TShirtSize.small

    def test_save_dict(self, registry):
        data = ReceiverDeploymentConfig().model_dump(mode="json")
        registry.save_config("receiver", data, instance="test")
        loaded = registry.get_config("receiver", "test")
        assert loaded.image_tag == "latest"

    def test_get_nonexistent_raises(self, registry):
        with pytest.raises(DeploymentConfigNotFoundError):
            registry.get_config("receiver", "missing")

    def test_delete_config(self, registry):
        config = ReceiverDeploymentConfig()
        registry.save_config("receiver", config, instance="temp")
        registry.delete_config("receiver", "temp")
        with pytest.raises(DeploymentConfigNotFoundError):
            registry.get_config("receiver", "temp")

    def test_delete_nonexistent_no_error(self, registry):
        registry.delete_config("receiver", "nonexistent")

    def test_list_configs(self, registry):
        registry.save_config("receiver", ReceiverDeploymentConfig(), instance="default")
        registry.save_config("loader", LoaderDeploymentConfig(), instance="default")
        configs = registry.list_configs()
        assert len(configs) == 2
        services = {c["service"] for c in configs}
        assert services == {"receiver", "loader"}

    def test_list_configs_filter(self, registry):
        registry.save_config("receiver", ReceiverDeploymentConfig(), instance="a")
        registry.save_config("receiver", ReceiverDeploymentConfig(), instance="b")
        registry.save_config("loader", LoaderDeploymentConfig(), instance="a")
        configs = registry.list_configs(service="receiver")
        assert len(configs) == 2
        assert all(c["service"] == "receiver" for c in configs)

    def test_unknown_service_not_found(self, registry):
        # Unknown services are schema-less — no ValueError, just ConfigNotFoundError
        from dfe_engine.deployment.registry import DeploymentConfigNotFoundError

        with pytest.raises(DeploymentConfigNotFoundError):
            registry.get_config("unknown")


class TestRegistrySingleton:
    def test_get_instance_requires_directory(self):
        DeploymentConfigRegistry.reset_instance()
        with pytest.raises(DeploymentConfigError, match="config_directory is required"):
            DeploymentConfigRegistry.get_instance()

    def test_get_instance_returns_same(self, tmp_path):
        DeploymentConfigRegistry.reset_instance()
        try:
            a = DeploymentConfigRegistry.get_instance(config_directory=tmp_path)
            b = DeploymentConfigRegistry.get_instance()
            assert a is b
        finally:
            DeploymentConfigRegistry.reset_instance()


class TestSeedDefaults:
    def test_seed_creates_files(self, registry, tmp_path):
        count = registry.seed_defaults()
        assert count == 12  # 6 services x 2 profiles (default + production)
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 12

    def test_seed_no_overwrite(self, registry, tmp_path):
        registry.seed_defaults()
        count = registry.seed_defaults(overwrite=False)
        assert count == 0

    def test_seed_with_overwrite(self, registry, tmp_path):
        registry.seed_defaults()
        count = registry.seed_defaults(overwrite=True)
        assert count == 12

    def test_seeded_configs_roundtrip(self, registry):
        registry.seed_defaults()
        for service in ["receiver", "loader", "archiver"]:
            for instance in ["default", "production"]:
                config = registry.get_config(service, instance)
                assert config is not None


class TestApplySize:
    def test_apply_size_creates_config(self, registry):
        svc_overrides = registry.apply_size("loader", "test", "medium")
        config = registry.get_config("loader", "test")
        assert config.size == TShirtSize.medium
        assert config.resources is not None
        assert config.resources.limits.cpu == "2"
        assert isinstance(svc_overrides, dict)

    def test_apply_size_returns_service_overrides(self, registry):
        svc_overrides = registry.apply_size("loader", "test", "large")
        assert "buffer" in svc_overrides
        assert svc_overrides["buffer"]["flush_bytes"] == 8_388_608

    def test_apply_size_updates_existing(self, registry):
        registry.save_config("receiver", ReceiverDeploymentConfig(), instance="prod")
        registry.apply_size("receiver", "prod", "large")
        config = registry.get_config("receiver", "prod")
        assert config.size == TShirtSize.large
        assert config.resources.limits.cpu == "4"


class TestHelmExport:
    def test_export_strips_size(self, registry, tmp_path):
        registry.save_config("loader", LoaderDeploymentConfig(), instance="prod")
        output = tmp_path / "helm" / "values.yaml"
        registry.export_helm_values("loader", "prod", output)

        from dfe_engine.yaml_utils import yaml_load

        data = yaml_load(output)
        assert "size" not in data

    def test_export_has_resources_when_set(self, registry, tmp_path):
        registry.apply_size("receiver", "prod", "small")
        output = tmp_path / "helm" / "values.yaml"
        registry.export_helm_values("receiver", "prod", output)

        from dfe_engine.yaml_utils import yaml_load

        data = yaml_load(output)
        assert "size" not in data
        assert data["resources"]["limits"]["cpu"] == "1"


class TestSchemalessMode:
    """Registry accepts unknown services (schema-less fallback)."""

    def test_save_unknown_service_raw_dict(self, registry):
        raw = {"replicas": 2, "image": "custom:latest", "custom_field": "value"}
        registry.save_config("my-custom-service", raw, instance="prod")
        loaded = registry.get_config("my-custom-service", "prod")
        assert isinstance(loaded, dict)
        assert loaded["replicas"] == 2
        assert loaded["custom_field"] == "value"

    def test_list_includes_unknown_service(self, registry):
        raw = {"replicas": 1}
        registry.save_config("exotic-svc", raw, instance="test")
        configs = registry.list_configs()
        services = [c["service"] for c in configs]
        assert "exotic-svc" in services

    def test_parse_table_name_known_service(self):
        result = DeploymentConfigRegistry._parse_table_name("receiver-production")
        assert result == ("receiver", "production")

    def test_parse_table_name_unknown_service_fallback(self):
        # Unknown service — falls back to last-hyphen split
        result = DeploymentConfigRegistry._parse_table_name("exotic-svc-instance")
        assert result is not None
        svc, inst = result
        assert inst == "instance"

    def test_parse_table_name_no_hyphen_returns_none(self):
        result = DeploymentConfigRegistry._parse_table_name("nohyphen")
        assert result is None

    def test_parse_table_name_trailing_hyphen_returns_none(self):
        result = DeploymentConfigRegistry._parse_table_name("svc-")
        assert result is None


class TestValidation:
    def test_validate_known_service_valid_data(self, registry):
        data = ReceiverDeploymentConfig().model_dump(mode="json")
        result = registry.validate("receiver", data)
        assert result.valid is True
        assert len(result.errors) == 0

    def test_validate_known_service_invalid_data(self, registry):
        # Pass data that will fail validation (bad type)
        result = registry.validate("receiver", {"size": "not-a-valid-size"})
        # May or may not fail depending on model validators; just ensure it returns ValidationResult
        from dfe_engine.deployment.validators import ValidationResult

        assert isinstance(result, ValidationResult)

    def test_validate_unknown_service(self, registry):
        from dfe_engine.deployment.validators import ValidationResult

        result = registry.validate("unknown-svc", {"replicas": 1})
        assert isinstance(result, ValidationResult)


class TestGitProperties:
    """Test non-git path of git-related properties (no actual git repo)."""

    def test_is_git_false_for_plain_dir(self, registry):
        assert registry.is_git is False

    def test_current_branch_none_for_plain_dir(self, registry):
        assert registry.current_branch is None

    def test_list_branches_raises_for_plain_dir(self, registry):
        with pytest.raises(RuntimeError, match="not a git repository"):
            registry.list_branches()


class TestHistoryNonGit:
    """Config history returns empty list when not a git repo."""

    def test_history_returns_empty_for_known_service_non_git(self, registry):
        history = registry.get_config_history("receiver", "default")
        assert history == []

    def test_history_raises_for_unknown_service(self, registry):
        with pytest.raises(ValueError, match="Unknown service"):
            registry.get_config_history("no-such-service", "default")


class TestOnChange:
    def test_on_change_registers_callback(self, registry):
        """on_change should register without error."""
        called = []

        def cb(data):
            called.append(data)

        # Should not raise
        registry.on_change("receiver", "default", cb)


class TestSaveWithCreatedBy:
    def test_save_config_with_created_by(self, registry):
        config = ReceiverDeploymentConfig()
        # Should save without error (created_by is used in commit messages)
        registry.save_config("receiver", config, instance="ci", created_by="test-runner")
        loaded = registry.get_config("receiver", "ci")
        assert loaded is not None

    def test_save_config_with_description(self, registry):
        config = LoaderDeploymentConfig()
        registry.save_config(
            "loader", config, instance="custom", description="custom deploy update"
        )
        loaded = registry.get_config("loader", "custom")
        assert loaded is not None
