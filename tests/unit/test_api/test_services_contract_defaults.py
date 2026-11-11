"""``/api/v1/services`` serves each app's own defaults, read off its mounted contract.

The services are the app manifest's apps that read a config file, and a key the
stored file leaves out is shown as the app's schema says it runs, so no default is
restated in the engine to drift.
"""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from dfe_engine.appmgmt import catalogue, contract

FIXTURES = Path(__file__).parents[2] / "fixtures"

APPS = {
    "receiver": FIXTURES / "contract" / "dfe-receiver",
    "loader": FIXTURES / "contract" / "dfe-loader",
    "archiver": FIXTURES / "contract" / "dfe-archiver",
    "fetcher": FIXTURES / "contract" / "dfe-fetcher",
    "transform-vector": FIXTURES / "contract" / "dfe-transform-vector",
    "transform-vrl": FIXTURES / "contract" / "dfe-transform-vrl",
    "transform-elastic": FIXTURES / "catalogue" / "contract" / "dfe-transform-elastic",
}
"""Every app in the manifest with a config file, and the contract fixture it emitted."""


@pytest.fixture
def mounted(tmp_path, monkeypatch) -> Iterator[Path]:
    """Each app's emitted contract under one directory, as a deployment mounts them."""
    root = tmp_path / "contract"
    for service, emitted in APPS.items():
        shutil.copytree(emitted, root / f"{catalogue.PROJECT_PREFIX}{service}")
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(root))
    contract.reload_contracts()
    yield root
    contract.reload_contracts()


def _stored(client, headers, service: str, instance: str, config: dict | None = None) -> None:
    """A config saved through the route, holding only what is given."""
    url = f"/api/v1/services/{service}/{instance}"
    resp = client.put(url, json=config or {}, headers=headers)
    assert resp.status_code == 200, resp.text


def _section_defaults(service: str) -> dict:
    """Each top-level section's default as the app's schema states it.

    A default holding the redaction literal is a marked secret's, never served.
    """
    schema = json.loads((APPS[service] / contract.SCHEMA_FILE).read_text())
    return {
        name: node["default"]
        for name, node in schema["properties"].items()
        if "default" in node and contract.REDACTED not in json.dumps(node["default"])
    }


def _differences(served: object, stated: object, path: str) -> list[str]:
    """Every path the schema states a default at that the route serves differently.

    A field the stated section omits is not judged: it is a field whose default
    the app's serialiser skipped, which the route takes from the field's own schema.
    """
    if isinstance(stated, dict) and isinstance(served, dict):
        return [
            found
            for key, value in stated.items()
            for found in (
                _differences(served[key], value, f"{path}.{key}")
                if key in served
                else [f"{path}.{key} not served"]
            )
        ]
    return [] if served == stated else [f"{path}: served {served!r}, schema {stated!r}"]


def test_the_table_covers_every_app_that_reads_a_config_file():
    apps = {
        app.service.removeprefix(catalogue.PROJECT_PREFIX)
        for app in catalogue.APP_CATALOGUE.values()
        if app.config_file
    }
    assert apps == set(APPS)


