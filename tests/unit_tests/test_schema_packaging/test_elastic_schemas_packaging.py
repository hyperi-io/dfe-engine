from typing import List
import pandas as pd
import pytest
import json
from pathlib import Path
from dfecli.utils.package_elastic_schemas import PackageElasticSchemas
from fixture_es_filebeat_template import logs_filebeat_template_subset
import logging
import shutil
from dfecli.dfe_schemabuilder.schema_util import SchemaUtils
from dfecli.dfe_config.config_loader import DFEConfigLoader

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture
def setup_directories(
    tmp_path, source_raw_beats_schemas, logs_filebeat_template_subset
):
    """
    Fixture for setting up the directories for the test.

    Args:
        tmp_path (Path): A temporary directory path for the test.
        source_raw_beats_schemas (str): Path to the raw beat schemas.
        logs_filebeat_template_subset (dict): Subset of the logs filebeat template for testing.

    Yields:
        Tuple[Path, Path]: Source and target directory paths.
    """
    source_directory = Path(source_raw_beats_schemas)
    target_directory = tmp_path / "target_schemas"
    target_directory.mkdir(parents=True, exist_ok=True)

    versioned_directory = source_directory / "v001_000_002"
    versioned_directory.mkdir(parents=True, exist_ok=True)
    subset_path = versioned_directory / "logs_beats_filebeat.json"
    with open(subset_path, "w") as f:
        json.dump(logs_filebeat_template_subset, f)

    yield source_directory, target_directory

    shutil.rmtree(source_directory, ignore_errors=True)
    shutil.rmtree(target_directory, ignore_errors=True)


@pytest.fixture
def source_raw_beats_schemas():
    """
    Fixture to provide the source path for raw beats schemas.

    Returns:
        str: Path to the source raw beats schemas.
    """
    return "tests/resources/test_filebeats_raw_schemas/logs_beats_filebeat"


def test_csv_structure(
    setup_directories,
    dfe_package,
    common_template_package: str,
    common_resource_version: str,
):
    """
    Test that the generated CSV structure matches expected columns and has no null values in critical columns.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )

    package_elastic_schemas.execute()

    output_csv_path = (
        target_directory
        / "logs_beats_filebeat"
        / "v001_000_002"
        / "logs_beats_filebeat.csv"
    )
    assert output_csv_path.exists(), f"{output_csv_path} does not exist"

    df = pd.read_csv(output_csv_path)
    expected_columns = [
        "column",
        "type",
        "default",
        "index_order",
        "index_type",
        "os_order",
        "comment",
    ]
    assert list(df.columns) == expected_columns, (
        f"Expected columns: {expected_columns}, but got: {list(df.columns)}"
    )
    assert df[["column", "type"]].notnull().all().all(), (
        "Columns 'column' and 'type' should not contain null values"
    )


def test_csv_data_types(
    setup_directories,
    dfe_package,
    common_resource_version: str,
    common_template_package: str,
):
    """
    Test that the data types of each column in the CSV match the expected types.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )

    package_elastic_schemas.execute()

    output_csv_path = (
        target_directory
        / "logs_beats_filebeat"
        / "v001_000_002"
        / "logs_beats_filebeat.csv"
    )
    df = pd.read_csv(output_csv_path)

    expected_types = {
        "column": str,
        "type": str,
        "index_order": float,
        "index_type": float,
        "os_order": float,
    }

    for column, expected_type in expected_types.items():
        assert pd.api.types.is_dtype_equal(
            df[column].dtype, pd.Series(dtype=expected_type).dtype
        ), f"Column '{column}' has type {df[column].dtype}, expected {expected_type}"


