#  Project:      dfe-engine
#  File:         tests/gitcrud/test_commit_policy.py
#  Purpose:      Tests for the GitOps commit-standard enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Commit-policy: message build, validation, change validation, mode resolution."""

import re

import pytest

from dfe_engine.appmgmt.catalogue import KEDA_ENABLED_PATH, KEDA_MAX_PATH, KEDA_MIN_PATH
from dfe_engine.gitcrud import commit_policy, flatten
from dfe_engine.gitcrud.commit_policy import (
    RULE_KEDA_REPLICAS,
    CommitContext,
    CommitPolicyError,
    build_message,
    resolve_mode,
    type_for_class,
    validate_name,
    validate_subject,
)


def _write(path: str, value: object, doc: dict | None = None) -> None:
    """A write of ``value`` at ``path`` into ``doc`` on a chart that runs KEDA by default."""
    commit_policy.validate_write(doc or {}, path, value, keda_by_default=True)


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


def test_a_per_instance_overlay_name_still_builds_a_subject():
    # This scope plus "set repository" is 53 characters, so
    # build_message raised and the helm-var write surfaced as a 500. Every
    # multi-instance app hits it, because its overlay is {service}-{instance}-values.
    ctx = CommitContext(
        ctype="cfg",
        scope="dfe-transform-vector-filebeat-values",
        summary="set repository",
        actor="admin",
        role="helmvars:write",
    )
    subject = build_message(ctx).splitlines()[0]
    assert len(subject) <= 50
    assert subject.startswith("cfg(dfe-transform-vector-filebeat-values): ")


def test_the_scope_gives_way_only_once_it_alone_overruns():
    ctx = CommitContext(ctype="cfg", scope="s" * 80, summary="set replicaCount", actor="admin")
    subject = build_message(ctx).splitlines()[0]
    assert len(subject) <= 50
    assert subject.startswith("cfg(sss")
    assert subject.endswith(")")


@pytest.mark.parametrize("scope_len", range(1, 70))
def test_build_message_never_raises_on_length(scope_len):
    ctx = CommitContext(
        ctype="cfg", scope="s" * scope_len, summary="set some.deeply.nested.path", actor="admin"
    )
    assert len(build_message(ctx).splitlines()[0]) <= 50


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


def test_a_floating_image_ref_is_refused():
    with pytest.raises(CommitPolicyError):
        _write("image.tag", "latest")
    with pytest.raises(CommitPolicyError):
        _write("image", "ghcr.io/x/y:latest")


def test_a_replica_count_is_refused_where_keda_runs_by_default():
    with pytest.raises(CommitPolicyError):
        _write("replicaCount", 3)
    with pytest.raises(CommitPolicyError):
        _write("deploy.replicaCount", 3)


def test_a_pinned_image_and_a_keda_bound_pass():
    _write("image.tag", "v1.2.3")  # no raise
    _write("keda.maxReplicas", 10)  # no raise


def test_replicacount_is_allowed_where_the_document_disables_keda():
    # With KEDA off nothing else owns the count, and refusing it leaves a
    # deployment with no settable replica number at all.
    _write("replicaCount", 3, {"keda": {"enabled": False}})  # no raise


@pytest.mark.parametrize(
    "doc",
    [None, {}, {"keda": {}}, {"keda": {"enabled": True}}, {"keda": "off"}, {"replicaCount": 1}],
)
def test_replicacount_stays_refused_without_an_explicit_keda_off(doc):
    # An unset flag is the chart default, and this chart's default runs KEDA. A stored
    # count does not license the next one written over it.
    with pytest.raises(CommitPolicyError):
        _write("replicaCount", 3, doc)


@pytest.mark.parametrize(
    ("path", "value", "refused_at"),
    [
        ("image", {"tag": "latest"}, "image.tag"),
        ("image", {"repository": "ghcr.io/x/y", "tag": ""}, "image.tag"),
        ("sub", {"replicaCount": 3}, "sub.replicaCount"),
        ("app", {"sidecar": {"image": {"tag": "latest"}}}, "app.sidecar.image.tag"),
        ("app", {"sidecar": {"image": "ghcr.io/x/y:latest"}}, "app.sidecar.image"),
        ("app", {"containers": [{"image": {"tag": "latest"}}]}, "app.containers[0].image.tag"),
        ("app", [{"replicaCount": 2}], "app[0].replicaCount"),
    ],
)
def test_a_map_value_is_refused_at_the_leaf_that_breaks_a_rule(path, value, refused_at):
    # A map write replaces every leaf below it, so each leaf meets the rule it would alone.
    with pytest.raises(CommitPolicyError, match=re.escape(refused_at)):
        _write(path, value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("image", {"repository": "ghcr.io/x/y", "tag": "v1.2.3"}),
        ("keda", {"minReplicas": 1, "maxReplicas": 4}),
        ("resources", {"limits": {"cpu": "2"}, "requests": {"memory": "1Gi"}}),
        ("image", {}),
        ("args", []),
    ],
)
def test_a_map_value_with_only_permitted_leaves_passes(path, value):
    _write(path, value)  # no raise


