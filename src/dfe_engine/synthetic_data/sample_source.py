#  Project:      dfe-engine
#  File:         synthetic_data/sample_source.py
#  Purpose:      Sample-derived lookalike event factory (scrub + regenerate)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sample-derived lookalike generation.

Takes a sample of real events (the sampler's parsed ``rows`` and/or raw
``lines``) and generates synthetic events that keep the sample's SHAPE while
never replaying its identities:

- **identity values** (IPs, MACs, UUIDs, emails, hosts, users) are
  SYNTHESISED from the entity pool - the sample's real identities never
  appear in the output.
- **enum-like keys** (low cardinality) replay the observed vocabulary,
  weighted by observed frequency.
- **numeric keys** draw from the observed range.
- **free-text keys** replay observed lines with identity tokens substituted
  in place, so message structure stays real but scrubbed.
- **timestamps** re-render the event clock in the observed format.

Classification is value-population based (does the column actually hold
IPs?), with the key NAME as the tie-breaker for semantics no regex can
validate (usernames, hostnames): a name-identified identity key is always
synthesised, never replayed, even when its cardinality looks enum-like.
Every value observed under an identity key also enters a LEXICON with a
stable synthesised replacement - free text is scrubbed against it and enum
replay excludes it, so an identity seen anywhere structured cannot ride out
through another key.

