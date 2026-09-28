#  Project:      dfe-engine
#  File:         admin_links.py
#  Purpose:      The admin UIs the deployer says this deployment runs, and whether each answers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The admin UIs a deployment runs, as its deployer listed them.

The deployer knows which consoles it stood up and where a browser reaches each
one, so it hands the engine that list in ``deployment.admin_links``
(``DFE_ADMIN_LINKS``) and the engine reports it; nothing here discovers a UI. An
entry that does not validate is dropped, logged and counted on
``admin_links_dropped_total`` by reason, so a typo loses that entry and never
the engine.

A link with a ``probe_url`` is ``up`` when a GET to it gets any HTTP answer
below 500 -- a login redirect or a 401 is a UI that is serving -- and ``down``
on a 5xx, a timeout or no connection. A link without one is ``unknown``. The
probes run together and one round answers for a short time, so a page refresh
does not send a request to every console.
"""

import asyncio
import json
import time
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AfterValidator, BaseModel, Field, ValidationError
from scalo.http import AsyncHttpClient
from scalo.logger import logger

LINKS_DROPPED = "admin_links_dropped_total"

# The engine's other outbound HTTP check, the HyperDX client, allows one attempt of five seconds.
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_ATTEMPTS = 1

# Long enough that a page refresh reuses the answer, short enough that a restart shows within a minute.
CACHE_TTL_SECONDS = 30.0

DropReason = Literal["unparseable", "not_a_mapping", "invalid"]
"""Why a listed entry was dropped.

- ``unparseable``: the value is not a JSON array, so none of it can be read.
- ``not_a_mapping``: the entry is not an object.
- ``invalid``: a field is missing, empty or unknown, or a URL is not an absolute
  http(s) URL, or carries credentials.
"""


class AdminLinkStatus(StrEnum):
    """Whether an admin UI answered its probe."""

    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


def _link_url(value: str) -> str:
    """Return *value* when it is an absolute http(s) URL with no credentials in it."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an absolute http or https URL")
    # Reading the port raises on one that is not a number in range.
    if parts.port == 0:
        raise ValueError("must not name port 0")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not carry credentials")
    return value


type LinkUrl = Annotated[str, AfterValidator(_link_url)]


class AdminLink(BaseModel, extra="forbid", frozen=True, str_strip_whitespace=True):
    """One admin UI, as the deployer listed it.

    Attributes:
        name: What the UI is, e.g. ``Argo CD``.
        purpose: What an admin opens it for, in one line.
        url: Where a browser reaches it.
        probe_url: The in-network address the engine GETs to report it up or down.
    """

    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    url: LinkUrl = Field(min_length=1)
    probe_url: LinkUrl | None = None


class AdminLinkMetrics:
    """The link list's instruments, or a no-op set when no backend is wired."""

    def __init__(self, manager: Any | None = None) -> None:
        """Register the counter on *manager*.

        Args:
            manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
                means no backend, and every record method returns without doing anything.
        """
        self._manager = manager
        if manager is None:
            return
        self._dropped = manager.counter(
            LINKS_DROPPED, "Listed admin UI entries dropped as not valid, by reason", ["reason"]
        )

    def dropped(self, reason: DropReason) -> None:
        """Record one entry dropped from the list."""
        if self._manager is None:
            return
        self._dropped.labels(reason=reason).inc()


def _drop(metrics: AdminLinkMetrics, reason: DropReason, **where: object) -> None:
    metrics.dropped(reason)
    logger.warning("admin link dropped: not a valid entry", reason=reason, **where)


def _validation_summary(exc: ValidationError) -> str:
    """Name each failing field and why, without the value, which may be a URL with a password."""
    failures = exc.errors(include_input=False, include_url=False)
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'entry'}: {e['msg']}" for e in failures)


def parse_links(raw: str | list[Any], metrics: AdminLinkMetrics) -> list[AdminLink]:
    """The valid entries of *raw*, in order; each invalid one is dropped, logged and counted.

    Args:
        raw: ``deployment.admin_links`` -- the JSON text the env var carries, or a
            list from a config file.
        metrics: where each dropped entry is counted.

    Returns:
        Every entry that validates. Never raises: an unreadable list is no links.
    """
    entries: Any = raw
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            _drop(metrics, "unparseable", error=str(exc))
            return []
    if not isinstance(entries, list):
        _drop(metrics, "unparseable", error=f"expected a JSON array, got {type(entries).__name__}")
        return []

    links: list[AdminLink] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _drop(metrics, "not_a_mapping", index=index, got=type(entry).__name__)
            continue
        try:
            links.append(AdminLink.model_validate(entry))
        except ValidationError as exc:
            name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            _drop(metrics, "invalid", index=index, name=name, error=_validation_summary(exc))
    return links


class AdminLinks:
    """The deployer's admin UIs, each with a status that one round of probes answers for a while."""

    def __init__(
        self,
        links: list[AdminLink],
        *,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        ttl: float = CACHE_TTL_SECONDS,
    ) -> None:
        """Hold *links*; nothing is probed until the first status is asked for.

        Args:
            links: the validated entries, in the order the deployer listed them.
            timeout: seconds one probe may take before the UI is reported down.
            ttl: seconds one round of probes answers for.
        """
        self._links = tuple(links)
        self._timeout = timeout
        self._ttl = ttl
        # One round at a time: a request arriving mid-round waits for it rather than starting another.
        self._lock = asyncio.Lock()
        self._statuses: tuple[AdminLinkStatus, ...] = ()
        self._expires = 0.0

    @property
    def links(self) -> tuple[AdminLink, ...]:
        """The validated entries, in the order the deployer listed them."""
        return self._links

    async def statuses(self) -> list[tuple[AdminLink, AdminLinkStatus]]:
        """Each link with its status, probing again only once the last round has expired."""
        async with self._lock:
            if time.monotonic() >= self._expires:
                self._statuses = await self._probe_all()
                self._expires = time.monotonic() + self._ttl
            return list(zip(self._links, self._statuses, strict=True))

    async def _probe_all(self) -> tuple[AdminLinkStatus, ...]:
        if not any(link.probe_url for link in self._links):
            return tuple(AdminLinkStatus.UNKNOWN for _ in self._links)
        async with AsyncHttpClient(timeout=self._timeout, retries=PROBE_ATTEMPTS) as client:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._probe(client, link)) for link in self._links]
        return tuple(task.result() for task in tasks)

    async def _probe(self, client: AsyncHttpClient, link: AdminLink) -> AdminLinkStatus:
        if link.probe_url is None:
            return AdminLinkStatus.UNKNOWN
        try:
            # httpx times each phase separately; this bounds the whole request.
            async with asyncio.timeout(self._timeout):
                await client.get(link.probe_url)
        except httpx.HTTPStatusError as exc:
            # A redirect to a login page, or a 401, is a UI that is serving.
            return AdminLinkStatus.UP if exc.response.status_code < 500 else AdminLinkStatus.DOWN
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as exc:
            logger.debug("admin UI probe failed", name=link.name, error=type(exc).__name__)
            return AdminLinkStatus.DOWN
        return AdminLinkStatus.UP


__all__ = [
    "CACHE_TTL_SECONDS",
    "LINKS_DROPPED",
    "PROBE_TIMEOUT_SECONDS",
    "AdminLink",
    "AdminLinkMetrics",
    "AdminLinks",
    "DropReason",
    "AdminLinkStatus",
    "parse_links",
]
