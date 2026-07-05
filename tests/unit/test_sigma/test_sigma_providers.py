#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_providers.py
#  Purpose:      Tests for the pluggable Sigma providers (git repo / valhalla / files)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Provider adapters - real local git repo + local dir; Valhalla HTTP is faked.

No live network: the git-repo provider clones a REAL local dulwich repo, the
local-files provider scans a real dir, and the Valhalla provider's network seam is
overridden / monkeypatched with canned data.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from dfe_engine.sigma.providers import (
    AuthKind,
    GitRepoProvider,
    LocalFilesProvider,
    ProviderConfig,
    ProviderKind,
    ValhallaProvider,
    parse_sigma_dicts,
    parse_sigma_yaml,
)

_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"


def _rule_yaml(rule_id: str = _UUID_A, modified: str = "2023-05-01", title: str = "Sample") -> str:
    return f"""title: {title}
id: {rule_id}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Image|endswith: \\evil.exe
    condition: selection
level: high
date: 2022-01-01
modified: {modified}
"""


def _rule_dict(rule_id: str = _UUID_A, modified: str = "2023-05-01") -> dict:
    return {
        "title": "Valhalla Rule",
        "id": rule_id,
        "status": "experimental",
        "logsource": {"category": "process_creation", "product": "windows"},
        "detection": {"selection": {"Image|endswith": "\\bad.exe"}, "condition": "selection"},
        "level": "high",
        "modified": modified,
    }


def _make_git_repo(path, files: dict[str, str]) -> str:
    """Create a real local git repo with the given files; return its branch name."""
    from dulwich import porcelain

    path.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(path))
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        porcelain.add(str(path), paths=[str(f)])
    porcelain.commit(str(path), message=b"seed", author=b"t <t@t>", committer=b"t <t@t>")
    return porcelain.active_branch(str(path)).decode()


# ── Normalisation ───────────────────────────────────────────


def test_parse_sigma_yaml_normalises_a_rule():
    docs, warnings = parse_sigma_yaml(_rule_yaml(), "provider:test", "rules/a.yml")
    assert warnings == []
    assert len(docs) == 1
    doc = docs[0]
    assert doc.id == _UUID_A
    assert doc.title == "Sample"
    assert doc.modified == "2023-05-01"
    assert doc.date == "2022-01-01"
    assert doc.origin == "provider:test"
    assert doc.source_ref == "rules/a.yml"
    # normalised content carries logsource + detection
    assert doc.rule["logsource"]["product"] == "windows"
    assert "detection" in doc.rule


def test_parse_skips_rule_without_id():
    text = """title: No Id
logsource:
    product: windows
detection:
    selection:
        a: b
    condition: selection
"""
    docs, warnings = parse_sigma_yaml(text, "provider:test")
    assert docs == []
    assert any("without id" in w for w in warnings)


def test_parse_sigma_dicts_valhalla_shape():
    docs, warnings = parse_sigma_dicts([_rule_dict()], "provider:valhalla")
    assert warnings == []
    assert len(docs) == 1
    assert docs[0].id == _UUID_A
    assert docs[0].origin == "provider:valhalla"


# ── LocalFilesProvider ──────────────────────────────────────


async def test_local_files_provider_scans_dir(tmp_path):
    d = tmp_path / "import"
    d.mkdir()
    (d / "a.yml").write_text(_rule_yaml(_UUID_A), encoding="utf-8")
    (d / "b.yml").write_text(_rule_yaml(_UUID_B, title="Second"), encoding="utf-8")
    provider = LocalFilesProvider(
        ProviderConfig(name="file", kind=ProviderKind.LOCAL_FILES, options={"directory": str(d)})
    )
    docs = await provider.fetch()
    assert {doc.id for doc in docs} == {_UUID_A, _UUID_B}
    # the default import file is the canonical 'file' origin
    assert all(doc.origin == "file" for doc in docs)


async def test_local_files_provider_since_filter(tmp_path):
    d = tmp_path / "import"
    d.mkdir()
    (d / "old.yml").write_text(_rule_yaml(_UUID_A, modified="2020-01-01"), encoding="utf-8")
    (d / "new.yml").write_text(_rule_yaml(_UUID_B, modified="2024-01-01"), encoding="utf-8")
    provider = LocalFilesProvider(
        ProviderConfig(name="file", kind=ProviderKind.LOCAL_FILES, options={"directory": str(d)})
    )
    docs = await provider.fetch(since=datetime(2023, 1, 1))
    assert {doc.id for doc in docs} == {_UUID_B}


async def test_local_files_provider_missing_dir_is_empty(tmp_path):
    provider = LocalFilesProvider(
        ProviderConfig(
            name="file",
            kind=ProviderKind.LOCAL_FILES,
            options={"directory": str(tmp_path / "nope")},
        )
    )
    assert await provider.fetch() == []


# ── GitRepoProvider (real local dulwich repo) ───────────────


