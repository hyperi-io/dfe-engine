#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/factories.py
#  Purpose:      Factories for the synthetic data unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for the synthetic data unit tests."""

from pathlib import Path

from dfe_engine.orgs.ingest_credentials import IngestCredential
from dfe_engine.secrets import DfeSecrets, build_secrets
from dfe_engine.settings import SecretsSettings
from dfe_engine.synthetic_data.credential import StreamCredential


def make_ingest_credential(name: str, org: str, **kwargs: object) -> IngestCredential:
    """Build an orgs.ingest_credentials.IngestCredential binding *org* alone, on the HTTP listener unless *kwargs* names others."""
    return IngestCredential.model_validate(
        {
            "default_org": org,
            "listeners": ["http"],
            "name": name,
            "org": org,
            "orgs": [org],
            **kwargs,
        }
    )


def make_secrets(*, path: Path) -> DfeSecrets:
    """Build a secrets.DfeSecrets on the file backend under *path*."""
    return build_secrets(cfg=SecretsSettings(path=str(path), provider="file"))


def make_stream_credential(
    *, minted: bool = False, refused_until: float = 0.0, token: str
) -> StreamCredential:
    """Build a synthetic_data.credential.StreamCredential."""
    return StreamCredential(minted=minted, refused_until=refused_until, token=token)
