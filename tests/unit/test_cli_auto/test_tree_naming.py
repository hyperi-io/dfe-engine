"""Tree build + verb-naming derivation from the live spec."""

from __future__ import annotations

import click
import pytest

from dfe_engine.cli.auto.naming import derive_group_and_verb
from dfe_engine.cli.auto.spec import iter_operations, load_live_spec


def test_orgs_group_has_crud_commands(harness):
    orgs = harness.root.commands["orgs"]
    assert isinstance(orgs, click.Group)
    assert set(orgs.commands) >= {"list", "describe", "create", "update", "delete"}


@pytest.mark.xfail(
    reason="route-level x-cli:false markers not yet ported to main (the repository "
    "UI-prefs endpoints should opt out of the CLI). The cli_enabled mechanism is "
    "covered by test_spec_units; this end-to-end assertion flips to pass once the "
    "x-cli route markers land - auto-CLI route-refinement follow-up.",
    strict=False,
)
def test_hidden_operation_absent(harness):
    # /api/v1/repository/* is x-cli hidden - no `repository` group at all.
    assert "repository" not in harness.root.commands


def test_group_nesting_auth_accounts(harness):
    auth = harness.root.commands["auth"]
    assert isinstance(auth, click.Group)
    accounts = auth.commands["accounts"]
    assert isinstance(accounts, click.Group)
    assert set(accounts.commands) >= {"list", "describe", "create", "update", "delete"}


def test_builtin_auth_merged_not_replacing_generated(harness):
    auth = harness.root.commands["auth"]
    # Built-in commands live alongside the generated sub-groups.
    assert "list" in auth.commands  # built-in: list stored credentials
    assert "print-access-token" in auth.commands
    assert "accounts" in auth.commands  # generated sub-group survived the merge


def test_verb_derivation_rules():
    cols = {"orgs", "auth/accounts"}
    assert derive_group_and_verb("/api/v1/orgs", "get", cols) == (["orgs"], "list")
    assert derive_group_and_verb("/api/v1/orgs/{name}", "get", cols) == (
        ["orgs"],
        "describe",
    )
    assert derive_group_and_verb("/api/v1/orgs", "post", cols) == (["orgs"], "create")
    assert derive_group_and_verb("/api/v1/orgs/{name}", "put", cols) == (
        ["orgs"],
        "update",
    )
    assert derive_group_and_verb("/api/v1/orgs/{name}", "delete", cols) == (
        ["orgs"],
        "delete",
    )
    # Action segment becomes the verb; group is the resource before it.
    assert derive_group_and_verb(
        "/api/v1/auth/accounts/{username}/reset-password", "post", cols
    ) == (["auth", "accounts"], "reset-password")


def test_paginated_list_suppresses_spec_page_params(harness):
    # `orgs list` is paginated: the paginator owns paging via the injected
    # --page-size / --limit. The spec's own --page / --per-page MUST be suppressed
    # so `list --page 2` cannot silently drop earlier pages while auto-follow makes
    # the output look complete (the silent-data-loss bug).
    orgs = harness.root.commands["orgs"]
    list_cmd = orgs.commands["list"]
    opt_names = {n for p in list_cmd.params for n in p.opts if n.startswith("--")}
    assert "--page" not in opt_names
    assert "--per-page" not in opt_names
    # The injected paging knobs are still present.
    assert "--page-size" in opt_names
    assert "--limit" in opt_names


def test_every_exposed_operation_has_a_command(harness):
    ops = iter_operations(load_live_spec(settings=harness.settings))
    for op in ops:
        node = harness.root
        for segment in op.group_path:
            node = node.commands[segment]
        assert op.verb in node.commands or f"{op.verb}-{op.method}" in node.commands
