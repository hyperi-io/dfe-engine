import pytest
import logging
from unittest.mock import Mock
from dfecli.dfe_opensearch.opensearch_tenancy import OpenSearchTenancy
from dfecli.dfe_opensearch.opensearch_apply import OpenSearchApply


class TestOpenSearchTenancy:
    @pytest.fixture(autouse=True)
    def setup_logging(self, mocker):
        logger = logging.getLogger("test_logger")
        logger.setLevel(logging.DEBUG)
        mocker.patch(
            "dfecli.dfe_logger.dfe_logger.DFELog.get_root_logger", return_value=logger
        )

    @pytest.fixture
    def opensearch_applier(self):
        return OpenSearchApply(
            opensearch_url="https://test-opensearch-url",
            bypass_auth=True,
            log_path="tests/logs",
        )

    @pytest.fixture
    def tenancy_manager(self, opensearch_applier):
        return OpenSearchTenancy(opensearch_applier)

    def test_list_tenancies_success(self, tenancy_manager, mocker):
        """Test successful listing of tenancies"""
        mock_request = mocker.patch("requests.request")

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 200

        tenants_response = Mock()
        tenants_response.status_code = 200
        tenants_response.json.return_value = {
            "tenant1": {},
            "tenant2": {},
            "global_tenant": {},
            "admin": {},
        }

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            elif (
                "/api/security/tenants" in url
                or "/_plugins/_security/api/tenants" in url
            ):
                return tenants_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == ["tenant1", "tenant2"]

    def test_list_tenancies_no_security_plugin(self, tenancy_manager, mocker):
        """Test when security plugin is not installed"""
        mock_request = mocker.patch("requests.request")

        mock_request.return_value.status_code = 200
        mock_request.return_value.text = "other-plugin"

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []

    def test_list_tenancies_security_not_configured(self, tenancy_manager, mocker):
        """Test when security plugin is installed but not configured"""
        mock_request = mocker.patch("requests.request")

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 404

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []

    def test_list_tenancies_api_error(self, tenancy_manager, mocker):
        """Test handling of API errors"""
        mock_request = mocker.patch("requests.request")

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 200

        error_response = Mock()
        error_response.status_code = 500
        error_response.text = "Internal Server Error"

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            elif "/_plugins/_security/api/tenants" in url:
                return error_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []

    def test_list_tenancies_aws_environment(self, tenancy_manager, mocker):
        """Test listing tenancies in AWS environment"""
        mock_request = mocker.patch("requests.request")

        tenancy_manager.opensearch.opensearch_url = "https://test-domain.amazonaws.com"

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 200

        tenants_response = Mock()
        tenants_response.status_code = 200
        tenants_response.json.return_value = {
            "aws-tenant1": {},
            "aws-tenant2": {},
            "global_tenant": {},
            "admin": {},
        }

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            elif "/_plugins/_security/api/tenants" in url:
                return tenants_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == ["aws-tenant1", "aws-tenant2"]

    def test_list_tenancies_connection_error(self, tenancy_manager, mocker):
        """Test handling of connection errors"""
        mock_request = mocker.patch("requests.request")
        mock_request.side_effect = Exception("Connection failed")

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []

    def test_list_tenancies_empty_response(self, tenancy_manager, mocker):
        """Test handling of empty response"""
        mock_request = mocker.patch("requests.request")

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 200

        tenants_response = Mock()
        tenants_response.status_code = 200
        tenants_response.json.return_value = {}

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            elif "/_plugins/_security/api/tenants" in url:
                return tenants_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []

    def test_list_tenancies_only_system_tenants(self, tenancy_manager, mocker):
        """Test when only system tenants exist"""
        mock_request = mocker.patch("requests.request")

        plugins_response = Mock()
        plugins_response.status_code = 200
        plugins_response.text = "opensearch-security"

        health_response = Mock()
        health_response.status_code = 200

        tenants_response = Mock()
        tenants_response.status_code = 200
        tenants_response.json.return_value = {"global_tenant": {}, "admin": {}}

        def request_response(method, url, **kwargs):
            if "/_cat/plugins" in url:
                return plugins_response
            elif "/_plugins/_security/health" in url:
                return health_response
            elif "/_plugins/_security/api/tenants" in url:
                return tenants_response
            return Mock(status_code=404)

        mock_request.side_effect = request_response

        tenants = tenancy_manager.list_tenancies()
        assert tenants == []
