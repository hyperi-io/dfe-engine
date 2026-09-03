#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_catalogue.py
#  Purpose:      Tests for the manifest's maturity ladder and the show gate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The maturity level an app declares, and which apps a gate lets through."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue
from dfe_engine.appmgmt.catalogue import Maturity

MANIFEST = """\
apps:
  dfe-loader:
    multiplicity: single
  dfe-transform-vrl:
    multiplicity: per_config
    maturity: release
  dfe-transform-next:
    multiplicity: per_config
    maturity: rc
  dfe-transform-elastic:
    multiplicity: per_config
    maturity: beta
  dfe-transform-wasm:
    multiplicity: per_config
    maturity: alpha
"""


def _write(tmp_path, text: str):
    path = tmp_path / "apps.yaml"
    path.write_text(text)
    return path


@pytest.fixture
def manifest(tmp_path):
    """Swap the live catalogue for MANIFEST, and put the bundled one back after."""
    catalogue.reload_catalogue(_write(tmp_path, MANIFEST))
    yield
    catalogue.reload_catalogue()


class TestParse:
    @pytest.mark.parametrize("level", ["alpha", "beta", "rc", "release"])
    def test_each_rung_parses(self, tmp_path, level):
        manifest = _write(tmp_path, f"apps:\n  dfe-x:\n    maturity: {level}\n")
        assert catalogue.load_catalogue(manifest)["dfe-x"].maturity is Maturity(level)

    def test_an_app_that_says_nothing_is_release(self, tmp_path):
        manifest = _write(tmp_path, "apps:\n  dfe-x:\n    multiplicity: single\n")
        assert catalogue.load_catalogue(manifest)["dfe-x"].maturity is Maturity.RELEASE

    @pytest.mark.parametrize("bad", ["stable", "spike", "GA", "Release", ""])
    def test_anything_off_the_ladder_is_refused(self, tmp_path, bad):
        manifest = _write(tmp_path, f"apps:\n  dfe-x:\n    maturity: '{bad}'\n")
        with pytest.raises(catalogue.CatalogueError, match="maturity"):
            catalogue.load_catalogue(manifest)

    def test_the_bundled_manifest_declares_the_transforms(self):
        # The snapshot in the image is pinned to dfe-infra's, and this is where
        # a level typed wrongly there first shows.
        loaded = catalogue.load_catalogue(catalogue.BUNDLED_MANIFEST)
        assert loaded["dfe-transform-vrl"].maturity is Maturity.RELEASE
        assert loaded["dfe-transform-vector"].maturity is Maturity.RELEASE
        assert loaded["dfe-transform-elastic"].maturity is Maturity.BETA


class TestLadder:
    def test_the_order_is_least_mature_first(self):
        assert list(Maturity) == [Maturity.ALPHA, Maturity.BETA, Maturity.RC, Maturity.RELEASE]

    def test_release_is_shown_at_every_gate(self):
        assert all(Maturity.RELEASE.shown_at(gate) for gate in Maturity)

    def test_alpha_is_shown_only_when_alpha_is_the_gate(self):
        assert [gate for gate in Maturity if Maturity.ALPHA.shown_at(gate)] == [Maturity.ALPHA]

    def test_a_gate_of_beta_admits_beta_and_up(self):
        shown = [level for level in Maturity if level.shown_at(Maturity.BETA)]
        assert shown == [Maturity.BETA, Maturity.RC, Maturity.RELEASE]


class TestVisibleServices:
    @pytest.mark.parametrize(
        ("gate", "expected"),
        [
            ("release", ["dfe-loader", "dfe-transform-vrl"]),
            ("rc", ["dfe-loader", "dfe-transform-next", "dfe-transform-vrl"]),
            (
                "beta",
                [
                    "dfe-loader",
                    "dfe-transform-elastic",
                    "dfe-transform-next",
                    "dfe-transform-vrl",
                ],
            ),
            (
                "alpha",
                [
                    "dfe-loader",
                    "dfe-transform-elastic",
                    "dfe-transform-next",
                    "dfe-transform-vrl",
                    "dfe-transform-wasm",
                ],
            ),
        ],
    )
    def test_each_gate_lists_that_level_and_up(self, manifest, gate, expected):
        assert catalogue.visible_services(gate) == expected

    def test_the_gate_takes_the_enum_as_well_as_its_value(self, manifest):
        assert catalogue.visible_services(Maturity.RC) == catalogue.visible_services("rc")

    def test_a_gate_off_the_ladder_is_refused(self, manifest):
        with pytest.raises(ValueError, match="stable"):
            catalogue.visible_services("stable")

    def test_services_stays_the_whole_manifest(self, manifest):
        # Listing is gated; resolving is not. An already-deployed instance of a
        # withheld app still has a descriptor to resolve against.
        assert len(catalogue.services()) == 5
        assert catalogue.descriptor("dfe-transform-wasm").maturity is Maturity.ALPHA
