"""Tests for the SurfaceRegistry — YAML-backed service surface CRUD."""

import importlib.resources
import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dfe_engine.services.surfaces.models import (
    ConfigSurfaceEntry,
    MetricEntry,
    ServiceSurface,
)
from dfe_engine.services.surfaces.registry import SurfaceNotFoundError, SurfaceRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load

MANIFESTS = Path(__file__).parents[2] / "fixtures" / "contract"
RESOURCES = importlib.resources.files("dfe_engine.services.surfaces.resources")


def _manifest(service: str) -> dict:
    """The app's full metric manifest, as its ``metrics-manifest`` command prints it."""
    return json.loads((MANIFESTS / service / "metrics-manifest.json").read_text())


def _shipped_metrics(service: str) -> list[MetricEntry]:
    """The metrics the built-in surface for *service* lists."""
    with importlib.resources.as_file(RESOURCES / f"{service}.yaml") as path:
        return ServiceSurface(**yaml_load(path)).metrics_surface


def _idle_manifest(service: str) -> dict:
    """The app's manifest with every metric its built-in surface lists taken out.

    That is the shape an idle app serves: its runtime metrics, and none of the
    curated ones.
    """
    manifest = _manifest(service)
    curated = {entry.name for entry in _shipped_metrics(service)}
    manifest["metrics"] = [m for m in manifest["metrics"] if m["name"] not in curated]
    return manifest


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


def _at(base: str):
    """A resolver naming each service's manifest under *base*."""
    return lambda service: f"{base}/{service}/metrics/manifest"


def _got(registry: SurfaceRegistry, service: str) -> ServiceSurface:
    surface = registry.get(service)
    assert surface is not None, f"no surface for {service}"
    return surface


class TestManifestAddress:
    def test_no_resolver_names_no_address(self, tmp_path: Path):
        registry = SurfaceRegistry(tmp_path / "surfaces")

        assert {s.manifest_url for s in registry.list()} == {""}

    def test_each_service_gets_the_resolver_s_address(self, tmp_path: Path):
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at("http://m"))

        assert _got(registry, "dfe-loader").manifest_url == "http://m/dfe-loader/metrics/manifest"

    def test_an_address_in_the_file_is_replaced_by_the_resolver_s(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump(
            {"service": "dfe-x", "manifest_url": "http://dfe-x.dfe-prod.svc.cluster.local:9090/m"},
            surfaces_dir / "dfe-x.yaml",
        )

        assert _got(SurfaceRegistry(surfaces_dir), "dfe-x").manifest_url == ""
        resolved = SurfaceRegistry(surfaces_dir, manifest_url_for=_at("http://m"))
        assert _got(resolved, "dfe-x").manifest_url == "http://m/dfe-x/metrics/manifest"

    def test_a_written_surface_stores_no_address(self, tmp_path: Path):
        surfaces_dir = tmp_path / "surfaces"
        surfaces_dir.mkdir()
        yaml_dump({"service": "existing"}, surfaces_dir / "existing.yaml")
        registry = SurfaceRegistry(surfaces_dir, manifest_url_for=_at("http://m"))

        surface = _make_surface("dfe-new")
        surface.manifest_url = "http://somewhere/else"
        registry.create(surface)

        assert "manifest_url" not in yaml_load(surfaces_dir / "dfe-new.yaml")
        assert _got(registry, "dfe-new").manifest_url == "http://m/dfe-new/metrics/manifest"


class _ManifestServer:
    """A local HTTP server answering each path with a fixed status and body."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.hits: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                server.hits.append(self.path)
                status, body = routes.get(self.path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve() -> Iterator[Callable[[dict[str, tuple[int, bytes]]], _ManifestServer]]:
    """Start a manifest server per call, every one closed at teardown."""
    servers: list[_ManifestServer] = []

    def start(routes: dict[str, tuple[int, bytes]]) -> _ManifestServer:
        server = _ManifestServer(routes)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def loader_app() -> Iterator[_ManifestServer]:
    """dfe-loader's real manifest, served where the resolver below points."""
    manifest = (MANIFESTS / "dfe-loader" / "metrics-manifest.json").read_bytes()
    server = _ManifestServer(
        {
            "/dfe-loader/metrics/manifest": (200, manifest),
            "/dfe-receiver/metrics/manifest": (200, b'{"metrics": [{"name": "no_type"}]}'),
            "/dfe-archiver/metrics/manifest": (200, b'{"app": "dfe-archiver"}'),
        }
    )
    yield server
    server.close()


class TestRefreshManifest:
    async def test_no_address_fetches_nothing_and_keeps_the_file(self, tmp_path: Path):
        registry = SurfaceRegistry(tmp_path / "surfaces")
        before = (tmp_path / "surfaces" / "dfe-loader.yaml").read_text()

        assert await registry.refresh_manifest("dfe-loader") is None
        assert (tmp_path / "surfaces" / "dfe-loader.yaml").read_text() == before

    async def test_an_unknown_service_is_not_refreshed(self, tmp_path: Path, loader_app):
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(loader_app.base))

        assert await registry.refresh_manifest("dfe-nothing") is None
        assert loader_app.hits == []

    async def test_the_app_s_full_manifest_adds_what_the_surface_left_out(
        self, tmp_path: Path, loader_app
    ):
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(loader_app.base))
        published = [m["name"] for m in _manifest("dfe-loader")["metrics"]]
        curated = [m.name for m in _shipped_metrics("dfe-loader")]

        refreshed = await registry.refresh_manifest("dfe-loader")

        assert loader_app.hits == ["/dfe-loader/metrics/manifest"]
        assert refreshed is not None
        assert refreshed.discovered_at
        stored = [m.name for m in _got(registry, "dfe-loader").metrics_surface]
        assert stored == [m.name for m in refreshed.metrics_surface]
        assert stored[: len(curated)] == curated
        assert sorted(stored) == sorted(published)
        assert "manifest_url" not in yaml_load(tmp_path / "surfaces" / "dfe-loader.yaml")

    async def test_a_non_success_answer_is_not_a_refresh(self, tmp_path: Path, loader_app):
        registry = SurfaceRegistry(
            tmp_path / "surfaces", manifest_url_for=lambda s: f"{loader_app.base}/gone/{s}"
        )
        before = (tmp_path / "surfaces" / "dfe-loader.yaml").read_text()

        assert await registry.refresh_manifest("dfe-loader") is None
        assert (tmp_path / "surfaces" / "dfe-loader.yaml").read_text() == before

    @pytest.mark.parametrize("service", ["dfe-receiver", "dfe-archiver"])
    async def test_a_body_that_is_not_a_manifest_is_not_a_refresh(
        self, tmp_path: Path, loader_app, service
    ):
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(loader_app.base))
        before = (tmp_path / "surfaces" / f"{service}.yaml").read_text()

        assert await registry.refresh_manifest(service) is None
        assert (tmp_path / "surfaces" / f"{service}.yaml").read_text() == before


