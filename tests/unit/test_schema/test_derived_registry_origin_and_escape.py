#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_derived_registry_origin_and_escape.py
#  Purpose:      A derived schema's origin comes from its root, and one bad entry never fails the list
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``origin`` says which root a document was read from, whatever the document says.

A deploy-repo file is editable and a shipped one is not, so a stored ``origin``
key must not move a file from one to the other. And a file whose path resolves
outside its root is refused by ``get`` and left out of ``list``, the way a
document that no longer parses is.
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.schema.derived_registry import (
    DerivedSchemaRegistry,
    DerivedSchemaValidationError,
)
from dfe_engine.yaml_utils import yaml_dump


def _doc(**extra) -> dict:
    return {
        "base": "meta/beats/filebeat",
        "base_version": "1.0.0",
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-09-21", "select": [{"name": "timestamp"}]}},
        **extra,
    }


def _put(root: Path, key: str, doc: dict) -> Path:
    path = root / f"{key}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_dump(doc, path)
    return path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "deploy" / "derived", tmp_path / "shipped" / "derived"


@pytest.fixture
def registry(roots: tuple[Path, Path]) -> DerivedSchemaRegistry:
    deploy, shipped = roots
    shipped.mkdir(parents=True)
    return DerivedSchemaRegistry(deploy, shipped_directory=shipped)


@pytest.fixture
def warnings():
    seen: list[str] = []
    handler = logger.add(lambda message: seen.append(message.record["message"]), level="WARNING")
    yield seen
    logger.remove(handler)


@pytest.mark.parametrize("stored", ["shipped", "elsewhere"])
def test_a_deploy_document_is_deploy_whatever_origin_it_carries(registry, roots, stored):
    _put(roots[0], "beats/auth", _doc(origin=stored))

    assert registry.get("beats/auth").origin == "deploy"
    assert [row["origin"] for row in registry.list()] == ["deploy"]


def test_a_shipped_document_is_shipped_whatever_origin_it_carries(registry, roots):
    _put(roots[1], "beats/auth", _doc(origin="deploy"))

    assert registry.get("beats/auth").origin == "shipped"
    assert [row["origin"] for row in registry.list()] == ["shipped"]


@pytest.mark.parametrize("root_index", [0, 1], ids=["deploy", "shipped"])
def test_an_entry_escaping_its_root_is_left_out_of_the_list(
    registry, roots, tmp_path, warnings, root_index
):
    outside = _put(tmp_path / "outside", "secret", _doc())
    root = roots[root_index]
    _put(root, "beats/good", _doc())
    (root / "beats" / "escape.yaml").symlink_to(outside)

    rows = registry.list()

    assert [row["path"] for row in rows] == ["derived/beats/good"]
    assert [w for w in warnings if "beats/escape" in w and "escapes its directory" in w]


def test_get_still_refuses_an_entry_escaping_its_root(registry, roots, tmp_path):
    outside = _put(tmp_path / "outside", "secret", _doc())
    (roots[1] / "beats").mkdir(parents=True)
    (roots[1] / "beats" / "escape.yaml").symlink_to(outside)

    with pytest.raises(DerivedSchemaValidationError, match="escapes its directory"):
        registry.get("beats/escape")
