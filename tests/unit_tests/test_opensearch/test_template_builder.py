class TestOpenSearchTemplateValidation:
    def test_framework_template_structure(self, framework_template):
        assert "template" in framework_template
        template = framework_template["template"]

        assert "mappings" in template
        mappings = template["mappings"]

        assert "dynamic_templates" in mappings
        dynamic_templates = mappings["dynamic_templates"]
        assert len(dynamic_templates) > 0

        string_template = next(
            t for t in dynamic_templates if "strings_as_keywords" in t
        )
        assert string_template["strings_as_keywords"]["match_mapping_type"] == "string"
        assert string_template["strings_as_keywords"]["mapping"]["type"] == "keyword"
        assert string_template["strings_as_keywords"]["mapping"]["ignore_above"] == 1024
        assert (
            string_template["strings_as_keywords"]["mapping"]["normalizer"]
            == "lowercase_normalizer"
        )

        assert "properties" in mappings
        properties = mappings["properties"]

        required_fields = ["@timestamp", "event_hash", "logoriginal", "org_id", "tags"]
        for field in required_fields:
            assert field in properties

        assert properties["@timestamp"]["type"] == "date"

        tags = properties["tags"]["properties"]
        assert "collector" in tags
        assert "event" in tags

        collector = tags["collector"]["properties"]
        collector_fields = ["host", "hostname", "source", "timestamp", "timezone"]
        for field in collector_fields:
            assert field in collector

        event = tags["event"]["properties"]
        event_fields = ["category", "org_id", "site_id", "type", "error"]
        for field in event_fields:
            assert field in event

        assert "settings" in template
        settings = template["settings"]
        assert "analysis" in settings
        assert "normalizer" in settings["analysis"]
        assert "lowercase_normalizer" in settings["analysis"]["normalizer"]

    def test_index_template_structure(self, index_template):
        assert "_meta" in index_template
        assert "description" in index_template["_meta"]
        assert "HyperSec" in index_template["_meta"]["description"]

        assert "composed_of" in index_template
        assert "hypersec-log-component-template" in index_template["composed_of"]

        assert "priority" in index_template
        assert isinstance(index_template["priority"], str)
        assert index_template["priority"].isdigit()

        assert "index_patterns" in index_template
        patterns = index_template["index_patterns"]
        assert isinstance(patterns, list)
        assert len(patterns) > 0
        assert all(isinstance(p, str) for p in patterns)
        assert all("*" in p for p in patterns)
        assert all(p.replace("*", "").islower() for p in patterns)

        assert "template" in index_template
        template = index_template["template"]

        assert "mappings" in template
        mappings = template["mappings"]

        assert "date_detection" in mappings
        assert isinstance(mappings["date_detection"], bool)

        assert "properties" in mappings
        properties = mappings["properties"]

        timestamp_fields = ["@timestamp", "timestamp_collector"]
        for field in timestamp_fields:
            assert field in properties
            assert properties[field]["type"] == "date"

        integer_fields = ["event_id", "logon_type", "port", "process_id"]
        for field in integer_fields:
            assert field in properties
            assert properties[field]["type"] == "integer"

        ip_fields = ["ip_address_v4", "ip_address_v6"]
        for field in ip_fields:
            assert field in properties
            assert properties[field]["type"] == "ip"

        keyword_fields = ["account_name", "logon_process_name"]
        for field in keyword_fields:
            assert field in properties
            assert properties[field]["type"] == "keyword"
            assert "normalizer" in properties[field]
            assert properties[field]["normalizer"] == "lowercase_normalizer"

        assert "settings" in template
        settings = template["settings"]
        assert "index" in settings
        assert "query" in settings["index"]
        assert "default_field" in settings["index"]["query"]
        assert isinstance(settings["index"]["query"]["default_field"], list)
        assert len(settings["index"]["query"]["default_field"]) > 0

    def test_dynamic_field_mapping(self, framework_template):
        template = framework_template["template"]
        mappings = template["mappings"]

        assert "dynamic_templates" in mappings
        dynamic_templates = mappings["dynamic_templates"]

        string_template = next(
            t for t in dynamic_templates if "strings_as_keywords" in t
        )
        mapping = string_template["strings_as_keywords"]["mapping"]
        assert mapping["type"] == "keyword"
        assert mapping["ignore_above"] == 1024
        assert mapping["normalizer"] == "lowercase_normalizer"

        assert string_template["strings_as_keywords"]["match_mapping_type"] == "string"

    def test_template_inheritance(self, index_template):
        assert "composed_of" in index_template
        composed_of = index_template["composed_of"]
        assert isinstance(composed_of, list)
        assert "hypersec-log-component-template" in composed_of

        template = index_template["template"]
        assert "mappings" in template
        mappings = template["mappings"]

        assert "properties" in mappings
        properties = mappings["properties"]

        for field in properties.values():
            if field.get("type") == "keyword":
                assert "normalizer" in field
                assert field["normalizer"] == "lowercase_normalizer"

    def test_field_analyzers(self, framework_template):
        template = framework_template["template"]
        settings = template["settings"]

        assert "analysis" in settings
        analysis = settings["analysis"]

        assert "normalizer" in analysis
        normalizer = analysis["normalizer"]

        assert "lowercase_normalizer" in normalizer
        lowercase = normalizer["lowercase_normalizer"]

        assert lowercase["type"] == "custom"
        assert "filter" in lowercase
        assert "lowercase" in lowercase["filter"]
        assert isinstance(lowercase["filter"], list)

        mappings = template["mappings"]
        properties = mappings["properties"]

        text_fields = [f for f in properties.values() if f.get("type") == "text"]
        for field in text_fields:
            assert "norms" in field
            assert not field["norms"]
