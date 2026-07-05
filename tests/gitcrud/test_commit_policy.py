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


def test_build_message_budget_truncates_long_subject():
    # the verified 53-char case ('cfg(receiver-default): set clickhouse_max_connections')
    # must fit 50, stay conforming, and keep a non-empty summary - NOT raise (500).
    ctx = CommitContext(
        ctype="cfg",
        scope="receiver-default",
        summary="set clickhouse_max_connections",
        actor="derek",
    )
    subject = build_message(ctx).splitlines()[0]
    assert len(subject) <= 50
    assert subject.startswith("cfg(receiver-default): set clickhouse")
    validate_subject(subject)  # still conforming (type + ascii + length)


def test_build_message_trims_scope_when_needed_keeps_summary():
    ctx = CommitContext(ctype="cfg", scope="x" * 80, summary="set y", actor="a")
    subject = build_message(ctx).splitlines()[0]
    assert len(subject) <= 50
    # summary is never dropped to empty (read_log's subject regex needs it)
    assert subject.endswith("set y") or subject.split(": ", 1)[1]


def test_build_message_still_raises_on_non_ascii():
    # a genuinely invalid message (non-ASCII) is a hard error the API maps to 422,
    # NOT something truncation papers over.
    with pytest.raises(CommitPolicyError):
        build_message(CommitContext(ctype="cfg", scope="x", summary="use an em-dash —", actor="a"))


def test_non_ascii_subject_rejected():
    with pytest.raises(CommitPolicyError):
        validate_subject("cfg(x): use an em—dash")


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
