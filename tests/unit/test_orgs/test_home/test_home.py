#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_home/test_home.py
#  Purpose:      Tests for the your-org store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the your-org store."""

import re
from pathlib import Path

import pytest

from dfe_engine.orgs.home import CLASS, NAME, PLAIN_FILE, HomeOrgStore
from dfe_engine.yaml_utils import yaml_load
from tests.unit.test_orgs.factories import make_gitcrud, make_home_org_store
from tests.unit.test_orgs.test_home.home_cases import (
    HOME_ORG_STORE_CASES,
    HOME_ORG_STORE_REJECT_CASES,
    HomeOrgStoreCase,
    HomeOrgStoreRejectCase,
)


def _write_doc(*, config_dir: Path, doc: str) -> None:
    """Write *doc* where a store under *config_dir* reads your org with gitops off."""
    path = config_dir / PLAIN_FILE
    path.parent.mkdir(parents=True)
    path.write_text(doc, encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> HomeOrgStore:
    """A your-org store with gitops off."""
    return make_home_org_store(config_dir=tmp_path)


class TestHomeOrgStore:
    class TestGet:
        @pytest.mark.parametrize(
            "case", HOME_ORG_STORE_CASES, ids=[case["id"] for case in HOME_ORG_STORE_CASES]
        )
        def test_matches_expected(
            self, store: HomeOrgStore, tmp_path: Path, case: HomeOrgStoreCase
        ):
            _write_doc(config_dir=tmp_path, doc=case["doc"])
            assert store.get() == case["expected_name"]

        def test_no_doc(self, store: HomeOrgStore):
            assert store.get() is None

        def test_no_doc_in_the_deploy_repo(self, tmp_path: Path):
            crud = make_gitcrud(path=tmp_path / "deploy")
            assert make_home_org_store(config_dir=tmp_path, crud=crud).get() is None

        @pytest.mark.parametrize(
            "case",
            HOME_ORG_STORE_REJECT_CASES,
            ids=[case["id"] for case in HOME_ORG_STORE_REJECT_CASES],
        )
        def test_rejects(self, store: HomeOrgStore, tmp_path: Path, case: HomeOrgStoreRejectCase):
            _write_doc(config_dir=tmp_path, doc=case["doc"])
            with pytest.raises(ValueError, match=re.escape(case["message"])):
                store.get()

    class TestSet:
        def test_writes_the_plain_file(self, store: HomeOrgStore, tmp_path: Path):
            store.set(actor="admin", name="acme")
            assert yaml_load(source=tmp_path / PLAIN_FILE) == {"name": "acme"}

        def test_commits_to_the_deploy_repo(self, tmp_path: Path):
            crud = make_gitcrud(path=tmp_path / "deploy")
            store = make_home_org_store(config_dir=tmp_path, crud=crud)
            store.set(actor="admin", name="acme")
            assert (
                store.get(),
                crud.get(cls_name=CLASS, name=NAME),
                (tmp_path / PLAIN_FILE).exists(),
            ) == ("acme", {"name": "acme"}, False)

        def test_replaces_your_org(self, store: HomeOrgStore):
            store.set(actor="admin", name="acme")
            store.set(actor="admin", name="beta")
            assert store.get() == "beta"
