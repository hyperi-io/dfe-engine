import pytest
import yaml

from dfe_engine.sigma.sigma_converter import SigmaRuleConverter
from dfe_engine.yaml_utils import YAMLError


@pytest.fixture
def converter(tmp_path, dfe_package):
    """Create a SigmaRuleConverter instance with temporary directories."""
    input_dir = tmp_path / "rules"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    return SigmaRuleConverter(
        args_dfe_package_file_path=dfe_package,
        input_directory=str(input_dir),
        output_directory=str(output_dir),
        dfe_root_log_path=str(tmp_path),
    )


@pytest.fixture
def mock_schema_config():
    """Create a mock schema config for testing."""
    return {
        "derived_schema_file_path": "test_schema/test_schema.csv",
        "sigma_rules": {},
    }


def test_empty_rule_file(converter, tmp_path, mock_schema_config):
    """Test handling of empty rule file."""
    rule_file = tmp_path / "rules" / "empty.yml"
    rule_file.write_text("")

    mock_schema_config["sigma_rules"]["empty_rule"] = {
        "path": "empty.yml",
        "alert_fields": {"Image": "process_name", "CommandLine": "command_line"},
    }

    with pytest.raises(AttributeError) as exc_info:
        converter.convert(str(rule_file), mock_schema_config)
    assert "'NoneType' object has no attribute 'get'" in str(exc_info.value)


def test_malformed_yaml(converter, tmp_path, mock_schema_config):
    """Test handling of malformed YAML."""
    rule_file = tmp_path / "rules" / "malformed.yml"
    rule_file.write_text("""
    title: Malformed Rule
    detection:
      selection: {
        field: value
    """)

    mock_schema_config["sigma_rules"]["malformed_rule"] = {
        "path": "malformed.yml",
        "alert_fields": {"Image": "process_name", "CommandLine": "command_line"},
    }

    with pytest.raises(YAMLError):
        converter.convert(str(rule_file), mock_schema_config)


def test_missing_required_fields(converter, tmp_path, mock_schema_config):
    """Test handling of rules missing required fields."""
    rule_file = tmp_path / "rules" / "missing_fields.yml"
    rule_file.write_text("""
    title: Missing Fields Rule
    detection:
        selection:
            field: value
    """)

    mock_schema_config["sigma_rules"]["missing_fields_rule"] = {
        "path": "missing_fields.yml",
        "alert_fields": {"field": "mapped_field"},
    }

    from sigma.exceptions import SigmaLogsourceError

    with pytest.raises(SigmaLogsourceError) as exc_info:
        converter.convert(str(rule_file), mock_schema_config)
    assert "Sigma rule must have a log source" in str(exc_info.value)


