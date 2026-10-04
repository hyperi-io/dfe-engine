"""Unit tests for datasource adapters."""

from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.clickhouse import ClickHouseManager
from dfe_engine.query.datasources import (
    DatasourceAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)
from dfe_engine.query.datasources.clickhouse import (
    ClickHouseAdapter,
    RestrictedReaderUnavailableError,
    restricted_password,
)
from dfe_engine.query.models import ExplainPlan, ExplainStepType
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import DFESettings, SecretsSettings


class TestAdapterRegistry:
    def test_list_adapters(self):
        adapters = list_adapters()
        assert "clickhouse" in adapters
        assert isinstance(adapters, list)

    def test_get_adapter_clickhouse(self):
        adapter = get_adapter("clickhouse:default")
        assert isinstance(adapter, ClickHouseAdapter)

    def test_get_adapter_with_target(self):
        adapter = get_adapter("clickhouse:analytics")
        assert adapter.target == "analytics"

    def test_get_adapter_unknown(self):
        with pytest.raises(ValueError, match="Unknown datasource scheme"):
            get_adapter("unknown:target")

    def test_register_adapter_decorator(self):
        @register_adapter("test_custom")
        class CustomAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return [{"x": 1}], ["x"]

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        assert "test_custom" in list_adapters()
        adapter = get_adapter("test_custom:default")
        assert isinstance(adapter, CustomAdapter)


class TestDatasourceAdapterBase:
    def test_execute_with_explain_sequential(self):
        class TestAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return [{"x": 1}, {"x": 2}, {"x": 3}], ["x"]

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=["test warning"])

            def healthcheck(self):
                return True

        adapter = TestAdapter("default")
        rows, columns, plan = adapter.execute_with_explain("SELECT 1", parallel=False)

        assert len(rows) == 3
        assert plan.warnings == ["test warning"]

    def test_execute_with_explain_parallel(self):
        call_order = []

        class TestAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                call_order.append("execute")
                return [{"x": 1}], ["x"]

            def explain(self, query, params=None):
                call_order.append("explain")
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        adapter = TestAdapter("default")
        rows, columns, plan = adapter.execute_with_explain("SELECT 1", parallel=True)

        assert "execute" in call_order
        assert "explain" in call_order
        assert len(rows) == 1


