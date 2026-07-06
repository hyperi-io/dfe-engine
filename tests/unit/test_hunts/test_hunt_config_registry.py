"""Tests for hunt config registry helpers."""

import pytest

from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigRegistry,
    default_display_name,
    resolve_display_name,
    strip_identity_fields_from_yaml,
)


class TestHuntNaming:
    def test_default_display_name(self):
        assert default_display_name("api_test_hunt") == "Api test hunt"
        assert default_display_name("my-hunt") == "My hunt"
        assert default_display_name("Test") == "Test"

    def test_resolve_display_name_prefers_display_name(self):
        assert resolve_display_name({"display_name": "Custom"}, "file") == "Custom"

    def test_resolve_display_name_legacy_name_field(self):
        assert resolve_display_name({"name": "Legacy"}, "file") == "Legacy"

    def test_strip_identity_fields(self):
        out = strip_identity_fields_from_yaml(
            {"name": "id", "hunt_id": "x", "display_name": "D", "cron": "*"}
        )
        assert "name" not in out
        assert "hunt_id" not in out
        assert out["display_name"] == "D"


class TestDeleteAttribution:
    """A save/delete is an auditable mutation - its git commit must credit the
    actor. 15.6-F1: delete committed anonymously; the deeper bug was that a BARE
    actor id (what the API passes) is an InvalidUserIdentity to dulwich, so BOTH
    save and delete silently failed to commit at all."""

    def test_save_and_delete_commits_credit_the_actor(self, tmp_path):
        from dulwich import porcelain
        from dulwich.repo import Repo

        porcelain.init(str(tmp_path))
        hunts_dir = tmp_path / "hunts"
        hunts_dir.mkdir()
        reg = HuntConfigRegistry(str(hunts_dir), writable=True)
        if not reg._store.is_git:
            pytest.skip("store not git-backed in this environment")

        reg.save("myhunt", {"cron": "* * * * *"}, created_by="alice")
        reg.delete("myhunt", deleted_by="bob")

        with Repo(str(tmp_path)) as repo:
            head = repo[repo.head()]
            parent = repo[head.parents[0]]  # the save commit (proves save committed too)

        # Delete commit credits bob with a dulwich-valid identity (not anonymous).
        assert b"hunt: delete myhunt (by bob)" in head.message
        assert head.author == b"bob <bob@dfe-engine>"
        # Save commit really landed (bare 'alice' would have silently failed before).
        assert parent.author == b"alice <alice@dfe-engine>"
