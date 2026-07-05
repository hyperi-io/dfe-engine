"""Tests for storage bootstrap: directory creation and one-time schema seeding."""

from pathlib import Path

import pytest

from dfe_engine.bootstrap import SEED_MARKER_NAME, ensure_storage
from dfe_engine.settings import DFESettings, SchemasSettings


def make_settings(*, config_dir: Path, schemas_dir: Path) -> DFESettings:
    """Build settings pointing config and schemas at the given directories."""
    return DFESettings(
        config_dir=str(config_dir),
        schemas=SchemasSettings(schemas_dir=str(schemas_dir)),
    )


@pytest.fixture
def config_dir(tmp_path) -> Path:
    """Uncreated config directory path."""
    return tmp_path / "config"


@pytest.fixture
def schemas_dir(tmp_path) -> Path:
    """Uncreated schemas directory path."""
    return tmp_path / "schemas"


@pytest.fixture
def seed_dir(monkeypatch, tmp_path) -> Path:
    """Populated seed directory wired via DFE_SCHEMAS_SEED_DIR."""
    path = tmp_path / "seed"
    (path / "meta").mkdir(parents=True)
    (path / "meta" / "core.yaml").write_text("name: core\n")
    (path / "top.yaml").write_text("name: top\n")
    monkeypatch.setenv("DFE_SCHEMAS_SEED_DIR", str(path))
    return path


class TestEnsureStorage:
    def test_creates_config_and_schemas_dirs(self, config_dir, schemas_dir, seed_dir):
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert config_dir.is_dir()
        assert schemas_dir.is_dir()

    def test_seeds_schemas_when_marker_absent(self, config_dir, schemas_dir, seed_dir):
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert (schemas_dir / "top.yaml").read_text() == "name: top\n"
        assert (schemas_dir / "meta" / "core.yaml").read_text() == "name: core\n"
        assert (schemas_dir / SEED_MARKER_NAME).exists()

    def test_does_not_reseed_when_marker_present(self, config_dir, schemas_dir, seed_dir):
        schemas_dir.mkdir(parents=True)
        (schemas_dir / SEED_MARKER_NAME).write_text("")
        (schemas_dir / "user.yaml").write_text("name: user\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert (schemas_dir / "user.yaml").read_text() == "name: user\n"
        assert not (schemas_dir / "top.yaml").exists()

    def test_config_is_never_seeded(self, config_dir, schemas_dir, seed_dir):
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert list(config_dir.iterdir()) == []

    def test_skips_git_entry_when_seeding(self, config_dir, schemas_dir, seed_dir):
        (seed_dir / ".git").write_text("gitdir: elsewhere\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert not (schemas_dir / ".git").exists()
        assert (schemas_dir / "top.yaml").exists()

    def test_missing_seed_leaves_schemas_empty(
        self, config_dir, monkeypatch, schemas_dir, tmp_path
    ):
        monkeypatch.setenv("DFE_SCHEMAS_SEED_DIR", str(tmp_path / "absent"))
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert schemas_dir.is_dir()
        assert list(schemas_dir.iterdir()) == []

    def test_fills_settings_with_effective_paths(self, config_dir, schemas_dir, seed_dir):
        settings = make_settings(config_dir=config_dir, schemas_dir=schemas_dir)
        ensure_storage(settings=settings)
        assert settings.config_dir == str(config_dir)
        assert settings.schemas.schemas_dir == str(schemas_dir)
