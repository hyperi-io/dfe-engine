#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_setup_marker.py
#  Purpose:      The marker that first-run setup finished
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from dfe_engine.auth import setup_marker


def test_the_marker_is_recorded_until_cleared(tmp_path):
    config_dir = str(tmp_path)
    assert setup_marker.is_recorded(config_dir=config_dir) is False

    setup_marker.record(config_dir=config_dir)
    setup_marker.record(config_dir=config_dir)
    assert setup_marker.is_recorded(config_dir=config_dir) is True

    setup_marker.clear(config_dir=config_dir)
    setup_marker.clear(config_dir=config_dir)
    assert setup_marker.is_recorded(config_dir=config_dir) is False


def test_no_config_directory_means_no_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    setup_marker.record(config_dir="")
    setup_marker.clear(config_dir="")

    assert setup_marker.is_recorded(config_dir="") is False
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_is_not_fatal(tmp_path):
    not_a_directory = tmp_path / "file"
    not_a_directory.touch()

    setup_marker.record(config_dir=str(not_a_directory))

    assert setup_marker.is_recorded(config_dir=str(not_a_directory)) is False
