#  Project:      dfe-engine
#  File:         synthetic_data/values.py
#  Purpose:      Semantic column classification + typed value generation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Semantic classification and typed value generation.

The classifier maps a schema column (name tokens + primitive type + attributes
+ comment) to a semantic type; the generator renders a realistic value for
that semantic type from the event's entity context, so values cohere within an
event (the hostname, FQDN, IP and MAC all belong to the same generated host).

This is deliberately heuristic, not ML: for machine-data fields the name and
type carry the semantics ("source_ip", "AwsAccountId", type datetime), and a
reference pack can always override a column with explicit ``synthetic:`` hints
where a heuristic cannot reach (finding-type vocabularies, message templates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from dfe_engine.source.type_registry import LOW_CARDINALITY

if TYPE_CHECKING:
    from dfe_engine.source.models import SchemaColumn
    from dfe_engine.synthetic_data.entities import CloudAccount, EntityPool, Host, User


class Semantic(StrEnum):
    """Semantic value families the generator knows how to render."""

    TIMESTAMP = "timestamp"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    MAC = "mac"
    FQDN = "fqdn"
    HOSTNAME = "hostname"
    EMAIL = "email"
    USERNAME = "username"
    USER_AGENT = "user_agent"
    URL = "url"
    UUID = "uuid"
    PORT = "port"
    PID = "pid"
    PROCESS_PATH = "process_path"
    FILE_PATH = "file_path"
    SHA256 = "sha256"
    MD5 = "md5"
    ARN = "arn"
    ACCOUNT_ID = "account_id"
    REGION = "region"
    AVAILABILITY_ZONE = "availability_zone"
    SEVERITY_WORD = "severity_word"
    STATUS_WORD = "status_word"
    HTTP_STATUS = "http_status"
    OPERATION = "operation"
    COUNTRY = "country"
    VERSION = "version"
    NUMBER = "number"
    FLOAT_RANGE = "float_range"
    LABEL = "label"
    TEXT = "text"
    TOKEN = "token"


@dataclass(frozen=True, slots=True)
class Inference:
    """A classified column: semantic family + generation parameters."""

    semantic: Semantic
    low: float | None = None
    high: float | None = None
    public: bool = False
    provider: str = "aws"


@dataclass(slots=True)
class EventContext:
    """Per-event entity context - one coherent draw shared by every column.

    Attributes:
        pool: The entity pool values are drawn from.
        host: This event's host (hostname/fqdn/ip/mac cohere).
        user: This event's user (username/email cohere).
        account: This event's cloud account.
        region: This event's region (stable across the event's columns).
        when: This event's timestamp.
    """

    pool: EntityPool
    host: Host
    user: User
    account: CloudAccount
    region: str
    when: datetime
    _template_map: dict[str, Any] | None = field(default=None, repr=False)

    def template_map(self) -> dict[str, Any]:
        """Placeholder values for hint templates, computed once per event."""
        if self._template_map is None:
            pool = self.pool
            self._template_map = {
                "hostname": self.host.hostname,
                "fqdn": self.host.fqdn,
                "ipv4": self.host.ipv4,
                "mac": self.host.mac,
                "os": self.host.os,
                "username": self.user.username,
                "email": self.user.email,
                "uid": self.user.uid,
                "account_id": self.account.account_id,
                "region": self.region,
                "external_ipv4": pool.external_ipv4(),
                "port": _port(pool),
                "pid": pool.rng.randint(300, 65000),
                "uuid": pool.fake.uuid4(),
                "process": _process_path(self),
                "service": pool.rng.choice(_LINUX_PROCS),
                "severity": pool.rng.choice(_SEVERITY_WORDS),
                "domain": pool.org_domain,
                "duration_ms": pool.rng.randint(2, 1800),
                "count": pool.rng.randint(1, 5000),
                "http_status": pool.rng.choices(_HTTP_STATUSES, weights=_HTTP_STATUS_WEIGHTS, k=1)[
                    0
                ],
                "user_agent": pool.fake.user_agent(),
                "apache_ts": self.when.astimezone(UTC).strftime("%d/%b/%Y:%H:%M:%S +0000"),
            }
        return self._template_map


_REGIONS: dict[str, tuple[str, ...]] = {
    "aws": (
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "eu-central-1",
        "ap-southeast-2",
        "ap-northeast-1",
    ),
    "azure": ("eastus", "westus2", "westeurope", "northeurope", "australiaeast", "southeastasia"),
    "gcp": ("us-central1", "us-east1", "europe-west1", "asia-east1", "australia-southeast1"),
}

_SEVERITY_WORDS = ("informational", "low", "low", "medium", "medium", "medium", "high", "critical")
_STATUS_WORDS = ("Success", "Success", "Success", "Success", "Failure", "InProgress")
_COUNTRIES = ("US", "US", "US", "GB", "DE", "AU", "AU", "IN", "NL", "FR", "BR", "CN")
_OPERATION_VERBS = ("Create", "Delete", "Update", "Get", "List", "Put", "Assume", "Describe")
_OPERATION_NOUNS = ("User", "Role", "Bucket", "Instance", "Key", "Policy", "Session", "Object")
_WELL_KNOWN_PORTS = (22, 443, 443, 443, 80, 80, 3389, 3306, 5432, 8080, 53, 25)
_HTTP_STATUSES = (200, 204, 301, 302, 400, 401, 403, 404, 429, 500, 502)
_HTTP_STATUS_WEIGHTS = (55, 5, 3, 3, 6, 8, 7, 8, 2, 2, 1)
_LINUX_PROCS = ("sshd", "nginx", "postgres", "python3", "java", "cron", "systemd", "dockerd")
_WINDOWS_PROCS = ("svchost.exe", "lsass.exe", "explorer.exe", "powershell.exe", "w3wp.exe")

# Comment-embedded numeric range, e.g. "Severity score (0-10)".
_RANGE_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\)")
# camelCase boundary for tokenisation.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _tokens(name: str) -> set[str]:
    """Lower-cased token set of a column/path name (camel, snake, dots, spaces)."""
    snake = _CAMEL_RE.sub("_", name)
    return {t for t in re.split(r"[^A-Za-z0-9]+", snake.lower()) if t}


