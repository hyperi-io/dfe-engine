"""login/logout/auth/config built-ins + credential resolution."""

from __future__ import annotations

import json
import stat

from dfe_engine.cli.auto.config import Credential, Store


def test_credentials_file_is_0600_at_creation(tmp_path):
    # The FIRST write must land the file already at 0600 (never briefly wider).
    store = Store(home=tmp_path / "home")
    store.save_credential(Credential(account="alice", token="tok"))
    path = store.credentials_file
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"
    # And the enclosing dirs are owner-only (0700).
    assert oct(store.home.stat().st_mode)[-3:] == "700"
    assert oct(store.configurations_dir.stat().st_mode)[-3:] == "700"


def test_login_bad_credentials_friendly_error(harness):
    # Wrong password -> 401 rendered as a friendly Error line + API exit code,
    # not a raw httpx traceback (exit 1).
    result = harness.invoke(
        ["login", "--url", "http://test", "--username", "admin", "--password", "WRONG"]
    )
    assert result.exit_code == 254, result.output
    assert "Error (" in result.output
    # No credential was stored for the failed login.
    assert harness.store.list_accounts() == []


def test_login_with_api_key_writes_creds_and_sends_header(harness):
    result = harness.invoke(["login", "--url", "http://test", "--api-key", harness.admin_api_key])
    assert result.exit_code == 0, result.output
    assert "You are now logged in" in result.output

    creds_file = harness.store.credentials_file
    assert creds_file.exists()
    stored = json.loads(creds_file.read_text())
    assert any(entry.get("api_key") == harness.admin_api_key for entry in stored.values())
    # chmod 0600 on the credential file.
    mode = stat.S_IMODE(creds_file.stat().st_mode)
    assert mode == 0o600

    # A generated call now carries the X-API-Key header AND is authorised (admin).
    listed = harness.invoke(["orgs", "list"])
    assert listed.exit_code == 0, listed.output
    assert any("x-api-key" in dict(r.headers) for r in harness.requests)


def test_login_with_password_exchanges_for_token(harness):
    result = harness.invoke(
        [
            "login",
            "--url",
            "http://test",
            "--username",
            "admin",
            "--password",
            "test-admin-pw",
        ]
    )
    assert result.exit_code == 0, result.output
    stored = json.loads(harness.store.credentials_file.read_text())
    assert "admin" in stored
    assert stored["admin"].get("token")
    # The stored token authorises a generated call over Bearer.
    listed = harness.invoke(["orgs", "list"])
    assert listed.exit_code == 0, listed.output
    assert any(
        dict(r.headers).get("authorization", "").startswith("Bearer ") for r in harness.requests
    )


def test_auth_list_marks_active(harness):
    harness.invoke(["login", "--url", "http://test", "--api-key", harness.admin_api_key])
    result = harness.invoke(["auth", "list"])
    assert result.exit_code == 0, result.output
    assert "(active)" in result.output


def test_logout_clears_credentials(harness):
    harness.invoke(["login", "--url", "http://test", "--api-key", harness.admin_api_key])
    result = harness.invoke(["logout"])
    assert result.exit_code == 0, result.output
    # No stored credentials remain.
    assert harness.store.list_accounts() == []


def test_config_set_get_list(harness):
    assert harness.invoke(["config", "set", "url", "http://x:8000"]).exit_code == 0
    got = harness.invoke(["config", "get", "url"])
    assert got.exit_code == 0
    assert got.output.strip() == "http://x:8000"
    listed = harness.invoke(["config", "list"])
    assert "url = http://x:8000" in listed.output


def test_config_configurations_lifecycle(harness):
    assert harness.invoke(["config", "configurations", "create", "prod"]).exit_code == 0
    assert harness.invoke(["config", "configurations", "activate", "prod"]).exit_code == 0
    listed = harness.invoke(["config", "configurations", "list"])
    assert "prod (active)" in listed.output
    assert harness.invoke(["config", "configurations", "delete", "prod"]).exit_code == 0


def test_print_access_token(harness):
    harness.invoke(
        [
            "login",
            "--url",
            "http://test",
            "--username",
            "admin",
            "--password",
            "test-admin-pw",
        ]
    )
    result = harness.invoke(["auth", "print-access-token"])
    assert result.exit_code == 0, result.output
    assert result.output.strip()  # a raw token line


def test_credential_host_mismatch_warns(harness):
    """Sending a stored credential to a --url whose host differs from the profile
    URL host emits a stderr warning (warn-only - the call still proceeds). This is
    the credential-exfil guard: a poisoned DFE_API_URL / stray --url would otherwise
    ship the active token to another origin silently."""
    harness.invoke(["login", "--url", "http://test", "--api-key", harness.admin_api_key])
    result = harness.invoke(["--url", "http://evil.example", "orgs", "list"])
    assert result.exit_code == 0, result.output  # warn-only, does not block
    assert "warning: sending" in result.output
    assert "evil.example" in result.output
    assert "differs from the profile URL host test" in result.output


def test_credential_same_host_no_warning(harness):
    """No warning when the effective host matches the profile URL host (no false
    positive on the normal path)."""
    harness.invoke(["login", "--url", "http://test", "--api-key", harness.admin_api_key])
    result = harness.invoke(["orgs", "list"])
    assert result.exit_code == 0, result.output
    assert "warning: sending" not in result.output
