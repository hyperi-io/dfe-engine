import pytest


@pytest.fixture(scope="session")
def logs_filebeat_template_subset():
    """
    Provides a subset of the logs filebeat Elasticsearch template.

    This fixture creates a small, manageable portion of a realistic logs
    filebeat Elasticsearch template. It is used for testing purposes to
    simulate input data, avoiding the need for a large external JSON object.

    The returned dictionary represents the relevant parts of the schema
    that are required for unit testing.

    Returns:
        dict: A dictionary representing a subset of the logs filebeat Elasticsearch template.
    """
    return {
        "template": {
            "mappings": {
                "_meta": {"beat": "filebeat", "version": "8.10.4"},
                "dynamic_templates": [
                    {
                        "labels": {
                            "path_match": "labels.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "container.labels": {
                            "path_match": "container.labels.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "fields": {
                            "path_match": "fields.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "docker.container.labels": {
                            "path_match": "docker.container.labels.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "kubernetes.labels.*": {
                            "path_match": "kubernetes.labels.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "*",
                        }
                    },
                    {
                        "kubernetes.annotations.*": {
                            "path_match": "kubernetes.annotations.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "*",
                        }
                    },
                    {
                        "kubernetes.selectors.*": {
                            "path_match": "kubernetes.selectors.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "*",
                        }
                    },
                    {
                        "docker.attrs": {
                            "path_match": "docker.attrs.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "azure.activitylogs.identity.claims.*": {
                            "path_match": "azure.activitylogs.identity.claims.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "*",
                        }
                    },
                    {
                        "kibana.log.meta": {
                            "path_match": "kibana.log.meta.*",
                            "mapping": {"type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                    {
                        "strings_as_keyword": {
                            "mapping": {"ignore_above": 1024, "type": "keyword"},
                            "match_mapping_type": "string",
                        }
                    },
                ],
                "date_detection": "false",
                "properties": {
                    "container": {
                        "properties": {
                            "image": {
                                "properties": {
                                    "name": {"ignore_above": 1024, "type": "keyword"},
                                    "tag": {"ignore_above": 1024, "type": "keyword"},
                                }
                            },
                            "disk": {
                                "properties": {
                                    "read": {"properties": {"bytes": {"type": "long"}}},
                                    "write": {"properties": {"bytes": {"type": "long"}}},
                                }
                            },
                            "memory": {
                                "properties": {
                                    "usage": {
                                        "scaling_factor": 1000,
                                        "type": "scaled_float",
                                    }
                                }
                            },
                            "name": {"ignore_above": 1024, "type": "keyword"},
                            "runtime": {"ignore_above": 1024, "type": "keyword"},
                            "cpu": {
                                "properties": {
                                    "usage": {
                                        "scaling_factor": 1000,
                                        "type": "scaled_float",
                                    }
                                }
                            },
                            "id": {"ignore_above": 1024, "type": "keyword"},
                            "labels": {"type": "object"},
                            "network": {
                                "properties": {
                                    "ingress": {"properties": {"bytes": {"type": "long"}}},
                                    "egress": {"properties": {"bytes": {"type": "long"}}},
                                }
                            },
                        }
                    },
                    "awscloudwatch": {
                        "properties": {
                            "log_group": {"ignore_above": 1024, "type": "keyword"},
                            "ingestion_time": {"ignore_above": 1024, "type": "keyword"},
                            "log_stream": {"ignore_above": 1024, "type": "keyword"},
                        }
                    },
                    "metadata": {"type": "flattened"},
                    "dns": {
                        "properties": {
                            "op_code": {"ignore_above": 1024, "type": "keyword"},
                            "response_code": {"ignore_above": 1024, "type": "keyword"},
                            "resolved_ip": {"type": "ip"},
                            "question": {
                                "properties": {
                                    "registered_domain": {
                                        "ignore_above": 1024,
                                        "type": "keyword",
                                    },
                                    "top_level_domain": {
                                        "ignore_above": 1024,
                                        "type": "keyword",
                                    },
                                    "name": {"ignore_above": 1024, "type": "keyword"},
                                    "subdomain": {
                                        "ignore_above": 1024,
                                        "type": "keyword",
                                    },
                                    "type": {"ignore_above": 1024, "type": "keyword"},
                                    "class": {"ignore_above": 1024, "type": "keyword"},
                                }
                            },
                            "answers": {
                                "type": "object",
                                "properties": {
                                    "data": {"ignore_above": 1024, "type": "keyword"},
                                    "name": {"ignore_above": 1024, "type": "keyword"},
                                    "type": {"ignore_above": 1024, "type": "keyword"},
                                    "class": {"ignore_above": 1024, "type": "keyword"},
                                    "ttl": {"type": "long"},
                                },
                            },
                            "header_flags": {"ignore_above": 1024, "type": "keyword"},
                            "id": {"ignore_above": 1024, "type": "keyword"},
                            "type": {"ignore_above": 1024, "type": "keyword"},
                        }
                    },
                },
            },
            "settings": {
                "index": {
                    "lifecycle": {"name": "filebeat"},
                    "mapping": {"total_fields": {"limit": "10000"}},
                    "refresh_interval": "5s",
                    "number_of_shards": "1",
                    "max_docvalue_fields_search": "200",
                    "query": {
                        "default_field": [
                            "dns.answers.class",
                            "dns.answers.data",
                            "dns.answers.name",
                            "dns.answers.type",
                            "dns.header_flags",
                            "dns.id",
                            "dns.op_code",
                            "dns.question.class",
                            "dns.question.name",
                            "dns.question.registered_domain",
                            "dns.question.subdomain",
                            "dns.question.top_level_domain",
                            "dns.question.type",
                            "dns.response_code",
                            "dns.type",
                            "fields.*",
                        ]
                    },
                }
            },
        }
    }
