#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_source_mapper_schema_roots.py
#  Purpose:      Sigma schema metadata resolves a source's schema files the way a deploy does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The mapper builds a source's schema under the same two roots every other build uses.

A meta schema resolves under the schemas tree, and a ``derived/...`` reference
under the deployment's derived root first. Without the roots both resolve
against the working directory and the metadata comes back empty.
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.sigma.source_mapper import SigmaSourceMapper
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError
from dfe_engine.yaml_utils import yaml_dump

META = "meta/acme/flat"


class _Sources:
    def __init__(self, *sources: Source) -> None:
        self._sources = {source.source: source for source in sources}

    def get_source(self, name: str) -> Source:
        if name not in self._sources:
            raise SourceNotFoundError(name)
        return self._sources[name]


def _source(name: str, **schema: str) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"engine": "MergeTree", **schema},
        }
    )


def _put(root: Path, reference: str, doc: dict) -> None:
    path = root / f"{reference}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_dump(doc, path)


def _meta(*names: str) -> dict:
    columns = [{"name": name, "type": "string", "use_case": "exact_match"} for name in names]
    return {
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-09-21", "type": "model", "columns": columns}},
    }


def _selection(*names: str) -> dict:
    return {
        "base": META,
        "base_version": "1.0.0",
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-09-21", "select": [{"name": name} for name in names]}},
    }


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    schemas = tmp_path / "schemas"
    deploy = tmp_path / "deploy" / "config" / "schemas"
    _put(schemas, META, _meta("event_action", "host_name", "source_ip"))
    _put(deploy, "derived/acme/narrow", _selection("source_ip", "event_action"))
    return schemas, deploy


def _mapper(*sources: Source, roots: tuple[Path, Path]) -> SigmaSourceMapper:
    schemas, deploy = roots
    return SigmaSourceMapper(_Sources(*sources), schemas_base_dir=schemas, derived_base_dir=deploy)


def test_a_meta_schema_under_the_schemas_tree_reports_its_columns(roots):
    mapper = _mapper(_source("acme", meta_schema=META), roots=roots)

    metadata = mapper.get_schema_metadata("acme")

    assert {"event_action", "host_name", "source_ip"} <= set(metadata)
    assert metadata["host_name"]["type"] == "string"


def test_a_derived_schema_under_the_deploy_root_narrows_the_columns(roots):
    source = _source("acme", meta_schema=META, derived_schema="derived/acme/narrow")
    mapper = _mapper(source, roots=roots)

    metadata = mapper.get_schema_metadata("acme")

    assert {"source_ip", "event_action"} <= set(metadata)
    assert "host_name" not in metadata


def test_a_schema_file_that_does_not_resolve_is_logged_and_reports_nothing(roots):
    mapper = _mapper(_source("acme", meta_schema="meta/acme/absent"), roots=roots)
    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    try:
        metadata = mapper.get_schema_metadata("acme")
    finally:
        logger.remove(handler_id)

    assert metadata == {}
    [record] = captured
    assert record.record["extra"]["source"] == "acme"
    assert "meta_schema" in record.record["extra"]["error"]