The scrub is best-effort, not proof: an identity that appears ONLY in free
text (never under a recognisable key) and matches no identity regex can
still replay - the raw ``lines`` mode carries exactly that residual risk.
Review output before publishing it outside the org.
"""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scalo.logger import logger

from dfe_engine.source.models import SchemaColumn
from dfe_engine.synthetic_data.entities import EntityPool
from dfe_engine.synthetic_data.models import SyntheticDataError
from dfe_engine.synthetic_data.values import (
    EventContext,
    Inference,
    Semantic,
    classify,
    generate,
    paced_timestamps,
    render_timestamp,
)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FQDN_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_PATH_RE = re.compile(r"^(/[^\s]*|[A-Za-z]:\\[^\s]*)$")

# Broad in-text identity patterns for scrubbing free text.
_TEXT_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TEXT_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[A-Za-z]{2,}\b")
_TEXT_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_TEXT_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")

# Name-identified semantics that must be synthesised, never replayed.
_IDENTITY_SEMANTICS = frozenset(
    {
        Semantic.USERNAME,
        Semantic.EMAIL,
        Semantic.HOSTNAME,
        Semantic.FQDN,
        Semantic.IPV4,
        Semantic.IPV6,
        Semantic.MAC,
        Semantic.UUID,
        Semantic.ACCOUNT_ID,
        Semantic.PID,
        Semantic.PROCESS_PATH,
        Semantic.FILE_PATH,
    }
)


@dataclass(slots=True)
class _KeyPlan:
    """How one flattened key is generated."""

    kind: str  # semantic | replay | number | float | text | timestamp | skip
    inference: Inference | None = None
    values: list[Any] | None = None
    weights: list[int] | None = None
    fmt: str | None = None
    low: float = 0.0
    high: float = 0.0
    observed: list[str] | None = None


def _is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def _is_ipv6(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def _frac(values: list[str], predicate) -> float:
    return sum(1 for v in values if predicate(v)) / len(values)


def _classify_strings(name: str, values: list[str]) -> _KeyPlan:
    """Value-population classification for string keys."""
    if _frac(values, _is_ipv4) >= 0.8:
        global_frac = _frac(values, lambda v: _is_ipv4(v) and ipaddress.ip_address(v).is_global)
        return _KeyPlan("semantic", inference=Inference(Semantic.IPV4, public=global_frac > 0.5))
    if _frac(values, _is_ipv6) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.IPV6))
    if _frac(values, lambda v: bool(_MAC_RE.match(v))) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.MAC))
    if _frac(values, lambda v: bool(_UUID_RE.match(v))) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.UUID))
    if _frac(values, lambda v: bool(_EMAIL_RE.match(v))) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.EMAIL))
    if _frac(values, lambda v: bool(_ISO_TS_RE.match(v))) >= 0.8:
        return _KeyPlan("timestamp", fmt="iso8601")
    if _frac(values, lambda v: v.startswith(("http://", "https://"))) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.URL))
    if _frac(values, lambda v: bool(_PATH_RE.match(v))) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.FILE_PATH))
    if _frac(values, lambda v: bool(_FQDN_RE.match(v)) and not v.replace(".", "").isdigit()) >= 0.8:
        return _KeyPlan("semantic", inference=Inference(Semantic.FQDN))

    # The key NAME breaks ties regexes cannot: an identity-named key is
    # synthesised even when its observed cardinality looks enum-like.
    name_inference = classify(SchemaColumn(name=name, type="string"))
    if name_inference.semantic in _IDENTITY_SEMANTICS:
        return _KeyPlan("semantic", inference=name_inference)

    counts = Counter(values)
    if len(counts) <= max(10, len(values) // 10):
        vocab = list(counts)
        return _KeyPlan("replay", values=vocab, weights=[counts[v] for v in vocab])
    if _frac(values, lambda v: bool(_HEX_RE.match(v))) >= 0.8:
        modal_len = Counter(len(v) for v in values).most_common(1)[0][0]
        return _KeyPlan("semantic", inference=Inference(Semantic.TOKEN, low=modal_len))
    if any(" " in v for v in values) or sum(len(v) for v in values) / len(values) > 30:
        counts = Counter(values)
        vocab = list(counts)
        return _KeyPlan("text", values=vocab, weights=[counts[v] for v in vocab])
    # Unknown high-cardinality short strings: an opaque token is the safe
    # default - replaying could leak an identity no regex recognised.
    return _KeyPlan("semantic", inference=Inference(Semantic.TOKEN, low=12))


def _plan_for(name: str, raw_values: list[Any]) -> _KeyPlan:
    values = [v for v in raw_values if v is not None]
    if not values:
        return _KeyPlan("skip")
    if all(isinstance(v, bool) for v in values):
        counts = Counter(values)
        vocab = list(counts)
        return _KeyPlan("replay", values=vocab, weights=[counts[v] for v in vocab])
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        low, high = min(values), max(values)
        # Epoch windows are bounded above (~year 2100) so snowflake-style ids
        # do not masquerade as timestamps.
        if 10**12 <= low and high < 4_102_444_800_000:
            return _KeyPlan("timestamp", fmt="epoch_ms")
        if 10**9 <= low and high < 4_102_444_800:
            return _KeyPlan("timestamp", fmt="epoch_s")
        return _KeyPlan("number", low=low, high=high)
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in values):
        return _KeyPlan("float", low=float(min(values)), high=float(max(values)))
    strings = [str(v) for v in values]
    plan = _classify_strings(name, strings)
    if (
        plan.kind == "semantic"
        and plan.inference is not None
        and plan.inference.semantic in _IDENTITY_SEMANTICS
    ):
        plan.observed = sorted(set(strings))
    return plan


def _flatten(
    row: dict[str, Any],
    prefix: tuple[str, ...] = (),
    dropped: set[tuple[str, ...]] | None = None,
) -> dict[tuple[str, ...], Any]:
    flat: dict[tuple[str, ...], Any] = {}
    for key, value in row.items():
        path = (*prefix, str(key))
        if isinstance(value, dict):
            flat.update(_flatten(value, path, dropped))
        elif isinstance(value, list):
            # List-valued keys are out of scope for lookalike v1.
            if dropped is not None:
                dropped.add(path)
        else:
            flat[path] = value
    return flat


def _set_nested(event: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = event
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


class SampleEventFactory:
    """Generates lookalike events from a sample's rows or raw lines.

    Args:
        rows: Parsed sample events (the sampler's ``rows``). Preferred input.
        lines: Raw sample lines; used when ``rows`` is empty - each generated
            event is ``{"message": <scrubbed line>}``.
        seed: Determinism seed.
        pool: Entity pool (built from ``seed`` when omitted).
        tags: Extra tags merged into the event's ``tags`` object.
        mark_synthetic: Emit ``tags.synthetic: true`` (default on).
        max_sample: Sample rows/lines analysed beyond which input is truncated.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        lines: list[str] | None = None,
        *,
        seed: int | None = None,
        pool: EntityPool | None = None,
        tags: dict[str, Any] | None = None,
        mark_synthetic: bool = True,
        max_sample: int = 1000,
    ) -> None:
        self.pool = pool or EntityPool(seed)
        self.tags = dict(tags or {})
        if mark_synthetic:
            self.tags.setdefault("synthetic", True)

        rows = (rows or [])[:max_sample]
        lines = (lines or [])[:max_sample]
        self._plans: dict[tuple[str, ...], _KeyPlan] = {}
        dropped: set[tuple[str, ...]] = set()
        if rows:
            populations: dict[tuple[str, ...], list[Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for path, value in _flatten(row, dropped=dropped).items():
                    populations.setdefault(path, []).append(value)
            for path, values in populations.items():
                plan = _plan_for(path[-1], values)
                if plan.kind != "skip":
                    self._plans[path] = plan
        elif lines:
            counts = Counter(str(line) for line in lines if str(line).strip())
            vocab = list(counts)
            if vocab:
                self._plans[("message",)] = _KeyPlan(
                    "text", values=vocab, weights=[counts[v] for v in vocab]
                )
        if dropped:
            logger.info(
                "lookalike: list-valued keys are not generated",
                keys=sorted(".".join(path) for path in dropped),
            )
        if not self._plans:
            raise SyntheticDataError("Sample has no usable rows or lines - nothing to generate")

        # Identity lexicon: every value observed under an identity-classified
        # key, mapped to a STABLE synthesised replacement. Text plans scrub
        # these tokens and enum replay excludes them, so a real username or
        # hostname can never ride out inside a message or a vocabulary.
        self._lexicon = self._build_lexicon()
        self._lexicon_re: re.Pattern[str] | None = None
        if self._lexicon:
            alternatives = "|".join(
                re.escape(value) for value in sorted(self._lexicon, key=len, reverse=True)
            )
            self._lexicon_re = re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])")
        self._filter_replay_vocabularies()

    def _build_lexicon(self) -> dict[str, str]:
        lexicon: dict[str, str] = {}
        identity_values: dict[str, Semantic] = {}
        for plan in self._plans.values():
            if (
                plan.kind == "semantic"
                and plan.inference is not None
                and plan.inference.semantic in _IDENTITY_SEMANTICS
                and plan.observed
            ):
                for value in plan.observed:
                    identity_values.setdefault(value, plan.inference.semantic)
        hosts = self.pool.hosts
        users = self.pool.users
        for i, (value, semantic) in enumerate(sorted(identity_values.items())):
            if semantic in (Semantic.HOSTNAME,):
                lexicon[value] = hosts[i % len(hosts)].hostname
            elif semantic in (Semantic.FQDN,):
                lexicon[value] = hosts[i % len(hosts)].fqdn
            elif semantic in (Semantic.EMAIL,):
                lexicon[value] = users[i % len(users)].email
            elif semantic in (Semantic.USERNAME,):
                lexicon[value] = users[i % len(users)].username
            else:
                lexicon[value] = self.pool.fake.hexify(text="^" * 12)
        return lexicon

    def _filter_replay_vocabularies(self) -> None:
        for path, plan in list(self._plans.items()):
            if plan.kind != "replay" or plan.values is None or plan.weights is None:
                continue
            kept = [
                (v, w)
                for v, w in zip(plan.values, plan.weights, strict=True)
                if not (isinstance(v, str) and v in self._lexicon)
            ]
            if not kept:
                self._plans[path] = _KeyPlan(
                    "semantic", inference=Inference(Semantic.TOKEN, low=12)
                )
                continue
            plan.values = [v for v, _ in kept]
            plan.weights = [w for _, w in kept]

    # -- event construction ----------------------------------------

    def event(self, when: datetime | None = None) -> dict[str, Any]:
        """Generate one lookalike event."""
        pool = self.pool
        account = pool.account()
        ctx = EventContext(
            pool=pool,
            host=pool.host(),
            user=pool.user(),
            account=account,
            region=pool.rng.choice(account.regions),
            when=when or datetime.now(UTC),
        )
        event: dict[str, Any] = {}
        for path, plan in self._plans.items():
            _set_nested(event, path, self._value(plan, ctx))
        tags_node = event.setdefault("tags", {})
        if isinstance(tags_node, dict):
            for key, value in self.tags.items():
                tags_node.setdefault(key, value)
        return event

    def events(
        self, count: int, *, end: datetime | None = None, rate_eps: float = 5.0
    ) -> list[dict[str, Any]]:
        """Generate a batch whose timestamps read as a live tail."""
        if count < 1:
            raise SyntheticDataError("count must be >= 1")
        stamps = paced_timestamps(self.pool.rng, count, end or datetime.now(UTC), rate_eps)
        return [self.event(when=stamp) for stamp in stamps]

    def _value(self, plan: _KeyPlan, ctx: EventContext) -> Any:
        rng = ctx.pool.rng
        match plan.kind:
            case "semantic" if plan.inference is not None:
                return generate(plan.inference, ctx)
            case "replay" if plan.values is not None:
                return rng.choices(plan.values, weights=plan.weights, k=1)[0]
            case "number":
                return rng.randint(int(plan.low), int(plan.high))
            case "float":
                return round(rng.uniform(plan.low, plan.high), 3)
            case "timestamp":
                return render_timestamp(ctx.when, plan.fmt)
            case "text" if plan.values is not None:
                line = rng.choices(plan.values, weights=plan.weights, k=1)[0]
                return self._scrub(line, ctx)
        raise SyntheticDataError(f"Unusable key plan {plan.kind!r}")  # pragma: no cover

    def _scrub(self, line: str, ctx: EventContext) -> str:
        """Substitute identity tokens in free text with synthesised ones."""
        pool = ctx.pool

        def sub_ip(match: re.Match[str]) -> str:
            value = match.group(0)
            if not _is_ipv4(value):
                return value
            if ipaddress.ip_address(value).is_global:
                return pool.external_ipv4()
            return ctx.host.ipv4

        line = _TEXT_IPV4_RE.sub(sub_ip, line)
        line = _TEXT_EMAIL_RE.sub(lambda _: ctx.user.email, line)
        line = _TEXT_UUID_RE.sub(lambda _: pool.fake.uuid4(), line)
        line = _TEXT_MAC_RE.sub(lambda _: ctx.host.mac, line)
        if self._lexicon_re is not None:
            line = self._lexicon_re.sub(lambda m: self._lexicon[m.group(0)], line)
        return line
