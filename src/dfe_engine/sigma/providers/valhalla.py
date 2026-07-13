#  Project:      dfe-engine
#  File:         sigma/providers/valhalla.py
#  Purpose:      Sigma provider: Nextron Valhalla JSON feed (scalo AsyncHttpClient)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Nextron Valhalla sigma provider - documented HTTP JSON API, no vendor client lib.

/deps VERDICT (checked 2026-07-06 via the PyPI JSON API): the official
`valhallaAPI` PyPI package was SKIPPED, so this adapter talks to the documented
HTTP JSON endpoint directly using the project's scalo AsyncHttpClient. Why skipped:
  - License: `info.license` is null and there is no license classifier - an
    undeclared licence is a hard blocker for a BUSL-1.1 commercial product.
  - Maintenance: 0.6.0 (2022-10-14) then a 3.25-year gap to 0.6.1 + 0.6.2 (both
    2026-01-13) - a single burst, not an actively-maintained cadence. (The 7-day
    cooldown itself PASSES: 0.6.2 is ~6 months old.)
  - Policy: it hard-depends on `requests` (+ a py2-era `configparser` backport),
    but DFE mandates scalo.http and never raw requests/httpx.
Net: zero new dependency, no uv.lock change - the HTTP contract + the public demo
key give the same feed.

Contract (github.com/NextronSystems/valhallaAPI): POST
`https://valhalla.nextron-systems.com/api/v1/getsigma` with form fields
`apikey` + `format=json`. The public DEMO key (64 '1' chars) returns the public
SigmaHQ set. Valhalla is AGGRESSIVELY rate-limited (bulk abuse -> bans), so this
backs off on 429 and the config's poll_interval_seconds is the caller's budget.
There is no server-side "modified since" filter, so incremental is done
client-side (`modified >= since`) - the store's upsert skips the unchanged rest.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from scalo.logger import logger

from .base import (
    AuthKind,
    ProviderConfig,
    SigmaProvider,
    SigmaRuleDoc,
    modified_since,
    parse_sigma_dicts,
)

_DEMO_KEY = "1" * 64  # public demo key: returns the public rule set (no subscription)
_DEFAULT_BASE = "https://valhalla.nextron-systems.com"
_GETSIGMA_PATH = "/api/v1/getsigma"


def _retry_after_seconds(response: Any) -> float | None:
    """Seconds to wait from a 429's ``Retry-After`` header, or None to fall back.

    Handles the delta-seconds form (the common API case). The HTTP-date form is
    rarer and left to the caller's linear backoff (returns None).
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class ValhallaProvider(SigmaProvider):
    """Fetch sigma rules from the Valhalla JSON feed over HTTP.

    `_fetch_raw` is the network seam - it POSTs to Valhalla and returns the raw
    list of rule dicts. Tests subclass/override it to exercise normalisation +
    incremental filtering with canned data and NO live network.
    """

    def __init__(self, config: ProviderConfig, *, secrets=None):
        super().__init__(config, secrets=secrets)
        self._base = str(config.options.get("base_url", _DEFAULT_BASE)).rstrip("/")
        self._max_retries = int(config.options.get("max_retries", 3))
        self._backoff_seconds = float(config.options.get("backoff_seconds", 2.0))

    def _api_key(self) -> str:
        """The configured api_key secret, else the public demo key.

        `options.demo=false` with no secret is a config error we surface loudly
        rather than silently hitting the feed unauthenticated.
        """
        if self.config.auth.kind == AuthKind.API_KEY and self.config.auth.secret_path:
            return self._secret()
        if self.config.options.get("demo", True):
            return _DEMO_KEY
        raise ValueError(
            f"valhalla provider {self.name!r}: demo disabled but no api_key secret configured"
        )

    @staticmethod
    def _extract_rules(data: Any) -> list[dict[str, Any]]:
        """Normalise the getsigma JSON envelope to a flat list of rule dicts.

        Tolerant of the shapes the feed / a fixture may use: a bare list, a
        ``{"rules": [...]}`` envelope, or a ``{name: rule}`` map.
        """
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            rules = data.get("rules")
            if isinstance(rules, list):
                return [r for r in rules if isinstance(r, dict)]
            return [v for v in data.values() if isinstance(v, dict)]
        return []

    async def _fetch_raw(self) -> list[dict[str, Any]]:
        """POST getsigma; return the raw rule dicts. Network seam.

        ``scalo.http`` already retries transport + 5xx errors (stamina) and raises
        for status, so the ONLY thing left to add here is the 429/Retry-After
        handler valhalla's aggressive rate limiting needs - scalo never retries a
        4xx. A non-429 4xx (e.g. a bad apikey -> 401) surfaces immediately; any
        other error propagates un-retried (scalo already spent its budget - no
        double retry).
        """
        import httpx
        from scalo.http import AsyncHttpClient

        apikey = self._api_key()
        payload = {"apikey": apikey, "format": "json"}
        async with AsyncHttpClient(base_url=self._base) as client:
            for attempt in range(1, self._max_retries + 1):
                try:
                    resp = await client.post(_GETSIGMA_PATH, data=payload)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 429 or attempt >= self._max_retries:
                        raise  # non-429 4xx (real error) or out of 429 retries
                    delay = _retry_after_seconds(exc.response) or self._backoff_seconds * attempt
                    logger.warning(
                        "valhalla rate-limited (429); backing off",
                        provider=self.name,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                return self._extract_rules(resp.json())
        raise RuntimeError(
            f"valhalla provider {self.name!r}: exhausted {self._max_retries} retries on 429"
        )

    async def fetch(self, since: datetime | None = None) -> list[SigmaRuleDoc]:
        raw = await self._fetch_raw()
        docs, warnings = parse_sigma_dicts(raw, self.origin, source_ref=self.name)
        if since is not None:
            docs = [d for d in docs if modified_since(d, since)]
        self.last_warnings = warnings
        return docs
