#  Project:      dfe-engine
#  File:         tests/support/dockerhub.py
#  Purpose:      Route Docker Hub image pulls through the CI proxy cache when one is set
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The image reference a test hands to ``docker run``, rewritten for the Docker Hub mirror.

hyperi-ci exports ``HYPERCI_DOCKERHUB_MIRROR`` (a ``host/project`` prefix) on the runners
that can reach the Harbor proxy cache for Docker Hub, which avoids Docker Hub's anonymous
pull limit. Anywhere else the variable is absent and the reference is used as written.
"""

import os

MIRROR_ENV = "HYPERCI_DOCKERHUB_MIRROR"


def _names_a_registry(reference: str) -> bool:
    """Whether the first path component is a registry host, by Docker's own rule."""
    first, separator, _ = reference.partition("/")
    return bool(separator) and ("." in first or ":" in first or first == "localhost")


def dockerhub_image(reference: str) -> str:
    """Return *reference* pulled through the Docker Hub mirror, if one is set.

    An official image (``busybox:stable``) gains ``library/``, a namespaced one
    (``apache/kafka:4.3.1``) keeps its namespace, and a reference that already names a
    registry host (``ghcr.io/...``) is left alone, as is any ``@sha256:`` digest.

    Args:
        reference: A Docker image reference, as ``docker run`` would take it.

    Returns:
        The reference prefixed with the mirror, or *reference* unchanged when
        ``HYPERCI_DOCKERHUB_MIRROR`` is unset or empty or the image is not on Docker Hub.
    """
    mirror = os.environ.get(MIRROR_ENV, "").strip().rstrip("/")
    if not mirror or _names_a_registry(reference):
        return reference
    path = reference if "/" in reference else f"library/{reference}"
    return f"{mirror}/{path}"
