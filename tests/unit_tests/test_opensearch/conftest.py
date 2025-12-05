import pytest
from dfecli.dfe_opensearch.opensearch_apply import OpenSearchApply


@pytest.fixture
def framework_dir():
    return "test_framework_dir"


@pytest.fixture
def templates_dir():
    return "test_templates_dir"


@pytest.fixture
def template_file():
    return "test_template.json"


@pytest.fixture
def framework_template():
    return {
        "template": {
            "mappings": {
                "dynamic_templates": [
                    {
                        "strings_as_keywords": {
                            "match_mapping_type": "string",
                            "mapping": {
                                "type": "keyword",
                                "ignore_above": 1024,
                                "normalizer": "lowercase_normalizer",
                            },
                        }
                    }
                ],
                "properties": {
                    "@timestamp": {"type": "date"},
                    "event_hash": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer",
                    },
                    "logoriginal": {"type": "text", "norms": False},
                    "org_id": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer",
                    },
                    "tags": {
                        "properties": {
                            "collector": {
                                "properties": {
                                    "host": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "hostname": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "source": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "timestamp": {"type": "date"},
                                    "timezone": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                }
                            },
                            "event": {
                                "properties": {
                                    "category": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "org_id": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "site_id": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "type": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                    "error": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                }
                            },
                        }
                    },
                },
            },
            "settings": {
                "analysis": {
                    "normalizer": {
                        "lowercase_normalizer": {
                            "type": "custom",
                            "char_filter": [],
                            "filter": ["lowercase"],
                        }
                    }
                }
            },
        }
    }


@pytest.fixture
def index_template():
    return {
        "_meta": {"description": "HyperSec logs_nxlog_windows OpenSearch template"},
        "composed_of": ["hypersec-log-component-template"],
        "priority": "100",
        "index_patterns": ["logs-nxlog-windows-*"],
        "template": {
            "mappings": {
                "date_detection": False,
                "properties": {
                    "@timestamp": {"type": "date"},
                    "timestamp_collector": {"type": "date"},
                    "event_id": {"type": "integer"},
                    "logon_type": {"type": "integer"},
                    "port": {"type": "integer"},
                    "process_id": {"type": "integer"},
                    "ip_address_v4": {"type": "ip"},
                    "ip_address_v6": {"type": "ip"},
                    "account_name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer",
                    },
                    "logon_process_name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer",
                    },
                },
            },
            "settings": {
                "index": {
                    "query": {
                        "default_field": [
                            "event_hash",
                            "logoriginal",
                            "org_id",
                            "tags.event.org_id",
                            "tags.event.site_id",
                            "message",
                        ]
                    }
                }
            },
        },
    }


@pytest.fixture
def test_log_data():
    return {
        "@timestamp": "2024-01-01T00:00:00Z",
        "event_hash": "abc123",
        "logoriginal": "Windows Security Event ID 4624",
        "org_id": "test_org",
        "tags": {
            "collector": {
                "host": "test-host",
                "source": "windows",
                "hostname": "win-server-01",
                "timestamp": "2024-01-01T00:00:00Z",
                "timezone": "UTC",
            },
            "event": {
                "category": "windows",
                "type": "eventlog",
                "org_id": "test_org",
                "site_id": "site_01",
            },
        },
        "event_id": 4624,
        "logon_type": 2,
        "account_name": "test_user",
        "logon_process_name": "Advapi",
        "ip_address_v4": "192.168.1.100",
        "port": 49152,
        "process_id": 1234,
    }


@pytest.fixture
def invalid_log_data():
    return {
        "@timestamp": "not-a-date",
        "event_id": "not-an-integer",
        "logon_type": "2",
        "ip_address_v4": "not-an-ip",
        "port": "not-an-integer",
    }


@pytest.fixture
def opensearch_applier():
    return OpenSearchApply(
        opensearch_url="https://test-opensearch-url",
        username="test",
        password="test",
        log_path="tests/logs",
        verbose=True,
        bypass_auth=True,
    )


@pytest.fixture(autouse=True)
def mock_boto3(mocker):
    return mocker.patch("boto3.Session")
