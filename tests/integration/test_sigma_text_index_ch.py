#  Project:      dfe-engine
#  File:         tests/integration/test_sigma_text_index_ch.py
#  Purpose:      A propagated sigma binding over a deployed sigma view, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A propagated sigma WHERE clause runs over the sigma view a source deploy creates.

Propagation matches a Sigma field aliased to a text-indexed column as
``lower(field) LIKE``. That form only earns its place if it selects exactly the
rows the ``ILIKE`` form does, and if the server reads the column's text index for
it, so both are asked of the server over the view and landing table a real deploy
creates.
"""

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.sigma.catalog import SigmaCatalogStore, SigmaSelectionStore
from dfe_engine.sigma.propagation import SigmaPropagator, binding_rule_id, sigma_view_name
from dfe_engine.sigma.providers import parse_sigma_yaml
from dfe_engine.sigma.source_mapper import SigmaSourceMapper
from dfe_engine.source.deployment import deploy_statements_for_build
from dfe_engine.source.registry import SourceRegistry

pytestmark = pytest.mark.integration

_SOURCE = "sigma-text-probe"
_ASCII_RULE = "ffffffff-ffff-ffff-ffff-fffffffffff1"
_NON_ASCII_RULE = "ffffffff-ffff-ffff-ffff-fffffffffff2"
_NEEDLES = {_ASCII_RULE: "Mimikatz", _NON_ASCII_RULE: "Ärger"}

# Three casings of the ASCII needle, then an upper-case non-ASCII line, a miss, and a NULL.
_ASCII_MATCHES = [
    "Invoke-Mimikatz -DumpCreds",
    "MIMIKATZ sekurlsa::logonpasswords",
    "mimikatz.exe privilege::debug",
]
_RAW_LINES = [*_ASCII_MATCHES, "ÄRGER beim Anmelden", "powershell Get-Process", None]


def _rule_yaml(rule_id: str, needle: str) -> str:
    return f"""
title: Keyword {needle}
id: {rule_id}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Message|contains: '{needle}'
    condition: selection
level: high
"""


@pytest.fixture
def deployed(ch_client, clickhouse_test_database, tmp_path):
    """A deployed source, two selected sigma rules, and their propagated bindings.

    The source aliases ``Message`` to the default profile's ``_raw``, whose use case
    renders a text index. The landing table and the ``{source}_sigma`` view are both
    the DDL a deploy generates.
    """
    db = clickhouse_test_database
    sources = SourceRegistry(sources_directory=str(tmp_path / "sources"))
    rules = RuleRegistry(rules_directory=str(tmp_path / "rules"))
    hunts = HuntConfigRegistry(hunts_directory=str(tmp_path / "hunts"))
    try:
        sources.save_source(
            {
                "source": _SOURCE,
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": _SOURCE},
                "header": {"type": "timeseries", "version": "1.0.0"},
                "schema": {"ttl_days": 90, "engine": ""},
                "views": [
                    {
                        "standard": "sigma",
                        "taxonomy": "windows",
                        "custom_mappings": {"Message": "_raw"},
                    }
                ],
            }
        )
        source = sources.get_source(_SOURCE)

        builder = SchemaBuilderV2(resolver=EngineResolver(client=ch_client))
        result = builder.build(source)
        assert not result.validation_errors, result.validation_errors
        statements, _ = deploy_statements_for_build(
            builder, source, source.runtime_version_id(), result, db=db, ch_client=ch_client
        )
        for statement in statements:
            ch_client.command(statement)
        ch_client.insert(
            f"`{db}`.`{source.table_name}`",
            [["acme", _SOURCE, line] for line in _RAW_LINES],
            column_names=["_org_id", "_source", "_raw"],
        )

        crud = GitCrud(
            GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
        )
        catalog = SigmaCatalogStore(crud)
        selection = SigmaSelectionStore(crud)
        for rule_id, needle in _NEEDLES.items():
            docs, _ = parse_sigma_yaml(_rule_yaml(rule_id, needle), origin="file")
            catalog.import_docs(docs, actor="tester", source="file")
            selection.select(rule_id, actor="tester")

        report = SigmaPropagator(
            catalog=catalog,
            selection=selection,
            source_mapper=SigmaSourceMapper(sources),
            rule_registry=rules,
            hunt_registry=hunts,
            actor="tester",
        ).propagate(create_hunts=False)
        assert report.failed == [], report.failed

        yield db, {rule_id: rules.get(binding_rule_id(rule_id, _SOURCE)) for rule_id in _NEEDLES}
    finally:
        rules.close()
        hunts.close()
        sources.close()


def _matching(ch_client, db: str, binding: Rule, where: str) -> list[str]:
    rows = ch_client.query(
        f"SELECT Message FROM `{db}`.`{binding.source_table}` WHERE {where} ORDER BY Message"
    ).result_rows
    return [row[0] for row in rows]


def test_the_lowered_binding_selects_what_ilike_selects(ch_client, deployed):
    db, bindings = deployed
    binding = bindings[_ASCII_RULE]
    assert binding.source_table == sigma_view_name(_SOURCE)
    assert binding.where_clause == "lower(Message) LIKE '%mimikatz%'"

    lowered = _matching(ch_client, db, binding, binding.where_clause)
    ilike = _matching(ch_client, db, binding, "Message ILIKE '%Mimikatz%'")

    assert lowered == ilike
    assert lowered == sorted(_ASCII_MATCHES)


@pytest.mark.xfail(
    strict=True,
    reason="ClickHouse lower() folds ASCII only while the needle is folded in full, "
    "so an upper-case non-ASCII letter in the data stops matching",
)
def test_a_non_ascii_needle_selects_what_ilike_selects(ch_client, deployed):
    db, bindings = deployed
    binding = bindings[_NON_ASCII_RULE]
    assert binding.where_clause == "lower(Message) LIKE '%ärger%'"

    lowered = _matching(ch_client, db, binding, binding.where_clause)
    ilike = _matching(ch_client, db, binding, "Message ILIKE '%Ärger%'")

    assert lowered == ilike


@pytest.mark.xfail(
    strict=True,
    reason="the deployed _raw text index is splitByNonAlpha over the bare column, so "
    "lower(_raw) LIKE '%x%' cannot read it (dfe-engine#496)",
)
def test_the_lowered_binding_reads_the_text_index(ch_client, deployed):
    db, bindings = deployed
    binding = bindings[_ASCII_RULE]

    plan = ch_client.query(
        f"EXPLAIN indexes = 1 SELECT Message FROM `{db}`.`{binding.source_table}` "
        f"WHERE {binding.where_clause}"
    ).result_rows
    explain = "\n".join(row[0] for row in plan)

    assert "idx__raw" in explain, explain
