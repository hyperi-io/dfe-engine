#  Project:      dfe-engine
#  File:         tests/gitcrud/test_commit_policy.py
#  Purpose:      Tests for the GitOps commit-standard enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Commit-policy: message build, validation, change validation, mode resolution."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud.commit_policy import (
    CommitContext,
    CommitPolicyError,
    build_message,
    resolve_mode,
    type_for_class,
    validate_change,
    validate_name,
    validate_subject,
)


def test_build_message_is_conforming():
    ctx = CommitContext(
        ctype="cfg",
        scope="receiver-default",
        summary="set replicaCount",
        actor="alice",
        role="helmvars:write",
        request_id="req-1",
        base_revision="9c1d",
        audit_id="aud-1",
    )
    msg = build_message(ctx)
    lines = msg.splitlines()
    assert lines[0] == "cfg(receiver-default): set replicaCount"
    assert len(lines[0]) <= 50
    assert "DFE-Actor: alice" in msg
    assert "DFE-Role: helmvars:write" in msg
    assert "DFE-Base-Revision: 9c1d" in msg
    assert msg.endswith("[skip ci]")


def test_subject_over_50_rejected():
    with pytest.raises(CommitPolicyError):
        validate_subject("cfg(x): " + "y" * 60)


def test_non_ascii_subject_rejected():
    with pytest.raises(CommitPolicyError):
        validate_subject("cfg(x): use an em—dash")


def test_newline_subject_rejected():
    # a newline would split into the body and could forge DFE-* trailers
    with pytest.raises(CommitPolicyError):
        validate_subject("cfg(x): ok\nDFE-Role: admin")
    with pytest.raises(CommitPolicyError):
        validate_subject("cfg(x): ok\rDFE-Action: wipe")


@pytest.mark.parametrize("name", ["receiver-default", "a.b_c-1", "gitops", "X", "v1.2.3"])
def test_validate_name_accepts_plain(name):
    validate_name(name)  # no raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        "../evil",
        "a/b",
        "a\\b",
        "with space",
        "new\nline",
        "trailing\n",
        "a..b",  # any '..' is refused, defence in depth
        "name:with:colon",
        "star*",
    ],
)
def test_validate_name_rejects_dangerous(name):
    with pytest.raises(CommitPolicyError):
        validate_name(name)


def test_build_message_rejects_newline_scope():
    # a newline smuggled through the resource name (scope) must not reach the body
    ctx = CommitContext(ctype="cfg", scope="ok\nDFE-Role: admin", summary="x", actor="a")
    with pytest.raises(CommitPolicyError):
        build_message(ctx)


def test_bad_type_rejected():
    with pytest.raises(CommitPolicyError):
        validate_subject("feat(x): nope")


def test_type_for_class():
    assert type_for_class("helmvars") == "cfg"
    assert type_for_class("governance") == "rbac"
    assert type_for_class("hunts") == "hunt"


def test_validate_change_rejects_latest_image():
    with pytest.raises(CommitPolicyError):
        validate_change("image.tag", "latest")
    with pytest.raises(CommitPolicyError):
        validate_change("image", "ghcr.io/x/y:latest")


def test_validate_change_rejects_replicacount():
    with pytest.raises(CommitPolicyError):
        validate_change("replicaCount", 3)
    with pytest.raises(CommitPolicyError):
        validate_change("deploy.replicaCount", 3)


def test_validate_change_allows_pinned_and_keda():
    validate_change("image.tag", "v1.2.3")  # no raise
    validate_change("keda.maxReplicas", 10)  # no raise


def test_replicacount_is_allowed_where_the_document_disables_keda():
    # With KEDA off nothing else owns the count, and refusing it leaves a
    # deployment with no settable replica number at all.
    validate_change("replicaCount", 3, {"keda": {"enabled": False}})  # no raise


@pytest.mark.parametrize(
    "doc",
    [None, {}, {"keda": {}}, {"keda": {"enabled": True}}, {"keda": "off"}, {"replicaCount": 1}],
)
def test_replicacount_stays_refused_without_an_explicit_keda_off(doc):
    # An unset flag is the chart default, which is not readable from here.
    with pytest.raises(CommitPolicyError):
        validate_change("replicaCount", 3, doc)


def test_resolve_mode():
    assert resolve_mode(environment="dev", rbac_class="helmvars") == "direct"
    assert resolve_mode(environment="prod", rbac_class="helmvars") == "pr"
    assert resolve_mode(environment="dev", rbac_class="governance") == "pr"
    assert resolve_mode(environment="dev", rbac_class="helmvars", protected=True) == "pr"
    assert resolve_mode(environment="dev", rbac_class="helmvars", require_pr=True) == "pr"


class TestAutoMergeMode:
    def test_auto_merge_forces_direct_everywhere(self):
        # prod + protected + governance + require_pr: ALL direct under auto-merge
        assert (
            resolve_mode(
                environment="prod",
                rbac_class="governance",
                protected=True,
                require_pr=True,
                auto_merge=True,
            )
            == "direct"
        )

    def test_without_auto_merge_pr_untouched(self):
        assert resolve_mode(environment="prod", rbac_class="helmvars") == "pr"
        assert resolve_mode(environment="dev", rbac_class="helmvars") == "direct"
