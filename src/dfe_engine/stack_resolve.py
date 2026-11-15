#  Project:      dfe-engine
#  File:         stack_resolve.py
#  Purpose:      Name the published DFE stack whose images a deployment is running
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve the stack a deployment runs from its image digests.

A stack name a build wrote down says what the build was made against, not what is
running. Each cut stack is published as an OCI artifact (``dfe-stack-manifest:<tag>``)
whose ``dfe-manifest-<tag>.yaml`` layer pins every app image by digest. This module
walks those artifacts, newest release first and then newest prerelease, and returns
the first whose digests agree with every image the deployment runs.

Reads are anonymous: a registry that wants a bearer token gets the anonymous one its
own challenge names. Signature verification of the artifact is not done here.
"""

import asyncio
import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from ruamel.yaml import YAML, YAMLError
from scalo.http import HttpClient
from scalo.logger import logger

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
_MANIFEST_LAYER = re.compile(r"^dfe-manifest-.+\.yaml$")
_TITLE_ANNOTATION = "org.opencontainers.image.title"
_OCI_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_TAGS_PAGE_SIZE = 1000
_MAX_MANIFEST_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})
_BUDGET_SPENT = "time budget spent before a matching stack was found"


class StackResolveError(Exception):
    """The registry could not be read or answered something unusable."""


class BudgetSpentError(StackResolveError):
    """The time allowed for the whole resolution ran out."""


def semver_key(tag: str) -> tuple[int, int, int, int, tuple[tuple[int, Any], ...]] | None:
    """Return the SemVer precedence key of ``tag``, or None when it is not semver-shaped.

    A release outranks every prerelease of the same ``X.Y.Z``, a numeric prerelease
    identifier ranks below an alphanumeric one, and identifiers compare left to right.
    This is the ordering dfe-infra's ``scripts/dfe-stack`` ``semver_key`` uses.

    Args:
        tag: A stack tag such as ``2.2.2`` or ``2.2.2-rc.5``.
    """
    match = _SEMVER.match(tag.strip())
    if not match:
        return None
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return (major, minor, patch, 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".")
    )
    return (major, minor, patch, 0, identifiers)


def newest_first(tags: Sequence[str]) -> list[str]:
    """Order stack tags releases newest first, then prereleases newest first.

    Tags that are not semver-shaped are dropped.
    """
    keyed = [(key, tag) for tag in tags if (key := semver_key(tag)) is not None]
    releases = [item for item in keyed if item[0][3] == 1]
    prereleases = [item for item in keyed if item[0][3] == 0]
    return [tag for _, tag in sorted(releases, reverse=True)] + [
        tag for _, tag in sorted(prereleases, reverse=True)
    ]


def image_name(reference: str) -> str:
    """Return the image name of ``registry/path/name[:tag][@sha256:digest]``."""
    without_digest = reference.strip().split("@", 1)[0]
    last = without_digest.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]


def image_digest(reference: str) -> str | None:
    """Return the ``sha256:...`` digest a reference pins, or None when it pins none."""
    _, separator, digest = reference.strip().partition("@")
    return digest if separator and digest.startswith("sha256:") else None


def running_digests(references: Sequence[str]) -> dict[str, str]:
    """Map image name to digest for the images a deployment runs.

    Raises:
        StackResolveError: A reference pins no digest, or two references name one image
            with different digests.
    """
    running: dict[str, str] = {}
    for reference in references:
        digest = image_digest(reference)
        if digest is None:
            raise StackResolveError(f"image {reference!r} pins no sha256 digest")
        name = image_name(reference)
        if running.setdefault(name, digest) != digest:
            raise StackResolveError(f"image {name} appears with two different digests")
    return running


def matches(manifest_images: Mapping[str, str], running: Mapping[str, str]) -> bool:
    """Return True when every running image is in the manifest at the same digest.

    The manifest may list more apps than the deployment runs. An image the manifest
    does not list at all, or lists at another digest, is not that stack.
    """
    if not running:
        return False
    for name, digest in running.items():
        listed = manifest_images.get(name)
        if not isinstance(listed, str) or image_digest(listed) != digest:
            return False
    return True


def manifest_images(document: Any) -> dict[str, str]:
    """Return the ``images`` mapping of a parsed stack manifest, name to reference."""
    images = document.get("images") if isinstance(document, dict) else None
    if not isinstance(images, dict):
        raise StackResolveError("manifest has no images mapping")
    return {str(name): ref for name, ref in images.items() if isinstance(ref, str)}


def _split_repository(repository: str) -> tuple[str, str]:
    """Split ``host/path`` into the registry base URL and the repository path.

    The base URL is https, except for a loopback host, which a registry serves over
    plain http the way ``docker`` treats it.
    """
    host, separator, path = repository.strip().strip("/").partition("/")
    looks_like_host = "." in host or ":" in host or host == "localhost"
    if not separator or not path or not looks_like_host:
        raise StackResolveError(f"manifest repository {repository!r} is not host/path")
    scheme = "http" if host.rsplit(":", 1)[0] in _LOOPBACK_HOSTS else "https"
    return f"{scheme}://{host}", path


def _layer_title(layer: Mapping[str, Any]) -> str:
    """Return the file name an OCI layer descriptor carries, or an empty string."""
    annotations = layer.get("annotations")
    title = annotations.get(_TITLE_ANNOTATION) if isinstance(annotations, dict) else None
    return title if isinstance(title, str) else ""


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Return the JSON object a registry answered with.

    Raises:
        StackResolveError: The body is not JSON, or is JSON that is not an object.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise StackResolveError("registry answered with something that is not JSON") from exc
    if not isinstance(body, dict):
        raise StackResolveError("registry answered with JSON that is not an object")
    return body


class RegistryReader:
    """Anonymous reader of one repository on an OCI registry.

    Every request is bounded by ``request_timeout_seconds`` and by what is left of
    ``budget_seconds`` for the whole resolution, so a slow registry costs the budget
    and no more.
    """

    def __init__(
        self,
        repository: str,
        *,
        request_timeout_seconds: float,
        budget_seconds: float,
    ) -> None:
        """Prepare a reader for ``host/path``. No request is sent until it is used.

        Args:
            repository: ``host/path``, e.g. ``ghcr.io/hyperi-io/dfe-stack-manifest``.
            request_timeout_seconds: Longest one request may take.
            budget_seconds: Longest the whole resolution may take.
        """
        self._base, self._path = _split_repository(repository)
        self._request_timeout = request_timeout_seconds
        self._deadline = time.monotonic() + budget_seconds
        self._token: str | None = None
        # One attempt per request: the caller falls back, so a retry only spends budget.
        self._client = HttpClient(
            base_url=self._base,
            timeout=request_timeout_seconds,
            retries=1,
            follow_redirects=True,
        )

    def __enter__(self) -> RegistryReader:
        """Return the reader."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetSpentError(_BUDGET_SPENT)
        return min(self._request_timeout, remaining)

    def _authenticate(self, challenge: str) -> None:
        """Fetch the anonymous bearer token the registry's own challenge names."""
        if not challenge.lower().startswith("bearer "):
            raise StackResolveError("registry wants credentials that are not a bearer token")
        params = dict(_CHALLENGE_PARAM.findall(challenge))
        realm = params.pop("realm", "")
        scheme = self._base.partition(":")[0]
        if not realm.startswith(f"{scheme}://"):
            raise StackResolveError(f"registry token realm is not an {scheme} URL")
        params.setdefault("scope", f"repository:{self._path}:pull")
        body = _json_object(self._client.get(realm, params=params, timeout=self._remaining()))
        token = body.get("token") or body.get("access_token")
        if not isinstance(token, str) or not token:
            raise StackResolveError("registry issued no anonymous token")
        self._token = token

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET ``url`` anonymously, authenticating once if the registry asks.

        Raises:
            StackResolveError: The request failed, was refused, or the budget is spent.
        """
        base_headers = dict(kwargs.pop("headers", None) or {})
        try:
            for _ in range(2):
                headers = dict(base_headers)
                if self._token:
                    headers["Authorization"] = f"Bearer {self._token}"
                try:
                    return self._client.get(
                        url, timeout=self._remaining(), headers=headers, **kwargs
                    )
                except httpx.HTTPStatusError as exc:
                    challenge = exc.response.headers.get("www-authenticate", "")
                    if exc.response.status_code != 401 or self._token or not challenge:
                        raise
                    self._authenticate(challenge)
            raise StackResolveError("registry kept refusing the anonymous token")
        except httpx.HTTPError as exc:
            raise StackResolveError(f"{type(exc).__name__} reading {urlsplit(url).path}") from exc

    def tags(self) -> list[str]:
        """List every tag of the repository, following the registry's paging."""
        tags: list[str] = []
        url: str | None = f"/v2/{self._path}/tags/list?n={_TAGS_PAGE_SIZE}"
        while url:
            response = self.get(url)
            page = _json_object(response).get("tags") or []
            if not isinstance(page, list):
                raise StackResolveError("registry tag list is not a list")
            tags.extend(str(tag) for tag in page)
            url = response.links.get("next", {}).get("url")
        return tags

    def manifest_document(self, tag: str) -> Any:
        """Fetch and parse the ``dfe-manifest-*.yaml`` layer of ``tag``.

        Raises:
            StackResolveError: The artifact has no such layer, or the layer's bytes do
                not hash to the digest its descriptor names.
        """
        response = self.get(f"/v2/{self._path}/manifests/{tag}", headers={"Accept": _OCI_ACCEPT})
        layers = [
            item for item in _json_object(response).get("layers") or [] if isinstance(item, dict)
        ]
        layer = next((item for item in layers if _MANIFEST_LAYER.match(_layer_title(item))), None)
        if layer is None:
            raise StackResolveError(f"{tag} carries no dfe-manifest layer")
        digest = str(layer.get("digest", ""))
        blob = self.get(f"/v2/{self._path}/blobs/{digest}").content
        if len(blob) > _MAX_MANIFEST_BYTES:
            raise StackResolveError(f"{tag} manifest layer is over {_MAX_MANIFEST_BYTES} bytes")
        if f"sha256:{hashlib.sha256(blob).hexdigest()}" != digest:
            raise StackResolveError(f"{tag} manifest layer does not match its digest")
        try:
            return YAML(typ="safe").load(blob.decode("utf-8"))
        except (YAMLError, UnicodeDecodeError) as exc:
            raise StackResolveError(f"{tag} manifest layer is not valid YAML") from exc