async def test_git_repo_provider_clones_and_scans(tmp_path):
    repo = tmp_path / "src"
    branch = _make_git_repo(
        repo, {"rules/windows/a.yml": _rule_yaml(_UUID_A), "README.md": "not a rule"}
    )
    provider = GitRepoProvider(
        ProviderConfig(
            name="localrepo",
            kind=ProviderKind.GIT_REPO,
            options={"url": str(repo), "branch": branch, "subdir": "rules"},
        ),
        work_dir=tmp_path / "cache",
    )
    docs = await provider.fetch()
    assert [doc.id for doc in docs] == [_UUID_A]
    assert docs[0].source_ref.startswith("rules/")


async def test_git_repo_provider_refresh_picks_up_new_commit(tmp_path):
    from dulwich import porcelain

    repo = tmp_path / "src"
    branch = _make_git_repo(repo, {"rules/a.yml": _rule_yaml(_UUID_A)})
    provider = GitRepoProvider(
        ProviderConfig(
            name="localrepo",
            kind=ProviderKind.GIT_REPO,
            options={"url": str(repo), "branch": branch, "subdir": "rules"},
        ),
        work_dir=tmp_path / "cache",
    )
    first = await provider.fetch()
    assert {doc.id for doc in first} == {_UUID_A}

    # add a second rule upstream and commit
    newfile = repo / "rules" / "b.yml"
    newfile.write_text(_rule_yaml(_UUID_B), encoding="utf-8")
    porcelain.add(str(repo), paths=[str(newfile)])
    porcelain.commit(str(repo), message=b"add b", author=b"t <t@t>", committer=b"t <t@t>")

    second = await provider.fetch()
    assert {doc.id for doc in second} == {_UUID_A, _UUID_B}


# ── ValhallaProvider (HTTP seam faked - no live network) ─────


class _FakeValhalla(ValhallaProvider):
    """Override the network seam so fetch() runs on canned rule dicts."""

    def __init__(self, config, rules):
        super().__init__(config)
        self._canned = rules

    async def _fetch_raw(self):
        return self._canned


def _valhalla_config(**opts) -> ProviderConfig:
    return ProviderConfig(name="valhalla", kind=ProviderKind.VALHALLA, options=opts)


async def test_valhalla_fetch_normalises_and_since_filters():
    provider = _FakeValhalla(
        _valhalla_config(),
        [_rule_dict(_UUID_A, "2020-01-01"), _rule_dict(_UUID_B, "2024-06-01")],
    )
    all_docs = await provider.fetch()
    assert {d.id for d in all_docs} == {_UUID_A, _UUID_B}
    recent = await provider.fetch(since=datetime(2023, 1, 1))
    assert {d.id for d in recent} == {_UUID_B}


def test_valhalla_api_key_defaults_to_demo():
    provider = ValhallaProvider(_valhalla_config(demo=True))
    assert provider._api_key() == "1" * 64


def test_valhalla_api_key_requires_secret_when_demo_disabled():
    provider = ValhallaProvider(_valhalla_config(demo=False))
    with pytest.raises(ValueError, match="no api_key secret"):
        provider._api_key()


def test_valhalla_extract_rules_tolerates_shapes():
    rule = _rule_dict()
    assert ValhallaProvider._extract_rules([rule]) == [rule]
    assert ValhallaProvider._extract_rules({"rules": [rule]}) == [rule]
    assert ValhallaProvider._extract_rules({"windows/foo": rule}) == [rule]
    assert ValhallaProvider._extract_rules("garbage") == []


async def test_valhalla_backoff_retries_on_429(monkeypatch):
    """The retry loop backs off on 429 then succeeds - exercised via a fake client."""

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

    class _FakeClient:
        def __init__(self, responses):
            self._responses = responses

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, path, data=None):
            return self._responses.pop(0)

    responses = [_Resp(429, {}), _Resp(200, {"rules": [_rule_dict(_UUID_A)]})]

    def _ctor(base_url=None):
        return _FakeClient(responses)

    monkeypatch.setattr("scalo.http.AsyncHttpClient", _ctor)
    # backoff_seconds=0 keeps the retry instant (no real wait).
    provider = ValhallaProvider(_valhalla_config(demo=True, backoff_seconds=0, max_retries=3))
    docs = await provider.fetch()
    assert {d.id for d in docs} == {_UUID_A}
    assert responses == []  # both fake responses consumed (429 then 200)


def test_auth_secret_resolves_via_seam():
    """A git_token provider pulls its token from the DfeSecrets seam."""

    class _FakeSecrets:
        def get(self, path):
            assert path == "sigma/token"
            return "ghp_secret"

    provider = GitRepoProvider(
        ProviderConfig(
            name="private",
            kind=ProviderKind.GIT_REPO,
            auth={"kind": AuthKind.GIT_TOKEN, "secret_path": "sigma/token", "username": "git"},
            options={"url": "https://example.com/repo.git"},
        ),
        secrets=_FakeSecrets(),
    )
    authed = provider._authed_url("https://example.com/repo.git")
    assert authed == "https://git:ghp_secret@example.com/repo.git"
