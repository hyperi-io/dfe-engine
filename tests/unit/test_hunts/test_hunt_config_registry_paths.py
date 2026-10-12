#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_hunt_config_registry_paths.py
#  Purpose:      Hunt files stay inside the hunts directory whatever name a caller passes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The directory backend keeps a hunt as ``<directory>/<name>.yaml``.

The store reads a name with its leading slashes stripped, so a read of ``/x`` finds
the hunt ``x`` while a write of ``/x`` joins an absolute path. Each test pins one
side of that so a read and a write of one name always address one file.
"""

from collections.abc import Iterator

import pytest

from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigNotFoundError,
    HuntConfigRegistry,
    HuntConfigRegistryError,
)

HUNT = {"display_name": "Escape probe", "cron": "*/5 * * * *", "rules": []}


@pytest.fixture
def registry(tmp_path) -> Iterator[HuntConfigRegistry]:
    found = HuntConfigRegistry(hunts_directory=tmp_path / "hunts")
    yield found
    found.close()


class TestHuntFilesStayInTheirDirectory:
    def test_a_leading_slash_does_not_reach_a_stored_hunt(self, registry):
        registry.save("myhunt", HUNT)

        assert registry.exists("myhunt")
        assert not registry.exists("/myhunt")
        with pytest.raises(HuntConfigNotFoundError):
            registry.get("/myhunt")

    def test_a_save_whose_file_leaves_the_directory_is_refused(self, registry, tmp_path):
        outside = tmp_path / "outside" / "escape"

        with pytest.raises(HuntConfigRegistryError):
            registry.save(str(outside), HUNT)

        assert not (tmp_path / "outside").exists()

    def test_a_delete_whose_file_leaves_the_directory_removes_nothing(self, registry, tmp_path):
        planted = tmp_path / "planted.yaml"
        planted.write_text("cron: '* * * * *'\n")

        for name in (str(tmp_path / "planted"), "../planted"):
            with pytest.raises(HuntConfigNotFoundError):
                registry.delete(name)

        assert planted.exists()

    def test_a_hunt_in_a_subdirectory_still_saves_and_reads(self, registry):
        registry.save("team/nightly", HUNT)

        assert registry.get("team/nightly")["cron"] == HUNT["cron"]
