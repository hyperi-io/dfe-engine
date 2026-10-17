#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_landing_table_name.py
#  Purpose:      The landing table's name comes from the dfe-schemas manifest and nowhere else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The landing table's name is the manifest's, whatever the settings or the code say.

The shipped manifest names it ``main``, which a literal in the engine would also
produce. A tree whose manifest names it differently is what tells the two apart.
"""

from pathlib import Path
from types import SimpleNamespace

import dfe_schemas
import pytest

from dfe_engine.schema.core_schema import landing_table_name
from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.source.core_sources import seed_core_sources
from dfe_engine.source.registry import SourceRegistry
from tests.support.core_sources import write_landing_definition

LANDING_DEFINITION_FILE = Path("tables/core/main.yaml")
RENAMED = "catchall"


def _mirror(src: Path, dst: Path, rewritten: dict[Path, str], rel: Path = Path()) -> None:
    """Symlink every file under *src* into *dst*, writing *rewritten* in place of its own."""
    (dst / rel).mkdir(parents=True, exist_ok=True)
    for entry in (src / rel).iterdir():
        path = rel / entry.name
        if path in rewritten:
            (dst / path).write_text(rewritten[path], encoding="utf-8")
        elif entry.is_dir() and any(target.is_relative_to(path) for target in rewritten):
            _mirror(src, dst, rewritten, path)
        else:
            (dst / path).symlink_to(entry)


@pytest.fixture
def renamed_landing_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The installed dfe-schemas tree with its landing table declared as ``catchall``."""
    shipped = Path(dfe_schemas.schemas_root())
    definition = (shipped / LANDING_DEFINITION_FILE).read_text(encoding="utf-8")
    assert 'name: "main"' in definition, "dfe-schemas no longer declares the landing name here"
    tree = tmp_path / "schemas-tree"
    _mirror(
        shipped,
        tree,
        {LANDING_DEFINITION_FILE: definition.replace('name: "main"', f'name: "{RENAMED}"')},
    )
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tree))
    return tree


def test_the_shipped_manifest_names_the_landing_table_main():
    assert landing_table_name() == "main"


def test_the_name_follows_the_manifest(renamed_landing_tree: Path):
    assert landing_table_name() == RENAMED


def test_the_ddl_writer_follows_the_manifest(renamed_landing_tree: Path):
    ddl = DDLFileWriter(database="dfe").generate_default_table()

    assert f"CREATE TABLE IF NOT EXISTS `dfe`.`{RENAMED}`" in ddl
    assert "`dfe`.`main`" not in ddl


def test_the_core_source_follows_the_manifest(renamed_landing_tree: Path, tmp_path: Path):
    SourceRegistry.reset_instance()
    sources = tmp_path / "sources"
    sources.mkdir()
    registry = SourceRegistry(sources_directory=sources, writable=True, refresh_interval=0)
    schemas = tmp_path / "schemas"
    write_landing_definition(schemas_dir=schemas)
    settings = SimpleNamespace(
        clickhouse=SimpleNamespace(
            default_engine="MergeTree",
            default_table_profile="timeseries",
            default_ttl_days=0,
            effective_data_database="dfe",
        ),
        schemas=SimpleNamespace(schemas_dir=str(schemas)),
        source=SimpleNamespace(
            builds_dir=str(tmp_path / "builds"),
            deploys_dir=str(tmp_path / "deploys"),
            plans_dir=str(tmp_path / "plans"),
            sources_dir=str(sources),
        ),
    )
    try:
        changed = seed_core_sources(registry=registry, settings=settings)
        stored = registry.get_source(RENAMED)
    finally:
        registry.close()
        SourceRegistry.reset_instance()

    assert changed == [RENAMED]
    assert stored.table_name == RENAMED
