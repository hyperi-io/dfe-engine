#  Project:      dfe-engine
#  File:         tests/gitcrud/test_source_review_routing.py
#  Purpose:      Source, derived-schema and meta-schema writes take the review path in production+team
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A production+team source, derived-schema or meta-schema write lands on a review branch, never main.

Real dulwich repos with a bare remote standing in for the deploy repo, as in
``test_routing.py``. The forge is the recording double at the ForgeProvider interface.
"""

import pytest
from dulwich import porcelain
from dulwich.refs import Ref
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitcrud.routing import ReviewRouting, WritePosture
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.derived import DerivedSchema
from dfe_engine.schema.derived_registry import DerivedSchemaRegistry, derived_reference
from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.registry import (
    SchemaNotFoundError,
    SchemaRegistry,
    SchemaValidationError,
    meta_deploy_root,
    resolve_meta_reference,
)
from dfe_engine.schema.schema_manager import add_version_in
from dfe_engine.source.models import Source, SourceMatch, SourceView
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.yaml_utils import yaml_dump


class _RecordingForge:
    def __init__(self) -> None:
        self.heads: list[str] = []

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.heads.append(head)
        return PullRequest(
            number=len(self.heads), url=f"http://forge/pr/{len(self.heads)}", branch=head
        )


def _seed_bare(tmp_path):
    bare = tmp_path / "remote.git"
    porcelain.init(str(bare), bare=True)
    seed = tmp_path / "seed"
    porcelain.init(str(seed))
    (seed / "README.md").write_text("seed\n")
    porcelain.add(str(seed), paths=[str(seed / "README.md")])
    porcelain.commit(str(seed), message=b"seed", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(bare), f"refs/heads/{branch}:refs/heads/main".encode())
    return bare


def _remote_main(bare) -> str:
    with Repo(str(bare)) as r:
        return r.refs[Ref(b"refs/heads/main")].decode()


def _review_branches(bare) -> list[str]:
    with Repo(str(bare)) as r:
        return sorted(k.decode() for k in r.refs.allkeys() if k.startswith(b"refs/heads/dfe/"))


@pytest.fixture
def wired(tmp_path):
    bare = _seed_bare(tmp_path)
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"), repo_url=str(bare), push=True, branch="main"
    )
    repo.ensure()
    return GitCrud(repo, default_registry()), bare


def _source() -> Source:
    return Source(  # ty: ignore[pydantic-discarded-extra-argument]  # Source lifts its flat fields into the version tree in a before-validator
        source="filebeat",
        enabled=True,
        description="filebeat test source",
        match=SourceMatch(field="tags.collector.type", value="filebeat"),
        views=[SourceView(standard="sigma", taxonomy="linux")],
    )


def _derived() -> DerivedSchema:
    block = {
        "date": "2026-09-21",
        "summary": "system.auth subset",
        "select": [{"name": "timestamp"}, {"name": "host_name", "index": "exact_match"}],
    }
    return DerivedSchema.model_validate(
        {
            "base": "meta/beats/filebeat",
            "base_version": "1.0.0",
            "current": "1.0.0",
            "versions": {"1.0.0": block},
            "path": derived_reference("beats/filebeat_auth"),
        }
    )


TEAM = {"environment": "production", "mode": "team"}
DEV = WritePosture(environment="dev", mode="team")

META_PATH = "meta/acme/events"
TREE_PATH = "meta/acme/shipped"


def _meta_doc(column: str = "event_id") -> dict:
    return {
        "current": "1.0.0",
        "resource_type": "custom",
        "versions": {
            "1.0.0": {
                "date": "2026-01-01",
                "type": "model",
                "summary": "init",
                "columns": [{"name": column, "type": "string", "expr": "@source: id"}],
            }
        },
    }


@pytest.fixture
def schemas_dir(tmp_path):
    """The schemas tree, holding a custom and a core schema the deploy repo does not."""
    root = tmp_path / "schemas"
    (root / "meta" / "acme").mkdir(parents=True)
    yaml_dump(_meta_doc("shipped_id"), root / "meta" / "acme" / "shipped.yaml")
    yaml_dump(
        {**_meta_doc("core_id"), "resource_type": "core"}, root / "meta" / "acme" / "core.yaml"
    )
    return root


def _meta_registry(gc, schemas_dir, posture: WritePosture) -> SchemaRegistry:
    return SchemaRegistry(schemas_dir, refresh_interval=0, crud=gc, posture=posture)


def _add_column_version(doc: dict) -> None:
    add_version_in(
        doc,
        "1.1.0",
        [{"name": "more", "type": "string", "expr": "@source: m"}],
        origin="test",
        summary="more",
    )


def _writes(gc, posture, schemas_dir):
    sources = SourceRegistry(crud=gc, posture=posture)
    derived = DerivedSchemaRegistry(crud=gc, posture=posture)
    meta = SchemaRegistry(schemas_dir, refresh_interval=0, crud=gc, posture=posture)
    return {
        "source save": lambda: sources.save_source(_source(), created_by="alice"),
        "derived save": lambda: derived.save(_derived(), created_by="alice"),
        "meta save": lambda: meta.save_schema(
            MetaSchema.model_validate({**_meta_doc(), "path": META_PATH}), created_by="alice"
        ),
        # The tree holds the schema, so the edit copies it into the deploy repo.
        "meta edit": lambda: meta.edit_schema(
            TREE_PATH, _add_column_version, message="schema: add 1.1.0", created_by="alice"
        ),
        "meta fork": lambda: meta.write_document(
            META_PATH, _meta_doc(), message="schema: fork", created_by="alice"
        ),
    }


WRITES = ["source save", "derived save", "meta save", "meta edit", "meta fork"]


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("forge", [True, False], ids=["forge", "no forge"])
def test_a_production_team_write_lands_on_a_review_branch(wired, schemas_dir, write, forge):
    gc, bare = wired
    recording = _RecordingForge() if forge else None
    before = _remote_main(bare)

    _writes(gc, WritePosture(**TEAM, forge=recording), schemas_dir)[write]()

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 1
    if recording is not None:
        assert len(recording.heads) == 1


@pytest.mark.parametrize("write", WRITES)
def test_a_dev_write_still_lands_on_main(wired, schemas_dir, write):
    gc, bare = wired
    before = _remote_main(bare)

    _writes(gc, DEV, schemas_dir)[write]()

    assert _remote_main(bare) != before
    assert _review_branches(bare) == []


def test_a_production_team_delete_lands_on_a_review_branch(wired, schemas_dir):
    gc, bare = wired
    SourceRegistry(crud=gc, posture=DEV).save_source(_source())
    derived_dev = DerivedSchemaRegistry(crud=gc, posture=DEV)
    derived_dev.save(_derived())
    _meta_registry(gc, schemas_dir, DEV).write_document(
        META_PATH, _meta_doc(), message="schema: seed"
    )
    before = _remote_main(bare)
    team = WritePosture(**TEAM, forge=_RecordingForge())
    routing = ReviewRouting()

    SourceRegistry(crud=gc, posture=team).delete_source("filebeat", created_by="alice")
    DerivedSchemaRegistry(crud=gc, posture=team).delete("beats/filebeat_auth")
    SchemaRegistry(schemas_dir, refresh_interval=0, crud=gc, posture=team).delete_schema(
        META_PATH, created_by="alice", routing=routing
    )

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 3
    assert routing.review_required is True
    assert routing.first_pr_url == "http://forge/pr/3"


@pytest.mark.parametrize(
    ("environment", "reviewed"),
    [("production", True), ("dev", False)],
    ids=["production+team", "dev"],
)
def test_a_meta_schema_write_reports_where_it_landed(wired, schemas_dir, environment, reviewed):
    gc, _bare = wired
    routing = ReviewRouting()
    posture = WritePosture(environment=environment, mode="team", forge=_RecordingForge())
    registry = _meta_registry(gc, schemas_dir, posture)

    saved = registry.write_document(META_PATH, _meta_doc(), message="schema: new", routing=routing)

    assert routing.review_required is reviewed
    assert (routing.first_pr_url is not None) is reviewed
    # What the write returns is what main carries once the branch merges.
    assert saved.path == META_PATH
    assert saved.current == "1.0.0"
    assert (META_PATH in {row["path"] for row in registry.list_schemas()}) is not reviewed


def _no_such_schema(reg):
    reg.edit_schema("meta/acme/missing", _add_column_version, message="schema: edit")


def _core_edit(reg):
    reg.edit_schema("meta/acme/core", _add_column_version, message="schema: edit")


def _tree_only_delete(reg):
    reg.delete_schema(TREE_PATH)


def _reserved_path(reg):
    reg.write_document("registries/types", _meta_doc(), message="schema: new")


def _no_columns(reg):
    bad = _meta_doc()
    bad["versions"]["1.0.0"]["columns"] = []
    reg.write_document(META_PATH, bad, message="schema: new")


@pytest.mark.parametrize(
    ("refused", "error", "match"),
    [
        (_no_such_schema, SchemaNotFoundError, "not found"),
        (_core_edit, SchemaValidationError, "changes through a release"),
        (_tree_only_delete, SchemaValidationError, "read-only"),
        (_reserved_path, SchemaValidationError, "registries"),
        (_no_columns, SchemaValidationError, "Invalid schema definition"),
    ],
    ids=["edit missing", "edit core", "delete tree-only", "reserved path", "no columns"],
)
def test_a_refused_meta_schema_write_commits_nothing(wired, schemas_dir, refused, error, match):
    gc, bare = wired
    before = _remote_main(bare)

    with pytest.raises(error, match=match):
        refused(_meta_registry(gc, schemas_dir, DEV))

    assert _remote_main(bare) == before
    assert _review_branches(bare) == []


def test_meta_schema_reads_see_the_deploy_repo_before_the_tree(wired, schemas_dir):
    gc, _bare = wired
    registry = _meta_registry(gc, schemas_dir, DEV)
    registry.write_document(TREE_PATH, _meta_doc("deployed_id"), message="schema: shadow")
    registry.write_document(META_PATH, _meta_doc(), message="schema: new")

    shadowed = registry.get_schema(TREE_PATH).versions["1.0.0"].columns[0].name
    listed = sorted(row["path"] for row in registry.list_schemas())

    assert shadowed == "deployed_id"
    assert listed == ["meta/acme/core", META_PATH, TREE_PATH]
    assert registry.find_schema_at_location(META_PATH) == META_PATH


def test_a_build_resolves_a_meta_schema_from_the_deploy_repo_first(wired, schemas_dir):
    gc, _bare = wired
    registry = _meta_registry(gc, schemas_dir, DEV)
    registry.write_document(META_PATH, _meta_doc(), message="schema: new")
    deploy_schemas = gc.repo_path / "config" / "schemas"
    root = meta_deploy_root(deploy_schemas, schemas_dir)

    assert root is not None
    assert resolve_meta_reference(META_PATH, schemas_dir, root) == (
        gc.repo_path / "config/schemas/definitions" / f"{META_PATH}.yaml"
    )
    assert resolve_meta_reference(TREE_PATH, schemas_dir, root) == (
        schemas_dir / "meta" / "acme" / "shipped.yaml"
    )
    # Gitops off leaves one root, the schemas tree.
    assert meta_deploy_root(schemas_dir, schemas_dir) is None


def test_a_registry_given_no_posture_never_commits_to_main_in_production(wired, monkeypatch):
    """With no posture passed, the loaded settings decide, and no forge means a branch."""
    gc, bare = wired
    monkeypatch.setenv("DFE_ENGINE_ENV", "production")
    monkeypatch.setenv("DFE_ENGINE_GITOPS_MODE", "team")
    monkeypatch.setenv("DFE_ENGINE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DFE_ENGINE_API_JWT_SECRET", "routing-test-" + "x" * 40)
    from dfe_engine.settings import reset_settings

    reset_settings()
    before = _remote_main(bare)
    try:
        SourceRegistry(crud=gc).save_source(_source())
    finally:
        reset_settings()

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 1


@pytest.mark.parametrize("write", ["save", "delete"])
def test_an_engine_owned_write_commits_to_main_in_production_team(wired, write):
    """Core-source reconciles are the engine's, not an operator's edit, so they never wait on review.

    Routed, every start would open another review branch for the landing source and
    main would never carry it.
    """
    gc, bare = wired
    team = SourceRegistry(crud=gc, posture=WritePosture(**TEAM, forge=_RecordingForge()))
    if write == "delete":
        team.save_source(_source(), created_by="engine", core_reconcile=True)
    before = _remote_main(bare)

    if write == "save":
        team.save_source(_source(), created_by="engine", core_reconcile=True)
    else:
        team.delete_source("filebeat", created_by="engine", core_reconcile=True)

    assert _remote_main(bare) != before
    assert _review_branches(bare) == []