@pytest.mark.parametrize(
    "expected_fields",
    [
        [
            "dns.response_code",
            "container.network.ingress.bytes",
            "dns.question.class",
            "container.disk.read.bytes",
            "container.image.name",
            "container.image.tag",
            "container.cpu.usage",
            "dns.answers.name",
            "container.name",
            "dns.id",
            "dns.op_code",
            "container.labels",
            "container.disk.write.bytes",
            "dns.question.subdomain",
            "dns.type",
            "container.network.egress.bytes",
            "dns.question.name",
            "dns.answers.data",
            "awscloudwatch.log_group",
            "dns.question.type",
            "dns.resolved_ip",
            "awscloudwatch.ingestion_time",
            "dns.header_flags",
            "dns.answers.ttl",
            "dns.question.registered_domain",
            "container.runtime",
            "container.memory.usage",
            "container.id",
            "awscloudwatch.log_stream",
            "dns.question.top_level_domain",
            "dns.answers.type",
            "metadata",
            "dns.answers.class",
            "dns.answers",
        ]
    ],
)
def test_expected_schema_fields(
    setup_directories,
    dfe_package,
    expected_fields: List[str],
    common_resource_version: str,
    common_template_package: str,
):
    """
    Test that specific expected fields are present in the CSV.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )

    package_elastic_schemas.execute()

    output_csv_path = (
        target_directory
        / "logs_beats_filebeat"
        / "v001_000_002"
        / "logs_beats_filebeat.csv"
    )
    df = pd.read_csv(output_csv_path)

    actual_fields = list(df["column"])

    missing_fields = [field for field in expected_fields if field not in actual_fields]
    extra_fields = [field for field in actual_fields if field not in expected_fields]

    assert not missing_fields, (
        f"The following fields are missing in the schema csv: {missing_fields}"
    )
    assert not extra_fields, (
        f"The following unexpected fields are present in the schema csv: {extra_fields}"
    )


def test_schema_integrity(
    setup_directories,
    dfe_package,
    common_resource_version: str,
    common_template_package: str,
):
    """
    Test the integrity of the schema, ensuring no duplicate columns and consistent type mapping.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )

    package_elastic_schemas.execute()

    output_csv_path = (
        target_directory
        / "logs_beats_filebeat"
        / "v001_000_002"
        / "logs_beats_filebeat.csv"
    )
    df = pd.read_csv(output_csv_path)

    assert df["column"].is_unique, "CSV contains duplicate columns"

    for column in df["column"].unique():
        types = df[df["column"] == column]["type"].unique()
        assert len(types) == 1, f"Column '{column}' maps to multiple types: {types}"


def test_logging_for_errors(
    setup_directories,
    dfe_package,
    caplog,
    common_resource_version: str,
    common_template_package: str,
):
    """
    Test that no errors were logged during the CSV creation process.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )

    package_elastic_schemas.execute()

    assert not any("ERROR" in message for message in caplog.text.splitlines()), (
        "Errors were logged during CSV creation. Please check the logs"
    )


@pytest.mark.parametrize(
    "expected_fields",
    [
        [
            "dns.response_code",
            "container.network.ingress.bytes",
            "dns.question.class",
            "container.disk.read.bytes",
            "container.image.name",
            "container.image.tag",
            "container.cpu.usage",
            "dns.answers.name",
            "container.name",
            "dns.id",
            "dns.op_code",
            "container.labels",
            "container.disk.write.bytes",
            "dns.question.subdomain",
            "dns.type",
            "container.network.egress.bytes",
            "dns.question.name",
            "dns.answers.data",
            "awscloudwatch.log_group",
            "dns.question.type",
            "dns.resolved_ip",
            "awscloudwatch.ingestion_time",
            "dns.header_flags",
            "dns.answers.ttl",
            "dns.question.registered_domain",
            "container.runtime",
            "container.memory.usage",
            "container.id",
            "awscloudwatch.log_stream",
            "dns.question.top_level_domain",
            "dns.answers.type",
            "metadata",
            "dns.answers.class",
        ]
    ],
)
def test_component_template_contains_all_cm_components(
    setup_directories,
    expected_filebeat_cm_template_json,
    expected_fields,
    dfe_package,
    common_resource_version: str,
    common_template_package: str,
):
    """
    Test that the component template contains all expected fields and is consistent.
    """
    source_directory, target_directory = setup_directories
    dfe_config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package)

    package_elastic_schemas = PackageElasticSchemas(
        dfe_config=dfe_config,
        source_directory=str(source_directory),
        target_directory=str(target_directory),
        common_template_package=common_template_package,
        common_resource_version=common_resource_version,
        logger=logger,
    )
    package_elastic_schemas.execute()

    cm_template_path = (
        target_directory
        / "logs_beats_filebeat"
        / "v001_000_002"
        / "logs_beats_filebeat_cm.json"
    )
    assert cm_template_path.exists(), f"{cm_template_path} does not exist"

    with open(cm_template_path, "r") as f:
        cm_template = json.load(f)

    expected_cm_template = json.loads(expected_filebeat_cm_template_json)

    cm_properties = cm_template["template"]["mappings"]["properties"]
    cm_fields = SchemaUtils.flatten_properties(cm_properties)

    missing_fields = [field for field in expected_fields if field not in cm_fields]
    extra_fields = [field for field in cm_fields if field not in expected_fields]

    assert not missing_fields, (
        f"The following fields are missing in the CM template: {missing_fields}"
    )
    assert not extra_fields, (
        f"The following unexpected fields are present in the CM template: {extra_fields}"
    )

    assert cm_template["template"].get("date_detection") == expected_cm_template[
        "template"
    ].get("date_detection"), "Date detection settings mismatch"
