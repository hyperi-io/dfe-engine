"""The header an unauthored source inherits is written down twice and must agree."""

from dfe_engine.settings import ClickHouseSettings
from dfe_engine.source.models import DEFAULT_HEADER_VERSION


def test_the_settings_default_header_matches_the_source_model() -> None:
    assert ClickHouseSettings().default_header_version == DEFAULT_HEADER_VERSION