@pytest.mark.parametrize(
    ("path", "value", "doc"),
    [
        ("image", {"tag": "latest", "pullPolicy": "Always"}, None),
        ("image", {"tag": "v2"}, None),
        ("sub", {"replicaCount": 3, "other": 1}, None),
        ("sub", {"replicaCount": 3}, {"keda": {"enabled": False}}),
        ("sub", {"replicaCount": 3}, {"keda": {"enabled": True}}),
        ("app", {"a": {"b": {"image": ""}}, "c": [1, {"tag": "x"}]}, None),
    ],
)
def test_a_map_write_matches_its_leaves_written_one_at_a_time(path, value, doc):
    def refused(p, v):
        try:
            _write(p, v, doc)
        except CommitPolicyError:
            return True
        return False

    one_at_a_time = any(refused(p, v) for p, v in flatten(value, path).items())
    assert refused(path, value) is one_at_a_time


_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "ref",
    [
        "ghcr.io/x/y",
        "y",
        "localhost:5000/x/y",
        "ghcr.io/x/y:",
        "ghcr.io/x/y@sha256:abc",
        "ghcr.io/x/y@" + _DIGEST.replace("sha256", "md5"),
    ],
)
def test_an_image_ref_with_no_tag_and_no_digest_is_refused(ref):
    # An untagged ref pulls whatever the registry calls latest today.
    with pytest.raises(CommitPolicyError, match="image"):
        _write("image", ref)
    with pytest.raises(CommitPolicyError, match=re.escape("app.sidecar.image")):
        _write("app", {"sidecar": {"image": ref}})


@pytest.mark.parametrize(
    "ref",
    [
        "ghcr.io/x/y:v1.2.3",
        "y:1",
        "localhost:5000/x/y:v1",
        f"ghcr.io/x/y@{_DIGEST}",
        f"localhost:5000/x/y@{_DIGEST}",
        f"ghcr.io/x/y:v1.2.3@{_DIGEST}",
    ],
)
def test_an_image_ref_pinned_by_tag_or_digest_passes(ref):
    _write("image", ref)  # no raise


_PINNED = {"keda": {"enabled": False}, "replicaCount": 3}


@pytest.mark.parametrize(
    ("path", "value"),
    [("keda.enabled", True), ("keda", {"enabled": True}), ("keda", {}), ("keda.enabled", None)],
)
def test_a_write_that_hands_a_pinned_replica_count_back_to_keda_is_refused(path, value):
    with pytest.raises(CommitPolicyError, match="replicaCount"):
        commit_policy.validate_write(_PINNED, path, value, keda_by_default=True)
    assert _PINNED == {"keda": {"enabled": False}, "replicaCount": 3}


@pytest.mark.parametrize("path", ["keda.enabled", "keda"])
def test_a_revert_that_hands_a_pinned_replica_count_back_to_keda_is_refused(path):
    with pytest.raises(CommitPolicyError, match="replicaCount"):
        commit_policy.validate_revert(_PINNED, path, keda_by_default=True)
    assert _PINNED == {"keda": {"enabled": False}, "replicaCount": 3}


def test_a_document_carrying_keda_off_and_a_replica_count_together_passes():
    assert (
        commit_policy.document_violations(
            {"replicaCount": 3, "keda": {"enabled": False}}, keda_by_default=True
        )
        == []
    )
    commit_policy.validate_write({}, "keda", {"enabled": False}, keda_by_default=True)
    commit_policy.validate_write(
        {"keda": {"enabled": False}}, "replicaCount", 3, keda_by_default=True
    )
    commit_policy.validate_revert(_PINNED, "replicaCount", keda_by_default=True)


def test_document_violations_names_every_offending_leaf():
    doc = {"replicaCount": 3, "image": "ghcr.io/x/y", "sub": {"image": {"tag": "latest"}}}
    found = commit_policy.document_violations(doc, keda_by_default=True)
    assert len(found) == 3
    assert {v.path for v in found} == {"replicaCount", "image", "sub.image.tag"}
    for leaf in ("replicaCount", "image", "sub.image.tag"):
        assert any(str(v).startswith(leaf) or f" {leaf}:" in str(v) for v in found), (leaf, found)


