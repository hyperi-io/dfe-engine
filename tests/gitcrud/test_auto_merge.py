"""Auto-merge gate matrix, stored flag round-trip, and PR-conversion helper."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.auto_merge import (
    AutoMergeState,
    apply_auto_merge,
    gate,
    resolve_state,
    set_stored,
)
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


class TestGate:
    @pytest.mark.parametrize(
        ("env", "mode", "expected"),
        [
            ("dev", "team", True),  # dev posture: the 80% case
            ("development", "team", True),
            ("local", "team", True),
            ("test", "team", True),
            ("ci", "team", True),
            ("production", "solo", True),  # solo production: the 20% case
            ("prod", "solo", True),
            ("dev", "solo", True),
            ("production", "team", False),  # team production: refused
            ("prod", "team", False),
            ("staging", "team", False),
        ],
    )
    def test_matrix(self, env, mode, expected):
        allowed, reason = gate(env, mode)
        assert allowed is expected
        assert reason  # always a human-readable explanation

    def test_refusal_reason_names_both_knobs(self):
        _, reason = gate("production", "team")
        assert "DFE_GITOPS_MODE" in reason
        assert "DFE_ENV" in reason


class TestStoredFlagBrokenFile:
    """A hand-edited gitops.yaml that is broken must never be read as ON."""

    def _write(self, crud, content: str) -> None:
        path = crud.repo_path / "governance" / "settings" / "gitops.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_malformed_yaml_does_not_raise(self, crud):
        self._write(crud, "[not: a mapping")
        state = resolve_state(crud, environment="production", mode="team")
        assert state.stored is False

    def test_list_doc_does_not_raise(self, crud):
        self._write(crud, "- one\n- two\n")
        state = resolve_state(crud, environment="production", mode="team")
        assert state.stored is False


class TestStoredFlag:
    def test_default_off(self, crud):
        state = resolve_state(crud, environment="dev", mode="team")
        assert state == AutoMergeState(
            stored=False, allowed=True, effective=False, reason=state.reason
        )

    def test_set_then_effective(self, crud):
        res = set_stored(crud, True, actor="derek")
        assert res.commit_sha  # the toggle is itself an audited commit
        doc = crud.get("gov_settings", "gitops")
        assert doc["auto_merge"] is True
        state = resolve_state(crud, environment="dev", mode="team")
        assert state.stored is True
        assert state.effective is True

    def test_toggle_commit_message_conforms(self, crud):
        set_stored(crud, True, actor="derek")
        # rbac(gitops) type + [skip ci] per the commit standard
        head = crud.head_revision()
        assert head is not None

    def test_stored_on_but_gate_refuses_means_effective_off(self, crud):
        set_stored(crud, True, actor="derek")
        state = resolve_state(crud, environment="production", mode="team")
        assert state.stored is True
        assert state.allowed is False
        assert state.effective is False

    def test_set_off(self, crud):
        set_stored(crud, True, actor="derek")
        set_stored(crud, False, actor="derek")
        state = resolve_state(crud, environment="dev", mode="team")
        assert state.stored is False


class TestApplyAutoMerge:
    def _state(self, effective: bool) -> AutoMergeState:
        return AutoMergeState(
            stored=effective, allowed=effective, effective=effective, reason="test"
        )

    def test_converts_would_be_pr(self):
        # governance class always resolves to PR mode -> converted when ON
        assert (
            apply_auto_merge(
                self._state(True),
                environment="production",
                rbac_class="governance",
                actor="derek",
                resource="governance/x",
            )
            is True
        )

    def test_no_conversion_when_off(self):
        assert (
            apply_auto_merge(
                self._state(False),
                environment="production",
                rbac_class="governance",
                actor="derek",
                resource="governance/x",
            )
            is False
        )

    def test_no_conversion_for_already_direct(self):
        # dev helmvars was direct anyway - nothing converted, no badge
        assert (
            apply_auto_merge(
                self._state(True),
                environment="dev",
                rbac_class="helmvars",
                actor="derek",
                resource="helmvars/x",
            )
            is False
        )
