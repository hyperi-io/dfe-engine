#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_alert_mirror.py
#  Purpose:      Alert destinations mirrored into a Kubernetes Secret the hunt runner mounts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""With DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET set, every destination also lands in that Secret.

The engine writes it through scalo's ``kubernetes`` provider, here against a loopback
stand-in for the API server. The runner mounts it at ``<secrets.path>/alert-destinations``
and reads it with the ``file`` provider, which the tests stand in for by writing the
Secret's keys out as the kubelet would.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from scalo.secrets import SecretsError

from dfe_engine.hunts.alert import AlertDestination, AlertDestinationRegistry
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import HuntsSettings, SecretsSettings, _get_env_overrides
from tests.support.local_kube_api import NAMESPACE, LocalKubeApi, local_kube_api, mirror_for

# The stand-in's service account token: a fixed placeholder, never a credential.
SA_TOKEN = "service-account-token-placeholder"
SECRET = "dfe-alert-destinations"


@pytest.fixture
def token_file(tmp_path) -> Path:
    path = tmp_path / "sa-token"
    path.write_text(SA_TOKEN, encoding="utf-8")
    return path


@pytest.fixture
def kube() -> Iterator[LocalKubeApi]:
    with local_kube_api(SA_TOKEN) as api:
        api.granted.add((NAMESPACE, SECRET))
        yield api


@pytest.fixture
def engine(tmp_path, kube, token_file) -> Iterator[AlertDestinationRegistry]:
    registry = AlertDestinationRegistry(
        secrets=build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "engine"))),
        directory=str(tmp_path / "dests"),
        mirror=mirror_for(kube, token_file, name=SECRET),
    )
    yield registry
    registry.close()


def _mounted_runner(tmp_path, kube) -> AlertDestinationRegistry:
    """The runner's registry over the Secret as the kubelet mounts it, one file per key."""
    mount = tmp_path / "runner" / "alert-destinations"
    mount.mkdir(parents=True, exist_ok=True)
    for stale in mount.iterdir():
        stale.unlink()
    for key, value in kube.data(SECRET).items():
        (mount / key).write_text(value, encoding="utf-8")
    runner_store = SecretsSettings(provider="file", path=str(tmp_path / "runner"))
    return AlertDestinationRegistry(secrets=build_secrets(runner_store))


class TestMirror:
    def test_a_destination_reaches_the_secret_and_a_runner_mounting_it(
        self, tmp_path, kube, engine
    ):
        kube.create_empty(SECRET)

        engine.add(AlertDestination(name="ops", url="slack://t/b/c", description="Ops"))

        assert json.loads(kube.data(SECRET)["ops"]) == {"url": "slack://t/b/c", "enabled": True}
        assert _mounted_runner(tmp_path, kube).resolve("ops") == "slack://t/b/c"

    def test_an_update_and_a_delete_reach_the_runner(self, tmp_path, kube, engine):
        kube.create_empty(SECRET)
        engine.add(AlertDestination(name="ops", url="slack://first"))
        engine.add(AlertDestination(name="keep", url="slack://keep"))

        engine.add(AlertDestination(name="ops", url="slack://second", enabled=False))
        runner = _mounted_runner(tmp_path, kube)
        assert runner.resolve("ops") is None
        assert runner.get("ops").url == "slack://second"

        assert engine.remove("ops") is True
        assert sorted(kube.data(SECRET)) == ["keep"]
        assert "ops" not in _mounted_runner(tmp_path, kube)

    def test_an_unchanged_write_does_not_update_the_secret(self, kube, engine):
        kube.create_empty(SECRET)
        engine.add(AlertDestination(name="ops", url="slack://t"))
        puts = kube.puts()

        engine.add(AlertDestination(name="ops", url="slack://t", description="metadata only"))

        assert kube.puts() == puts

    def test_a_missing_secret_fails_the_write_once_the_backend_holds_it(
        self, tmp_path, kube, engine
    ):
        with pytest.raises(SecretsError, match="does not exist"):
            engine.add(AlertDestination(name="ops", url="slack://t"))

        stored = tmp_path / "engine" / "alert-destinations" / "ops"
        assert json.loads(stored.read_text(encoding="utf-8"))["url"] == "slack://t"
        assert kube.puts() == 0

    def test_a_secret_the_role_does_not_name_is_refused(self, tmp_path, kube, token_file):
        kube.create_empty("someone-elses-secret")
        registry = AlertDestinationRegistry(
            secrets=build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "e"))),
            directory=str(tmp_path / "d"),
            mirror=mirror_for(kube, token_file, name="someone-elses-secret"),
        )
        try:
            with pytest.raises(SecretsError):
                registry.add(AlertDestination(name="ops", url="slack://t"))
        finally:
            registry.close()

    def test_with_no_mirror_the_api_server_is_never_called(self, tmp_path, kube):
        kube.create_empty(SECRET)
        registry = AlertDestinationRegistry(
            secrets=build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "e"))),
            directory=str(tmp_path / "d"),
        )
        try:
            registry.add(AlertDestination(name="ops", url="slack://t"))
            registry.remove("ops")
            assert registry.sync_mirror() == []
        finally:
            registry.close()
        assert kube.requests == []

    def test_a_mirror_with_no_secrets_backend_is_refused(self, kube, token_file):
        with pytest.raises(ValueError, match="needs a secrets backend"):
            AlertDestinationRegistry(mirror=mirror_for(kube, token_file, name=SECRET))