class TestKedaDefault:
    """Where KEDA is not the chart's default, only the document can turn the rule on."""

    def test_a_chart_without_keda_takes_a_replica_count(self):
        assert commit_policy.validate_write({}, "replicaCount", 2, keda_by_default=False) == []

    @pytest.mark.parametrize(
        "doc", [{"keda": {"enabled": True}}, {"keda": {"enabled": True, "x": 1}}]
    )
    def test_a_document_that_enables_keda_brings_the_rule_back(self, doc):
        with pytest.raises(CommitPolicyError, match="replicaCount"):
            commit_policy.validate_write(doc, "replicaCount", 2, keda_by_default=False)

    def test_enabling_keda_over_a_stored_count_is_refused(self):
        with pytest.raises(CommitPolicyError, match="replicaCount"):
            commit_policy.validate_write(
                {"replicaCount": 2}, "keda.enabled", True, keda_by_default=False
            )

    @pytest.mark.parametrize(
        ("enabled", "default", "owned"),
        [(True, False, True), (False, True, False), (None, True, True), (None, False, False)],
    )
    def test_an_explicit_flag_beats_the_chart_default(self, enabled, default, owned):
        assert commit_policy.keda_owns_replicas(enabled, keda_by_default=default) is owned

    def test_a_flag_that_is_not_a_boolean_leaves_the_default(self):
        assert commit_policy.keda_owns_replicas("false", keda_by_default=True) is True


class TestOnlyAddedViolationsRefuse:
    """A document already breaking a rule stays writable; the write may not add one."""

    STORED = {"replicaCount": 2, "resources": {"limits": {"cpu": "1"}}, "image": {"tag": "v1"}}

    @pytest.mark.parametrize(
        ("path", "value"),
        [("resources.limits.cpu", "2"), ("image.tag", "v2"), ("storage", {"size": "10Gi"})],
    )
    def test_an_unrelated_write_passes_and_reports_what_the_document_carries(self, path, value):
        kept = commit_policy.validate_write(self.STORED, path, value, keda_by_default=True)
        assert [(v.path, v.rule) for v in kept] == [("replicaCount", RULE_KEDA_REPLICAS)]

    @pytest.mark.parametrize("path", ["resources.limits.cpu", "resources", "image"])
    def test_an_unrelated_revert_passes(self, path):
        kept = commit_policy.validate_revert(self.STORED, path, keda_by_default=True)
        assert [v.path for v in kept] == ["replicaCount"]

    def test_reverting_the_violation_itself_passes_clean(self):
        assert (
            commit_policy.validate_revert(self.STORED, "replicaCount", keda_by_default=True) == []
        )

    def test_a_new_value_at_the_refused_leaf_is_still_refused(self):
        with pytest.raises(CommitPolicyError, match="replicaCount"):
            commit_policy.validate_write(self.STORED, "replicaCount", 3, keda_by_default=True)

    def test_rewriting_the_same_value_is_not_an_addition(self):
        kept = commit_policy.validate_write(self.STORED, "replicaCount", 2, keda_by_default=True)
        assert [v.path for v in kept] == ["replicaCount"]

    def test_a_new_violation_elsewhere_is_refused(self):
        with pytest.raises(CommitPolicyError, match=re.escape("image.tag")):
            commit_policy.validate_write(self.STORED, "image.tag", "latest", keda_by_default=True)

    def test_two_stored_violations_can_each_be_repaired_in_turn(self):
        # With every write refused, a document with two violations has no write that repairs either.
        broken = {"replicaCount": 2, "image": {"tag": "latest"}}
        commit_policy.validate_revert(broken, "replicaCount", keda_by_default=True)
        commit_policy.validate_write(broken, "image.tag", "v1", keda_by_default=True)

    def test_a_count_and_a_boolean_are_not_the_same_value(self):
        with pytest.raises(CommitPolicyError, match="replicaCount"):
            commit_policy.validate_write(
                {"replicaCount": 1}, "replicaCount", True, keda_by_default=True
            )


def test_the_keda_refusal_names_the_chart_keys_it_tells_the_caller_to_use():
    # The advice has to be followable: the keys are the KEDA library template's own.
    with pytest.raises(CommitPolicyError) as exc:
        _write("replicaCount", 3)
    message = str(exc.value)
    for key in (KEDA_ENABLED_PATH, KEDA_MIN_PATH, KEDA_MAX_PATH.rsplit(".", 1)[-1]):
        assert key in message, (key, message)
    assert "same write" not in message


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
