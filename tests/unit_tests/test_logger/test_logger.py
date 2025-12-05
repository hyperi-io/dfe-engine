import os
from datetime import datetime
from dfecli.dfe_logger.dfe_logger import DFELog


def test_get_root_logger_default_suffix(tmp_path):
    """Test default timestamp suffix behavior"""
    DFELog.get_root_logger(logging_directory=str(tmp_path), log_file_prefix="test")

    files = os.listdir(tmp_path)
    filename = files[0]
    assert filename.startswith("test-")
    assert filename.endswith(".log")


def test_get_root_logger_custom_date_suffix(tmp_path):
    """Test custom date format suffix"""
    custom_suffix = "-%Y-%m-%d"
    DFELog.get_root_logger(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix=custom_suffix,
    )

    expected_date = datetime.now().strftime(custom_suffix)
    expected_filename = f"test{expected_date}.log"

    files = os.listdir(tmp_path)
    assert files[0] == expected_filename


def test_get_root_logger_static_suffix(tmp_path):
    """Test static string suffix"""
    DFELog.get_root_logger(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix="-static",
    )

    files = os.listdir(tmp_path)
    assert files[0] == "test-static.log"


def test_logger_instance_default_suffix(tmp_path):
    """Test default timestamp suffix in class instance"""
    DFELog(logging_directory=str(tmp_path), log_file_prefix="test")

    files = os.listdir(tmp_path)
    filename = files[0]
    assert filename.startswith("test-")
    assert filename.endswith(".log")


def test_logger_instance_custom_date_suffix(tmp_path):
    """Test custom date format suffix in class instance"""
    custom_suffix = "-%Y-%m-%d"
    DFELog(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix=custom_suffix,
    )

    expected_date = datetime.now().strftime(custom_suffix)
    expected_filename = f"test{expected_date}.log"

    files = os.listdir(tmp_path)
    assert files[0] == expected_filename


def test_logger_instance_static_suffix(tmp_path):
    """Test static string suffix in class instance"""
    DFELog(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix="-static",
    )

    files = os.listdir(tmp_path)
    assert files[0] == "test-static.log"


def test_logger_file_reuse(tmp_path):
    """Test that we can reference and reuse an existing log file"""
    logger1 = DFELog.get_root_logger(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix="-reuse",
    )
    logger1.info("First log message")
    logger2 = DFELog.get_root_logger(
        logging_directory=str(tmp_path),
        log_file_prefix="test",
        log_file_suffix="-reuse",
    )
    logger2.info("Second log message")
    files = os.listdir(tmp_path)
    assert files[0] == "test-reuse.log"

    with open(os.path.join(tmp_path, "test-reuse.log")) as f:
        content = f.read()
        assert "First log message" in content
        assert "Second log message" in content
