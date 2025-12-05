import time
import logging
from typing import Dict
from dfecli.dfe_schemabuilder.schema_builder import SchemaBuilder
from pathlib import Path
import line_profiler
import pytest

PROFILE = line_profiler.LineProfiler()
logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def profiler():
    PROFILE.enable_by_count()
    yield
    PROFILE.disable_by_count()
    PROFILE.print_stats()


@pytest.mark.usefixtures("profiler")
def test_build_schema(dfe_config_fixtures: Dict):
    config_data = dfe_config_fixtures

    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
    )
    start_time = time.time()
    builder.build()
    end_time = time.time()

    duration = end_time - start_time
    logger.info(f"test_build_schema executed in {duration:.2f} seconds")
