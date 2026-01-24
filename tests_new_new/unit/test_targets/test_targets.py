import pytest
import yaml

from dfe_engine.targets.custom_exceptions import TargetKeyNotFoundError, TargetNotFoundError, TargetsFileNotFoundError
from dfe_engine.targets.targets import Targets
from pathlib import Path


@pytest.fixture
def init_targets_obj(tmp_path):
    targets_file_path = Path(tmp_path / "targets.yaml")

    yield Targets(
        targets_file_path = targets_file_path
    )


@pytest.fixture
def init_targets_file(init_targets_obj):
    result = init_targets_obj.targets_file_create()
    assert(result == init_targets_obj.targets_file_path)


@pytest.fixture
def init_targets_file_with_target(init_targets_obj, init_targets_file):
    target_name = "test_target_1"
    target_data = {
        "test_key_1": "test_value_1"
    }
    init_targets_obj.target_add(
        target_name = target_name,
        target_data = target_data
    )
    yield target_name


@pytest.fixture
def init_targets_file_with_complex_target(init_targets_obj, init_targets_file):
    target_name = "test_target_1"
    target_data = {
        "test_complex_key_1": {
            "test_key_1": "test_value_1"
        }
    }
    init_targets_obj.target_add(
        target_name = target_name,
        target_data = target_data
    )
    yield target_name


@pytest.fixture
def init_targets_file_with_two_targets(init_targets_obj, init_targets_file):
    target_name_1 = "test_target_1"
    target_data = {
        "test_key_1": "test_value_1"
    }
    init_targets_obj.target_add(
        target_name = target_name_1,
        target_data = target_data
    )

    target_name_2 = "test_target_2"
    init_targets_obj.target_add(
        target_name = target_name_2,
        target_data = target_data
    )
    yield {
        "target_name_1": target_name_1,
        "target_name_2": target_name_2
    }


def test_targets_file_not_exists(init_targets_obj):
    assert(not init_targets_obj.targets_file_exists())


def test_targets_file_exists(init_targets_obj, init_targets_file):
    assert(init_targets_obj.targets_file_exists())


def test_target_exists(init_targets_obj, init_targets_file):
    target_name = "test_target_1"
    assert(not init_targets_obj.target_exists(
        target_name = target_name
    ))


def test_target_add_initial(init_targets_obj, init_targets_file_with_target):
    assert(init_targets_obj.target_exists(
        target_name = init_targets_file_with_target
    ))
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_target
    ))


def test_target_add_with_existing(init_targets_obj, init_targets_file_with_two_targets):
    assert(init_targets_obj.target_exists(
        target_name = init_targets_file_with_two_targets["target_name_2"]
    ))
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_two_targets["target_name_1"]
    ))


def test_target_get(init_targets_obj, init_targets_file_with_target):
    expected_target_data = {
        "test_key_1": "test_value_1"
    }

    assert(init_targets_obj.target_get(
        target_name = init_targets_file_with_target
    ) == expected_target_data)


def test_target_get_simple_key_spec(init_targets_obj, init_targets_file_with_target):
    target_key_to_get = "test_key_1"
    expected_target_data = "test_value_1"

    assert(init_targets_obj.target_get(
        target_name = init_targets_file_with_target,
        target_key = target_key_to_get
    ) == expected_target_data)


def test_target_get_complex_key_spec(init_targets_obj, init_targets_file_with_complex_target):
    target_key_to_get = "test_complex_key_1.test_key_1"
    expected_target_data = "test_value_1"

    assert(init_targets_obj.target_get(
        target_name = init_targets_file_with_complex_target,
        target_key = target_key_to_get
    ) == expected_target_data)


def test_target_set_default(init_targets_obj, init_targets_file_with_two_targets):
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_two_targets["target_name_1"]
    ))

    init_targets_obj.target_set_default(
        target_name = init_targets_file_with_two_targets["target_name_2"]
    )
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_two_targets["target_name_2"]
    ))


def test_target_add_with_existing_using_set_default(init_targets_obj, init_targets_file_with_target):
    new_target_name = "test_target_2"
    new_target_data = {
        "test_key_1": "test_value_1"
    }

    init_targets_obj.target_add(
        target_name = new_target_name,
        target_data = new_target_data,
        set_to_default = True
    )
    assert(init_targets_obj.target_exists(
        target_name = new_target_name
    ))
    assert(init_targets_obj.target_is_default(
        target_name = new_target_name
    ))


def test_target_update(init_targets_obj, init_targets_file_with_target):
    key = "test_key_1"
    value = "updated_value"
    new_target_data = {
        key: value
    }

    init_targets_obj.target_update(
        target_name = init_targets_file_with_target,
        new_data = new_target_data
    )

    assert(init_targets_obj.target_exists(
        target_name = init_targets_file_with_target
    ))


def test_target_update_using_set_default(init_targets_obj, init_targets_file_with_two_targets):
    key = "test_key_1"
    value = "updated_value"
    new_target_data = {
        key: value
    }

    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_two_targets["target_name_1"]
    ))

    init_targets_obj.target_update(
        target_name = init_targets_file_with_two_targets["target_name_2"],
        new_data = new_target_data,
        set_to_default = True
    )
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_two_targets["target_name_2"]
    ))


def test_target_delete(init_targets_obj, init_targets_file_with_target):
    assert(init_targets_obj.target_exists(
        target_name = init_targets_file_with_target
    ))
    assert(init_targets_obj.target_is_default(
        target_name = init_targets_file_with_target
    ))

    init_targets_obj.target_delete(init_targets_file_with_target)
    assert(not init_targets_obj.target_exists(
        target_name = init_targets_file_with_target
    ))