class TestClickHouseAdapter:
    @pytest.fixture
    def mock_manager(self):
        manager = MagicMock()
        client = MagicMock()
        # query() returns a result with column_names and result_rows
        query_result = MagicMock()
        query_result.column_names = ["id", "name"]
        query_result.result_rows = [(1, "a"), (2, "b"), (3, "c")]
        client.query.return_value = query_result
        manager.get_clickhouse_client.return_value = client
        return manager

    @pytest.fixture
    def adapter_with_mock(self, mock_manager):
        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager
        return adapter

    def test_execute(self, adapter_with_mock):
        rows, columns = adapter_with_mock.execute("SELECT * FROM logs")
        assert len(rows) == 3
        assert columns == ["id", "name"]
        assert rows[0] == {"id": 1, "name": "a"}

    def test_execute_with_params(self, adapter_with_mock, mock_manager):
        adapter_with_mock.execute(
            "SELECT * FROM logs WHERE id = {id:Int}",
            params={"id": 1},
        )
        client = mock_manager.get_clickhouse_client()
        call_kwargs = client.query.call_args[1]
        assert call_kwargs["parameters"] == {"id": 1}

    def test_execute_with_timeout(self, adapter_with_mock, mock_manager):
        adapter_with_mock.execute("SELECT 1", timeout_seconds=120)
        client = mock_manager.get_clickhouse_client()
        call_kwargs = client.query.call_args[1]
        assert call_kwargs["settings"]["max_execution_time"] == 120

    def test_explain(self, adapter_with_mock, mock_manager):
        # Set up explain-specific mock response
        client = mock_manager.get_clickhouse_client()
        client.query.return_value = MagicMock(
            result_rows=[("Expression",), ("  ReadFromMergeTree (logs)",), ("    Filter",)]
        )
        plan = adapter_with_mock.explain("SELECT * FROM logs")
        assert isinstance(plan, ExplainPlan)
        assert len(plan.steps) > 0
        assert plan.raw_plan is not None

    def test_explain_sends_readonly_on_every_query(self, adapter_with_mock, mock_manager):
        client = mock_manager.get_clickhouse_client()
        client.query.side_effect = [
            Exception("EXPLAIN PLAN refused"),
            MagicMock(result_rows=[("Expression",)]),
            MagicMock(column_names=["rows"], result_rows=[]),
        ]
        adapter_with_mock.explain("SELECT * FROM url('http://example.invalid/x', 'CSV')")
        sent = [
            (call.args[0].split()[:2], call.kwargs.get("settings"))
            for call in client.query.call_args_list
        ]
        assert sent == [
            (["EXPLAIN", "PLAN"], {"readonly": 1}),
            (["EXPLAIN", "SELECT"], {"readonly": 1}),
            (["EXPLAIN", "ESTIMATE"], {"readonly": 1}),
        ]

    def test_healthcheck_success(self, mock_manager):
        client = mock_manager.get_clickhouse_client()
        client.query.return_value = MagicMock(result_rows=[(1,)])
        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager
        assert adapter.healthcheck() is True

    def test_healthcheck_failure(self, mock_manager):
        client = mock_manager.get_clickhouse_client()
        client.query.side_effect = Exception("Connection refused")
        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager
        assert adapter.healthcheck() is False

    def test_close_releases_only_what_the_adapter_opened(self):
        # spec= refuses any method ClickHouseManager does not have.
        shared = MagicMock(spec=ClickHouseManager)
        restricted = MagicMock()
        adapter = ClickHouseAdapter("default")
        adapter._manager = shared
        adapter._restricted_client = restricted
        adapter.close()
        restricted.close.assert_called_once()
        shared.close.assert_not_called()
        assert adapter._manager is None
        assert adapter._restricted_client is None

    def test_a_configured_adapter_builds_its_own_manager(self):
        before = ClickHouseManager._instance
        first = ClickHouseAdapter("default", config={"ch_host": "ch-a.example"})
        second = ClickHouseAdapter("default", config={"ch_host": "ch-b.example"})
        assert first.manager is not second.manager
        assert first.manager.target_config_data == {"ch_host": "ch-a.example"}
        assert second.manager.target_config_data == {"ch_host": "ch-b.example"}
        assert ClickHouseManager._instance is before

    def test_close_cleans_up_the_manager_a_configured_adapter_owns(self):
        owned = MagicMock(spec=ClickHouseManager)
        adapter = ClickHouseAdapter("default", config={"ch_host": "ch-a.example"})
        adapter._manager = owned
        adapter.close()
        owned.close.assert_called_once()
        assert adapter._manager is None


class TestGetRestrictedClientTls:
    """The restricted query client takes the engine's ClickHouseSettings TLS posture."""

    def test_secure_settings_pass_verify_and_ca_cert(self, monkeypatch, tmp_path) -> None:
        ca = tmp_path / "internal-ca.pem"
        ca.write_text("cert")
        settings = DFESettings(env="test")
        settings.query_views.restricted_password = "reader-pw"
        settings.clickhouse.secure = True
        settings.clickhouse.verify = True
        settings.clickhouse.ca_cert = str(ca)
        monkeypatch.setattr("dfe_engine.settings.get_settings", lambda: settings)

        adapter = ClickHouseAdapter("default")
        with patch("clickhouse_connect.get_client") as get_client:
            adapter.get_restricted_client()
        kwargs = get_client.call_args[1]
        assert kwargs["secure"] is True
        assert kwargs["verify"] is True
        assert kwargs["ca_cert"] == str(ca)

    def test_insecure_settings_pass_no_tls_kwargs(self, monkeypatch) -> None:
        settings = DFESettings(env="test")
        settings.query_views.restricted_password = "reader-pw"
        settings.clickhouse.secure = False
        monkeypatch.setattr("dfe_engine.settings.get_settings", lambda: settings)

        adapter = ClickHouseAdapter("default")
        with patch("clickhouse_connect.get_client") as get_client:
            adapter.get_restricted_client()
        kwargs = get_client.call_args[1]
        assert "secure" not in kwargs
        assert "verify" not in kwargs
        assert "ca_cert" not in kwargs