def _served(manifest: dict) -> tuple[int, bytes]:
    return 200, json.dumps(manifest).encode()


class TestRefreshMerges:
    """A refresh adds and updates metrics, and never drops one the surface lists."""

    async def test_an_idle_app_keeps_every_curated_metric(self, tmp_path: Path, serve):
        idle = _idle_manifest("dfe-archiver")
        curated = _shipped_metrics("dfe-archiver")
        assert 0 < len(idle["metrics"]) < len(_manifest("dfe-archiver")["metrics"])
        app = serve({"/dfe-archiver/metrics/manifest": _served(idle)})
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(app.base))

        refreshed = await registry.refresh_manifest("dfe-archiver")

        assert refreshed is not None
        stored = _got(registry, "dfe-archiver").metrics_surface
        assert stored[: len(curated)] == curated
        assert [m.name for m in stored[len(curated) :]] == [m["name"] for m in idle["metrics"]]

    async def test_a_listed_metric_the_app_describes_is_updated_in_place(
        self, tmp_path: Path, serve
    ):
        curated = _shipped_metrics("dfe-archiver")
        described = {**curated[2].model_dump(), "description": "Archive files opened"}
        app = serve({"/dfe-archiver/metrics/manifest": _served({"metrics": [described]})})
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(app.base))

        await registry.refresh_manifest("dfe-archiver")

        stored = _got(registry, "dfe-archiver").metrics_surface
        assert [m.name for m in stored] == [m.name for m in curated]
        assert stored[2].description == "Archive files opened"
        assert stored[:2] + stored[3:] == curated[:2] + curated[3:]

    async def test_a_second_refresh_lists_nothing_twice(self, tmp_path: Path, serve):
        app = serve({"/dfe-archiver/metrics/manifest": _served(_idle_manifest("dfe-archiver"))})
        registry = SurfaceRegistry(tmp_path / "surfaces", manifest_url_for=_at(app.base))

        first = await registry.refresh_manifest("dfe-archiver")
        second = await registry.refresh_manifest("dfe-archiver")

        assert first is not None
        assert second is not None
        assert [m.name for m in second.metrics_surface] == [m.name for m in first.metrics_surface]