def test_target_has_key(init_targets_obj, init_targets_file_with_target):
    target_key_to_find = "test_key_1"

    assert(init_targets_obj.target_has_key(
        target_name = init_targets_file_with_target,
        target_key = target_key_to_find
    ))


def test_target_has_complex_key(init_targets_obj, init_targets_file_with_complex_target):
    target_key_to_find = "test_complex_key_1.test_key_1"

    assert(init_targets_obj.target_has_key(
        target_name = init_targets_file_with_complex_target,
        target_key = target_key_to_find
    ))


def test_target_has_complex_missing_key(init_targets_obj, init_targets_file_with_complex_target):
    target_key_to_find = "test_complex_key_1.not_found.test_key_1"

    assert(not(init_targets_obj.target_has_key(
        target_name = init_targets_file_with_complex_target,
        target_key = target_key_to_find
    )))


def test_targets_list(init_targets_obj, init_targets_file_with_two_targets):
    assert(init_targets_obj.targets_file_list() == [init_targets_file_with_two_targets["target_name_1"], init_targets_file_with_two_targets["target_name_2"]])


def test_targets_file_default_target(init_targets_obj, init_targets_file_with_target):
    assert(init_targets_obj.targets_file_default_target() == init_targets_file_with_target)


def test_get_active_target(init_targets_obj, init_targets_file_with_two_targets, monkeypatch):
    expected_target_1_data = init_targets_obj.target_get(
        init_targets_file_with_two_targets["target_name_1"]
    ) | {"target_name": init_targets_file_with_two_targets["target_name_1"]}
    expected_target_2_data = init_targets_obj.target_get(
        init_targets_file_with_two_targets["target_name_2"]
    ) | {"target_name": init_targets_file_with_two_targets["target_name_2"]}
    default_target_data = init_targets_obj.target_get(
        init_targets_obj.targets_file_default_target()
    ) | {"target_name": init_targets_obj.targets_file_default_target()}

    # 1. Use parsed target_name (target_name_1)
    active_target_data = init_targets_obj.get_active_target(
        target_name = init_targets_file_with_two_targets["target_name_1"]
    )
    assert(active_target_data == expected_target_1_data)

    # 2. Use env var target_name (target_name_2)
    monkeypatch.setenv("TARGET", init_targets_file_with_two_targets["target_name_2"])
    active_target_data = init_targets_obj.get_active_target(
        target_name = None
    )
    assert(active_target_data == expected_target_2_data)
    monkeypatch.delenv("TARGET", raising=False)

    # 3. Use default target
    active_target_data = init_targets_obj.get_active_target(
        target_name = None
    )
    assert(active_target_data["target_name"] == init_targets_obj.targets_file_default_target())
    assert(active_target_data == default_target_data)

    # 4. Missing target error
    target_name = "non_existent_target"

    with open(init_targets_obj.targets_file_path, "w") as file:
        yaml.dump({}, file)
    with pytest.raises(TargetNotFoundError) as exc_info:
        init_targets_obj.get_active_target(
            target_name = target_name
        )
    assert(str(exc_info.value) == f"The target '{target_name}' could not be found in '{init_targets_obj.targets_file_path}'.")


def test_target_exists_missing_file(init_targets_obj):
    target_name = "test_target_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_exists(
            target_name = target_name
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_is_default_missing_file(init_targets_obj):
    target_name = "test_target_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_is_default(
            target_name = target_name
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_add_missing_file(init_targets_obj):
    target_name = "test_target_1"
    target_data = {
        "test_key_1": "test_value_1"
    }

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_add(
            target_name = target_name,
            target_data = target_data
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_set_default_missing_file(init_targets_obj):
    target_name = "test_target_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_set_default(
            target_name = target_name
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_get_data_missing_file(init_targets_obj):
    target_name = "test_target_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_get(
            target_name = target_name
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_update_missing_file(init_targets_obj):
    target_name = "test_target_1"
    key = "test_key_1"
    value = "updated_value"
    new_target_data = {
        key: value
    }

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_update(
            target_name = target_name,
            new_data = new_target_data
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_delete_missing_file(init_targets_obj):
    target_name = "test_target_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_delete(
            target_name = target_name
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_has_key_missing_file(init_targets_obj):
    target_name = "test_target_1"
    target_key_to_find = "test_key_1"

    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.target_has_key(
            target_name = target_name,
            target_key = target_key_to_find
        )
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_targets_list_missing_file(init_targets_obj):
    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.targets_file_list()
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_targets_file_default_target_missing_file(init_targets_obj):
    with pytest.raises(TargetsFileNotFoundError) as exc_info:
        init_targets_obj.targets_file_default_target()
    
    assert(str(exc_info.value) == f"The targets file '{init_targets_obj.targets_file_path}' could not be found.")


def test_target_get_non_existant_key(init_targets_obj, init_targets_file_with_target):
    target_name = "test_target_1"
    target_key_to_find = "test_non_existant_key"

    with pytest.raises(TargetKeyNotFoundError) as exc_info:
        init_targets_obj.target_get(
            target_name = target_name,
            target_key = target_key_to_find
        )
    
    assert(str(exc_info.value) == f"The key '{target_key_to_find}' could not be found for target '{target_name}' in '{init_targets_obj.targets_file_path}'.")