class TestSyncMirror:
    def test_a_secret_recreated_empty_is_refilled(self, tmp_path, kube, engine):
        kube.create_empty(SECRET)
        engine.add(AlertDestination(name="ops", url="slack://t"))
        engine.add(AlertDestination(name="pd", url="pagerduty://k"))
        kube.create_empty(SECRET)

        assert engine.sync_mirror() == ["ops", "pd"]
        assert _mounted_runner(tmp_path, kube).resolve_many(["ops", "pd"]) == [
            "slack://t",
            "pagerduty://k",
        ]
        assert engine.sync_mirror() == []

    def test_a_stored_value_that_is_no_destination_is_a_secrets_error(self, tmp_path, kube, engine):
        # Startup catches SecretsError, so a corrupted secret is logged and the engine still boots.
        kube.create_empty(SECRET)
        engine.add(AlertDestination(name="ops", url="slack://t"))
        (tmp_path / "engine" / "alert-destinations" / "ops").write_text(
            "not json", encoding="utf-8"
        )

        with pytest.raises(SecretsError, match="no readable destination"):
            engine.sync_mirror()

    def test_a_key_no_destination_has_is_removed(self, kube, engine, token_file):
        kube.create_empty(SECRET)
        engine.add(AlertDestination(name="ops", url="slack://t"))
        engine.remove("ops")
        engine.add(AlertDestination(name="kept", url="slack://k"))
        mirror_for(kube, token_file, name=SECRET).create_sync("stray", b"{}", provider="kubernetes")

        assert engine.sync_mirror() == ["stray"]
        assert sorted(kube.data(SECRET)) == ["kept"]


class TestSecretsDirectory:
    def test_the_engine_creates_the_directory_a_runner_mounts_before_any_write(self, tmp_path):
        store = SecretsSettings(provider="file", path=str(tmp_path / "secrets"))
        registry = AlertDestinationRegistry(
            secrets=build_secrets(store), directory=str(tmp_path / "dests")
        )
        registry.close()

        directory = tmp_path / "secrets" / "alert-destinations"
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
        assert list(directory.iterdir()) == []

    def test_the_runner_creates_nothing(self, tmp_path):
        store = SecretsSettings(provider="file", path=str(tmp_path / "secrets"))
        AlertDestinationRegistry(secrets=build_secrets(store))

        assert not (tmp_path / "secrets").exists()


class TestSetting:
    def test_the_environment_names_the_secret(self, monkeypatch):
        monkeypatch.setenv("DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET", SECRET)

        assert _get_env_overrides()["hunts"]["alert_destinations_secret"] == SECRET

    def test_unset_by_default(self):
        assert HuntsSettings().alert_destinations_secret == ""

    @pytest.mark.parametrize("bad", ["Upper", "under_score", "-lead", "x" * 254])
    def test_a_name_the_api_server_would_refuse_is_refused(self, bad):
        with pytest.raises(ValueError, match="DFE_ENGINE_HUNTS_ALERT_DESTINATIONS_SECRET"):
            HuntsSettings(alert_destinations_secret=bad)