@pytest.mark.parametrize("service", sorted(APPS))
def test_an_unset_section_is_served_as_the_app_s_schema_default(
    mounted, client, admin_headers, service
):
    _stored(client, admin_headers, service, "bare")
    resp = client.get(f"/api/v1/services/{service}/bare", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    served = resp.json()["config"]
    expected = _section_defaults(service)
    assert expected, f"{service}'s fixture states no section default"
    assert _differences(served, expected, service) == []


@pytest.mark.parametrize(
    ("service", "path", "value"),
    [
        # dfe-loader src/config/pipeline.rs BufferConfig::default.
        ("loader", ("buffer", "flush_rows"), 20_000),
        ("loader", ("buffer", "flush_bytes"), 64 * 1024 * 1024),
        ("archiver", ("buffer", "flush_records"), 100_000),
        ("receiver", ("server", "bind_address"), "0.0.0.0:8080"),
    ],
)
def test_a_known_default_is_the_app_s_not_a_copy(
    mounted, client, admin_headers, service, path, value
):
    _stored(client, admin_headers, service, "bare")
    served = client.get(f"/api/v1/services/{service}/bare", headers=admin_headers).json()
    leaf = served["config"]
    for part in path:
        leaf = leaf[part]
    assert leaf == value


def test_a_stored_key_wins_and_its_siblings_keep_the_app_s_defaults(mounted, client, admin_headers):
    _stored(client, admin_headers, "loader", "tuned", {"buffer": {"flush_rows": 5}})
    served = client.get("/api/v1/services/loader/tuned", headers=admin_headers).json()
    assert served["config"]["buffer"] == {
        "flush_bytes": 64 * 1024 * 1024,
        "flush_rows": 5,
        "flush_age_secs": 5,
    }


def test_a_save_writes_only_what_was_sent(mounted, client, admin_headers, api_settings):
    body = {"buffer": {"flush_rows": 7}}
    assert (
        client.put("/api/v1/services/loader/x", json=body, headers=admin_headers).status_code == 200
    )
    saved = Path(api_settings.services.config_yaml_dir) / "loader-x.yaml"
    assert saved.read_text(encoding="utf-8").strip() == "buffer:\n  flush_rows: 7"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"buffer": {"flush_rows": "lots"}}, "buffer.flush_rows takes integer, not string"),
        ({"buffer": {"flush_rows": None}}, "buffer.flush_rows does not accept a null"),
        ({"transport": 3}, "transport takes string, not integer"),
    ],
)
def test_a_value_the_app_refuses_is_not_saved(
    mounted, client, admin_headers, api_settings, body, error
):
    resp = client.put("/api/v1/services/loader/bad", json=body, headers=admin_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_config"
    assert error in resp.json()["detail"]
    assert not (Path(api_settings.services.config_yaml_dir) / "loader-bad.yaml").exists()

    checked = client.post("/api/v1/services/loader/bad/validate", json=body, headers=admin_headers)
    assert checked.json() == {"valid": False, "errors": [error]}


def test_with_no_contract_mounted_only_the_stored_keys_are_served(
    client, admin_headers, monkeypatch
):
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, "/nonexistent/contract")
    contract.reload_contracts()
    _stored(client, admin_headers, "loader", "bare", {"buffer": {"flush_rows": 5}})
    served = client.get("/api/v1/services/loader/bare", headers=admin_headers).json()
    assert served["config"] == {"buffer": {"flush_rows": 5}}
    contract.reload_contracts()


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/api/v1/services/loader/bare"),
        ("put", "/api/v1/services/loader/bare"),
        ("post", "/api/v1/services/loader/bare/validate"),
    ],
)
def test_an_unreadable_contract_answers_as_the_contract_routes_do(
    mounted, client, admin_headers, method, url
):
    _stored(client, admin_headers, "loader", "bare")
    (mounted / "dfe-loader" / contract.SCHEMA_FILE).write_text("{not json", encoding="utf-8")
    contract.reload_contracts()
    kwargs = {} if method == "get" else {"json": {}}
    resp = getattr(client, method)(url, headers=admin_headers, **kwargs)
    assert resp.status_code == 500, resp.text
    assert resp.json()["code"] == "contract_unreadable"


class TestARetiredApp:
    """transform-wasm is in no manifest, so it has no service."""

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_its_routes_answer_unknown_service(self, client, admin_headers, method):
        kwargs = {"json": {}} if method == "put" else {}
        resp = getattr(client, method)(
            "/api/v1/services/transform-wasm/default", headers=admin_headers, **kwargs
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "unknown_service"

    def test_validation_refuses_it(self, client, admin_headers):
        resp = client.post(
            "/api/v1/services/transform-wasm/default/validate", json={}, headers=admin_headers
        )
        assert resp.json()["valid"] is False
        assert "Unknown service: transform-wasm" in resp.json()["errors"][0]

    def test_a_stray_file_is_not_listed_and_none_is_seeded(
        self, client, admin_headers, api_settings
    ):
        directory = Path(api_settings.services.config_yaml_dir)
        (directory / "transform-wasm-default.yaml").write_text("{}\n", encoding="utf-8")
        assert client.post("/api/v1/services/seed", headers=admin_headers).status_code == 200
        listed = client.get("/api/v1/services?page_size=100", headers=admin_headers).json()
        services = {item["service"] for item in listed["items"]}
        assert "transform-wasm" not in services
        assert services <= set(APPS)
        written = {p.name for p in Path(api_settings.services.config_yaml_dir).iterdir()}
        assert not {name for name in written if name.startswith("transform-wasm")} - {
            "transform-wasm-default.yaml"
        }
