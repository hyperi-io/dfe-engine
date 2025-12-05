import pytest
import json
from unittest.mock import patch, mock_open, MagicMock
from dfecli.dfe_opensearch.apply_cm_templates import CMTemplateApply


@pytest.fixture
def cm_template_apply():
    return CMTemplateApply(
        opensearch_url="http://localhost:9200", username="admin", password="admin"
    )


@pytest.fixture
def sample_cm_template():
    return {
        "index_patterns": ["test-*"],
        "template": {
            "settings": {
                "index": {"lifecycle": {"name": "test-policy"}, "number_of_shards": 1}
            },
            "mappings": {
                "properties": {
                    "field1": {"type": "keyword"},
                    "field2": {
                        "type": "object",
                        "properties": {
                            "nested1": {"type": "keyword"},
                            "nested2": {"type": "keyword"},
                        },
                    },
                    "field3": {
                        "type": "text",
                        "fields": {
                            "keyword": {"type": "keyword"},
                            "raw": {"type": "keyword"},
                        },
                    },
                }
            },
        },
    }


def test_find_cm_templates_directory(tmp_path, cm_template_apply):
    """Test finding CM templates in a directory"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    sub_dir = templates_dir / "subdir"
    sub_dir.mkdir()
    (templates_dir / "test1_cm.json").touch()
    (templates_dir / "normal.json").touch()
    (sub_dir / "test2_cm.json").touch()

    templates = cm_template_apply.find_cm_templates(str(templates_dir))

    assert len(templates) == 2
    assert any("test1_cm.json" in t for t in templates)
    assert any("test2_cm.json" in t for t in templates)
    assert not any("normal.json" in t for t in templates)


def test_find_cm_templates_single_file(tmp_path, cm_template_apply):
    """Test finding CM templates when path is a single file"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    cm_file = templates_dir / "test_cm.json"
    cm_file.touch()

    templates = cm_template_apply.find_cm_templates(str(cm_file))
    assert len(templates) == 1
    assert templates[0] == str(cm_file)


def test_find_cm_templates_non_cm_file(tmp_path, cm_template_apply):
    """Test finding CM templates when path is a non-CM file"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    non_cm_file = templates_dir / "test.json"
    non_cm_file.touch()

    templates = cm_template_apply.find_cm_templates(str(non_cm_file))
    assert len(templates) == 0


def test_load_cm_template(cm_template_apply, sample_cm_template):
    mock_file = mock_open(read_data=json.dumps(sample_cm_template))
    with patch("builtins.open", mock_file):
        loaded = cm_template_apply.load_cm_template("fake_path.json")
        assert loaded == sample_cm_template


def test_load_cm_template_invalid_json(cm_template_apply):
    mock_file = mock_open(read_data="invalid json")
    with patch("builtins.open", mock_file):
        with pytest.raises(Exception):
            cm_template_apply.load_cm_template("fake_path.json")


def test_count_fields(cm_template_apply):
    properties = {
        "field1": {"type": "keyword"},
        "field2": {
            "type": "object",
            "properties": {
                "nested1": {"type": "keyword"},
                "nested2": {"type": "keyword"},
            },
        },
        "field3": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword"}, "raw": {"type": "keyword"}},
        },
    }

    count = cm_template_apply._count_fields(properties)
    assert count == 7


@patch("requests.get")
@patch("requests.put")
def test_apply_cm_template_new_template(
    mock_put, mock_get, cm_template_apply, sample_cm_template
):
    health_response = MagicMock()
    health_response.status_code = 200
    health_response.text = (
        '{"status":"green","cluster_name":"opensearch","version":{"number":"2.3.0"}}'
    )
    health_response.json.return_value = {
        "status": "green",
        "cluster_name": "opensearch",
        "version": {"number": "2.3.0"},
    }

    template_check = MagicMock()
    template_check.status_code = 404
    mock_get.side_effect = [health_response, template_check]
    mock_put.return_value.status_code = 200

    with patch.object(
        cm_template_apply.opensearch, "detect_cluster_type", return_value="DEVTEST"
    ):
        mock_file = mock_open(read_data=json.dumps(sample_cm_template))
        with patch("builtins.open", mock_file):
            success = cm_template_apply.apply_cm_template("test_cm.json")

        assert success
    assert mock_put.call_count == 1
    put_template = mock_put.call_args[1]["json"]
    assert "template" in put_template
    assert "settings" in put_template["template"]
    assert "mappings" in put_template["template"]
    assert put_template["version"] == 1
    assert "lifecycle" not in put_template["template"]["settings"]["index"]


@patch("requests.get")
@patch("requests.put")
def test_apply_cm_template_version_increment(
    mock_put, mock_get, cm_template_apply, sample_cm_template
):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "index_templates": [
            {
                "name": "test-cm",
                "index_template": {
                    "version": 2,
                    "template": {"settings": {}, "mappings": {}},
                },
            }
        ]
    }
    mock_put.return_value.status_code = 200
    mock_file = mock_open(read_data=json.dumps(sample_cm_template))
    with patch("builtins.open", mock_file):
        success = cm_template_apply.apply_cm_template("test_cm.json")

    assert success
    put_template = mock_put.call_args[1]["json"]
    assert put_template["version"] == 3


@patch("requests.get")
@patch("requests.put")
def test_apply_cm_template_large_version(
    mock_put, mock_get, cm_template_apply, sample_cm_template
):
    """Test version handling when version number is large (>1000)"""
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "index_templates": [
            {
                "name": "test-cm",
                "index_template": {
                    "version": 1234,
                    "template": {"settings": {}, "mappings": {}},
                },
            }
        ]
    }
    mock_put.return_value.status_code = 200

    mock_file = mock_open(read_data=json.dumps(sample_cm_template))
    with patch("builtins.open", mock_file):
        success = cm_template_apply.apply_cm_template("test_cm.json")

    assert success
    put_template = mock_put.call_args[1]["json"]
    assert put_template["version"] == 1235


@patch("requests.get")
def test_apply_cm_templates_connection_error(
    mock_get, tmp_path, cm_template_apply, sample_cm_template
):
    """Test handling of connection errors"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    cm_file = templates_dir / "test_cm.json"
    with open(cm_file, "w") as f:
        json.dump(sample_cm_template, f)

    mock_get.side_effect = Exception("Connection refused")
    success = cm_template_apply.apply_cm_templates(str(cm_file))
    assert not success
    assert mock_get.call_count == 1
    assert "_cluster/health" in mock_get.call_args[0][0]


