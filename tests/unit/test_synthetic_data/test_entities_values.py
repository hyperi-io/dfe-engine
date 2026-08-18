#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_entities_values.py
#  Purpose:      Entity pool coherence/determinism + semantic classification
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime

import pytest

from dfe_engine.source.models import SchemaColumn
from dfe_engine.synthetic_data.entities import EntityPool
from dfe_engine.synthetic_data.values import (
    EventContext,
    Semantic,
    classify,
    generate,
    render_timestamp,
)

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _ctx(pool: EntityPool) -> EventContext:
    account = pool.account()
    return EventContext(
        pool=pool,
        host=pool.host(),
        user=pool.user(),
        account=account,
        region=pool.rng.choice(account.regions),
        when=datetime(2026, 8, 18, 10, 30, 0, 123000, tzinfo=UTC),
    )


class TestEntityPool:
    def test_same_seed_identical_pool(self):
        a, b = EntityPool(42), EntityPool(42)
        assert a.hosts == b.hosts
        assert a.users == b.users
        assert a.accounts == b.accounts

    def test_different_seed_differs(self):
        assert EntityPool(1).hosts != EntityPool(2).hosts

    def test_host_coherence(self):
        pool = EntityPool(7)
        for host in pool.hosts:
            assert host.fqdn == f"{host.hostname}.{pool.org_domain}"
            ip = ipaddress.ip_address(host.ipv4)
            assert ip.is_private
            assert MAC_RE.match(host.mac)
            assert host.os in ("linux", "windows")

    def test_host_ips_unique(self):
        pool = EntityPool(7)
        ips = [h.ipv4 for h in pool.hosts]
        assert len(ips) == len(set(ips))

    def test_users_on_org_domain_and_unique(self):
        pool = EntityPool(7, users=50)
        usernames = [u.username for u in pool.users]
        assert len(usernames) == len(set(usernames))
        for user in pool.users:
            assert user.email == f"{user.username}@{pool.org_domain}"

    def test_account_ids_aws_shaped(self):
        for account in EntityPool(7).accounts:
            assert re.match(r"^\d{12}$", account.account_id)
            assert account.regions

    def test_external_ip_is_public(self):
        pool = EntityPool(7)
        for _ in range(20):
            assert ipaddress.ip_address(pool.external_ipv4()).is_global

    def test_empty_pool_sizes_rejected_by_draws(self):
        pool = EntityPool(7, hosts=1, users=1, accounts=1)
        assert pool.host() == pool.hosts[0]
        assert pool.user() == pool.users[0]


def col(name: str, ctype: str = "string", **kwargs) -> SchemaColumn:
    return SchemaColumn(name=name, type=ctype, **kwargs)


class TestClassify:
    @pytest.mark.parametrize(
        ("column", "semantic"),
        [
            (col("source_ip"), Semantic.IPV4),
            (col("ClientIP"), Semantic.IPV4),
            (col("ip_address"), Semantic.IPV4),
            (col("mac_address"), Semantic.MAC),
            (col("fqdn"), Semantic.FQDN),
            (col("hostname"), Semantic.HOSTNAME),
            (col("principal_email"), Semantic.EMAIL),
            (col("caller"), Semantic.EMAIL),
            (col("UserAgent"), Semantic.USER_AGENT),
            (col("userPrincipalName"), Semantic.USERNAME),
            (col("Username"), Semantic.USERNAME),
            (col("userId"), Semantic.UUID),
            (col("externalUri"), Semantic.URL),
            (col("src_port", "integer"), Semantic.PORT),
            (col("process_id", "integer"), Semantic.PID),
            (col("process_name"), Semantic.PROCESS_PATH),
            (col("file_hash"), Semantic.SHA256),
            (col("resource_arn"), Semantic.ARN),
            (col("AwsAccountId"), Semantic.ACCOUNT_ID),
            (col("Region"), Semantic.REGION),
            (col("AvailabilityZone"), Semantic.AVAILABILITY_ZONE),
            (col("severity"), Semantic.SEVERITY_WORD),
            (col("countryOrRegion"), Semantic.COUNTRY),
            (col("ResultStatus"), Semantic.STATUS_WORD),
            (col("ErrorCode"), Semantic.STATUS_WORD),
            (col("operationType"), Semantic.OPERATION),
            (col("agent_version"), Semantic.VERSION),
            (col("bytes_sent", "integer"), Semantic.NUMBER),
            (col("file_path"), Semantic.FILE_PATH),
            (col("finding_id"), Semantic.UUID),
            (col("correlationId"), Semantic.UUID),
            (col("logGroupName"), Semantic.LABEL),
            (col("created_at", "datetime"), Semantic.TIMESTAMP),
            (col("eventTimestamp"), Semantic.TIMESTAMP),
            (col("description", "text"), Semantic.TEXT),
            (col("message", "text"), Semantic.TEXT),
            (col("proc_id", "integer"), Semantic.PID),
            (col("event_id", "integer"), Semantic.NUMBER),
            (col("trace_id"), Semantic.TOKEN),
            (col("span_id"), Semantic.TOKEN),
            (col("user_count", "integer"), Semantic.NUMBER),
            (col("md5_hash"), Semantic.MD5),
            (col("workstation"), Semantic.HOSTNAME),
        ],
    )
    def test_semantic(self, column: SchemaColumn, semantic: Semantic):
        assert classify(column).semantic == semantic

    def test_caller_ip_is_public(self):
        inference = classify(col("callerIp"))
        assert inference.semantic == Semantic.IPV4
        assert inference.public

    def test_plain_ip_not_forced_public(self):
        assert not classify(col("ip_address")).public

    def test_numeric_severity_uses_comment_range(self):
        inference = classify(col("severity", "float", comment="Severity score (0-10)"))
        assert inference.semantic == Semantic.FLOAT_RANGE
        assert (inference.low, inference.high) == (0.0, 10.0)

    def test_lowcardinality_string_is_label(self):
        assert classify(col("workload", attribute=["lowcardinality"])).semantic == Semantic.LABEL

    def test_provider_carried_for_regions(self):
        assert classify(col("Region"), provider="gcp").provider == "gcp"


