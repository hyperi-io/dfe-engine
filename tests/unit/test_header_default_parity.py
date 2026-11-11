"""The header an unauthored source inherits is dfe-schemas' own, never a version written here."""

from dfe_schemas import schemas_root
from dfe_schemas.loader import read_yaml

from dfe_engine.settings import ClickHouseSettings
from dfe_engine.source.models import DEFAULT_HEADER_TYPE, SourceHeader, inherited_header_version


def test_the_inherited_header_is_the_current_version_dfe_schemas_ships() -> None:
    profile = schemas_root() / "common-header" / f"{DEFAULT_HEADER_TYPE}.yaml"
    current = str(read_yaml(profile)["current"])
    assert inherited_header_version() == current
    assert SourceHeader().version == current
    assert ClickHouseSettings().default_header_version == current
    assert ClickHouseSettings().default_header_type == DEFAULT_HEADER_TYPE


def test_a_new_current_version_reaches_every_default(tmp_path, monkeypatch) -> None:
    header = tmp_path / "common-header"
    header.mkdir()
    (header / f"{DEFAULT_HEADER_TYPE}.yaml").write_text(
        'current: "9.9.9"\nversions:\n  "9.9.9":\n    columns: []\n', encoding="utf-8"
    )
    monkeypatch.setenv("DFE_ENGINE_SCHEMAS_DIR", str(tmp_path))
    inherited_header_version.cache_clear()
    try:
        assert inherited_header_version() == "9.9.9"
        assert SourceHeader().version == "9.9.9"
        assert ClickHouseSettings().default_header_version == "9.9.9"
    finally:
        inherited_header_version.cache_clear()
