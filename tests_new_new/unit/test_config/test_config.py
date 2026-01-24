import pytest

from dfe_engine.config.config import Config
from dfe_engine.config.custom_exceptions import ConfigFileNotFoundError, ConfigMissingKeyError
from pathlib import Path


@pytest.fixture
def init_config_obj(tmp_path):
    config_file_path = Path(tmp_path / "config" / "config.yaml")

    yield Config(
        config_file_path = str(config_file_path)
    )


@pytest.fixture
def init_config_file(init_config_obj):
    result = init_config_obj.config_file_create()
    assert(result == init_config_obj.config_file_path)


@pytest.fixture
def init_config_file_with_data(init_config_obj, init_config_file):
    config_data = {
        "test_config_1": "test_value_1",
        "test_config_path": {
            "test_nested_key": "test_nested_value"
        }
    }
    init_config_obj.config_update(
        new_data = config_data
    )
    yield config_data


def test_config_file_not_exists(init_config_obj):
    assert(not init_config_obj.config_file_exists())


def test_config_file_exists(init_config_obj, init_config_file):
    assert(init_config_obj.config_file_exists())


def test_config_update_initial(init_config_obj, init_config_file_with_data):
    config_key = "test_config_1"
    expected_value = "test_value_1"

    assert(init_config_obj.config_get_key(
        config_key = config_key
    ) == expected_value)


def test_config_update_with_existing_data(init_config_obj, init_config_file_with_data):
    key = "test_config_2"
    value = "test_value_2"
    new_config_data = {
        key: value
    }
    expected_diff = {
        "values_changed": {
            "root": {
                "new_value": new_config_data,
                "old_value": init_config_file_with_data,
            },
        },
    }

    diff = init_config_obj.config_update(new_config_data)

    assert(diff == expected_diff)
    assert(init_config_obj.config_get_key(key) == value)


def test_config_update_with_root_key(init_config_obj, init_config_file_with_data):
    root_config_key = "test_config_path"
    key = "test_nested_key_2"
    value = "test_nested_value_2"
    new_config_data = {
        key: value
    }
    expected_diff = {
        "values_changed": {
            f"root['{root_config_key}']": {
                "new_value": new_config_data,
                "old_value": init_config_file_with_data[root_config_key],
            },
        },
    }

    diff = init_config_obj.config_update(
        new_data = new_config_data,
        root_config_key = root_config_key
    )

    assert(diff == expected_diff)
    assert(init_config_obj.config_get_key(f"{root_config_key}.{key}") == value)


def test_config_update_with_complex_root_key(init_config_obj, init_config_file_with_data):
    root_config_key = "test_config_path.test_complex"
    key = "test_config_2"
    value = "test_value_2"
    new_config_data = {
        key: value
    }
    expected_diff = {
        "dictionary_item_added": [
            f"root{"".join(f"['{config_key}']" for config_key in root_config_key.split("."))}"
        ]
    }

    diff = init_config_obj.config_update(
        new_data = new_config_data,
        root_config_key = root_config_key
    )

    assert(diff == expected_diff)
    assert(init_config_obj.config_get_key(f"{root_config_key}.{key}") == value)


def test_config_update_with_missing_complex_root_key(init_config_obj, init_config_file_with_data):
    root_config_key = "test_config_path.complex_missing_key.test_complex"
    key = "test_config_2"
    value = "test_value_2"
    new_config_data = {
        key: value
    }
    expected_diff = {
        "dictionary_item_added": [
            f"root{"".join(f"['{config_key}']" for config_key in root_config_key.split(".")[:-1])}"
        ]
    }

    diff = init_config_obj.config_update(
        new_data = new_config_data,
        root_config_key = root_config_key
    )

    assert(diff == expected_diff)
    assert(init_config_obj.config_get_key(f"{root_config_key}.{key}") == value)


def test_config_has_key(init_config_obj, init_config_file_with_data):
    key = "test_config_1"
    expected_value = True

    assert(init_config_obj.config_has_key(key) == expected_value)


def test_config_has_missing_key(init_config_obj, init_config_file_with_data):
    key = "test_config_missing_1"
    expected_value = False

    assert(init_config_obj.config_has_key(key) == expected_value)


def test_config_has_complex_key(init_config_obj, init_config_file_with_data):
    key = "test_config_path.test_nested_key"
    expected_value = True

    assert(init_config_obj.config_has_key(key) == expected_value)


def test_config_has_missing_complex_key(init_config_obj, init_config_file_with_data):
    key = "test_missing_root_key.test_missing_nested_key"
    expected_value = False

    assert(init_config_obj.config_has_key(key) == expected_value)


def test_config_get_key(init_config_obj, init_config_file_with_data):
    key_1 = "test_config_1"
    expected_value_1 = "test_value_1"
    key_2 = "test_config_path.test_nested_key"
    expected_value_2 = "test_nested_value"

    assert(init_config_obj.config_get_key(key_1) == expected_value_1)
    
    assert(init_config_obj.config_get_key(key_2) == expected_value_2)


def test_config_update_missing_file(init_config_obj):
    new_config_data = {
        "test_config_2": "test_value_2"
    }

    with pytest.raises(ConfigFileNotFoundError) as exc_info:
        init_config_obj.config_update(new_config_data)
    
    assert(str(exc_info.value) == f"The config file '{init_config_obj.config_file_path}' could not be found.")


def test_config_has_key_missing_file(init_config_obj):
    key = "test_config_1"

    with pytest.raises(ConfigFileNotFoundError) as exc_info:
        init_config_obj.config_has_key(key)
    
    assert(str(exc_info.value) == f"The config file '{init_config_obj.config_file_path}' could not be found.")


def test_config_get_key_missing_file(init_config_obj):
    key = "test_config_1"

    with pytest.raises(ConfigFileNotFoundError) as exc_info:
        init_config_obj.config_get_key(key)
    
    assert(str(exc_info.value) == f"The config file '{init_config_obj.config_file_path}' could not be found.")


def test_config_get_non_existant_key(init_config_obj, init_config_file_with_data):
    key = "test_non_existant_key"

    with pytest.raises(ConfigMissingKeyError) as exc_info:
        init_config_obj.config_get_key(key)
    
    assert(str(exc_info.value) == f"The key '{key}' could not be found in the config file '{init_config_obj.config_file_path}'.")