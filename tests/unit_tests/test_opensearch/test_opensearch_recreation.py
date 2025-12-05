import pytest
import logging
from dfecli.dfe_opensearch.opensearch_apply import OpenSearchApply


class TestOpenSearchRecreation:
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

    def test_update_opensearch_object_version_control(self, opensearch_applier, mocker):
        """Test template update with version control"""
        mock_get = mocker.patch("requests.get")
        mock_post = mocker.patch("requests.post")
        mock_put = mocker.patch("requests.put")

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "component_templates": [
                {
                    "name": "test-template",
                    "component_template": {"template": {"mappings": {}}, "version": 1},
                }
            ]
        }
        mock_get.return_value.headers = {"_seq_no": "1", "_primary_term": "1"}

        mock_post.return_value.status_code = 200
        mock_put.return_value.status_code = 200

        result = opensearch_applier.update_opensearch_object(
            object_type="component_template",
            object_id="test-template",
            payload={"template": {"mappings": {}}},
        )

        assert result is True
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        assert kwargs["json"]["version"] == 2
        mock_post.assert_not_called()

    def test_update_opensearch_object_no_version(self, opensearch_applier, mocker):
        """Test template update when version info is not available"""
        mock_get = mocker.patch("requests.get")
        mock_put = mocker.patch("requests.put")

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "component_templates": [
                {
                    "name": "test-template",
                    "component_template": {"template": {"mappings": {}}},
                }
            ]
        }
        mock_get.return_value.headers = {}

        mock_put.return_value.status_code = 200

        result = opensearch_applier.update_opensearch_object(
            object_type="component_template",
            object_id="test-template",
            payload={"template": {"mappings": {}}},
        )

        assert result is True
        mock_put.assert_called_once()

    def test_update_opensearch_object_update_error(self, opensearch_applier, mocker):
        """Test error handling when update fails"""
        mock_get = mocker.patch("requests.get")
        mock_put = mocker.patch("requests.put")

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "component_templates": [
                {
                    "name": "test-template",
                    "component_template": {"template": {"mappings": {}}},
                }
            ]
        }
        mock_get.return_value.headers = {}

        mock_put.return_value.status_code = 500

        result = opensearch_applier.update_opensearch_object(
            object_type="component_template",
            object_id="test-template",
            payload={"template": {"mappings": {}}},
        )

        assert result is False
        mock_put.assert_called_once()

    def test_update_opensearch_object_ism_policy(self, opensearch_applier, mocker):
        """Test ISM policy update with version control"""
        mock_get = mocker.patch("requests.get")
        mock_put = mocker.patch("requests.put")

        mock_get.return_value.status_code = 200
        mock_get.return_value.headers = {}
        mock_get.return_value.json.return_value = {"policy": {"description": "test"}}

        mock_put.return_value.status_code = 200

        result = opensearch_applier.update_opensearch_object(
            object_type="ism_policy",
            object_id="test-policy",
            payload={"policy": {"description": "updated"}},
        )

        assert result is True
        mock_put.assert_called_once()

    def test_update_opensearch_object_create_new(self, opensearch_applier, mocker):
        """Test creating new template when it doesn't exist"""
        mock_get = mocker.patch("requests.get")
        mock_put = mocker.patch("requests.put")

        mock_get.return_value.status_code = 404
        mock_put.return_value.status_code = 200

        result = opensearch_applier.update_opensearch_object(
            object_type="component_template",
            object_id="test-template",
            payload={"template": {"mappings": {}}},
        )

        assert result is True
        mock_put.assert_called_once()

    def test_update_opensearch_object_invalid_type(self, opensearch_applier):
        """Test error handling for invalid object type"""
        result = opensearch_applier.update_opensearch_object(
            object_type="invalid_type", object_id="test-template", payload={}
        )
        assert result is False

    def test_update_opensearch_object_connection_error(
        self, opensearch_applier, mocker
    ):
        """Test error handling for connection issues"""
        mock_get = mocker.patch("requests.get")
        mock_get.side_effect = Exception("Connection error")

        result = opensearch_applier.update_opensearch_object(
            object_type="component_template", object_id="test-template", payload={}
        )
        assert result is False
