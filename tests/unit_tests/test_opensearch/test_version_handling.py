import pytest
from unittest.mock import Mock, patch
from typing import Optional
from dfecli.dfe_opensearch.opensearch_apply import OpenSearchApply


@pytest.fixture
def opensearch_apply() -> OpenSearchApply:
    opensearch_url: str = "https://test-opensearch-url"
    username: Optional[str] = None
    password: Optional[str] = None
    bypass_auth: bool = True
    log_path: str = "tests/logs"
    verbose: bool = True

    return OpenSearchApply(
        opensearch_url=opensearch_url,
        username=username,
        password=password,
        bypass_auth=bypass_auth,
        log_path=log_path,
        verbose=verbose,
    )


@pytest.fixture
def mock_response():
    return Mock(status_code=200)


def test_component_template_version_increment(opensearch_apply, mock_response):
    """Test component template version is incremented on update"""
    template_content = {"template": {"settings": {"index": {"number_of_shards": 1}}}}

    with patch("requests.get") as mock_get, patch("requests.put") as mock_put:
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "component_templates": [
                        {
                            "name": "test-template",
                            "component_template": {
                                "template": {"mappings": {}},
                                "version": 5,
                            },
                        }
                    ]
                }
            ),
        )
        mock_put.return_value = mock_response

        result = opensearch_apply.update_opensearch_object(
            object_type="component_template",
            object_id="test-template",
            payload=template_content,
        )

        assert result is True
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        assert args[0].endswith("/_component_template/test-template")
        assert kwargs["headers"] == {"Content-Type": "application/json"}
        assert kwargs["json"]["version"] == 6
        assert kwargs["json"]["template"] == template_content["template"]
        assert opensearch_apply.detect_cluster_type() == "DEVTEST"
