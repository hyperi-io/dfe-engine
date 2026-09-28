#  Project:      dfe-engine
#  File:         tests/support/producer_contract.py
#  Purpose:      Read what another DFE repo ships, for the tests that hold the engine to it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The files the cross-repo contract tests compare against, and where they come from.

A contract test holds an engine fixture or model against what a producer repo ships.
``producer_file`` reads the producer's copy from the first of:

1. a checkout named by the producer's variable (``DFE_LOADER_DIR``, ``SCALO_RS_DIR``);
2. a checkout beside this one (``../dfe-loader``, ``../scalo-rs``), so an uncommitted
   producer change is tested before it ships;
3. GitHub, at the release pinned in ``PRODUCERS``.

When none of them answers, a CI run fails and a local run skips: a skip in CI is a green
build that compared nothing. The one exception is a producer marked ``private``, which
GitHub answers with a 404 until someone can read it: that skips everywhere, saying why.
``GH_TOKEN`` or ``GITHUB_TOKEN`` authenticates the fetch.

To bump a pin, set ``ref`` to the release the suite ships, then run the contract tests
with no checkout beside this one so they read the pin. ``check_pin`` warns with
``ContractPinWarning`` when a producer's latest release has changed a pinned file.
"""

import json
import os
import random
import time
import warnings
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import httpx
import pytest

OWNER = "hyperi-io"
RAW_BASE = "https://raw.githubusercontent.com"
API_BASE = "https://api.github.com"

# Where a developer keeps the producer repos: the directory holding this checkout.
CHECKOUTS = Path(__file__).resolve().parents[2].parent

_ATTEMPTS = 3
_TIMEOUT_SECONDS = 10.0


class ContractUnreadableError(Exception):
    """GitHub did not answer with the producer file or release asked for.

    Attributes:
        status: The last HTTP status, or None when no response arrived.
    """

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


class ContractPinWarning(UserWarning):
    """A pin is behind a producer release that changed a pinned file, or could not be checked."""


@dataclass(frozen=True, slots=True)
class Producer:
    """A repo whose shipped files the contract tests read.

    Attributes:
        repo: Repository name under ``OWNER``, and the directory name of its checkout.
        ref: The release tag the engine is tested against.
        dir_env: Variable naming a checkout of ``repo`` kept anywhere.
        files: Every file the contract tests read, relative to the repo root.
        private: GitHub hides the repo from a caller without read on it, so a 404 is
            the expected answer in CI and skips rather than fails.
    """

    repo: str
    ref: str
    dir_env: str
    files: tuple[str, ...]
    private: bool


@dataclass(frozen=True, slots=True)
class ProducerFile:
    """A producer file's bytes, and where they were read from.

    Attributes:
        data: The file, byte for byte.
        origin: The checkout path or ``owner/repo@ref`` it came from.
    """

    data: bytes
    origin: str

    @property
    def text(self) -> str:
        """The file decoded as UTF-8."""
        return self.data.decode("utf-8")


# The producer releases this engine is tested against. Each moves with the suite's
# release, never to a branch: a branch compares against whatever merged last.
DFE_LOADER = Producer(
    repo="dfe-loader",
    ref="v1.18.45",
    dir_env="DFE_LOADER_DIR",
    files=("src/config/pipeline.rs", "src/config/loader.rs"),
    # Risk accepted until GA: CI holds no token that reads it, so its tests skip there.
    private=True,
)
SCALO_RS = Producer(
    repo="scalo-rs",
    ref="v2.12.11",
    dir_env="SCALO_RS_DIR",
    files=("tests/fixtures/cel_classifier_parity.json",),
    private=False,
)
PRODUCERS = (DFE_LOADER, SCALO_RS)


def in_ci() -> bool:
    """Whether this is a CI run, where an unreadable contract fails rather than skips."""
    ci = os.environ.get("CI", "").strip().lower()
    return ci in {"1", "true", "yes"} or os.environ.get("GITHUB_ACTIONS") == "true"


def hidden_private_reason(producer: Producer, exc: ContractUnreadableError) -> str | None:
    """Why a private producer is out of reach, or None when the failure is anything else.

    Only a 404 counts: it is how GitHub hides a private repo. A network failure on a
    private producer is still a failure.
    """
    if producer.private and exc.status == httpx.codes.NOT_FOUND:
        return (
            f"{producer.repo} is private until GA; set GH_TOKEN with read on it, or it runs "
            "once the repo is public (dfe-engine #553)"
        )
    return None


def _get(url: str, *, accept: str | None = None) -> bytes:
    """The body of a 200 from ``url``, retrying only a transport error, a 5xx or a 429.

    A token goes only to GitHub over HTTPS, and redirects are not followed, so it never
    reaches another host.
    """
    headers = {"Accept": accept} if accept else {}
    to_github = url.startswith((f"{RAW_BASE}/", f"{API_BASE}/"))
    if to_github and (token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        headers["Authorization"] = f"Bearer {token}"
    outcome = ""
    status: int | None = None
    for attempt in range(_ATTEMPTS):
        if attempt:
            time.sleep(0.25 * 2**attempt * random.SystemRandom().uniform(0.5, 1.0))
        try:
            response = httpx.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
        except httpx.TransportError as exc:
            outcome, status = f"{type(exc).__name__}: {exc}", None
            continue
        status = response.status_code
        if status == httpx.codes.OK:
            return response.content
        outcome = f"HTTP {status}"
        if status < 500 and status != httpx.codes.TOO_MANY_REQUESTS:
            break
    raise ContractUnreadableError(f"{url}: {outcome}", status)


@cache
def fetch_at(repo: str, ref: str, relative: str, base_url: str = RAW_BASE) -> bytes:
    """A producer file as committed at ``ref``.

    Raises:
        ContractUnreadableError: GitHub did not return the file.
    """
    return _get(f"{base_url}/{OWNER}/{repo}/{ref}/{relative}")


def latest_release(producer: Producer, *, api_base: str = API_BASE) -> str:
    """The tag of the producer's newest release.

    Raises:
        ContractUnreadableError: The release API did not answer.
    """
    url = f"{api_base}/repos/{OWNER}/{producer.repo}/releases/latest"
    return json.loads(_get(url, accept="application/vnd.github+json"))["tag_name"]


def check_pin(producer: Producer, *, api_base: str = API_BASE, base_url: str = RAW_BASE) -> None:
    """Warn when the producer's latest release changed a file the engine pins older.

    A release that leaves every pinned file alone needs no bump, so it passes quietly. A
    private producer GitHub hides skips quietly too, or it would warn on every run. Any
    other failure to check skips locally and warns in CI rather than passing as current.

    Args:
        producer: The repo whose pin is checked.
        api_base: Where the release API is asked.
        base_url: Where the raw files are fetched from.
    """
    moved: list[str] = []
    try:
        latest = latest_release(producer, api_base=api_base)
        if latest != producer.ref:
            pinned = {
                path: fetch_at(producer.repo, producer.ref, path, base_url)
                for path in producer.files
            }
            moved = [
                path
                for path in producer.files
                if fetch_at(producer.repo, latest, path, base_url) != pinned[path]
            ]
    except ContractUnreadableError as exc:
        if hidden := hidden_private_reason(producer, exc):
            pytest.skip(hidden)
        if not in_ci():
            pytest.skip(f"could not check {producer.repo}'s pin: {exc}")
        warnings.warn(
            ContractPinWarning(f"{producer.repo}'s pin {producer.ref} could not be checked: {exc}"),
            stacklevel=2,
        )
        return
    if moved:
        warnings.warn(
            ContractPinWarning(
                f"{producer.repo} {latest} changed {', '.join(moved)} since the pinned "
                f"{producer.ref}; move the pin in tests/support/producer_contract.py once the "
                "suite ships it"
            ),
            stacklevel=2,
        )


def _checkout_file(producer: Producer, relative: str, checkouts: Path) -> Path | None:
    roots = [Path(named)] if (named := os.environ.get(producer.dir_env)) else []
    roots.append(checkouts / producer.repo)
    for root in roots:
        if (candidate := root / relative).is_file():
            return candidate
    return None


def producer_file(
    producer: Producer,
    relative: str,
    *,
    checkouts: Path = CHECKOUTS,
    base_url: str = RAW_BASE,
) -> ProducerFile:
    """Read one of ``producer.files`` from a checkout, else from GitHub at the pin.

    When neither answers it skips the calling test, and in CI fails it instead, unless
    the producer is private and GitHub hid it, which skips everywhere.

    Args:
        producer: The repo that ships the file.
        relative: The file's path in that repo; must be listed in ``producer.files``.
        checkouts: Directory holding a checkout of the producer beside this one.
        base_url: Where the raw file is fetched from.

    Returns:
        The file and where it came from.

    Raises:
        ValueError: ``relative`` is not in ``producer.files``, so the stale-pin check
            would never look at it.
    """
    if relative not in producer.files:
        raise ValueError(f"{producer.repo} does not declare {relative!r} in its files")
    local = _checkout_file(producer, relative, checkouts)
    if local is not None:
        return ProducerFile(local.read_bytes(), str(local))
    try:
        data = fetch_at(producer.repo, producer.ref, relative, base_url)
    except ContractUnreadableError as exc:
        if hidden := hidden_private_reason(producer, exc):
            pytest.skip(hidden)
        reason = (
            f"{OWNER}/{producer.repo}@{producer.ref} {relative} could not be read ({exc}), "
            f"and there is no checkout at ${producer.dir_env} or {checkouts / producer.repo}."
        )
        if exc.status == httpx.codes.NOT_FOUND:
            # GitHub answers 404, not 403, for a private repo the caller cannot read.
            reason += " A private repo needs GH_TOKEN or GITHUB_TOKEN with read access to it."
        if in_ci():
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)
    return ProducerFile(data, f"{OWNER}/{producer.repo}@{producer.ref}")