class TestRestrictedReaderPassword:
    """The views run as the minted reader, on the password the reconcile gave it."""

    @pytest.fixture(autouse=True)
    def _no_provided_password(self, monkeypatch):
        monkeypatch.delenv("DFE_CLICKHOUSE_QUERY_READER_PASSWORD", raising=False)

    def _settings(self, tmp_path) -> DFESettings:
        return DFESettings(env="test", secrets=SecretsSettings(provider="file", path=str(tmp_path)))

    def test_the_configured_password_wins(self, tmp_path) -> None:
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/query_reader", "stored-pw")
        settings.query_views.restricted_password = "configured-pw"
        assert restricted_password(settings) == "configured-pw"

    def test_unset_reads_the_minted_readers_stored_secret(self, tmp_path) -> None:
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/query_reader", "stored-pw")
        assert restricted_password(settings) == "stored-pw"

    def test_the_client_connects_as_the_reader_with_the_stored_secret(self, tmp_path) -> None:
        settings = self._settings(tmp_path)
        build_secrets(settings.secrets).put("ch/service/query_reader", "stored-pw")

        with patch("clickhouse_connect.get_client") as get_client:
            ClickHouseAdapter("default").get_restricted_client(settings)
        kwargs = get_client.call_args[1]
        assert kwargs["username"] == "dfe_query_reader"
        assert kwargs["password"] == "stored-pw"

    def test_no_password_anywhere_is_refused_before_connecting(self, tmp_path) -> None:
        with patch("clickhouse_connect.get_client") as get_client:
            with pytest.raises(RestrictedReaderUnavailableError, match="dfe_query_reader"):
                ClickHouseAdapter("default").get_restricted_client(self._settings(tmp_path))
        get_client.assert_not_called()


class TestClickHouseAdapterExplainParsing:
    def test_classify_step_read(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        assert adapter._classify_step("ReadFromMergeTree") == ExplainStepType.READ

    def test_classify_step_filter(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        assert adapter._classify_step("Filter (x > 10)") == ExplainStepType.FILTER

    def test_classify_step_aggregate(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        assert adapter._classify_step("Aggregating") == ExplainStepType.AGGREGATE

    def test_classify_step_sort(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        assert adapter._classify_step("Sorting") == ExplainStepType.SORT

    def test_classify_step_unknown(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        assert adapter._classify_step("SomethingElse") == ExplainStepType.UNKNOWN

    def test_extract_details_table(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        details = adapter._extract_details("ReadFromMergeTree(events)")
        assert details is not None
        assert details["table"] == "events"

    def test_extract_details_none(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        details = adapter._extract_details("Expression")
        assert details is None

    def test_parse_explain_plan(self):
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)
        raw_plan = (
            "Expression\n  ReadFromMergeTree (logs)\n    Filter (level = 'ERROR')\n    Limit 100"
        )
        steps = adapter._parse_explain_plan(raw_plan)
        assert len(steps) == 4


class TestAdapterConfig:
    def test_adapter_with_config(self):
        class ConfigurableAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return [], []

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return self.config.get("test_key") == "test_value"

        adapter = ConfigurableAdapter("default", config={"test_key": "test_value"})
        assert adapter.healthcheck() is True

    def test_adapter_default_config(self):
        class SimpleAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return [], []

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        adapter = SimpleAdapter("default")
        assert adapter.config == {}
