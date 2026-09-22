#  Project:      dfe-engine
#  File:         tests/unit/test_exchange/test_meta_schema_exchange.py
#  Purpose:      Meta-schema export and import across two deployments
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Two git-backed schema registries stand in for two deployments.

``alpha`` is where an operator authored a schema; ``beta`` is a clean deployment
carrying only what dfe-schemas ships. Nothing is shared between them, so a
schema only reaches beta by travelling in an exchange document.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dulwich.repo import Repo
from pydantic import ValidationError

from dfe_engine.exchange.models import EXCHANGE_FORMAT, MetaSchemaExport, ResourceReference
from dfe_engine.exchange.schemas import (
    ExchangeConflictError,
    ExchangeError,
    ExchangeUnresolvedError,
    apply_meta_schema_export,
    build_meta_schema_export,
)
from dfe_engine.schema.registry import SchemaNotFoundError, SchemaRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_dump_string

AUTHOR = "tester <tester@dfe.local>"

CUSTOM_SCHEMA = {
    "current": "1.1.0",
    "resource_type": "custom",
    "versions": {
        "1.0.0": {
            "date": "2026-01-01",
            "type": "model",
            "summary": "init",
            "columns": [{"name": "host_name", "type": "string", "expr": "@source: host.name"}],
        },
        "1.1.0": {
            "date": "2026-02-01",
            "type": "addition",
            "summary": "add the event code",
            "columns": [
                {"name": "host_name", "type": "string", "expr": "@source: host.name"},
                {"name": "event_code", "type": "string", "expr": "@source: event.code"},
            ],
        },
    },
}

CORE_SCHEMA = {
    "current": "2.0.0",
    "resource_type": "core",
    "versions": {
        "2.0.0": {
            "date": "2026-03-01",
            "type": "model",
            "summary": "pre-supplied",
            "columns": [
                {"name": "_json", "type": "json", "expr": "@captured: raw_payload as JSON"},
                {"name": "observed_at", "type": "datetime64"},
            ],
        }
    },
}


def _registry(root: Path, *, seed: dict[str, dict]) -> SchemaRegistry:
    """A git-backed schemas directory holding *seed*, keyed by registry path."""
    root.mkdir(parents=True, exist_ok=True)
    Repo.init(str(root))
    schemas = root / "schemas"
    schemas.mkdir()
    for path, document in seed.items():
        yaml_dump(document, schemas / f"{path}.yaml")
    return SchemaRegistry(schemas_directory=schemas, writable=True, refresh_interval=0)


def _commit_count(registry: SchemaRegistry) -> int:
    repo = registry._store._repo
    assert repo is not None, "the fixture registry must be git-backed"
    try:
        return len(list(repo.get_walker()))
    except KeyError:
        return 0


@pytest.fixture
def alpha(tmp_path: Path):
    """The authoring deployment: one operator schema, one pre-supplied schema."""
    registry = _registry(
        tmp_path / "alpha",
        seed={"meta/cisco_ios": CUSTOM_SCHEMA, "meta/core_tpl": CORE_SCHEMA},
    )
    yield registry
    registry.close()


@pytest.fixture
def beta(tmp_path: Path):
    """A clean deployment: only what dfe-schemas ships, nothing operator-authored."""
    registry = _registry(tmp_path / "beta", seed={"meta/core_tpl": CORE_SCHEMA})
    yield registry
    registry.close()