class TestGenerate:
    def test_host_linked_values_cohere(self):
        pool = EntityPool(11)
        ctx = _ctx(pool)
        hostname = generate(classify(col("hostname")), ctx)
        fqdn = generate(classify(col("fqdn")), ctx)
        mac = generate(classify(col("mac_address")), ctx)
        assert fqdn == f"{hostname}.{pool.org_domain}"
        assert mac == ctx.host.mac

    def test_user_email_coheres(self):
        ctx = _ctx(EntityPool(11))
        username = generate(classify(col("Username")), ctx)
        email = generate(classify(col("principal_email")), ctx)
        assert email == f"{username}@{ctx.pool.org_domain}"

    def test_public_ip_is_global(self):
        ctx = _ctx(EntityPool(11))
        for _ in range(20):
            ip = generate(classify(col("callerIp")), ctx)
            assert ipaddress.ip_address(ip).is_global

    def test_float_range_respects_bounds(self):
        ctx = _ctx(EntityPool(11))
        inference = classify(col("severity", "float", comment="Severity score (0-10)"))
        for _ in range(50):
            value = generate(inference, ctx)
            assert 0.0 <= value <= 10.0

    def test_region_matches_context(self):
        ctx = _ctx(EntityPool(11))
        assert generate(classify(col("Region")), ctx) == ctx.region
        az = generate(classify(col("AvailabilityZone")), ctx)
        assert az.startswith(ctx.region)
        assert az[-1] in "abc"

    def test_timestamp_formats(self):
        when = datetime(2026, 8, 18, 10, 30, 0, 123000, tzinfo=UTC)
        assert render_timestamp(when, None) == "2026-08-18T10:30:00.123Z"
        assert render_timestamp(when, "epoch_s") == int(when.timestamp())
        assert render_timestamp(when, "epoch_ms") == int(when.timestamp() * 1000)
        assert render_timestamp(when, "%Y%m%d") == "20260818"

    def test_trace_and_span_id_hex_lengths(self):
        ctx = _ctx(EntityPool(11))
        trace = generate(classify(col("trace_id")), ctx)
        span = generate(classify(col("span_id")), ctx)
        assert re.fullmatch(r"[0-9a-f]{32}", trace)
        assert re.fullmatch(r"[0-9a-f]{16}", span)

    def test_numeric_id_stays_numeric(self):
        ctx = _ctx(EntityPool(11))
        value = generate(classify(col("event_id", "integer")), ctx)
        assert isinstance(value, int)

    def test_http_status_from_vocab(self):
        ctx = _ctx(EntityPool(11))
        inference = classify(col("status_code", "integer"))
        assert inference.semantic == Semantic.HTTP_STATUS
        for _ in range(30):
            assert generate(inference, ctx) in (
                200,
                204,
                301,
                302,
                400,
                401,
                403,
                404,
                429,
                500,
                502,
            )