def resolve_stack(
    images: Sequence[str],
    repository: str,
    *,
    request_timeout_seconds: float,
    budget_seconds: float,
    stop: threading.Event | None = None,
) -> str | None:
    """Return the newest published stack whose digests agree with ``images``, or None.

    Blocking: it makes network calls, so a caller on the event loop runs it in a thread.
    A tag whose artifact cannot be read is skipped. A registry that cannot be reached,
    or a spent budget, ends the search.

    Args:
        images: The ``ref@sha256:...`` of every DFE image the deployment runs.
        repository: ``host/path`` of the stack-manifest repository.
        request_timeout_seconds: Longest one registry request may take.
        budget_seconds: Longest the whole resolution may take.
        stop: When set, the search ends before the next tag and returns None.

    Returns:
        The stack tag, or None when no published stack matches every image.

    Raises:
        StackResolveError: The registry could not be read, or an image has no digest.
    """
    running = running_digests(images)
    with RegistryReader(
        repository,
        request_timeout_seconds=request_timeout_seconds,
        budget_seconds=budget_seconds,
    ) as reader:
        for tag in newest_first(reader.tags()):
            if stop is not None and stop.is_set():
                return None
            try:
                listed = manifest_images(reader.manifest_document(tag))
            except BudgetSpentError:
                raise
            except StackResolveError as exc:
                logger.debug("stack manifest skipped", tag=tag, reason=str(exc))
                continue
            if matches(listed, running):
                return tag
    return None


