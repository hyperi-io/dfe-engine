"""Tests for storage bootstrap: directory creation and one-time schema seeding."""

from pathlib import Path

import pytest

from dfe_engine.bootstrap import SEED_MARKER_NAME, _seed_stamp, ensure_storage
from dfe_engine.settings import DFESettings, SchemasSettings


def make_settings(*, config_dir: Path, schemas_dir: Path) -> DFESettings:
    """Build settings pointing config and schemas at the given directories."""
    # env="test": DFESettings is a plain BaseModel, so a direct construction never
    # reads DFE_ENGINE_ENV and `env` falls back to its "production" default, which
    # requires auth on with a real jwt_secret. Declare the posture the test wants.
    return DFESettings(
        env="test",
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
    """Populated seed directory wired via DFE_ENGINE_SCHEMAS_SEED_DIR."""
    path = tmp_path / "seed"
    (path / "meta").mkdir(parents=True)
    (path / "meta" / "core.yaml").write_text("name: core\n")
    (path / "top.yaml").write_text("name: top\n")
    monkeypatch.setenv("DFE_ENGINE_SCHEMAS_SEED_DIR", str(path))
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

    def test_does_not_reseed_when_marker_matches_this_version(
        self, config_dir, schemas_dir, seed_dir
    ):
        schemas_dir.mkdir(parents=True)
        (schemas_dir / SEED_MARKER_NAME).write_text(f"{_seed_stamp()}\n")
        (schemas_dir / "user.yaml").write_text("name: user\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert (schemas_dir / "user.yaml").read_text() == "name: user\n"
        assert not (schemas_dir / "top.yaml").exists()

    def test_reseeds_when_marker_names_another_version(self, config_dir, schemas_dir, seed_dir):
        # A tree seeded by an older image lacks whatever that image did not ship, so
        # the marker must not suppress the refresh.
        schemas_dir.mkdir(parents=True)
        (schemas_dir / SEED_MARKER_NAME).write_text("dfe-engine 0.0.1\n")
        (schemas_dir / "user.yaml").write_text("name: user\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert (schemas_dir / "top.yaml").read_text() == "name: top\n"
        assert (schemas_dir / "meta" / "core.yaml").read_text() == "name: core\n"
        assert (schemas_dir / "user.yaml").read_text() == "name: user\n"
        assert (schemas_dir / SEED_MARKER_NAME).read_text().strip() == _seed_stamp()

    def test_a_reseed_moves_the_inherited_header_to_the_new_tree(
        self, monkeypatch, config_dir, schemas_dir, seed_dir
    ):
        # An upgraded pod reads its header version before startup re-seeds the tree.
        from dfe_engine.schema_cache import registered
        from dfe_engine.source.models import DEFAULT_HEADER_TYPE, inherited_header_version

        def profile(root: Path, current: str) -> None:
            (root / "common-header").mkdir(parents=True, exist_ok=True)
            (root / "common-header" / f"{DEFAULT_HEADER_TYPE}.yaml").write_text(
                f'current: "{current}"\nversions:\n  "{current}":\n    columns: []\n'
            )

        profile(schemas_dir, "1.0.3")
        (schemas_dir / SEED_MARKER_NAME).write_text("dfe-engine 0.0.1\n")
        profile(seed_dir, "1.0.4")
        monkeypatch.setenv("DFE_ENGINE_SCHEMAS_DIR", str(schemas_dir))
        inherited_header_version.cache_clear()
        try:
            assert inherited_header_version() == "1.0.3"
            ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
            assert [c for c in registered() if c.cache_info().currsize] == []
            assert inherited_header_version() == "1.0.4"
        finally:
            inherited_header_version.cache_clear()

    def test_marker_records_the_seeding_version(self, config_dir, schemas_dir, seed_dir):
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert (schemas_dir / SEED_MARKER_NAME).read_text().strip() == _seed_stamp()

    def test_config_is_never_seeded(self, config_dir, schemas_dir, seed_dir):
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert list(config_dir.iterdir()) == []

    def test_skips_git_entry_when_seeding(self, config_dir, schemas_dir, seed_dir):
        (seed_dir / ".git").write_text("gitdir: elsewhere\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert not (schemas_dir / ".git").exists()
        assert (schemas_dir / "top.yaml").exists()

    def test_skips_the_source_catalogue_when_seeding(self, config_dir, schemas_dir, seed_dir):
        # The engine reads the catalogue from its installed package, and the schema store would parse every file.
        (seed_dir / "catalogue").mkdir()
        (seed_dir / "catalogue" / "acme-log.yaml").write_text("source: acme-log\n")
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert not (schemas_dir / "catalogue").exists()
        assert (schemas_dir / "top.yaml").exists()

    def test_missing_seed_leaves_schemas_empty(
        self, config_dir, monkeypatch, schemas_dir, tmp_path
    ):
        monkeypatch.setenv("DFE_ENGINE_SCHEMAS_SEED_DIR", str(tmp_path / "absent"))
        ensure_storage(settings=make_settings(config_dir=config_dir, schemas_dir=schemas_dir))
        assert schemas_dir.is_dir()
        assert list(schemas_dir.iterdir()) == []

    def test_fills_settings_with_effective_paths(self, config_dir, schemas_dir, seed_dir):
        settings = make_settings(config_dir=config_dir, schemas_dir=schemas_dir)
        ensure_storage(settings=settings)
        assert settings.config_dir == str(config_dir)
        assert settings.schemas.schemas_dir == str(schemas_dir)

    def test_unset_schemas_dir_skips_schema_bootstrap(self, config_dir, seed_dir):
        settings = DFESettings(env="test", config_dir=str(config_dir))
        assert settings.schemas.schemas_dir == ""
        ensure_storage(settings=settings)
        assert config_dir.is_dir()
        assert settings.schemas.schemas_dir == ""


# functools caches that read nothing from the configured schema tree.
_NOT_TREE_CACHES = {
    "dfe_engine.appmgmt.operations._named_queries": "reads a file inside the package",
    "dfe_engine.auth.accounts._dummy_hash": "a password hash, no file",
    "dfe_engine.auth.oidc.adapters.entra._confidential_client": "MSAL clients",
    "dfe_engine.governance.actions._warn_stored_credential": "a warn-once guard",
    "dfe_engine.governance.ch.models.catalogue": "reads the pinned dfe-schemas wheel",
    "dfe_engine.governance.policies._prefix_matcher": "a compiled regex",
}


def _module_caches() -> dict[str, object]:
    """Every module-level functools cache defined in the engine, by qualified name."""
    import importlib
    import pkgutil

    import dfe_engine

    found: dict[str, object] = {}
    for info in pkgutil.walk_packages(dfe_engine.__path__, "dfe_engine."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if not (hasattr(value, "cache_clear") and hasattr(value, "__wrapped__")):
                continue
            if getattr(value, "__module__", None) == info.name:
                found[f"{info.name}.{name}"] = value
    return found


def test_every_module_cache_is_registered_or_named_as_not_reading_the_tree():
    from dfe_engine.schema_cache import registered

    caches = _module_caches()
    tracked = {id(c) for c in registered()}
    unaccounted = sorted(
        name for name, c in caches.items() if id(c) not in tracked and name not in _NOT_TREE_CACHES
    )
    assert unaccounted == [], (
        "a re-seed leaves these caches holding the old schema tree: decorate each with "
        "@reseed_clears, or add it to _NOT_TREE_CACHES with the reason it reads no tree"
    )
    assert sorted(set(_NOT_TREE_CACHES) - set(caches)) == []