@patch("requests.get")
@patch("requests.put")
def test_apply_cm_templates_single_file(
    mock_put, mock_get, tmp_path, cm_template_apply, sample_cm_template
):
    """Test applying templates when path is a single file"""

    # Mock responses for all API calls
    health_response = MagicMock()
    health_response.status_code = 200
    health_response.text = (
        '{"status":"green","cluster_name":"opensearch","version":{"number":"2.3.0"}}'
    )
    health_response.json.return_value = {
        "status": "green",
        "cluster_name": "opensearch",
        "version": {"number": "2.3.0"},
    }

    aws_check = MagicMock()
    aws_check.status_code = 200
    aws_check.json.return_value = {
        "cluster_name": "test-cluster",
        "version": {"distribution": "opensearch", "number": "2.3.0"},
    }

    template_check = MagicMock()
    template_check.status_code = 404

    mock_get.side_effect = [health_response, aws_check, template_check]
    mock_put.return_value = MagicMock()
    mock_put.return_value.status_code = 200

    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    cm_file = templates_dir / "test_cm.json"
    with open(cm_file, "w") as f:
        json.dump(sample_cm_template, f)

    with patch.object(
        cm_template_apply.opensearch, "detect_cluster_type", return_value="DEVTEST"
    ):
        success = cm_template_apply.apply_cm_templates(str(cm_file))
    assert success
    assert mock_get.call_count == 3
    assert "_cluster/health" in mock_get.call_args_list[0][0][0]
    assert "/" in mock_get.call_args_list[1][0][0]  # AWS cluster check
    assert "_template/test-cm" in mock_get.call_args_list[2][0][0]
    assert mock_put.call_count == 1

    put_template = mock_put.call_args[1]["json"]

    assert "template" in put_template
    assert "settings" in put_template["template"]
    assert "mappings" in put_template["template"]
    assert put_template["version"] == 1


@patch("requests.get")
@patch("requests.put")
def test_apply_cm_templates_template_failure(
    mock_put, mock_get, tmp_path, cm_template_apply, sample_cm_template
):
    """Test handling of template application failure"""

    # Mock responses for all API calls
    health_response = MagicMock()
    health_response.status_code = 200
    health_response.text = (
        '{"status":"green","cluster_name":"opensearch","version":{"number":"2.3.0"}}'
    )
    health_response.json.return_value = {
        "status": "green",
        "cluster_name": "opensearch",
        "version": {"number": "2.3.0"},
    }

    aws_check = MagicMock()
    aws_check.status_code = 200
    aws_check.json.return_value = {
        "cluster_name": "test-cluster",
        "version": {"distribution": "opensearch", "number": "2.3.0"},
    }

    template_response = MagicMock()
    template_response.status_code = 404

    mock_get.side_effect = [health_response, aws_check, template_response]
    mock_put.return_value.status_code = 500
    mock_put.return_value.text = "Internal server error"

    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    cm_file = templates_dir / "test_cm.json"
    with open(cm_file, "w") as f:
        json.dump(sample_cm_template, f)

    with patch.object(
        cm_template_apply.opensearch, "detect_cluster_type", return_value="DEVTEST"
    ):
        success = cm_template_apply.apply_cm_templates(str(cm_file))
    assert not success
    assert mock_put.call_count == 1
    assert "_template/test-cm" in mock_put.call_args[0][0]


@patch("requests.get")
def test_apply_cm_templates_health_check_failure(
    mock_get, tmp_path, cm_template_apply, sample_cm_template
):
    """Test handling of health check failure"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    cm_file = templates_dir / "test_cm.json"
    with open(cm_file, "w") as f:
        json.dump(sample_cm_template, f)

    health_response = MagicMock()
    health_response.status_code = 503
    health_response.text = "Service Unavailable"
    mock_get.return_value = health_response

    success = cm_template_apply.apply_cm_templates(str(cm_file))
    assert not success
    assert mock_get.call_count == 1
    assert "_cluster/health" in mock_get.call_args[0][0]
