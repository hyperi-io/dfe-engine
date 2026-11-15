#  Project:      dfe-engine
#  File:         tests/unit/test_stack_resolve.py
#  Purpose:      Resolving the running stack from published manifests and image digests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The stack a deployment runs is the newest published stack whose digests match its images.

The registry here is a real HTTP server on loopback that speaks the OCI distribution
calls the resolver makes, including the bearer-token challenge and paged tag lists. The
published artifact is the real shape: a manifest whose layer is a ``dfe-manifest-<tag>.yaml``
file. The same resolver is held to the real ``ghcr.io`` registry in
``tests/integration/test_stack_resolve_registry.py``.
"""

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from dfe_engine.settings import DFESettings
from dfe_engine.stack_resolve import (
    StackResolveError,
    image_name,
    matches,
    newest_first,
    resolve_into,
    resolve_stack,
    running_digests,
    semver_key,
)

REPO_PATH = "org/dfe-stack-manifest"
LOADER = "dfe-loader"
RECEIVER = "dfe-receiver"
UI = "dfe-ui"
PAGE_SIZE = 2


def digest_of(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def image_ref(app: str, label: str) -> str:
    return f"registry.example/org/{app}:{label}@{digest_of(f'{app}-{label}')}"


def manifest_yaml(tag: str, apps: dict[str, str]) -> str:
    """A stack manifest as dfe-infra renders it: apps by name, each pinned by digest."""
    lines = [f'stack_version: "{tag}"', "images:"]
    lines += [f'  {app}: "{image_ref(app, label)}"' for app, label in sorted(apps.items())]
    return "\n".join(lines) + "\n"


def running(apps: dict[str, str]) -> list[str]:
    return [image_ref(app, label) for app, label in apps.items()]


ALL_V1 = {LOADER: "v1", RECEIVER: "v1", UI: "v1"}
ALL_V2 = {LOADER: "v2", RECEIVER: "v2", UI: "v2"}


@dataclass
class FakeRegistry:
    """A loopback OCI registry holding stack-manifest artifacts, counting what it serves."""

    host: str = ""
    stacks: dict[str, dict[str, str]] = field(default_factory=dict)
    damage: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    token_requests: int = 0
    requests: int = 0

    @property
    def repository(self) -> str:
        return f"{self.host}/{REPO_PATH}"

    def layer(self, tag: str) -> bytes:
        if self.damage.get(tag) == "not-yaml":
            return b"images: [unclosed\n  - {"
        return manifest_yaml(tag, self.stacks[tag]).encode()

    def manifest(self, tag: str) -> dict:
        blob = self.layer(tag)
        title = "readme.txt" if self.damage.get(tag) == "no-layer" else f"dfe-manifest-{tag}.yaml"
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        return {
            "schemaVersion": 2,
            "layers": [
                {
                    "digest": digest,
                    "size": len(blob),
                    "annotations": {"org.opencontainers.image.title": title},
                }
            ],
        }

    def blob(self, digest: str) -> bytes | None:
        for tag in self.stacks:
            real = self.layer(tag)
            if "sha256:" + hashlib.sha256(real).hexdigest() == digest:
                return b"tampered: true\n" if self.damage.get(tag) == "tampered" else real
        return None


def _handler(registry: FakeRegistry) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            registry.requests += 1
            time.sleep(registry.delay_seconds)
            parts = urlsplit(self.path)
            if parts.path == "/token":
                registry.token_requests += 1
                self._send(200, json.dumps({"token": "anonymous"}).encode())
                return
            if self.headers.get("Authorization") != "Bearer anonymous":
                challenge = (
                    f'Bearer realm="http://{registry.host}/token",service="test",'
                    f'scope="repository:{REPO_PATH}:pull"'
                )
                self._send(401, b"{}", {"WWW-Authenticate": challenge})
                return
            prefix = f"/v2/{REPO_PATH}/"
            if not parts.path.startswith(prefix):
                self._send(404, b"{}")
                return
            tail = parts.path.removeprefix(prefix)
            if tail == "tags/list":
                self._tags(parse_qs(parts.query))
            elif (
                tail.startswith("manifests/") and tail.removeprefix("manifests/") in registry.stacks
            ):
                tag = tail.removeprefix("manifests/")
                answer = (
                    [1, 2]
                    if registry.damage.get(tag) == "manifest-array"
                    else registry.manifest(tag)
                )
                body = json.dumps(answer)
                self._send(200, body.encode(), {"Content-Type": "application/json"})
            elif tail.startswith("blobs/") and registry.blob(tail.removeprefix("blobs/")):
                self._send(200, registry.blob(tail.removeprefix("blobs/")) or b"")
            else:
                self._send(404, b"{}")

        def _tags(self, query: dict[str, list[str]]) -> None:
            tags = sorted(registry.stacks)
            last = query.get("last", [""])[0]
            remaining = [tag for tag in tags if tag > last] if last else tags
            page, more = remaining[:PAGE_SIZE], len(remaining) > PAGE_SIZE
            headers = {}
            if more:
                headers["Link"] = f'</v2/{REPO_PATH}/tags/list?last={page[-1]}&n=1000>; rel="next"'
            self._send(200, json.dumps({"name": REPO_PATH, "tags": page}).encode(), headers)

    return Handler


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.host = f"127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def resolve(registry: FakeRegistry, apps: dict[str, str], **limits: float) -> str | None:
    return resolve_stack(
        running(apps),
        registry.repository,
        request_timeout_seconds=limits.get("request", 5.0),
        budget_seconds=limits.get("budget", 30.0),
    )


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        pytest.param(
            ["2.2.1", "2.2.2-rc.5", "2.2.2", "2.2.0", "2.2.2-rc.8"],
            ["2.2.2", "2.2.1", "2.2.0", "2.2.2-rc.8", "2.2.2-rc.5"],
            id="releases_then_prereleases",
        ),
        pytest.param(
            ["2.2.2-rc.9", "2.2.2-rc.10", "2.2.2-rc.2"],
            ["2.2.2-rc.10", "2.2.2-rc.9", "2.2.2-rc.2"],
            id="prerelease_numbers_compare_as_numbers",
        ),
        pytest.param(
            ["2.2.2-rc.1", "2.2.2-beta.1", "2.2.2-alpha.3"],
            ["2.2.2-rc.1", "2.2.2-beta.1", "2.2.2-alpha.3"],
            id="alphanumeric_identifiers_compare_lexically",
        ),
        pytest.param(
            ["2.10.0", "2.9.0", "10.0.0"],
            ["10.0.0", "2.10.0", "2.9.0"],
            id="versions_compare_as_numbers",
        ),
        pytest.param(
            ["latest", "v2.2.1", "sha-abc123", "2.2", "2.2.2"],
            ["2.2.2", "v2.2.1"],
            id="tags_that_are_not_semver_are_dropped",
        ),
        pytest.param([], [], id="no_tags"),
    ],
)
def test_newest_first(tags, expected):
    assert newest_first(tags) == expected


def test_a_release_outranks_its_own_prerelease_and_not_the_next_patch():
    candidate, release, next_patch = (
        semver_key(tag) for tag in ("2.2.2-rc.9", "2.2.2", "2.2.3-rc.1")
    )
    assert candidate
    assert release
    assert next_patch
    assert candidate < release < next_patch
    assert semver_key("not-a-version") is None


@pytest.mark.parametrize(
    ("reference", "name"),
    [
        ("ghcr.io/hyperi-io/dfe-loader:v1.2.3@sha256:ab", "dfe-loader"),
        ("ghcr.io/hyperi-io/dfe-loader@sha256:ab", "dfe-loader"),
        ("dfe-loader:v1", "dfe-loader"),
        ("localhost:5000/org/dfe-loader:v1@sha256:ab", "dfe-loader"),
    ],
)
def test_image_name(reference, name):
    assert image_name(reference) == name


@pytest.mark.parametrize(
    ("manifest", "run", "expected"),
    [
        pytest.param(ALL_V1, ALL_V1, True, id="same_apps_same_digests"),
        pytest.param(ALL_V1, {LOADER: "v1"}, True, id="deployment_runs_a_subset"),
        pytest.param(ALL_V1, {LOADER: "v2"}, False, id="digest_differs"),
        pytest.param(ALL_V1, {LOADER: "v1", RECEIVER: "v2"}, False, id="one_of_two_differs"),
        pytest.param({LOADER: "v1"}, {LOADER: "v1", UI: "v1"}, False, id="app_the_stack_lacks"),
        pytest.param(ALL_V1, {}, False, id="nothing_running"),
    ],
)
def test_matches(manifest, run, expected):
    listed = {app: image_ref(app, label) for app, label in manifest.items()}
    assert matches(listed, running_digests(running(run))) is expected


@pytest.mark.parametrize(
    ("references", "refusal"),
    [
        pytest.param(["ghcr.io/hyperi-io/dfe-loader:v1"], "pins no sha256 digest", id="no_digest"),
        pytest.param(["ghcr.io/hyperi-io/dfe-loader@sha512:ab"], "pins no sha256", id="wrong_algo"),
        pytest.param(
            ["r/dfe-loader:v1@sha256:aa", "r/dfe-loader:v2@sha256:bb"],
            "two different digests",
            id="one_image_two_digests",
        ),
    ],
)
def test_running_images_must_pin_one_digest_each(references, refusal):
    with pytest.raises(StackResolveError, match=refusal):
        running_digests(references)


@pytest.mark.parametrize(
    ("stacks", "damage", "run", "expected"),
    [
        pytest.param(
            {"2.2.2-rc.6": ALL_V1, "2.2.2": ALL_V1, "2.2.2-rc.5": ALL_V1},
            {},
            ALL_V1,
            "2.2.2",
            id="a_release_wins_over_prereleases_with_the_same_images",
        ),
        pytest.param(
            {"2.2.3": ALL_V2, "2.2.2": ALL_V1},
            {},
            ALL_V1,
            "2.2.2",
            id="an_older_release_matches_when_the_newest_differs",
        ),
        pytest.param(
            {"2.2.3-rc.1": ALL_V1, "2.2.2": ALL_V2},
            {},
            ALL_V1,
            "2.2.3-rc.1",
            id="a_prerelease_answers_when_no_release_matches",
        ),
        pytest.param(
            {"2.2.2-rc.9": ALL_V1, "2.2.2-rc.10": ALL_V1},
            {},
            ALL_V1,
            "2.2.2-rc.10",
            id="prereleases_order_by_number",
        ),
        pytest.param(
            {"2.2.2": ALL_V1},
            {},
            {LOADER: "v1"},
            "2.2.2",
            id="a_subset_of_the_apps_matches",
        ),
        pytest.param(
            {"2.2.1": ALL_V1, "2.2.2": {**ALL_V2, LOADER: "v1"}},
            {},
            {LOADER: "v1"},
            "2.2.2",
            id="stacks_sharing_the_running_subset_resolve_to_the_newest",
        ),
        pytest.param(
            {"2.2.2": ALL_V2},
            {},
            ALL_V1,
            None,
            id="no_stack_has_the_digests",
        ),
        pytest.param(
            {"2.2.2": {LOADER: "v1"}},
            {},
            ALL_V1,
            None,
            id="no_stack_lists_every_running_app",
        ),
        pytest.param({"latest": ALL_V1, "main": ALL_V1}, {}, ALL_V1, None, id="no_semver_tags"),
        pytest.param({}, {}, ALL_V1, None, id="an_empty_repository"),
        pytest.param(
            {"2.2.3": ALL_V1, "2.2.2": ALL_V1},
            {"2.2.3": "no-layer"},
            ALL_V1,
            "2.2.2",
            id="an_artifact_with_no_manifest_layer_is_skipped",
        ),
        pytest.param(
            {"2.2.3": ALL_V1, "2.2.2": ALL_V1},
            {"2.2.3": "tampered"},
            ALL_V1,
            "2.2.2",
            id="a_layer_that_does_not_hash_to_its_digest_is_skipped",
        ),
        pytest.param(
            {"2.2.3": ALL_V1, "2.2.2": ALL_V1},
            {"2.2.3": "manifest-array"},
            ALL_V1,
            "2.2.2",
            id="an_artifact_manifest_that_is_not_an_object_is_skipped",
        ),
        pytest.param(
            {"2.2.3": ALL_V1, "2.2.2": ALL_V1},
            {"2.2.3": "not-yaml"},
            ALL_V1,
            "2.2.2",
            id="a_layer_that_is_not_yaml_is_skipped",
        ),
    ],
)
def test_resolve_stack(registry, stacks, damage, run, expected):
    registry.stacks = stacks
    registry.damage = damage
    assert resolve(registry, run) == expected


def test_one_anonymous_token_serves_every_request_and_every_page(registry):
    registry.stacks = {f"2.2.{patch}": ALL_V2 for patch in range(5)} | {"2.1.0": ALL_V1}
    assert resolve(registry, ALL_V1) == "2.1.0"
    assert registry.token_requests == 1


@pytest.mark.parametrize(
    ("repository", "run", "refusal"),
    [
        pytest.param("org/dfe-stack-manifest", ALL_V1, "is not host/path", id="no_host"),
        pytest.param("registry.example", ALL_V1, "is not host/path", id="no_path"),
        pytest.param("{host}/org/other", ALL_V1, "HTTPStatusError", id="repository_not_found"),
        pytest.param("127.0.0.1:1/org/x", ALL_V1, "ConnectError", id="registry_unreachable"),
    ],
)
def test_a_registry_that_cannot_be_read_stops_the_search(registry, repository, run, refusal):
    registry.stacks = {"2.2.2": ALL_V1}
    target = repository.format(host=registry.host)
    with pytest.raises(StackResolveError, match=refusal):
        resolve_stack(running(run), target, request_timeout_seconds=2.0, budget_seconds=10.0)


def test_a_slow_registry_costs_the_budget_and_no_more(registry):
    registry.stacks = {"2.2.2": ALL_V1}
    registry.delay_seconds = 0.5
    started = time.monotonic()
    with pytest.raises(StackResolveError):
        resolve(registry, ALL_V1, request=0.2, budget=0.4)
    assert time.monotonic() - started < 3


def test_a_stop_request_ends_the_search_before_the_next_tag(registry):
    registry.stacks = {"2.2.3": ALL_V2, "2.2.2": ALL_V1}
    stop = threading.Event()
    stop.set()
    found = resolve_stack(
        running(ALL_V1),
        registry.repository,
        request_timeout_seconds=5.0,
        budget_seconds=30.0,
        stop=stop,
    )
    assert found is None


def settings_for(registry: FakeRegistry, run: dict[str, str] | None, **extra) -> DFESettings:
    return DFESettings(
        env="dev",
        stack_images=running(run) if run else [],
        stack_manifest_repository=registry.repository,
        stack_version="2.2.2-rc.5",
        **extra,
    )


@pytest.mark.parametrize(
    ("run", "extra", "stacks", "expected", "calls_registry"),
    [
        pytest.param(ALL_V1, {}, {"2.2.2": ALL_V1}, "2.2.2", True, id="resolved"),
        pytest.param(ALL_V1, {}, {"2.2.2": ALL_V2}, None, True, id="no_match_leaves_the_label"),
        pytest.param(None, {}, {"2.2.2": ALL_V1}, None, False, id="no_images_skips_resolution"),
        pytest.param(
            ALL_V1,
            {"stack_resolve": False},
            {"2.2.2": ALL_V1},
            None,
            False,
            id="switched_off_for_an_air_gapped_site",
        ),
    ],
)
async def test_resolve_into(registry, run, extra, stacks, expected, calls_registry):
    registry.stacks = stacks
    state = SimpleNamespace(resolved_stack=None)

    await resolve_into(state, settings_for(registry, run, **extra))

    assert state.resolved_stack == expected
    assert (registry.requests > 0) is calls_registry


@pytest.mark.parametrize(
    "stack_images",
    [
        pytest.param(["registry.example/org/dfe-loader:v1"], id="an_image_with_no_digest"),
        pytest.param([image_ref(LOADER, "v1")], id="a_registry_that_is_down"),
    ],
)
async def test_a_failure_leaves_the_label_and_never_raises(stack_images):
    state = SimpleNamespace(resolved_stack=None)
    settings = DFESettings(
        env="dev",
        stack_images=stack_images,
        stack_manifest_repository="127.0.0.1:1/org/dfe-stack-manifest",
        stack_version="2.2.2-rc.5",
        stack_resolve_request_timeout_seconds=1.0,
        stack_resolve_budget_seconds=3.0,
    )

    await resolve_into(state, settings)

    assert state.resolved_stack is None