class TestUserAuthoredRoundTrip:
    def test_export_then_import_reproduces_the_schema(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/cisco_ios")

        result = apply_meta_schema_export(beta, document, created_by=AUTHOR)

        assert result.action == "created"
        assert (
            beta.get_schema("meta/cisco_ios").to_yaml_dict()
            == alpha.get_schema("meta/cisco_ios").to_yaml_dict()
        )

    def test_export_carries_the_whole_version_history(self, alpha):
        document = build_meta_schema_export(alpha, "meta/cisco_ios")

        assert document.current == "1.1.0"
        assert sorted(document.versions) == ["1.0.0", "1.1.0"]

    def test_a_single_version_export_pins_current_to_it(self, alpha):
        document = build_meta_schema_export(alpha, "meta/cisco_ios", version="1.0.0")

        assert document.current == "1.0.0"
        assert list(document.versions) == ["1.0.0"]

    def test_the_import_is_a_git_commit(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/cisco_ios")
        before = _commit_count(beta)

        apply_meta_schema_export(beta, document, created_by=AUTHOR)

        assert _commit_count(beta) == before + 1

    def test_a_second_import_is_refused_rather_than_overwriting(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/cisco_ios")
        apply_meta_schema_export(beta, document, created_by=AUTHOR)

        with pytest.raises(ExchangeConflictError, match="already exists"):
            apply_meta_schema_export(beta, document, created_by=AUTHOR)

    def test_an_unknown_version_is_refused(self, alpha):
        with pytest.raises(ExchangeError, match=re.escape("9.9.9")):
            build_meta_schema_export(alpha, "meta/cisco_ios", version="9.9.9")


class TestCoreSchemaTravelsAsAReference:
    def test_the_export_is_a_reference_and_a_pin(self, alpha):
        document = build_meta_schema_export(alpha, "meta/core_tpl")

        assert document.resource_type == "core"
        assert document.reference == ResourceReference(path="meta/core_tpl", version="2.0.0")
        assert document.versions is None
        assert document.current is None

    def test_the_exported_document_carries_no_columns(self, alpha):
        document = build_meta_schema_export(alpha, "meta/core_tpl")

        rendered = yaml_dump_string(document.model_dump(mode="json", exclude_none=True))

        assert "columns" not in rendered
        assert "observed_at" not in rendered

    def test_the_model_refuses_a_core_document_that_carries_columns(self):
        with pytest.raises(ValidationError, match="forks the"):
            MetaSchemaExport(
                path="meta/core_tpl",
                resource_type="core",
                reference=ResourceReference(path="meta/core_tpl", version="2.0.0"),
                current="2.0.0",
                versions=CORE_SCHEMA["versions"],
            )

    def test_the_import_resolves_and_writes_nothing(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/core_tpl")
        before_paths = sorted(entry["path"] for entry in beta.list_schemas())
        before_commits = _commit_count(beta)

        result = apply_meta_schema_export(beta, document, created_by=AUTHOR)

        assert result.action == "resolved"
        assert result.version == "2.0.0"
        assert sorted(entry["path"] for entry in beta.list_schemas()) == before_paths
        assert _commit_count(beta) == before_commits

    def test_the_import_does_not_fork_the_read_only_schema(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/core_tpl")

        apply_meta_schema_export(beta, document, created_by=AUTHOR)

        resolved = beta.get_schema("meta/core_tpl")
        assert resolved.resource_type == "core"
        assert resolved.to_yaml_dict() == alpha.get_schema("meta/core_tpl").to_yaml_dict()

    def test_a_reference_the_deployment_does_not_carry_is_refused(self, beta):
        document = MetaSchemaExport(
            path="meta/absent",
            resource_type="core",
            reference=ResourceReference(path="meta/absent", version="1.0.0"),
        )

        with pytest.raises(ExchangeUnresolvedError, match="dfe-schemas"):
            apply_meta_schema_export(beta, document, created_by=AUTHOR)

    def test_a_pin_the_deployment_cannot_reach_names_what_it_has(self, beta):
        document = MetaSchemaExport(
            path="meta/core_tpl",
            resource_type="core",
            reference=ResourceReference(path="meta/core_tpl", version="3.0.0"),
        )

        with pytest.raises(ExchangeUnresolvedError, match=re.escape("present: 2.0.0")):
            apply_meta_schema_export(beta, document, created_by=AUTHOR)

    def test_a_custom_document_may_not_overwrite_a_core_schema(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/cisco_ios").model_copy(
            update={"path": "meta/core_tpl"}
        )

        with pytest.raises(ExchangeConflictError, match="core meta schema here"):
            apply_meta_schema_export(beta, document, created_by=AUTHOR)


class TestDocumentFormat:
    def test_a_newer_document_format_is_refused(self, alpha, beta):
        document = build_meta_schema_export(alpha, "meta/cisco_ios").model_copy(
            update={"format": EXCHANGE_FORMAT + 1}
        )

        with pytest.raises(ExchangeError, match="newer than this deployment"):
            apply_meta_schema_export(beta, document, created_by=AUTHOR)

    def test_exporting_a_schema_that_is_not_there_raises_not_found(self, alpha):
        with pytest.raises(SchemaNotFoundError):
            build_meta_schema_export(alpha, "meta/absent")
