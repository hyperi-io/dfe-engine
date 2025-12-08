import pytest
import os

# Skip all tests in this module - CLI tests belong to dfe-cli, not dfe-engine library
pytestmark = pytest.mark.skip(reason="CLI tests belong to dfe-cli package, not dfe-engine library")

# Placeholder imports for skipped tests - actual implementations are in dfe-cli
CliRunner = None
cli = None


@pytest.fixture
def runner():
    """Provides a Click runner to invoke CLI commands."""
    return CliRunner()


def test_build_schema(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test building schema without requiring ClickHouse."""
    with capsys.disabled():
        result = runner.invoke(
            cli,
            [
                "build-schemas",
                "--dfe_package_file_path",
                dfe_package,
                "--schema_directory",
                dfe_config_fixtures["global_settings"]["derived_schema_paths"],
                "--schema_filter_list",
                "logs_alerts",
                "--no_cluster_declarations_needed",
                str(dfe_config_fixtures["build_schemas"]["no_cluster_declarations_needed"]),
                "--use_replicated_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_replicated_merge_tree"]),
                "--use_shared_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_shared_merge_tree"]),
                "--use_subsampling_feature",
                str(dfe_config_fixtures["global_settings"].get("use_subsampling_feature", False)),
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
            ],
            catch_exceptions=False,
        )

    print(f"Command output:\n{result.output}")
    print(f"Command exception info:\n{result.exc_info}")

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"
    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_alerts/logs_alerts.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"


def test_build_schema_wildchar(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test building schema with wildcard pattern without requiring ClickHouse."""
    with capsys.disabled():
        result = runner.invoke(
            cli,
            [
                "build-schemas",
                "--dfe_package_file_path",
                dfe_package,
                "--schema_directory",
                dfe_config_fixtures["global_settings"]["derived_schema_paths"],
                "--schema_filter_wildchar",
                "logs_alerts*",
                "--no_cluster_declarations_needed",
                str(dfe_config_fixtures["build_schemas"]["no_cluster_declarations_needed"]),
                "--use_replicated_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_replicated_merge_tree"]),
                "--use_shared_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_shared_merge_tree"]),
                "--use_subsampling_feature",
                str(dfe_config_fixtures["global_settings"].get("use_subsampling_feature", False)),
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
            ],
            catch_exceptions=False,
        )

    print(f"Command output:\n{result.output}")
    print(f"Command exception info:\n{result.exc_info}")

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"
    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_alerts/logs_alerts.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"


def test_build_schema_all(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test building all schemas without requiring ClickHouse."""
    with capsys.disabled():
        result = runner.invoke(
            cli,
            [
                "build-schemas",
                "--dfe_package_file_path",
                dfe_package,
                "--schema_directory",
                dfe_config_fixtures["global_settings"]["derived_schema_paths"],
                "--no_cluster_declarations_needed",
                str(dfe_config_fixtures["build_schemas"]["no_cluster_declarations_needed"]),
                "--use_replicated_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_replicated_merge_tree"]),
                "--use_shared_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_shared_merge_tree"]),
                "--use_subsampling_feature",
                str(dfe_config_fixtures["global_settings"].get("use_subsampling_feature", False)),
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
                "--all",
            ],
            catch_exceptions=False,
        )

    print(f"Command output:\n{result.output}")
    print(f"Command exception info:\n{result.exc_info}")

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"
    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_alerts/logs_alerts.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"


def test_build_schema_only_beats(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test building beats-only schemas without requiring ClickHouse."""
    with capsys.disabled():
        result = runner.invoke(
            cli,
            [
                "build-schemas",
                "--dfe_package_file_path",
                dfe_package,
                "--schema_directory",
                dfe_config_fixtures["global_settings"]["derived_schema_paths"],
                "--no_cluster_declarations_needed",
                str(dfe_config_fixtures["build_schemas"]["no_cluster_declarations_needed"]),
                "--use_replicated_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_replicated_merge_tree"]),
                "--use_shared_merge_tree",
                str(dfe_config_fixtures["build_schemas"]["use_shared_merge_tree"]),
                "--use_subsampling_feature",
                str(dfe_config_fixtures["global_settings"].get("use_subsampling_feature", False)),
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
                "--only-beats",
            ],
            catch_exceptions=False,
        )

    print(f"Command output:\n{result.output}")
    print(f"Command exception info:\n{result.exc_info}")

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"
    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_beats_winlogbeat/logs_beats_winlogbeat.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"


def test_plan_schemas(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test planning schemas without requiring ClickHouse."""
    with capsys.disabled():
        runner.invoke(
            cli,
            [
                "plan-schemas",
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
                "--schema_filter_list",
                "logs_alerts",
                "--org_filter_list",
                "org321",
            ],
            catch_exceptions=True,
        )

    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_alerts/logs_alerts.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"


def test_plan_schemas_wildchar(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, dfe_package, setup_paths
):
    """Test planning schemas with wildcard pattern without requiring ClickHouse."""
    with capsys.disabled():
        runner.invoke(
            cli,
            [
                "plan-schemas",
                "--log_path",
                setup_paths,
                "--target",
                dfe_config_fixtures["global_settings"]["default_target"],
                "--target_file_path",
                dfe_config_fixtures["global_settings"]["target_path"],
                "--schema_filter_wildchar",
                "logs_alerts*",
                "--org_filter_list",
                "org321",
            ],
            catch_exceptions=True,
        )

    output_dir = dfe_config_fixtures["global_settings"]["schema_output_path"]
    expected_sql_files = [
        "logs_alerts/logs_alerts.sql",
    ]
    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        assert os.path.exists(file_path), f"Expected file {file_path} does not exist"

    for file_name in expected_sql_files:
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "r") as f:
            content = f.read().strip()
            assert content, f"Output file {file_path} is empty"
