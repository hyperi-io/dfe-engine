import csv
import io
import pytest
import yaml

from pathlib import Path


@pytest.fixture
def init_schema_paths(tmp_path):
    derived_schemas_path = Path(tmp_path) / "derived_schemas"
    derived_schemas_path.mkdir(exist_ok = True)

    meta_schemas_path = Path(tmp_path) / "meta_schemas"
    meta_schemas_path.mkdir(exist_ok = True)

    output_path = Path(tmp_path) / "output"
    output_path.mkdir(exist_ok = True)

    schema_config_dir = Path(tmp_path) / "config"
    schema_config_path = Path(schema_config_dir) / "dfe_package.yaml"
    schema_config_dir.mkdir(exist_ok = True)
    
    yield {
        "derived_schemas_path": derived_schemas_path,
        "meta_schemas_path": meta_schemas_path,
        "output_path": output_path,
        "schema_config_path": schema_config_path
    }


@pytest.fixture
def init_schema_config(init_schema_paths):
    
    def _init_(schema_config_data):
        with open(init_schema_paths["schema_config_path"], "w") as file:
            yaml.dump(schema_config_data, file, sort_keys = False)
        return init_schema_paths
    
    return _init_


@pytest.fixture
def init_common_header():

    def _init_(common_header_dict):
        common_header_data = common_header_dict["common_header_data"]
        common_header_path = common_header_dict["common_header_path"]
        common_header_version = common_header_dict["common_header_version"].replace(".", "_")

        common_header_path = Path(common_header_path) / common_header_version / "common_header.csv"
        common_header_path.parent.mkdir(parents = True, exist_ok = True)
        with open(common_header_path, "w") as file:
            reader = csv.reader(io.StringIO(common_header_data))
            writer = csv.writer(file)
            writer.writerows(reader)
        
        return {
            "common_header_version": common_header_version,
            "common_header_path": common_header_path
        }
    
    return _init_


@pytest.fixture
def init_derived_schema(init_schema_paths):

    def _init_(derived_schema_dict):
        derived_schema_name = derived_schema_dict["derived_schema_name"]
        derived_schema_version = derived_schema_dict["derived_schema_version"].replace(".", "_")
        derived_schema_data = derived_schema_dict["derived_schema_data"]
        meta_schema_name = derived_schema_dict["meta_schema_name"]

        derived_schema_path = init_schema_paths["derived_schemas_path"] / meta_schema_name / derived_schema_name / derived_schema_version / f"{derived_schema_name}.csv"

        derived_schema_path.parent.mkdir(parents = True, exist_ok = True)
        with open(derived_schema_path, "w") as file:
            reader = csv.reader(io.StringIO(derived_schema_data))
            writer = csv.writer(file)
            writer.writerows(reader)
    
        return {
            "derived_schema_name": derived_schema_name,
            "derived_schema_version": derived_schema_version,
            "derived_schema_path": derived_schema_path,
            "meta_schema_name": meta_schema_name
        }
    
    return _init_


@pytest.fixture
def init_meta_schema(init_schema_paths):

    def _init_(meta_schema_dict):
        meta_schema_name = meta_schema_dict["meta_schema_name"]
        meta_schema_version = meta_schema_dict["meta_schema_version"].replace(".", "_")
        meta_schema_data = meta_schema_dict["meta_schema_data"]

        meta_schema_path = init_schema_paths["meta_schemas_path"] / meta_schema_name / meta_schema_version / f"{meta_schema_name}.csv"

        meta_schema_path.parent.mkdir(parents = True, exist_ok = True)
        with open(meta_schema_path, "w") as file:
            reader = csv.reader(io.StringIO(meta_schema_data))
            writer = csv.writer(file)
            writer.writerows(reader)
    
        return {
            "meta_schema_name": meta_schema_name,
            "meta_schema_version": meta_schema_version,
            "meta_schema_path": meta_schema_path
        }
    
    return _init_


@pytest.fixture
def init_type_maps():

    def _init_(type_maps_dict):
        type_maps_data = type_maps_dict["type_maps_data"]
        type_maps_path = type_maps_dict["type_maps_path"]
        type_maps_version = type_maps_dict["type_maps_version"].replace(".", "_")

        type_maps_path = Path(type_maps_path) / type_maps_version / "type_maps.csv"
        type_maps_path.parent.mkdir(parents = True, exist_ok = True)
        with open(type_maps_path, "w") as file:
            reader = csv.reader(io.StringIO(type_maps_data))
            writer = csv.writer(file)
            writer.writerows(reader)
        
        return {
            "type_maps_version": type_maps_version,
            "type_maps_path": type_maps_path
        }
    
    return _init_