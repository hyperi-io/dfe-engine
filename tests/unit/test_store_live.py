#  Project:      dfe-engine
#  File:         tests/unit/test_store_live.py
#  Purpose:      A value built from a collection, built again when its write counter moves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``Live`` over a plain counter and builder: what it builds, and when it builds again."""

from dfe_engine.store.live import Live


class _Collection:
    """A write counter and a builder that numbers each build."""

    def __init__(self) -> None:
        self.revision = 0
        self.builds = 0

    def build(self) -> int:
        self.builds += 1
        return self.builds


def test_it_builds_once_up_front():
    collection = _Collection()

    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    assert (live.value, collection.builds) == (1, 1)


def test_an_unmoved_counter_keeps_the_value():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    assert [live.get() for _ in range(3)] == [1, 1, 1]


def test_a_moved_counter_builds_again():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    collection.revision += 1

    assert (live.get(), live.get()) == (2, 2)


def test_within_the_floor_the_counter_is_not_read():
    collection = _Collection()
    reads: list[int] = []

    def revision() -> int:
        reads.append(collection.revision)
        return collection.revision

    live = Live(collection.build, revision, refresh_seconds=3600)
    collection.revision += 1

    assert (live.get(), len(reads)) == (1, 1)


def test_refresh_builds_now_whatever_the_floor():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=3600)

    assert (live.refresh(), live.value) == (2, 2)


def test_a_build_finishing_after_a_newer_one_is_built_again_at_the_next_check():
    """The writer's own refresh can land while another thread's slower build is still running."""
    collection = _Collection()
    live: Live[int] | None = None

    def build() -> int:
        value = collection.build()
        if value == 2:
            # Another replica writes, and this replica's writer refreshes, mid-build.
            collection.revision += 1
            assert live is not None
            live.refresh()
        return value

    live = Live(build, lambda: collection.revision, refresh_seconds=0)
    assert live.refresh() == 2

    assert live.get() == 4
