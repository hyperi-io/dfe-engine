#  Project:      dfe-engine
#  File:         tests/integration/test_sigma_clickhouse.py
#  Purpose:      Real SigmaHQ rules, converted as propagation converts them, match known rows
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SigmaHQ rules run on real ClickHouse, over the same ``{source}_sigma`` view hunts read.

Each rule is converted by ``convert_detection_to_where``, the call propagation makes,
then filtered over a view that pulls Sigma fields out of ``_json``. The landed rows
are Sysmon-shaped JSON, so a rule only matches if the generated SQL means in
ClickHouse what the Sigma rule means.

The rules in ``tests/fixtures/sigma`` are SigmaHQ's, verbatim, under the Detection
Rule License 1.1.
"""

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from common.hunt_files import write_hunt
from sigma.collection import SigmaCollection

from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.hunts.runner import HuntRunner
from dfe_engine.hunts.schedule import current_fire
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.hunts.worker import HuntWorker
from dfe_engine.sigma.propagation import build_binding_rule, convert_detection_to_where
from dfe_engine.sigma.views import SigmaViewColumn, SigmaViewDefinition, build_sigma_view_ddl

FIXTURES = Path(__file__).parent.parent / "fixtures" / "sigma"
VIEW = "main_sigma"
JSON_FIELDS = (
    "Image",
    "OriginalFileName",
    "CommandLine",
    "Initiated",
    "DestinationIp",
    "EventID",
)

DECODE = "certutil.exe -decode in.b64 out.exe"
ROWS: dict[str, dict[str, str | int]] = {
    "decode_by_image": {"Image": r"C:\Windows\System32\certutil.exe", "CommandLine": DECODE},
    "decode_by_slash_flag": {
        "Image": r"C:\Windows\System32\certutil.exe",
        "CommandLine": "certutil.exe /decode in out",
    },
    "decode_image_in_capitals": {
        "Image": r"C:\WINDOWS\SYSTEM32\CERTUTIL.EXE",
        "CommandLine": "CERTUTIL.EXE -DECODE in out",
    },
    "decode_renamed_binary": {
        "Image": r"C:\temp\renamed.exe",
        "OriginalFileName": "CertUtil.exe",
        "CommandLine": DECODE,
    },
    "decode_other_binary": {"Image": r"C:\Windows\notepad.exe", "CommandLine": DECODE},
    "certutil_without_decode": {
        "Image": r"C:\Windows\System32\certutil.exe",
        "CommandLine": "certutil.exe -urlcache -f http://x/y",
    },
    "regini_stream": {"Image": r"C:\Windows\regini.exe", "CommandLine": r"regini.exe c:\a.txt:b"},
    "regini_plain": {"Image": r"C:\Windows\regini.exe", "CommandLine": r"regini.exe c:\a.txt"},
    "schtasks_change_temp": {
        "Image": r"C:\Windows\System32\schtasks.exe",
        "CommandLine": r"schtasks /Change /TN x /TR C:\Users\Public\p.exe powershell",
    },
    "schtasks_change_percent": {
        "Image": r"C:\Windows\System32\schtasks.exe",
        "CommandLine": "schtasks /Change /TN x /TR %appdata%\\a.exe cmd /c calc",
    },
    "schtasks_change_benign": {
        "Image": r"C:\Windows\System32\schtasks.exe",
        "CommandLine": r"schtasks /Change /TN x /ENABLE",
    },
    "schtasks_create": {
        "Image": r"C:\Windows\System32\schtasks.exe",
        "CommandLine": r"schtasks /Create /TN x /TR C:\Users\Public\p.exe powershell",
    },
    "script_to_public_ipv4": {
        "Image": r"C:\Windows\System32\wscript.exe",
        "Initiated": "true",
        "DestinationIp": "93.184.216.34",
    },
    "script_to_private_ipv4": {
        "Image": r"C:\Windows\System32\wscript.exe",
        "Initiated": "true",
        "DestinationIp": "10.1.2.3",
    },
    "script_to_ipv6_link_local": {
        "Image": r"C:\Windows\System32\cscript.exe",
        "Initiated": "true",
        "DestinationIp": "fe80::1",
    },
    "script_to_microsoft_range": {
        "Image": r"C:\Windows\System32\cscript.exe",
        "Initiated": "true",
        "DestinationIp": "20.1.2.3",
    },
    "script_to_a_dash": {
        "Image": r"C:\Windows\System32\wscript.exe",
        "Initiated": "true",
        "DestinationIp": "-",
    },
    "script_without_an_address": {"Image": r"C:\Windows\System32\wscript.exe", "Initiated": "true"},
    "other_binary_to_public_ipv4": {
        "Image": r"C:\Windows\System32\curl.exe",
        "Initiated": "true",
        "DestinationIp": "93.184.216.34",
    },
    "replay_attack_event": {"EventID": 4649},
    "replay_attack_event_as_text": {"EventID": "4649"},
    "other_event": {"EventID": 4624},
}

# rule fixture -> the rows it must match, and no others.
EXPECTED: dict[str, set[str]] = {
    "certutil_decode.yml": {
        "decode_by_image",
        "decode_by_slash_flag",
        "decode_image_in_capitals",
        "decode_renamed_binary",
    },
    "regini_ads.yml": {"regini_stream"},
    "schtasks_change.yml": {"schtasks_change_temp", "schtasks_change_percent"},
    "wscript_outbound.yml": {
        "script_to_public_ipv4",
        "script_to_a_dash",
        "script_without_an_address",
    },
    "replay_attack.yml": {"replay_attack_event", "replay_attack_event_as_text"},
}


@pytest.fixture
def sigma_view(ch_client, dfe_db) -> str:
    """The landing table's Sigma view, which pulls the Sigma fields out of ``_json``."""
    definition = SigmaViewDefinition(
        source_name="main",
        columns=[SigmaViewColumn(sigma_field=name, json_path=name) for name in JSON_FIELDS],
    )
    ch_client.command(build_sigma_view_ddl(definition, db=dfe_db, table_name="main"))
    return f"`{dfe_db}`.`{VIEW}`"


