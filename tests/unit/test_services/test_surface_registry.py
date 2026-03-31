"""Tests for the SurfaceRegistry — YAML-backed service surface CRUD."""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.services.surfaces.models import (
    ConfigSurfaceEntry,
    MetricEntry,
    ServiceSurface,
)
from dfe_engine.services.surfaces.registry import SurfaceNotFoundError, SurfaceRegistry
from dfe_engine.yaml_utils import yaml_dump


def _make_surface(
    service: str = "dfe-test",
    description: str = "Test service",
) -> ServiceSurface:
    """Create a minimal ServiceSurface for testing."""
    return ServiceSurface(
        service=service,
        description=description,
        config_surface={
            "config.buffer.max_bytes": ConfigSurfaceEntry(
                type="integer",
                description="Buffer limit",
                default=1024,
            ),
        },
        metrics_surface=[
            MetricEntry(
                name=f"{service.replace('-', '_')}_requests_total",
                type="counter",
                group="app",
            ),
        ],
    )


class TestSurfaceRegistryInit:
    def test_creates_directory(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        assert not surfaces_dir.exists()
        SurfaceRegistry(surfaces_dir)
        assert surfaces_dir.exists()

    def test_seeds_built_ins_on_empty_dir(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        registry = SurfaceRegistry(surfaces_dir)
        surfaces = registry.list()
        # Built-in resources include dfe-receiver, dfe-loader, dfe-archiver
        assert len(surfaces) >= 3
        names = {s.service for s in surfaces}
        assert "dfe-receiver" in names
        assert "dfe-loader" in names
        assert "dfe-archiver" in names

    def test_does_not_reseed_existing(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        # Pre-populate with a single file
        yaml_dump(
            {"service": "custom-svc", "description": "Custom"},
            surfaces_dir / "custom-svc.yaml",
        )
        registry = SurfaceRegistry(surfaces_dir)
        surfaces = registry.list()
        # Should only have the pre-existing file, not built-ins
        assert len(surfaces) == 1
        assert surfaces[0].service == "custom-svc"


class TestSurfaceRegistryGet:
    def test_get_existing(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        surface = _make_surface("dfe-test")
        yaml_dump(surface.model_dump(mode="json"), surfaces_dir / "dfe-test.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        result = registry.get("dfe-test")
        assert result is not None
        assert result.service == "dfe-test"
        assert len(result.config_surface) == 1
        assert len(result.metrics_surface) == 1

    def test_get_missing_returns_none(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "x"}, surfaces_dir / "x.yaml")
        registry = SurfaceRegistry(surfaces_dir)
        assert registry.get("nonexistent") is None

    def test_get_invalid_yaml_returns_none(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        (surfaces_dir / "bad.yaml").write_text("not: [valid: yaml: {{")
        # Need a valid file so seeding is skipped
        yaml_dump({"service": "ok"}, surfaces_dir / "ok.yaml")
        registry = SurfaceRegistry(surfaces_dir)
        # bad.yaml will fail to parse, should return None
        assert registry.get("bad") is None


class TestSurfaceRegistryList:
    def test_list_returns_sorted(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        for name in ["dfe-z", "dfe-a", "dfe-m"]:
            yaml_dump({"service": name}, surfaces_dir / f"{name}.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        surfaces = registry.list()
        assert [s.service for s in surfaces] == ["dfe-a", "dfe-m", "dfe-z"]

    def test_list_empty_dir(self, tmp_path: Path):
        # An empty dir gets seeded with built-ins, so use a dir that
        # already has a file to avoid seeding, then delete the file.
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        sentinel = surfaces_dir / "temp.yaml"
        yaml_dump({"service": "temp"}, sentinel)
        registry = SurfaceRegistry(surfaces_dir)
        sentinel.unlink()
        # Re-list after manual delete
        assert registry.list() == []


class TestSurfaceRegistryCRUD:
    def test_create(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "existing"}, surfaces_dir / "existing.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        surface = _make_surface("dfe-new")
        registry.create(surface)

        result = registry.get("dfe-new")
        assert result is not None
        assert result.service == "dfe-new"
        assert result.description == "Test service"

    def test_create_duplicate_raises(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "dfe-dup"}, surfaces_dir / "dfe-dup.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        with pytest.raises(FileExistsError, match="already exists"):
            registry.create(_make_surface("dfe-dup"))

    def test_update(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump(
            {"service": "dfe-svc", "description": "old"},
            surfaces_dir / "dfe-svc.yaml",
        )

        registry = SurfaceRegistry(surfaces_dir)
        updated = _make_surface("dfe-svc")
        updated.description = "new description"
        registry.update("dfe-svc", updated)

        result = registry.get("dfe-svc")
        assert result is not None
        assert result.description == "new description"

    def test_update_missing_raises(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "x"}, surfaces_dir / "x.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        with pytest.raises(SurfaceNotFoundError, match="not found"):
            registry.update("nonexistent", _make_surface())

    def test_delete(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "dfe-del"}, surfaces_dir / "dfe-del.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        registry.delete("dfe-del")
        assert registry.get("dfe-del") is None

    def test_delete_missing_raises(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "x"}, surfaces_dir / "x.yaml")

        registry = SurfaceRegistry(surfaces_dir)
        with pytest.raises(SurfaceNotFoundError, match="not found"):
            registry.delete("nonexistent")


class TestSurfaceRegistryBuiltIns:
    def test_builtin_receiver_has_config_and_metrics(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        registry = SurfaceRegistry(surfaces_dir)
        receiver = registry.get("dfe-receiver")
        assert receiver is not None
        assert len(receiver.config_surface) > 0
        assert len(receiver.metrics_surface) > 0
        assert receiver.manifest_url != ""

    def test_builtin_loader_has_config_and_metrics(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        registry = SurfaceRegistry(surfaces_dir)
        loader = registry.get("dfe-loader")
        assert loader is not None
        assert len(loader.config_surface) > 0
        assert len(loader.metrics_surface) > 0

    def test_builtin_archiver_has_config_and_metrics(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        registry = SurfaceRegistry(surfaces_dir)
        archiver = registry.get("dfe-archiver")
        assert archiver is not None
        assert len(archiver.config_surface) > 0
        assert len(archiver.metrics_surface) > 0
