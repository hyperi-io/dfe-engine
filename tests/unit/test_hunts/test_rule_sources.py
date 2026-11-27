#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_rule_sources.py
#  Purpose:      Which labels a rule may read as its registered source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A label names a source only when the registry holds one that loads, and a lookup never raises."""

from collections.abc import Iterator

import pytest

from dfe_engine.hunts.rule_sources import source_check
from dfe_engine.source.registry import SourceRegistry


@pytest.fixture
def registry(tmp_path) -> Iterator[SourceRegistry]:
    """A registry holding one valid source and one stored doc that no longer validates."""
    SourceRegistry.reset_instance()
    broken = "source: broken\nexternal: {}\nversions: 7\n"
    (tmp_path / "broken.yaml").write_text(broken, encoding="utf-8")
    reg = SourceRegistry(sources_directory=tmp_path, writable=True, refresh_interval=0)
    reg.create_source_from_write({"source": "winlog", "external": {}})
    yield reg
    reg.close()
    SourceRegistry.reset_instance()


@pytest.mark.parametrize(
    ("label", "named"),
    [
        ("winlog", True),
        ("never-registered", False),
        ("broken", False),
        ("..", False),
        ("a/b", False),
    ],
    ids=["registered", "absent", "stored_doc_fails_validation", "parent_dir", "path"],
)
def test_a_label_names_a_source_only_when_one_loads(registry, label, named):
    assert source_check(registry)(label) is named