def _comment_range(comment: str | None) -> tuple[float, float] | None:
    if not comment:
        return None
    m = _RANGE_RE.search(comment)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


_NUMERIC_TYPES = frozenset({"integer", "int", "float", "double", "long"})


def classify(column: SchemaColumn, *, provider: str = "aws") -> Inference:
    """Classify a schema column into a semantic family.

    Rules are ordered most-specific first; the fallbacks at the end key off
    the primitive type alone. ``provider`` seeds cloud-specific vocabularies
    (regions) and normally comes from the meta-schema path (``meta/aws/...``).

    Args:
        column: The schema column (name/type/attribute/use_case/comment).
        provider: Cloud provider hint - ``aws``, ``azure`` or ``gcp``.
    """
    t = _tokens(column.name)
    ctype = (column.type or "string").lower()
    numeric = ctype in _NUMERIC_TYPES
    rng_hint = _comment_range(column.comment)

    if ctype == "datetime" or t & {"time", "timestamp", "date", "at", "utc"}:
        return Inference(Semantic.TIMESTAMP)
    if "ipv6" in t:
        return Inference(Semantic.IPV6)
    if t & {"ip", "ipaddress"}:
        public = bool(t & {"source", "client", "caller", "remote", "external"})
        return Inference(Semantic.IPV4, public=public)
    if "mac" in t:
        return Inference(Semantic.MAC)
    if t & {"fqdn", "domain"}:
        return Inference(Semantic.FQDN)
    if t & {"hostname", "host", "workstation", "machine", "computer", "device", "endpoint"}:
        return Inference(Semantic.HOSTNAME)
    if t & {"email", "caller"}:
        return Inference(Semantic.EMAIL)
    if {"user", "agent"} <= t:
        return Inference(Semantic.USER_AGENT)
    if {"user", "id"} <= t or "uid" in t:
        return Inference(Semantic.UUID)
    if not numeric and ("username" in t or {"user", "name"} <= t or t & {"user", "actor"}):
        return Inference(Semantic.USERNAME)
    if t & {"url", "uri"}:
        return Inference(Semantic.URL)
    if "port" in t:
        return Inference(Semantic.PORT)
    if t & {"pid", "proc"} or {"process", "id"} <= t:
        return Inference(Semantic.PID)
    if {"trace", "id"} <= t:
        return Inference(Semantic.TOKEN, low=32)
    if {"span", "id"} <= t:
        return Inference(Semantic.TOKEN, low=16)
    if t & {"process", "image"}:
        return Inference(Semantic.PROCESS_PATH)
    if "md5" in t:
        return Inference(Semantic.MD5)
    if t & {"sha256", "hash"}:
        return Inference(Semantic.SHA256)
    if "arn" in t:
        return Inference(Semantic.ARN)
    if "account" in t and "id" in t:
        return Inference(Semantic.ACCOUNT_ID)
    if {"availability", "zone"} <= t:
        return Inference(Semantic.AVAILABILITY_ZONE, provider=provider)
    if "country" in t:
        return Inference(Semantic.COUNTRY)
    if "region" in t:
        return Inference(Semantic.REGION, provider=provider)
    if t & {"severity", "level"}:
        if numeric:
            low, high = rng_hint or (0.0, 10.0)
            return Inference(Semantic.FLOAT_RANGE, low=low, high=high)
        return Inference(Semantic.SEVERITY_WORD)
    if t & {"status", "state", "result", "error", "determination", "classification", "disposition"}:
        if numeric:
            return Inference(Semantic.HTTP_STATUS)
        return Inference(Semantic.STATUS_WORD)
    if t & {"operation", "method", "action", "event"} and t & {"name", "type", "operation"}:
        return Inference(Semantic.OPERATION)
    if "version" in t:
        return Inference(Semantic.VERSION)
    if t & {"bytes", "size", "count"}:
        low, high = rng_hint or (0.0, 0.0)
        return Inference(Semantic.NUMBER, low=low or None, high=high or None)
    if t & {"path", "file"}:
        return Inference(Semantic.FILE_PATH)
    if "id" in t or t & {"guid", "uuid"}:
        # A numeric id is a counter-style value, not a GUID.
        return (
            Inference(Semantic.NUMBER, low=1000, high=10**9)
            if numeric
            else Inference(Semantic.UUID)
        )
    if "name" in t:
        return Inference(Semantic.LABEL)

    # Type-only fallbacks.
    if ctype == "text" or t & {"message", "description", "title", "summary", "payload"}:
        return Inference(Semantic.TEXT)
    if numeric and ctype in {"float", "double"}:
        low, high = rng_hint or (0.0, 100.0)
        return Inference(Semantic.FLOAT_RANGE, low=low, high=high)
    if numeric:
        low, high = rng_hint or (0, 10000)
        return Inference(Semantic.NUMBER, low=low, high=high)
    if column.declared_cardinality == LOW_CARDINALITY or column.use_case == "dimension":
        return Inference(Semantic.LABEL)
    return Inference(Semantic.TOKEN)


