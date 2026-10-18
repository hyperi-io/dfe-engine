#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_require.py
#  Purpose:      A manifest-object check reports absence and unreadable servers, and nothing else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``missing_objects`` and ``require_objects`` against a client that answers as told.

The client stands in for ``system.tables``. A server error and a dropped socket are
what a read of it can raise, so those become ``SchemaNotAppliedError``. Anything else
is a defect in the caller and reaches it unwrapped.
"""

from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from dfe_engine.schema.require import SchemaNotAppliedError, missing_objects, require_objects

_IDS = ("meta.orgs", "meta.ch_tiers")


class _Tables:
    """Answers the ``system.tables`` read with the names it holds, or raises ``fails``."""

    def __init__(self, present: list[str], fails: BaseException | None = None) -> None:
        self._present = present
        self._fails = fails

    def query(self, sql: str, parameters: dict) -> SimpleNamespace:
        if self._fails is not None:
            raise self._fails
        wanted = set(parameters["names"])
        return SimpleNamespace(result_rows=[(name,) for name in self._present if name in wanted])


def test_missing_objects_names_each_absent_one_qualified_and_sorted():
    assert missing_objects(_Tables(["orgs"]), database="dfe_meta", object_ids=_IDS) == [
        "dfe_meta.ch_tiers"
    ]
    assert missing_objects(_Tables([]), database="dfe_meta", object_ids=_IDS) == [
        "dfe_meta.ch_tiers",
        "dfe_meta.orgs",
    ]


def test_missing_objects_is_empty_when_every_one_is_there():
    assert (
        missing_objects(_Tables(["orgs", "ch_tiers"]), database="dfe_meta", object_ids=_IDS) == []
    )


def test_missing_objects_lets_a_read_failure_through():
    with pytest.raises(OperationalError):
        missing_objects(
            _Tables([], fails=OperationalError("Connection refused")),
            database="dfe_meta",
            object_ids=_IDS,
        )


def test_require_objects_names_the_absent_tables():
    with pytest.raises(SchemaNotAppliedError, match=r"dfe_meta\.ch_tiers"):
        require_objects(_Tables(["orgs"]), database="dfe_meta", object_ids=_IDS, what="the test")


@pytest.mark.parametrize(
    "error",
    [
        DatabaseError("Code: 497. ACCESS_DENIED"),
        OperationalError("refused"),
        ConnectionResetError(),
    ],
    ids=["server-error", "driver-transport", "socket"],
)
def test_require_objects_reports_a_server_it_could_not_read(error):
    with pytest.raises(SchemaNotAppliedError, match="could not check the test"):
        require_objects(
            _Tables([], fails=error), database="dfe_meta", object_ids=_IDS, what="the test"
        )


def test_require_objects_lets_a_defect_through_unwrapped():
    """A bug in the caller is not a schema the phase has yet to apply."""
    with pytest.raises(TypeError):
        require_objects(
            _Tables([], fails=TypeError("bad argument")),
            database="dfe_meta",
            object_ids=_IDS,
            what="the test",
        )
