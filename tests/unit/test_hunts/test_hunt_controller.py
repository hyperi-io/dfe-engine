import pytest
from dfe_engine.hunts.controller import HuntController


def test_print_hunt_parameters(dfe_config_fixtures, setup_paths, dfe_package):
    try:
        HuntController.print_hunt_parameters(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths[0],
            args_hunt_log_path=setup_paths[1],
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
    except Exception as e:
        pytest.fail(f"Print Hunt Parameters failed: {e}")
