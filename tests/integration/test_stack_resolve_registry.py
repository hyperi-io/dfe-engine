#  Project:      dfe-engine
#  File:         tests/integration/test_stack_resolve_registry.py
#  Purpose:      The stack resolver against the real published stack manifests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The resolver reads the stack manifests dfe-infra really publishes to ghcr.io.

The image references come from a published tag's own manifest, so the answer is checked
against the registry's current content and not a number written into the test. An
unreachable registry skips the test locally and fails it in CI, where a skip would be a
green build that resolved nothing.
"""

import pytest

from dfe_engine.stack_resolve import (
    RegistryReader,
    StackResolveError,
    manifest_images,
    newest_first,
    resolve_stack,
)
from tests.support.producer_contract import in_ci

pytestmark = pytest.mark.integration

REPOSITORY = "ghcr.io/hyperi-io/dfe-stack-manifest"
REQUEST_TIMEOUT_SECONDS = 10.0
BUDGET_SECONDS = 120.0


@pytest.fixture(scope="module")
def published() -> tuple[str, dict[str, str]]:
    """The newest published release tag and its manifest's images, name to reference."""
    try:
        with RegistryReader(
            REPOSITORY,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            budget_seconds=BUDGET_SECONDS,
        ) as reader:
            tags = newest_first(reader.tags())
            releases = [tag for tag in tags if "-" not in tag]
            assert releases, f"{REPOSITORY} publishes no release tag"
            return releases[0], manifest_images(reader.manifest_document(releases[0]))
    except StackResolveError as exc:
        if in_ci():
            pytest.fail(f"{REPOSITORY} unreadable: {exc}")
        pytest.skip(f"{REPOSITORY} unreadable: {exc}")


def _resolve(images: list[str]) -> str | None:
    return resolve_stack(
        images,
        REPOSITORY,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        budget_seconds=BUDGET_SECONDS,
    )


def test_every_image_of_the_newest_release_resolves_to_that_release(published):
    tag, images = published
    assert len(images) > 1
    assert _resolve(list(images.values())) == tag


def test_one_image_of_the_newest_release_resolves_to_that_release(published):
    tag, images = published
    assert _resolve([images["dfe-engine"]]) == tag


def test_a_digest_no_stack_published_resolves_to_nothing(published):
    _, images = published
    name, reference = next(iter(images.items()))
    unpublished = reference.split("@", 1)[0] + "@sha256:" + "0" * 64
    assert _resolve([unpublished]) is None, name
