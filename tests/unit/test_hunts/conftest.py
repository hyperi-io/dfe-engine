import os
import shutil
import stat

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.settings import reset_settings
from tests.support.recording_metrics import RecordingManager

# Reset settings and ClickHouseManager to pick up dotenv values
reset_settings()
ClickHouseManager.reset_instance()


@pytest.fixture
def target_config_data():
    """Mock target config data for unit tests."""
    return {"ch_host": "localhost", "ch_port": 8123}


@pytest.fixture(scope="session", autouse=True)
def cleanup(tmp_path_factory):
    """Fixture to clean up any residual files or directories created during tests."""
    temp_dir = tmp_path_factory.mktemp("test_dfe_data_engine")
    yield
    if os.path.exists(temp_dir):

        def on_rm_error(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(temp_dir, onexc=on_rm_error)


@pytest.fixture
def manager() -> RecordingManager:
    """A recording metrics manager to build a real ``HuntRunnerMetrics`` over."""
    return RecordingManager()
