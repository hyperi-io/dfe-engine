"""Tests for hunt Source model wiring — SourceRegistry resolution."""

from unittest.mock import MagicMock
from jinja2 import Environment, DictLoader

from dfe_engine.hunts.hunt import Hunt


def _make_hunt(tmp_path, source_registry=None, **kwargs):
    """Create a Hunt with minimal defaults."""
    defaults = {
        "cron": "*/5 * * * *",
        "log_buffer": 10,
        "customer": "acme",
        "rules": [{"rule_name": "detect_priv_esc"}],
        "name": "test_hunt",
        "global_source_table_name": "windows_audit",
        "global_target_table_name": "detection",
        "hunt_log_path": str(tmp_path / "logs"),
        "target_config_data": {
            "ch_host": "localhost",
            "ch_port": 9000,
            "ch_username": "default",
            "ch_password": "",
        },
        "source_registry": source_registry,
    }
    defaults.update(kwargs)
    return Hunt(**defaults)


def _make_env(template_sql="SELECT 1 FROM {{ source_table_name }}"):
    """Create a Jinja2 env with a test template."""
    return Environment(
        loader=DictLoader(
            {
                "detect_priv_esc.jinja2": template_sql,
            }
        )
    )


class TestSourceRegistryWiring:
    """Test that Hunt resolves source table names from SourceRegistry."""

    def test_no_registry_uses_global(self, tmp_path):
        hunt = _make_hunt(tmp_path)
        env = _make_env()

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert len(queries) == 1
        assert "windows_audit" in queries[0]

    def test_registry_resolves_source_name(self, tmp_path):
        """When rule has 'source' field, table name comes from registry."""
        mock_source = MagicMock()
        mock_source.table_name = "crowdstrike_edr"

        registry = MagicMock()
        registry.get_source.return_value = mock_source

        hunt = _make_hunt(
            tmp_path,
            source_registry=registry,
            rules=[{"rule_name": "detect_priv_esc", "source": "crowdstrike_edr"}],
        )
        env = _make_env()

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert len(queries) == 1
        assert "crowdstrike_edr" in queries[0]
        registry.get_source.assert_called_once_with("crowdstrike_edr")

    def test_registry_fallback_on_error(self, tmp_path):
        """When registry lookup fails, falls back to global source table."""
        registry = MagicMock()
        registry.get_source.side_effect = KeyError("not_found")

        hunt = _make_hunt(
            tmp_path,
            source_registry=registry,
            rules=[{"rule_name": "detect_priv_esc", "source": "not_found"}],
        )
        env = _make_env()

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert len(queries) == 1
        assert "windows_audit" in queries[0]

    def test_no_source_field_ignores_registry(self, tmp_path):
        """Rules without 'source' field don't use the registry."""
        registry = MagicMock()

        hunt = _make_hunt(
            tmp_path,
            source_registry=registry,
            rules=[{"rule_name": "detect_priv_esc"}],
        )
        env = _make_env()

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert len(queries) == 1
        assert "windows_audit" in queries[0]
        registry.get_source.assert_not_called()

    def test_source_field_without_registry_uses_global(self, tmp_path):
        """Rule has 'source' but no registry → uses global table name."""
        hunt = _make_hunt(
            tmp_path,
            source_registry=None,
            rules=[{"rule_name": "detect_priv_esc", "source": "crowdstrike_edr"}],
        )
        env = _make_env()

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert "windows_audit" in queries[0]

    def test_source_template_variable_available(self, tmp_path):
        """The {{ source }} template variable should resolve to the table name."""
        mock_source = MagicMock()
        mock_source.table_name = "aws_cloudtrail"

        registry = MagicMock()
        registry.get_source.return_value = mock_source

        hunt = _make_hunt(
            tmp_path,
            source_registry=registry,
            rules=[{"rule_name": "detect_priv_esc", "source": "aws_cloudtrail"}],
        )
        env = Environment(
            loader=DictLoader(
                {
                    "detect_priv_esc.jinja2": "SELECT 1 FROM {{ org_id }}.{{ source }}",
                }
            )
        )

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert "org1.aws_cloudtrail" in queries[0]

    def test_per_rule_source_override(self, tmp_path):
        """Each rule can have a different source, resolved independently."""
        source_a = MagicMock()
        source_a.table_name = "table_a"
        source_b = MagicMock()
        source_b.table_name = "table_b"

        registry = MagicMock()
        registry.get_source.side_effect = lambda name: {"src_a": source_a, "src_b": source_b}[name]

        hunt = _make_hunt(
            tmp_path,
            source_registry=registry,
            rules=[
                {"rule_name": "detect_priv_esc", "source": "src_a"},
                {"rule_name": "detect_priv_esc", "source": "src_b"},
            ],
        )
        env = _make_env("FROM {{ source_table_name }}")

        queries = hunt.convert_yaml_to_sql(env, "org1", {})
        assert len(queries) == 2
        assert "table_a" in queries[0]
        assert "table_b" in queries[1]