async def resolve_into(
    state: Any, settings: Any, deploy_repo_stack: Callable[[], str | None] | None = None
) -> None:
    """Resolve the running stack off the event loop and store it on ``state``.

    Sets ``state.resolved_stack`` to the tag on a match. A failure of any kind, or no
    match, logs once at warn and leaves the configured ``stack_version`` to answer. It
    raises nothing but cancellation, so neither startup nor readiness waits on it and a
    dead task never goes unremarked.

    Args:
        state: Where ``resolved_stack`` is stored.
        settings: The engine settings carrying the ``stack_*`` fields.
        deploy_repo_stack: Reads the stack the deploy repo pins, blocking. A pin always
            wins over the registry, so when one exists the registry is not read.
    """
    images = list(settings.stack_images)
    if not settings.stack_resolve or not images:
        return
    fallback = settings.stack_version or None
    stop = threading.Event()
    try:
        if deploy_repo_stack is not None and await asyncio.to_thread(deploy_repo_stack):
            logger.debug("deploy repo pins the stack, registry not read")
            return
        tag = await asyncio.to_thread(
            resolve_stack,
            images,
            settings.stack_manifest_repository,
            request_timeout_seconds=settings.stack_resolve_request_timeout_seconds,
            budget_seconds=settings.stack_resolve_budget_seconds,
            stop=stop,
        )
    except asyncio.CancelledError:
        # The thread cannot be cancelled, so it is told to stop before its next tag.
        stop.set()
        raise
    except Exception as exc:
        # Whatever ends this task is logged here, since nothing awaits it before shutdown.
        reason = str(exc) if isinstance(exc, StackResolveError) else f"{type(exc).__name__}: {exc}"
        logger.warning(
            "stack not resolved from the registry, keeping the configured label",
            reason=reason,
            fallback=fallback,
        )
        return
    if tag is None:
        logger.warning(
            "no published stack matches the running images, keeping the configured label",
            repository=settings.stack_manifest_repository,
            fallback=fallback,
        )
        return
    state.resolved_stack = tag
    logger.info("stack resolved from the registry", stack=tag)
