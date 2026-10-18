"""The RBAC scope exporter reads scope_constants.py by AST, so only literal dicts are exportable."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

GENERATOR = Path(__file__).resolve().parents[2] / "rbac-scopes-spec" / "generate.py"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rbac_scopes_generate", GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "scope_constants.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_literal_scope_dicts_are_exported(generator, tmp_path):
    path = _write(tmp_path, 'read_scopes = {"VIEW": "view"}\nwrite_scopes = {"EDIT": "edit"}\n')

    assert generator._parse_scope_groups(path) == [
        ("read_scopes", {"VIEW": "view"}),
        ("write_scopes", {"EDIT": "edit"}),
    ]


def test_dict_unpacking_is_refused_by_name(generator, tmp_path):
    """A `**` entry has no key node, so it must not surface as an unsupported NoneType."""
    path = _write(tmp_path, 'base = {"VIEW": "view"}\nread_scopes = {**base, "EDIT": "edit"}\n')

    with pytest.raises(ValueError, match=r"dict unpacking \(\*\*\) is not supported"):
        generator._parse_scope_groups(path)


def test_the_real_scope_constants_still_export(generator):
    groups = generator._parse_scope_groups(generator.SCOPE_CONSTANTS)

    assert groups
    assert all(name.endswith("_scopes") for name, _ in groups)