# ── generation ────────────────────────────────────────────────────


def _port(pool: EntityPool) -> int:
    if pool.rng.random() < 0.6:
        return pool.rng.choice(_WELL_KNOWN_PORTS)
    return pool.rng.randint(1024, 65535)


def _process_path(ctx: EventContext) -> str:
    if ctx.host.os == "windows":
        return f"C:\\Windows\\System32\\{ctx.pool.rng.choice(_WINDOWS_PROCS)}"
    return f"/usr/sbin/{ctx.pool.rng.choice(_LINUX_PROCS)}"


def paced_timestamps(rng: Any, count: int, end: datetime, rate_eps: float) -> list[datetime]:
    """Poisson-spaced timestamps ending at ``end``, oldest first.

    A batch stamped this way reads as a live tail that has been running,
    not a block insert.
    """
    offsets = [0.0]
    for _ in range(count - 1):
        offsets.append(offsets[-1] + rng.expovariate(rate_eps))
    return [end - timedelta(seconds=offsets[-1] - offset) for offset in offsets]


def render_timestamp(when: datetime, fmt: str | None) -> Any:
    """Render a timestamp in the requested format (default RFC 3339 millis)."""
    if fmt in (None, "iso8601"):
        return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if fmt == "epoch_ms":
        return int(when.timestamp() * 1000)
    if fmt == "epoch_s":
        return int(when.timestamp())
    return when.astimezone(UTC).strftime(fmt)


