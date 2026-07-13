#  Project:      dfe-engine
#  File:         tests/unit/test_backend_literals.py
#  Purpose:      Enforce: the engine codes to seams, never a backend SDK
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine must not import a SECRETS-backend SDK directly.

Secrets backend coupling belongs in scalo.secrets (provider by config), never in
engine code - that is what makes the secrets backing service swappable (file /
openbao / aws / gcp / azure) without touching dfe-engine. See docs/deployment/backing-services.md.

Note: boto3 is deliberately NOT forbidden - it is the client for the object-store
seam (the S3 API), which is portable across MinIO / S3 / any S3-compatible endpoint
by config, so it is not product lock-in.
"""

from __future__ import annotations

import pathlib
import re

# Secrets-backend SDKs that must be reached through scalo.secrets, never imported
# directly by the engine. (boto3 is allowed - it is the S3-API object-store seam.)
_FORBIDDEN = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(hvac|openbao|google\.cloud\.secretmanager|azure\.keyvault|azure\.identity)\b",
    re.MULTILINE,
)


def test_no_backend_sdk_imports():
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "dfe_engine"
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if _FORBIDDEN.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"backend SDK imported directly (route via scalo): {offenders}"