def _where_for(fixture: str) -> str:
    """The WHERE condition propagation generates for a vendored rule."""
    rule = SigmaCollection.from_yaml((FIXTURES / fixture).read_text(encoding="utf-8")).rules[0]
    return convert_detection_to_where(rule.to_dict())


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_a_sigmahq_rule_matches_exactly_the_rows_it_describes(
    ch_client, dfe_db, sigma_view, fixture
):
    for case, payload in ROWS.items():
        ch_client.insert(
            f"{dfe_db}.main", [[case, json.dumps(payload)]], column_names=["_org_id", "_json"]
        )
    where = _where_for(fixture)

    result = ch_client.query(f"SELECT _org_id FROM {sigma_view} WHERE ({where}) ORDER BY _org_id")

    assert {row[0] for row in result.result_rows} == EXPECTED[fixture], where


@pytest.mark.xfail(
    strict=True,
    reason="a plain string value is matched with =, which is case-sensitive where Sigma is not",
)
def test_a_plain_value_matches_whatever_its_case(ch_client, dfe_db, sigma_view):
    """OriginalFileName is a plain value in certutil_decode.yml, so 'certutil.exe' must match."""
    payload = {"Image": r"C:\temp\r.exe", "OriginalFileName": "certutil.exe", "CommandLine": DECODE}
    ch_client.insert(
        f"{dfe_db}.main",
        [["lowercase_name", json.dumps(payload)]],
        column_names=["_org_id", "_json"],
    )

    result = ch_client.query(
        f"SELECT _org_id FROM {sigma_view} WHERE ({_where_for('certutil_decode.yml')})"
    )

    assert result.result_rows == [("lowercase_name",)]


def test_a_sigmahq_rule_runs_as_a_hunt_over_the_sigma_view(ch_client, dfe_db, sigma_view, tmp_path):
    """The generated rule and the per-source hunt propagation writes land a detection row."""
    marker = f"org-{uuid.uuid4().hex[:8]}"
    sigma_id = "cc9cbe82-7bc0-4ef5-bc23-bbfb83947be7"
    hunt = f"sigma_hunt_{uuid.uuid4().hex[:8]}"
    rule = build_binding_rule(
        sigma_id,
        {"title": "File Decoded Via Certutil", "rule": {"level": "high"}},
        "main",
        _where_for("certutil_decode.yml"),
        database=dfe_db,
    )
    registry = RuleRegistry(rules_directory=tmp_path / "rules", writable=True, refresh_interval=0)
    registry.save(rule)
    registry.close()
    write_hunt(tmp_path / "hunts", hunt, rule.rule_id, dfe_db, source=VIEW, customers=[marker])

    fire = current_fire(hunt, 60, int(time.time()))
    landed = datetime.fromtimestamp(fire - 1, UTC)
    for payload in (ROWS["decode_by_image"], ROWS["decode_other_binary"]):
        ch_client.insert(
            f"{dfe_db}.main",
            [[landed, landed, marker, json.dumps(payload)]],
            column_names=["_timestamp_load", "_timestamp", "_org_id", "_json"],
        )
    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()

    assert HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=8).tick(fire + 1) == 1

    rows = ch_client.query(
        f"SELECT rule_id, source_table FROM `{dfe_db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows
    assert rows == [(rule.rule_id, VIEW)]