def generate(inference: Inference, ctx: EventContext, *, fmt: str | None = None) -> Any:
    """Render a value for a classified column from the event context."""
    pool = ctx.pool
    rng = pool.rng
    fake = pool.fake
    match inference.semantic:
        case Semantic.TIMESTAMP:
            return render_timestamp(ctx.when, fmt)
        case Semantic.IPV4:
            if inference.public:
                return pool.external_ipv4()
            return ctx.host.ipv4 if rng.random() < 0.7 else pool.external_ipv4()
        case Semantic.IPV6:
            return fake.ipv6()
        case Semantic.MAC:
            return ctx.host.mac
        case Semantic.FQDN:
            return ctx.host.fqdn
        case Semantic.HOSTNAME:
            return ctx.host.hostname
        case Semantic.EMAIL:
            return ctx.user.email
        case Semantic.USERNAME:
            return ctx.user.username
        case Semantic.USER_AGENT:
            return fake.user_agent()
        case Semantic.URL:
            return fake.uri()
        case Semantic.UUID:
            return fake.uuid4()
        case Semantic.PORT:
            return _port(pool)
        case Semantic.PID:
            return rng.randint(300, 65000)
        case Semantic.PROCESS_PATH:
            return _process_path(ctx)
        case Semantic.FILE_PATH:
            return fake.file_path(depth=rng.randint(1, 3))
        case Semantic.SHA256:
            return fake.sha256()
        case Semantic.MD5:
            return fake.md5()
        case Semantic.ARN:
            return rng.choice(
                (
                    f"arn:aws:iam::{ctx.account.account_id}:user/{ctx.user.username}",
                    f"arn:aws:s3:::{fake.word()}-{fake.word()}",
                    f"arn:aws:ec2:{ctx.region}:{ctx.account.account_id}:instance/i-"
                    + fake.hexify(text="^" * 17),
                )
            )
        case Semantic.ACCOUNT_ID:
            return ctx.account.account_id
        case Semantic.REGION:
            return ctx.region
        case Semantic.AVAILABILITY_ZONE:
            return f"{ctx.region}{rng.choice('abc')}"
        case Semantic.SEVERITY_WORD:
            return rng.choice(_SEVERITY_WORDS)
        case Semantic.STATUS_WORD:
            return rng.choice(_STATUS_WORDS)
        case Semantic.HTTP_STATUS:
            return rng.choices(_HTTP_STATUSES, weights=_HTTP_STATUS_WEIGHTS, k=1)[0]
        case Semantic.OPERATION:
            return rng.choice(_OPERATION_VERBS) + rng.choice(_OPERATION_NOUNS)
        case Semantic.COUNTRY:
            return rng.choice(_COUNTRIES)
        case Semantic.VERSION:
            return f"{rng.randint(1, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 30)}"
        case Semantic.NUMBER:
            if inference.low is not None and inference.high is not None:
                return rng.randint(int(inference.low), int(inference.high))
            # Heavy-tailed byte/size counts look more real than uniform draws.
            return min(int(rng.lognormvariate(6, 2)), 10**9)
        case Semantic.FLOAT_RANGE:
            return round(rng.uniform(inference.low or 0.0, inference.high or 100.0), 1)
        case Semantic.LABEL:
            return f"{fake.word()}-{fake.word()}"
        case Semantic.TEXT:
            return fake.sentence(nb_words=9)
        case Semantic.TOKEN:
            return fake.hexify(text="^" * int(inference.low or 16))
