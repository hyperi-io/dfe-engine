import pytest
import logging
from dfecli.dfe_opensearch.opensearch_apply import OpenSearchApply


class TestOpenSearchApply:
    @pytest.fixture(autouse=True)
    def setup_logging(self, mocker):
        logger = logging.getLogger("test_logger")
        logger.setLevel(logging.DEBUG)
        mocker.patch(
            "dfecli.dfe_logger.dfe_logger.DFELog.get_root_logger", return_value=logger
        )

    def test_auth_methods(self, mock_boto3):
        applier = OpenSearchApply(
            opensearch_url="https://test-opensearch-url",
            aws_profile="test-profile",
            log_path="tests/logs",
        )
        assert applier.auth is None

        applier = OpenSearchApply(
            opensearch_url="https://test-opensearch-url",
            iam_role="arn:aws:iam::123456789012:role/test-role",
            log_path="tests/logs",
        )
        assert applier.auth is not None

        applier = OpenSearchApply(
            opensearch_url="https://test-opensearch-url",
            username="test",
            password="test",
            log_path="tests/logs",
        )
        assert applier.auth is not None

        with pytest.raises(ValueError):
            OpenSearchApply(
                opensearch_url="https://test-opensearch-url", log_path="tests/logs"
            )

    def test_detect_cluster_type(self, opensearch_applier, mocker):
        mock_get = mocker.patch("requests.get")

        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"number_of_nodes": 10}
        assert opensearch_applier.detect_cluster_type() == "PRODUCTION"

        opensearch_applier._cluster_type = None
        mock_get.return_value.json.return_value = {"number_of_nodes": 3}
        assert opensearch_applier.detect_cluster_type() == "DEVTEST"

        opensearch_applier._cluster_type = None
        mock_get.return_value.status_code = 500
        with pytest.raises(Exception):
            opensearch_applier.detect_cluster_type()

    def test_component_template_validation(self, opensearch_applier):
        from dfecli.dfe_opensearch.opensearch_component import ValidationError

        # Test invalid templates - should raise ValidationError
        invalid_templates = [
            (None, "Invalid template: Template cannot be empty or None"),
            ([], "Invalid template: Template must be a dictionary"),
            (
                {},
                "Invalid template: Template must contain either 'mappings' or 'settings' section",
            ),
            (
                {"template": {}},
                "Invalid template: Template must contain either 'mappings' or 'settings' section",
            ),
            (
                {"template": {"mappings": "invalid"}},
                "Invalid template: 'mappings' must be a dictionary containing field definitions",
            ),
            (
                {"template": {"mappings": {"dynamic_templates": "invalid"}}},
                "Invalid template: 'dynamic_templates' must be a list of mapping rules",
            ),
            (
                {"template": {"mappings": {"dynamic_templates": []}}},
                "Invalid template: 'dynamic_templates' must contain at least one mapping rule",
            ),
            (
                {"template": {"mappings": {"properties": "invalid"}}},
                "Invalid template: 'properties' must be a dictionary defining field mappings",
            ),
            (
                {"template": {"settings": "invalid"}},
                "Invalid template: 'settings' must be a dictionary containing index settings",
            ),
            (
                {"template": {}},
                "Invalid template: Template must contain either 'mappings' or 'settings' section",
            ),
        ]

        for template, expected_error in invalid_templates:
            with pytest.raises(ValidationError) as exc_info:
                opensearch_applier.component.validate_component_template(template)
            assert expected_error in str(exc_info.value)

        valid_templates = [
            {"settings": {}},
            {"mappings": {"properties": {}}},
            {
                "template": {
                    "mappings": {
                        "dynamic_templates": [
                            {
                                "strings_as_keywords": {
                                    "match_mapping_type": "string",
                                    "mapping": {"type": "keyword"},
                                }
                            }
                        ]
                    }
                }
            },
            {
                "template": {
                    "settings": {"index": {"number_of_shards": 1}},
                    "mappings": {
                        "dynamic_templates": [
                            {
                                "strings_as_keywords": {
                                    "match_mapping_type": "string",
                                    "mapping": {"type": "keyword"},
                                }
                            }
                        ],
                        "properties": {"field1": {"type": "keyword"}},
                    },
                }
            },
        ]

        for template in valid_templates:
            assert (
                opensearch_applier.component.validate_component_template(template)
                is True
            )

    def test_index_template_validation(self, opensearch_applier):
        # Test invalid templates with specific validation failures
        invalid_templates = [
            (None, "Template cannot be empty"),
            ([], "Template must be a dictionary"),
            ({}, "Template must contain index_patterns"),
            ({"template": {"mappings": {}}}, "Template must contain index_patterns"),
            ({"index_patterns": "invalid"}, "index_patterns must be a list"),
            ({"index_patterns": [], "template": {}}, "index_patterns cannot be empty"),
            (
                {"index_patterns": ["test-*"], "composed_of": "invalid"},
                "composed_of must be a list",
            ),
            (
                {"index_patterns": ["test-*"], "template": "invalid"},
                "template must be a dictionary",
            ),
        ]

        for template, expected_error in invalid_templates:
            assert not opensearch_applier.index_templates.validate_index_template(
                template
            )

        # Test valid templates that meet all requirements
        valid_template = {
            "index_patterns": ["test-*"],
            "composed_of": ["component-template"],
            "template": {
                "settings": {"index": {"number_of_shards": 1}},
                "mappings": {"properties": {"field1": {"type": "keyword"}}},
            },
        }

        assert opensearch_applier.index_templates.validate_index_template(
            valid_template
        )
