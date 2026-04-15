import pytest
from sigma.collection import SigmaCollection

from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend
from dfe_engine.sigma.sigma_pipelines import SigmaPipeline


@pytest.fixture
def backend():
    field_mappings = {
        "EventID": "event.code",
        "Image": "process.name",
        "CommandLine": "process.command_line",
        "ParentImage": "process.parent.name",
        "ParentCommandLine": "process.parent.command_line",
        "ProcessId": "process.pid",
        "ParentProcessId": "process.parent.pid",
        "ProcessHash": "process.hash.md5",
        "Channel": "log.channel",
    }

    pipeline_config = SigmaPipeline(field_mappings=field_mappings)
    pipeline = pipeline_config.create_pipeline()
    return SqlBackend(pipeline)


@pytest.fixture
def base_rule_yaml():
    return """
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                EventID: 1234
                Image: test_image.exe
            condition: selection
    """


@pytest.mark.parametrize(
    ("rule_yaml", "expected_query"),
    [
        (
            """
        title: Test Basic Field Mappings
        {base_rule_yaml}
        """,
            "(event.code = '1234' AND process.name = 'test_image.exe')",
        ),
        (
            """
        title: Test Process Creation with Parent
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                Image: cmd.exe
                CommandLine: 'whoami'
                ParentImage: explorer.exe
                ParentCommandLine: ''
            condition: selection
        """,
            "(process.name = 'cmd.exe' AND process.command_line = 'whoami' AND process.parent.name = 'explorer.exe' AND process.parent.command_line = '')",
        ),
        (
            """
        title: Test Process with Additional Fields
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                ProcessId: 1234
                ProcessHash: 'd41d8cd98f00b204e9800998ecf8427e'
            condition: selection
        """,
            "(process.pid = '1234' AND process.hash.md5 = 'd41d8cd98f00b204e9800998ecf8427e')",
        ),
        (
            """
            title: Test Complex Conditions
            logsource:
                product: logs_nxlog_windows
                category: ps_classic_start
            detection:
                selection_proc:
                    Image: powershell.exe
                    CommandLine|contains:
                        - '-enc'
                        - '-encodedcommand'
                selection_parent:
                    ParentImage|endswith:
                        - cmd.exe
                        - powershell.exe
                condition: selection_proc and selection_parent
            """,
            "((process.name = 'powershell.exe' AND (process.command_line ILIKE '%-enc%' OR process.command_line ILIKE '%-encodedcommand%')) AND (process.parent.name ILIKE '%cmd.exe' OR process.parent.name ILIKE '%powershell.exe'))",
        ),
        (
            """
        title: Test Alert Metadata
        description: Test rule with MITRE ATT&CK tags
        tags:
            - attack.execution
            - attack.t1059.001
            - attack.defense_evasion
            - attack.t1140
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                EventID: 1234
            condition: selection
        level: high
        """,
            "event.code = '1234'",
        ),
    ],
)
def test_pipeline_field_mappings(backend, base_rule_yaml, rule_yaml, expected_query):
    """Test field mapping inheritance and alert metadata through the pipeline."""
    full_rule_yaml = rule_yaml.format(base_rule_yaml=base_rule_yaml)
    sigma_collection = SigmaCollection.from_yaml(full_rule_yaml)

    converted_queries = backend.convert(sigma_collection)
    assert len(converted_queries) == 1
    assert converted_queries[0] == expected_query

    if "tags" in rule_yaml:
        full_alert = backend.convert(sigma_collection, output_format="full_alert")
        assert "alert_framework" in full_alert
        assert "'MITRE ATT&CK'" in full_alert
        assert "'execution, defense_evasion'" in full_alert
        assert "'T1059.001, T1140'" in full_alert


def test_pipeline_alert_metadata(backend):
    """Test alert metadata handling in the pipeline."""
    rule_yaml = """
        title: Test Alert Metadata
        description: Test alert metadata handling
        tags:
            - attack.execution
            - attack.t1059.001
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                EventID: 1234
            condition: selection
        level: high
    """

    sigma_collection = SigmaCollection.from_yaml(rule_yaml)
    full_alert = backend.convert(sigma_collection, output_format="full_alert")

    assert "alert_schedule, " in full_alert
    assert "'smd'" in full_alert
    assert "alert_framework, " in full_alert
    assert "'MITRE ATT&CK'" in full_alert
    assert "alert_type, " in full_alert
    assert "'scheduled alert'" in full_alert
    assert "alert_severity, " in full_alert
    assert "'high'" in full_alert
    assert "alert_triage_score, " in full_alert
    assert "40" in full_alert
    assert "'execution'" in full_alert
    assert "'T1059.001'" in full_alert


def test_pipeline_field_mapping_precedence(backend):
    """Test field mapping precedence in the pipeline."""
    rule_yaml = """
        title: Test Field Mapping Precedence
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                Image: test.exe
                ParentImage: cmd.exe
                ProcessId: 1234
                ProcessHash: 'abc123'
            condition: selection
    """

    sigma_collection = SigmaCollection.from_yaml(rule_yaml)
    converted_query = backend.convert(sigma_collection)

    assert len(converted_query) == 1
    query = converted_query[0]
    assert "process.name = 'test.exe'" in query
    assert "process.parent.name = 'cmd.exe'" in query
    assert "process.pid = '1234'" in query  # Updated to expect quoted numeric value
    assert "process.hash.md5 = 'abc123'" in query


def test_dynamic_metadata_handling(backend):
    """Test handling of dynamic metadata in SQL generation."""
    rule_yaml = """
    title: Dynamic Metadata Rule
    logsource:
        product: windows
        service: security
    detection:
        selection:
            field1: value1
        condition: selection
    level: medium
    """

    backend.dynamic_metadata = {
        "dynamic_field1": "dynamic_value1",
        "dynamic_field2": "dynamic_value2",
    }

    sigma_collection = SigmaCollection.from_yaml(rule_yaml)
    converted_query = backend.convert(sigma_collection, output_format="full_alert")
    assert "dynamic_field1" in converted_query
    assert "dynamic_field2" in converted_query
