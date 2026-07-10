"""Error taxonomy, did-you-mean, and no-args help behaviour."""

from __future__ import annotations


def test_unknown_command_did_you_mean(harness):
    result = harness.invoke(["orgz"])
    assert result.exit_code == 2
    assert "Did you mean 'orgs'?" in result.output


def test_404_maps_to_api_exit_code(harness):
    harness.seed_admin()
    result = harness.invoke(["orgs", "describe", "does-not-exist"])
    assert result.exit_code == 254
    assert "Error (" in result.output
    assert "not_found" in result.output or "not found" in result.output


def test_missing_required_arg_is_usage_error(harness):
    harness.seed_admin()
    # create requires --name.
    result = harness.invoke(["orgs", "create"])
    assert result.exit_code == 2
    assert "Missing option" in result.output or "Error" in result.output


def test_missing_path_argument_is_usage_error(harness):
    harness.seed_admin()
    result = harness.invoke(["orgs", "describe"])
    assert result.exit_code == 2


def test_no_credentials_is_config_exit_code(harness):
    # No seed_admin, no login -> no url/credential resolved.
    result = harness.invoke(["orgs", "list"])
    assert result.exit_code == 253
    assert "URL" in result.output or "login" in result.output


def test_bare_dfe_prints_help_exit_zero(harness):
    result = harness.invoke([])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_bare_group_prints_help_exit_zero(harness):
    result = harness.invoke(["orgs"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    # Lists the generated subcommands.
    assert "list" in result.output
    assert "describe" in result.output
