#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_all_sources/test_all_sources.py
#  Purpose:      The all_sources Merge table's members, its DDL and the one-at-a-time rebuild
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The all_sources Merge table's members, its DDL and the one-at-a-time rebuild.

The rebuild itself reads and writes ClickHouse, so the integration tier covers it; these tests cover every decision it makes on the way.
"""

import contextlib
import re
import threading
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from dfe_engine.clickhouse.all_sources import (
    ALL_SOURCES,
    AllSourcesError,
    Coalescer,
    member_pattern,
    member_tables,
    merge_columns,
    merge_table_ddl,
    rebuild_all_sources,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistryError
from tests.unit.test_clickhouse.factories import make_coalescer, make_source
from tests.unit.test_clickhouse.test_all_sources.all_sources_cases import (
    MEMBER_PATTERN_CASES,
    MEMBER_PATTERN_SELECTS_CASES,
    MEMBER_TABLES_CASES,
    MEMBER_TABLES_REJECTS_CASES,
    MERGE_COLUMNS_CASES,
    MERGE_COLUMNS_REJECTS_CASES,
    MERGE_TABLE_DDL_CASES,
    SELECTS_MEMBERS,
    MemberPatternCase,
    MemberPatternSelectsCase,
    MemberTablesCase,
    MemberTablesRejectsCase,
    MergeColumnsCase,
    MergeColumnsRejectsCase,
    MergeTableDdlCase,
)

# Long enough that only a hung run reaches it; every wait below ends on an event first.
EVENT_TIMEOUT_SECONDS = 5.0


def _start_run(
    *, coalescer: Coalescer[str], outcomes: list[str | None], work: Callable[[], str]
) -> threading.Thread:
    """Run *work* through *coalescer* on a new thread, appending what the run returns to *outcomes*."""

    def run() -> None:
        """Record what the run returns."""
        outcomes.append(coalescer.run(work=work))

    thread = threading.Thread(target=run)
    thread.start()
    return thread


class TestAllSources:
    def test_no_source_can_take_the_name(self):
        message = (
            "Source name 'all_sources' must be a Kubernetes DNS-1123 label starting with a "
            "letter (use '-' instead of '_'): a source-bound app such as a transform or a "
            "fetcher is deployed as one instance per source, named for the source, so the name "
            "becomes an Argo Application and a set of Kubernetes object names"
        )
        with pytest.raises(ValidationError, match=re.escape(message)):
            make_source(name=ALL_SOURCES)


class TestCoalescer:
    class TestRun:
        def test_runs_the_latest_work_handed_in_after_its_own(self):
            def first() -> str:
                ran.append("first")
                started.set()
                release.wait(timeout=EVENT_TIMEOUT_SECONDS)
                return "first"

            def second() -> str:
                ran.append("second")
                return "second"

            def third() -> str:
                ran.append("third")
                return "third"

            coalescer = make_coalescer()
            outcomes = []
            ran = []
            release = threading.Event()
            started = threading.Event()
            runner = _start_run(coalescer=coalescer, outcomes=outcomes, work=first)
            started.wait(timeout=EVENT_TIMEOUT_SECONDS)
            handed = [coalescer.run(work=second), coalescer.run(work=third)]
            release.set()
            runner.join(timeout=EVENT_TIMEOUT_SECONDS)

            assert (handed, outcomes, ran) == ([None, None], ["third"], ["first", "third"])

        def test_a_failed_run_still_runs_the_work_handed_in(self):
            def first() -> str:
                started.set()
                release.wait(timeout=EVENT_TIMEOUT_SECONDS)
                raise RuntimeError("the first run failed")

            def second() -> str:
                return "second"

            coalescer = make_coalescer()
            outcomes = []
            release = threading.Event()
            started = threading.Event()
            runner = _start_run(coalescer=coalescer, outcomes=outcomes, work=first)
            started.wait(timeout=EVENT_TIMEOUT_SECONDS)
            handed = coalescer.run(work=second)
            release.set()
            runner.join(timeout=EVENT_TIMEOUT_SECONDS)

            assert (handed, outcomes) == (None, ["second"])

        def test_failed_run(self):
            def fail() -> str:
                raise RuntimeError(message)

            message = "the rebuild failed"
            coalescer = make_coalescer()

            with pytest.raises(RuntimeError, match=message):
                coalescer.run(work=fail)

        def test_a_failed_run_frees_the_next(self):
            def fail() -> str:
                raise RuntimeError("the rebuild failed")

            def succeed() -> str:
                return "rebuilt"

            coalescer = make_coalescer()
            with contextlib.suppress(RuntimeError):
                coalescer.run(work=fail)

            assert coalescer.run(work=succeed) == "rebuilt"


class TestMemberPattern:
    @pytest.mark.parametrize(
        "case", MEMBER_PATTERN_CASES, ids=[case["id"] for case in MEMBER_PATTERN_CASES]
    )
    def test_matches_expected(self, case: MemberPatternCase):
        assert member_pattern(members=case["members"]) == case["expected_pattern"]

    def test_no_members(self):
        message = "all_sources has no member table to read"
        with pytest.raises(AllSourcesError, match=message):
            member_pattern(members=[])

    # Python's re reads literals, '-', '|', one group and both anchors the way ClickHouse's re2 does.
    @pytest.mark.parametrize(
        "case",
        MEMBER_PATTERN_SELECTS_CASES,
        ids=[case["id"] for case in MEMBER_PATTERN_SELECTS_CASES],
    )
    def test_selects(self, case: MemberPatternSelectsCase):
        pattern = member_pattern(members=SELECTS_MEMBERS)
        assert bool(re.search(pattern=pattern, string=case["table"])) is case["expected_selected"]

    def test_unsafe_name(self):
        message = (
            "table name 'dfe.main' must be one or more letters, digits, '_' or '-', "
            "so all_sources can list it by regex"
        )
        with pytest.raises(ValueError, match=re.escape(message)):
            member_pattern(members=["main", "dfe.main"])


class TestMemberTables:
    @pytest.mark.parametrize(
        "case", MEMBER_TABLES_CASES, ids=[case["id"] for case in MEMBER_TABLES_CASES]
    )
    def test_matches_expected(self, case: MemberTablesCase):
        members = member_tables(
            landing_table=case["landing_table"],
            live=case["live"],
            reserved=case["reserved"],
            sources=case["sources"],
            with_org_id=case["with_org_id"],
        )
        assert members == case["expected_members"]

    @pytest.mark.parametrize(
        "case",
        MEMBER_TABLES_REJECTS_CASES,
        ids=[case["id"] for case in MEMBER_TABLES_REJECTS_CASES],
    )
    def test_rejects(self, case: MemberTablesRejectsCase):
        with pytest.raises(ValueError, match=re.escape(case["message"])):
            member_tables(
                landing_table=case["landing_table"],
                live={},
                reserved=frozenset(),
                sources=[],
                with_org_id=frozenset(),
            )


class TestMergeColumns:
    @pytest.mark.parametrize(
        "case", MERGE_COLUMNS_CASES, ids=[case["id"] for case in MERGE_COLUMNS_CASES]
    )
    def test_matches_expected(self, case: MergeColumnsCase):
        columns = merge_columns(landing_columns=case["landing_columns"], landing_table="main")
        assert columns == case["expected_columns"]

    @pytest.mark.parametrize(
        "case",
        MERGE_COLUMNS_REJECTS_CASES,
        ids=[case["id"] for case in MERGE_COLUMNS_REJECTS_CASES],
    )
    def test_rejects(self, case: MergeColumnsRejectsCase):
        with pytest.raises(AllSourcesError, match=re.escape(case["message"])):
            merge_columns(landing_columns=case["landing_columns"], landing_table="main")


class TestMergeTableDdl:
    @pytest.mark.parametrize(
        "case", MERGE_TABLE_DDL_CASES, ids=[case["id"] for case in MERGE_TABLE_DDL_CASES]
    )
    def test_matches_expected(self, case: MergeTableDdlCase):
        ddl = merge_table_ddl(
            columns=case["columns"],
            database=case["database"],
            members=case["members"],
            on_cluster=case["on_cluster"],
        )
        assert ddl == case["expected_ddl"]


class TestRebuildAllSources:
    # The client is a bare object, so any ClickHouse call would fail with an AttributeError rather than the registry's error.
    def test_unreadable_registry(self):
        def unreadable() -> list[Source]:
            raise SourceRegistryError(message)

        message = "the sources directory could not be read"

        with pytest.raises(SourceRegistryError, match=message):
            rebuild_all_sources(
                client=object(), database="dfe", load_sources=unreadable, topology=None
            )
