import tempfile
import pytest
import textwrap
import yaml
from pathlib import Path
from datetime import datetime, timezone, timedelta
from jinja2 import Environment, FileSystemLoader
from dfe_engine.hunts.hunts.hunts import Hunt
from dfe_engine.config.config_loader import DFEConfigLoader


def load_config(file_path: Path) -> dict:
    """Load YAML configuration from a file."""
    with open(file_path, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def template_dir() -> Path:
    """Fixture to create Jinja2 templates for rules."""
    tmpdirname = tempfile.mkdtemp()
    template_path = Path(tmpdirname)

    rule_template_path = template_path / "win_account_a_common_activities.jinja2"

    rule_template_path.write_text(
        textwrap.dedent("""
        INSERT INTO {{ org_id }}.{{ target_table_name }} (org_id, source_table)
        SELECT '{{ org_id }}', '{{ source_table_name }}' FROM {{ org_id }}.{{ source_table_name }} WHERE {timestamp_condition} AND (event_id IN ('4624')) 
    """)
        .replace("\n", " ")
        .replace("\t", "")
        .strip()
    )

    yield template_path

    # Cleanup
    for file in template_path.glob("*"):
        file.unlink()
    template_path.rmdir()


@pytest.fixture
def file_and_folder_paths() -> dict:
    """Fixture to set up and return file and folder paths for tests."""
    default_test_params = {
        "customer": "detectionlab",
        "source_table_name": "logs_nxlog_windows",
        "target_table_name": "logs_alerts",
    }
    return {"default_test_params": default_test_params}


@pytest.fixture
def setup_paths(tmp_path) -> dict:
    """Fixture to set up necessary paths for hunts and ensure cleanup."""
    dfe_root_log_path = tmp_path / "logs_path"
    dfe_root_log_path.mkdir(parents=True, exist_ok=True)

    hunt_log_path = tmp_path / "hunt_path"
    hunt_log_path.mkdir(parents=True, exist_ok=True)

    hunt_checkpoint_path = tmp_path / ".dfe_hunt_checkpoint"
    hunt_checkpoint_path.mkdir(parents=True, exist_ok=True)

    return {
        "tmp/logs": dfe_root_log_path,
        "hunt_log_path": hunt_log_path,
        "hunt_checkpoint_path": hunt_checkpoint_path,
    }


@pytest.mark.parametrize(
    "rule_name, org_id, target_table_name, source_table_name, checkpoint_timestamp_field, filter_clause, expected_query",
    [
        (
            "win_account_a_common_activities",
            "detectionlab",
            "logs_alerts",
            "logs_nxlog_windows",
            "timestamp",
            "",
            textwrap.dedent("""
                INSERT INTO detectionlab.logs_alerts (org_id, source_table)
                SELECT 'detectionlab', 'logs_nxlog_windows' FROM detectionlab.logs_nxlog_windows WHERE {timestamp_condition} AND (event_id IN ('4624'))
            """)
            .replace("\n", " ")
            .replace("\t", "")
            .strip(),
        )
    ],
)
def test_basic_hunt_alerts(
    rule_name: str,
    org_id: str,
    target_table_name: str,
    source_table_name: str,
    checkpoint_timestamp_field: str,
    filter_clause: str,
    expected_query: str,
    file_and_folder_paths: dict,
    setup_paths: dict,
    template_dir: Path,
    dfe_config_fixtures,
) -> None:
    file_and_folder_paths["default_test_params"]
    env = Environment(loader=FileSystemLoader(template_dir))
    target_name = dfe_config_fixtures["global_settings"]["default_target"]
    targets_file_path = dfe_config_fixtures["global_settings"]["target_path"]
    target_config_data = DFEConfigLoader.read_target_config(target_name, targets_file_path)

    cronlist = ["* * * * *", "*/2 * * * *"]

    for cron in cronlist:
        hunt = Hunt(
            name="Windows SMD Detections",
            cron=cron,
            customer=org_id,
            log_buffer=60,
            thread_id="11111",
            rules=[{"rule_name": rule_name, "target_table_name": target_table_name}],
            global_source_table_name=source_table_name,
            global_target_table_name=target_table_name,
            checkpoint_timestamp_field=checkpoint_timestamp_field,
            customer_filters={
                "detectionlab": {"rules": [{"name": rule_name, "filter_clause": filter_clause}]}
            },
            hunt_log_path=str(setup_paths["hunt_log_path"]),
            target_config_data=target_config_data,
            checkpoint_destination="clickhouse",
            hunt_checkpoint_path=str(setup_paths["hunt_checkpoint_path"]),
        )

        hunt.convert_yaml_to_sql(env, org_id=org_id, customer_filters=hunt.customer_filters)

        assert len(hunt.queries_by_customer) == 1, (
            f"Expected 1 query, found {len(hunt.queries_by_customer)}"
        )
        assert expected_query == hunt.queries_by_customer["detectionlab"][0], (
            f"-->Expected query:{expected_query}-->Generated query:{hunt.queries_by_customer['detectionlab'][0]}"
        )
        scheduled_start_time_w_buffer = datetime.now(timezone.utc) - timedelta(seconds=60)
        hunt.execute_hunt(customer=org_id, scheduled_start_time=scheduled_start_time_w_buffer)