def test_special_characters_in_fields(converter, tmp_path, mock_schema_config):
    """Test handling of special characters in field names and values."""
    rule_file = tmp_path / "rules" / "special_chars.yml"
    rule_file.write_text("""
    title: Special Characters Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            'field!@#$%': 'value!@#$%'
            'field\\with\\backslash': 'value\\with\\backslash'
            'field"with"quotes': 'value"with"quotes'
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["special_characters_rule"] = {
        "path": "special_chars.yml",
        "alert_fields": {
            "field!@#$%": "mapped_field1",
            "field\\with\\backslash": "mapped_field2",
            'field"with"quotes': "mapped_field3",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_large_rule(converter, tmp_path, mock_schema_config):
    """Test handling of extremely large rules."""
    large_conditions = {"field" + str(i): "value" + str(i) for i in range(1000)}
    rule = {
        "title": "Large Rule",
        "logsource": {"product": "windows", "service": "security"},
        "detection": {"selection": large_conditions, "condition": "selection"},
        "level": "medium",
    }

    rule_file = tmp_path / "rules" / "large.yml"
    rule_file.write_text(yaml.dump(rule))

    mock_schema_config["sigma_rules"]["large_rule"] = {
        "path": "large.yml",
        "alert_fields": {"field0": "mapped_field0", "field1": "mapped_field1"},
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_complex_nested_conditions(converter, tmp_path, mock_schema_config):
    """Test handling of complex nested conditions."""
    rule_file = tmp_path / "rules" / "complex.yml"
    rule_file.write_text("""
    title: Complex Nested Rule
    logsource:
        product: windows
        service: security
    detection:
        selection1:
            field1: value1
        selection2:
            field2|contains:
                - value2
                - value3
        selection3:
            field3|endswith:
                - value4
                - value5
        filter1:
            field4|startswith: 
                - prefix1
                - prefix2
        filter2:
            field5|re: 'pattern.*'
        condition: (selection1 and selection2 or selection3) and not 1 of filter*
    level: medium
    """)

    mock_schema_config["sigma_rules"]["complex_nested_rule"] = {
        "path": "complex.yml",
        "alert_fields": {
            "field1": "mapped_field1",
            "field2": "mapped_field2",
            "field3": "mapped_field3",
            "field4": "mapped_field4",
            "field5": "mapped_field5",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_invalid_mitre_tags(converter, tmp_path, mock_schema_config):
    """Test handling of invalid MITRE ATT&CK tags."""
    rule_file = tmp_path / "rules" / "invalid_tags.yml"
    rule_file.write_text("""
    title: Invalid Tags Rule
    logsource:
        product: windows
        service: security
    tags:
        - attack.invalid_tactic
        - attack.t99999
        - attack.execution
        - attack.t1059.001
    detection:
        selection:
            field: value
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["invalid_tags_rule"] = {
        "path": "invalid_tags.yml",
        "alert_fields": {"field": "mapped_field"},
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_unicode_values(converter, tmp_path, mock_schema_config):
    """Test handling of Unicode characters in values."""
    rule_file = tmp_path / "rules" / "unicode.yml"
    rule_file.write_text("""
    title: Unicode Values Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            field1: 'value with unicode ♠♣♥♦'
            field2: 'value with emoji 🌟🎉'
            field3: 'value with accents éèêë'
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["unicode_values_rule"] = {
        "path": "unicode.yml",
        "alert_fields": {
            "field1": "mapped_field1",
            "field2": "mapped_field2",
            "field3": "mapped_field3",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_boundary_conditions(converter, tmp_path, mock_schema_config):
    """Test handling of boundary conditions in field values."""
    rule_file = tmp_path / "rules" / "boundary.yml"
    rule_file.write_text("""
    title: Boundary Conditions Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            field1: ''  # Empty string
            field2: ' '  # Single space
            field3: '   '  # Multiple spaces
            field4: '\\n'  # Newline
            field5: '\\t'  # Tab
            field6: '\\r\\n'  # Windows newline
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["boundary_conditions_rule"] = {
        "path": "boundary.yml",
        "alert_fields": {
            "field1": "mapped_field1",
            "field2": "mapped_field2",
            "field3": "mapped_field3",
            "field4": "mapped_field4",
            "field5": "mapped_field5",
            "field6": "mapped_field6",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_invalid_alert_metadata(converter, tmp_path, mock_schema_config):
    """Test handling of invalid alert metadata."""
    rule_file = tmp_path / "rules" / "invalid_metadata.yml"
    rule_file.write_text("""
    title: Invalid Metadata Rule
    logsource:
        product: windows
        service: security
    description: Test rule with invalid metadata
    tags:
        - attack.execution
        - attack.t1059.001
    detection:
        selection:
            field: value
        condition: selection
    level: invalid_level
    status: invalid_status
    """)

    mock_schema_config["sigma_rules"]["invalid_metadata_rule"] = {
        "path": "invalid_metadata.yml",
        "alert_fields": {"field": "mapped_field"},
    }

    from sigma.exceptions import SigmaLevelError

    with pytest.raises(SigmaLevelError) as exc_info:
        converter.convert(str(rule_file), mock_schema_config)
    assert "'invalid_level' is not a valid Sigma rule level" in str(exc_info.value)


def test_duplicate_field_mappings(converter, tmp_path, mock_schema_config):
    """Test handling of duplicate field mappings."""
    rule_file = tmp_path / "rules" / "duplicate_mappings.yml"
    rule_file.write_text("""
    title: Duplicate Mappings Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            Image: value1
            process.name: value2  # Duplicate of Image after mapping
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["duplicate_mappings_rule"] = {
        "path": "duplicate_mappings.yml",
        "alert_fields": {"Image": "process_name", "process.name": "process_name"},
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_circular_field_mappings(converter, tmp_path, mock_schema_config):
    """Test handling of circular field mappings."""
    rule_file = tmp_path / "rules" / "circular_mappings.yml"
    rule_file.write_text("""
    title: Circular Mappings Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            field1: value1
            field2: value2
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["circular_mappings_rule"] = {
        "path": "circular_mappings.yml",
        "alert_fields": {"field1": "field2", "field2": "field1"},
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_null_none_values(converter, tmp_path, mock_schema_config):
    """Test handling of null and None values in fields."""
    rule_file = tmp_path / "rules" / "null_values.yml"
    rule_file.write_text("""
    title: Null Values Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            field1: null
            field2: None
            field3: ~
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["null_values_rule"] = {
        "path": "null_values.yml",
        "alert_fields": {
            "field1": "mapped_field1",
            "field2": "mapped_field2",
            "field3": "mapped_field3",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_long_field_names_values(converter, tmp_path, mock_schema_config):
    """Test handling of very long field names and values."""
    long_field_name = "a" * 255  # Max field name length
    long_value = "b" * 1000  # Long field value

    rule_file = tmp_path / "rules" / "long_fields.yml"
    rule_file.write_text(f"""
    title: Long Fields Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            {long_field_name}: '{long_value}'
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["long_fields_rule"] = {
        "path": "long_fields.yml",
        "alert_fields": {long_field_name: "mapped_long_field"},
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_nested_field_names(converter, tmp_path, mock_schema_config):
    """Test handling of nested field names with dots."""
    rule_file = tmp_path / "rules" / "nested_fields.yml"
    rule_file.write_text("""
    title: Nested Fields Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            'process.parent.child.name': value1
            'very.deeply.nested.field.name': value2
            'field.with.multiple.dots': value3
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["nested_fields_rule"] = {
        "path": "nested_fields.yml",
        "alert_fields": {
            "process.parent.child.name": "mapped_field1",
            "very.deeply.nested.field.name": "mapped_field2",
            "field.with.multiple.dots": "mapped_field3",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_mixed_case_fields(converter, tmp_path, mock_schema_config):
    """Test handling of mixed case field names."""
    rule_file = tmp_path / "rules" / "mixed_case.yml"
    rule_file.write_text("""
    title: Mixed Case Fields Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            ProcessName: value1
            processName: value2
            PROCESSNAME: value3
            process_name: value4
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["mixed_case_fields_rule"] = {
        "path": "mixed_case.yml",
        "alert_fields": {
            "ProcessName": "mapped_field1",
            "processName": "mapped_field2",
            "PROCESSNAME": "mapped_field3",
            "process_name": "mapped_field4",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_overlapping_field_mappings(converter, tmp_path, mock_schema_config):
    """Test handling of overlapping field mappings."""
    rule_file = tmp_path / "rules" / "overlapping.yml"
    rule_file.write_text("""
    title: Overlapping Fields Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            process.name: value1
            Image: value2
            ProcessName: value3
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["overlapping_fields_rule"] = {
        "path": "overlapping.yml",
        "alert_fields": {
            "process.name": "process_name",
            "Image": "process_name",
            "ProcessName": "process_name",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


def test_invalid_field_types(converter, tmp_path, mock_schema_config):
    """Test handling of invalid field types."""
    rule_file = tmp_path / "rules" / "invalid_types.yml"
    rule_file.write_text("""
    title: Invalid Types Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            numeric_field: abc  # Should be number
            bool_field: maybe  # Should be true/false
            list_field: not_a_list  # Should be list
        condition: selection
    level: medium
    """)

    mock_schema_config["sigma_rules"]["invalid_types_rule"] = {
        "path": "invalid_types.yml",
        "alert_fields": {
            "numeric_field": "mapped_numeric",
            "bool_field": "mapped_bool",
            "list_field": "mapped_list",
        },
    }

    converter.convert(str(rule_file), mock_schema_config)


@pytest.fixture
def nested_conditions_rule():
    return """
    title: Nested Conditions Rule
    logsource:
        product: windows
        service: security
    detection:
        selection1:
            field1: value1
        selection2:
            field2|contains:
                - value2
                - value3
        selection3:
            field3|endswith:
                - value4
                - value5
        filter1:
            field4|startswith: 
                - prefix1
                - prefix2
        filter2:
            field5|re: 'pattern.*'
        condition: (selection1 and selection2 or selection3) and not 1 of filter*
    level: medium
    """


def test_nested_conditions(
    converter, nested_conditions_rule, mock_schema_config, tmp_path, monkeypatch
):
    """Test handling of nested conditions in Sigma rules."""
    rule_file = tmp_path / "rules" / "nested_conditions.yml"
    rule_file.write_text(nested_conditions_rule)

    schema_csv_dir = tmp_path / "derived_schemas" / "test_schema"
    schema_csv_dir.mkdir(parents=True, exist_ok=True)
    schema_csv_file = schema_csv_dir / "test_schema.csv"
    schema_csv_file.write_text("""column,type,index_order,sigma_field_name
mapped_field1,string,1,field1
mapped_field2,string,2,field2
mapped_field3,string,3,field3
mapped_field4,string,4,field4
mapped_field5,string,5,field5""")

    mock_config = {
        "global_settings": {
            "meta_schema_paths": str(tmp_path / "meta_schemas"),
            "derived_schema_paths": str(tmp_path / "derived_schemas"),
        },
        "schemas": {
            "test_schema1": {
                "derived_schema_file_path": "test_schema/test_schema.csv",
                "sigma_rules": {
                    "nested_conditions_rule": {
                        "path": "nested_conditions.yml",
                        "alert_fields": {
                            "field1": "mapped_field1",
                            "field2": "mapped_field2",
                            "field3": "mapped_field3",
                            "field4": "mapped_field4",
                            "field5": "mapped_field5",
                        },
                    }
                },
            }
        },
    }

    monkeypatch.setattr(converter, "config", mock_config)

    converter.convert(str(rule_file), mock_config["schemas"]["test_schema1"])

    output_file = tmp_path / "output" / "nested_conditions.jinja2"
    assert output_file.exists()
    content = output_file.read_text()

    assert "field1" in content
    assert "field2" in content
    assert "field3" in content
    assert "field4" in content
    assert "field5" in content
    assert "AND" in content
    assert "OR" in content
    assert "NOT" in content
