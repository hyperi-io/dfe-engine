#  Project:      dfe-engine
#  File:         tests/unit/test_source/factories.py
#  Purpose:      Factories for source unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for source unit test inputs."""

from pathlib import Path

from dfe_engine.source.catalogue import SourceCatalogue, load_source_catalogue
from dfe_engine.source.models import SourceTransform
from dfe_engine.source.registry import SourceRegistry

CATALOGUE_FIXTURES = Path(__file__).parents[2] / "fixtures" / "catalogue"
"""Catalogue files copied verbatim from dfe-schemas, and a trimmed dfe-transform-elastic contract."""


def make_source_catalogue(*, directory: Path = CATALOGUE_FIXTURES / "entries") -> SourceCatalogue:
    """Build a source.catalogue.SourceCatalogue from a directory of catalogue files."""
    return load_source_catalogue(directory=directory)


def make_source_registry(*, sources_directory: Path) -> SourceRegistry:
    """Build a source.registry.SourceRegistry over a directory, with no background refresh."""
    return SourceRegistry(refresh_interval=0, sources_directory=sources_directory, writable=True)


def make_source_transform(
    *, engine: str = "elastic", variant: str | None = None
) -> SourceTransform:
    """Build a source.models.SourceTransform."""
    return SourceTransform.model_validate({"engine": engine, "variant": variant})


def make_unvalidated_source_transform(*, engine: str, variant: str) -> SourceTransform:
    """Build a source.models.SourceTransform without validation, so it may name an engine no app manifest declares."""
    return SourceTransform.model_construct(engine=engine, variant=variant)
