"""Tests for the alerts router — alert destination CRUD."""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import _registries
from dfe_engine.appmgmt.contract import REDACTED
from tests.support.accounts import admin_on_its_own_password


@pytest.fixture
def app_with_alerts(tmp_path):
    """App fixture with alert destinations store initialized."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.settings import (
        APISettings,
        AuthSettings,
        DFESettings,
        HuntsSettings,
        ServicesSettings,
        SourceSettings,
    )

    alert_dir = tmp_path / "alert-destinations"
    alert_dir.mkdir()
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()

    settings = DFESettings(
        env="test",
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        hunts=HuntsSettings(
            alert_destinations_dir=str(alert_dir),
            hunt_dir=str(hunts_dir),
        ),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
        ),
        api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes"),
    )
    for d in ["sources", "services"]:
        (tmp_path / d).mkdir(exist_ok=True)

    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def alert_client(app_with_alerts):
    with TestClient(app_with_alerts, raise_server_exceptions=False) as c:
        admin_on_its_own_password(app_with_alerts)
        yield c


@pytest.fixture
def alert_admin_headers(app_with_alerts):
    from dfe_engine.api.deps import create_access_token
    from dfe_engine.settings import APISettings, DFESettings

    settings = DFESettings(
        env="test", api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes")
    )
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_destination():
    return {
        "name": "slack-alerts",
        "url": "slack://T00000000/B00000000/X0000000000000000000000/",
        "description": "Slack channel for DFE alerts",
        "enabled": True,
    }


class TestAlertDestinationsBootstrap:
    def test_bootstrap_creates_missing_directory(self, tmp_path):
        from dfe_engine.api.deps import _registries, bootstrap_registries, shutdown_registries
        from dfe_engine.settings import DFESettings, HuntsSettings

        dest_dir = tmp_path / "config" / "alert-destinations"
        assert not dest_dir.exists()
        settings = DFESettings(
            env="test", hunts=HuntsSettings(alert_destinations_dir=str(dest_dir))
        )
        try:
            bootstrap_registries(settings)
            assert dest_dir.is_dir()
            assert "alert_destinations" in _registries
        finally:
            shutdown_registries()

    def test_api_created_destination_is_hunt_engine_readable(
        self, alert_admin_headers, alert_client, sample_destination, tmp_path
    ):
        from dfe_engine.hunts.alert import AlertDestinationRegistry

        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 201

        # A fresh registry (as the hunt engine builds it) must resolve the
        # API-written file — proving both layers share one on-disk format.
        hunt_engine_registry = AlertDestinationRegistry(
            directory=str(tmp_path / "alert-destinations")
        )
        try:
            assert hunt_engine_registry.resolve("slack-alerts") == sample_destination["url"]
        finally:
            hunt_engine_registry.close()


class TestAlertDestinationsList:
    def test_list_empty(self, alert_client, alert_admin_headers):
        resp = alert_client.get("/api/v1/alerts/destinations", headers=alert_admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_requires_auth(self, alert_client):
        resp = alert_client.get("/api/v1/alerts/destinations")
        assert resp.status_code == 401


class TestAlertDestinationsCRUD:
    def test_create_with_hunt_name_links_hunt(
        self, alert_client, alert_admin_headers, sample_destination
    ):
        from dfe_engine.api.deps import _registries

        hunt_registry = _registries["hunt_configs"]
        hunt_registry.save(
            "owned_hunt",
            {
                "display_name": "Owned",
                "cron": "* * * * *",
                "log_buffer": 60,
                "global_target_table_name": "logs_alerts",
                "customers": ["org_a"],
                "rules": [{"rule_name": "r1"}],
            },
        )

        payload = {**sample_destination, "hunt_name": "owned_hunt"}
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=payload,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["hunt_name"] == "owned_hunt"

        hunt = hunt_registry.get("owned_hunt")
        assert "slack-alerts" in hunt["alerts"]["destinations"]

        filtered = alert_client.get(
            "/api/v1/alerts/destinations",
            params={"hunt": "owned_hunt"},
            headers=alert_admin_headers,
        )
        assert filtered.json()["total"] == 1

        assert (
            alert_client.delete("/api/v1/hunts/owned_hunt", headers=alert_admin_headers).status_code
            == 204
        )

    def test_create_with_unknown_hunt_returns_404(
        self, alert_client, alert_admin_headers, sample_destination
    ):
        payload = {**sample_destination, "hunt_name": "no_such_hunt"}
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=payload,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_delete_hunt_deletes_owned_destinations(self, alert_client, alert_admin_headers):
        from dfe_engine.api.deps import _registries

        hunt_registry = _registries["hunt_configs"]
        hunt_registry.save(
            "cascade_hunt",
            {
                "display_name": "Cascade",
                "cron": "* * * * *",
                "log_buffer": 60,
                "global_target_table_name": "logs_alerts",
                "customers": ["org_a"],
                "rules": [{"rule_name": "r1"}],
            },
        )

        dest = {
            "name": "hunt-owned-slack",
            "url": "slack://T00000000/B00000000/X0000000000000000000000/",
            "hunt_name": "cascade_hunt",
        }
        assert (
            alert_client.post(
                "/api/v1/alerts/destinations", json=dest, headers=alert_admin_headers
            ).status_code
            == 201
        )

        assert (
            alert_client.delete(
                "/api/v1/hunts/cascade_hunt", headers=alert_admin_headers
            ).status_code
            == 204
        )

        assert (
            alert_client.get(
                "/api/v1/alerts/destinations/hunt-owned-slack", headers=alert_admin_headers
            ).status_code
            == 404
        )

    def test_delete_hunt_keeps_destination_shared_across_hunts(
        self, alert_client, alert_admin_headers
    ):
        from dfe_engine.api.deps import _registries

        hunt_registry = _registries["hunt_configs"]
        for hunt_name in ("hunt_a", "hunt_b"):
            hunt_registry.save(
                hunt_name,
                {
                    "display_name": hunt_name,
                    "cron": "* * * * *",
                    "log_buffer": 60,
                    "global_target_table_name": "logs_alerts",
                    "customers": ["org_a"],
                    "rules": [{"rule_name": "r1"}],
                    "alerts": {"destinations": ["shared-slack"]},
                },
            )

        dest = {
            "name": "shared-slack",
            "url": "slack://T00000000/B00000000/X0000000000000000000000/",
            "hunt_name": "hunt_a",
        }
        assert (
            alert_client.post(
                "/api/v1/alerts/destinations", json=dest, headers=alert_admin_headers
            ).status_code
            == 201
        )

        assert (
            alert_client.delete("/api/v1/hunts/hunt_a", headers=alert_admin_headers).status_code
            == 204
        )

        assert (
            alert_client.get(
                "/api/v1/alerts/destinations/shared-slack", headers=alert_admin_headers
            ).status_code
            == 200
        )
        assert (
            alert_client.get(
                "/api/v1/alerts/destinations/shared-slack", headers=alert_admin_headers
            )
            .json()
            .get("hunt_name")
            is None
        )

        hunt_registry.delete("hunt_b")

    def test_create_destination(self, alert_client, alert_admin_headers, sample_destination):
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "slack-alerts"
        assert data["url"] == REDACTED
        assert data["url_scheme"] == "slack"
        assert data["enabled"] is True

    def test_create_then_list(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.get("/api/v1/alerts/destinations", headers=alert_admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "slack-alerts"
        assert data["items"][0]["url_scheme"] == "slack"

    def test_list_filter_by_hunt(self, alert_client, alert_admin_headers, sample_destination):
        from dfe_engine.api.deps import _registries

        other = {
            "name": "pagerduty-oncall",
            "url": "pagerduty://integration-key",
            "description": "On-call",
            "enabled": True,
        }
        for dest in (sample_destination, other):
            alert_client.post(
                "/api/v1/alerts/destinations",
                json=dest,
                headers=alert_admin_headers,
            )

        hunt_registry = _registries["hunt_configs"]
        hunt_registry.save(
            "alert_filter_hunt",
            {
                "display_name": "Alert filter hunt",
                "cron": "* * * * *",
                "log_buffer": 60,
                "global_target_table_name": "logs_alerts",
                "customers": ["org_a"],
                "rules": [{"rule_name": "some_rule"}],
                "alerts": {"destinations": ["slack-alerts"]},
            },
        )

        resp = alert_client.get(
            "/api/v1/alerts/destinations",
            params={"hunt": "alert_filter_hunt"},
            headers=alert_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "slack-alerts"

        hunt_registry.delete("alert_filter_hunt")

    def test_list_filter_by_hunt_not_found(self, alert_client, alert_admin_headers):
        resp = alert_client.get(
            "/api/v1/alerts/destinations",
            params={"hunt": "missing_hunt"},
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.get(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "slack-alerts"

    def test_get_not_found(self, alert_client, alert_admin_headers):
        resp = alert_client.get(
            "/api/v1/alerts/destinations/nonexistent",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_update_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        updated = {**sample_destination, "description": "Updated description", "enabled": False}
        resp = alert_client.put(
            "/api/v1/alerts/destinations/slack-alerts",
            json=updated,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"
        assert resp.json()["enabled"] is False

    def test_delete_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.delete(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 204

        # Verify gone
        resp = alert_client.get(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404

    def test_duplicate_create(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_viewer_read_allowed(self, alert_client, alert_admin_headers):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(
            env="test", api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes")
        )
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}

        resp = alert_client.get("/api/v1/alerts/destinations", headers=viewer_headers)
        assert resp.status_code == 403

    def test_viewer_write_forbidden(self, alert_client, sample_destination):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(
            env="test", api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes")
        )
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}

        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestTheDestinationUrlIsACredential:
    """An Apprise URL carries the credential for its service, so no read shows one."""

    SECRET_URL = "slack://T00000000/B00000000/X0000000000000000000000/"

    @pytest.fixture
    def analyst_headers(self, app_with_alerts, alert_client):
        """A session holding data_analyst_viewer, the read-only role that reaches alert:read."""
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        groups = app_with_alerts.state.group_store
        if groups.get("dfe-analyst-viewers") is None:
            groups.create("dfe-analyst-viewers", roles=["data_analyst_viewer"])
        app_with_alerts.state.account_store.create(
            "analyst", "analyst-pw-4901", groups=["dfe-analyst-viewers"]
        )
        groups.add_member("dfe-analyst-viewers", "analyst")
        token = create_access_token(
            data={"sub": "analyst", "org_id": "test-org"},
            settings=DFESettings(
                env="test", api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes")
            ),
        )
        return {"Authorization": f"Bearer {token}"}

    @pytest.fixture
    def stored(self, alert_client, alert_admin_headers):
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json={"name": "slack-alerts", "url": self.SECRET_URL, "enabled": True},
            headers=alert_admin_headers,
        )
        assert resp.status_code == 201, resp.text
        return "slack-alerts"

    def test_the_analyst_role_can_read_a_destination(self, alert_client, analyst_headers, stored):
        """The role this masking exists for: it reads the surface, so the mask is the control."""
        resp = alert_client.get(f"/api/v1/alerts/destinations/{stored}", headers=analyst_headers)

        assert resp.status_code == 200, resp.text

    def test_a_read_shows_the_scheme_and_masks_the_url(self, alert_client, analyst_headers, stored):
        resp = alert_client.get(f"/api/v1/alerts/destinations/{stored}", headers=analyst_headers)

        assert resp.json()["url"] == REDACTED
        assert resp.json()["url_scheme"] == "slack"
        assert self.SECRET_URL not in resp.text

    def test_the_listing_carries_no_url(self, alert_client, analyst_headers, stored):
        resp = alert_client.get("/api/v1/alerts/destinations", headers=analyst_headers)

        assert resp.status_code == 200, resp.text
        assert self.SECRET_URL not in resp.text

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_sorting_by_url_cannot_order_on_the_credential(
        self, alert_client, alert_admin_headers, analyst_headers, order
    ):
        """URL order matches name order in neither direction, so ordering on the URL would show."""
        urls = {
            "a-dest": "slack://mmm-4903",
            "b-dest": "slack://zzz-4904",
            "c-dest": "slack://aaa-4905",
        }
        for name, url in urls.items():
            created = alert_client.post(
                "/api/v1/alerts/destinations",
                json={"name": name, "url": url, "enabled": True},
                headers=alert_admin_headers,
            )
            assert created.status_code == 201, created.text

        resp = alert_client.get(
            "/api/v1/alerts/destinations",
            params={"sort_by": "url", "sort_order": order},
            headers=analyst_headers,
        )

        assert resp.status_code == 200, resp.text
        assert [item["name"] for item in resp.json()["items"]] == ["a-dest", "b-dest", "c-dest"]
        assert not any(url in resp.text for url in urls.values())

    def test_the_mask_written_back_keeps_the_stored_url(
        self, alert_client, alert_admin_headers, stored
    ):
        resp = alert_client.put(
            f"/api/v1/alerts/destinations/{stored}",
            json={"name": stored, "url": REDACTED, "description": "renamed", "enabled": False},
            headers=alert_admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "renamed"
        assert resp.json()["url"] == REDACTED
        registry = _registries["alert_destinations"]
        assert registry.get(stored).url == self.SECRET_URL

    def test_a_new_url_written_in_full_replaces_it(self, alert_client, alert_admin_headers, stored):
        resp = alert_client.put(
            f"/api/v1/alerts/destinations/{stored}",
            json={"name": stored, "url": "pagerduty://key-4902", "enabled": True},
            headers=alert_admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == REDACTED
        registry = _registries["alert_destinations"]
        assert registry.get(stored).url == "pagerduty://key-4902"

    def test_creating_one_from_the_mask_is_refused(self, alert_client, alert_admin_headers):
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json={"name": "from-a-mask", "url": REDACTED, "enabled": True},
            headers=alert_admin_headers,
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert (
            alert_client.get(
                "/api/v1/alerts/destinations/from-a-mask", headers=alert_admin_headers
            ).status_code
            == 404
        )

    def test_the_hunt_engine_still_resolves_the_real_url(self, stored):
        """The mask is on the API read alone -- what dispatches an alert reads the store."""
        registry = _registries["alert_destinations"]

        assert registry.resolve(stored) == self.SECRET_URL

    def test_a_guess_around_a_masked_password_is_answered_the_same_way(
        self, alert_client, alert_admin_headers
    ):
        """Every read shows the URL whole as the mask, so its host and user are never shown."""
        url = "json://svc-4906:pw-4907@hooks.example/notify"
        created = alert_client.post(
            "/api/v1/alerts/destinations",
            json={"name": "json-hook", "url": url, "enabled": True},
            headers=alert_admin_headers,
        )
        assert created.status_code == 201, created.text

        answers = []
        for guess in ("svc-4906@hooks.example", "svc-wrong@elsewhere.example"):
            user, host = guess.split("@")
            resp = alert_client.put(
                "/api/v1/alerts/destinations/json-hook",
                json={"name": "json-hook", "url": f"json://{user}:{REDACTED}@{host}/notify"},
                headers=alert_admin_headers,
            )
            answers.append((resp.status_code, resp.json().get("code")))

        assert answers[0] == answers[1] == (400, "credential_reentry_required")
        assert _registries["alert_destinations"].get("json-hook").url == url


class TestDestinationNameContainment:
    """The name is the destination's file name, so a create must not place it anywhere else."""

    @pytest.mark.parametrize("name", ["../escape", "nested/escape", "..", ""])
    def test_a_name_that_is_not_one_file_name_is_refused(
        self, alert_client, alert_admin_headers, sample_destination, tmp_path, name
    ):
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json={**sample_destination, "name": name},
            headers=alert_admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"
        assert not (tmp_path / "escape.yaml").exists()
        assert list((tmp_path / "alert-destinations").iterdir()) == []

    def test_an_absolute_name_writes_nothing_outside_the_directory(
        self, alert_client, alert_admin_headers, sample_destination, tmp_path
    ):
        outside = tmp_path / "outside" / "escape"
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json={**sample_destination, "name": str(outside)},
            headers=alert_admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert not (tmp_path / "outside").exists()

    def test_a_dotted_path_name_reads_as_not_found(self, alert_client, alert_admin_headers):
        resp = alert_client.get("/api/v1/alerts/destinations/%2E%2E", headers=alert_admin_headers)

        assert resp.status_code == 404, resp.text
