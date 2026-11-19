#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_credential/test_credential.py
#  Purpose:      Tests for the synthetic-data stream credential
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the synthetic-data stream credential."""

from pathlib import Path

import pytest

from dfe_engine.orgs.home import PLAIN_FILE
from dfe_engine.orgs.ingest_credentials import SYNTHETIC_DATA_CREDENTIAL, IngestCredential
from dfe_engine.orgs.ingest_tokens import credential_secret_path, store_token
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.secrets import DfeSecrets
from dfe_engine.synthetic_data.credential import stream_credential
from tests.support.present import present
from tests.unit.test_orgs.factories import make_home_org_store, make_org_registry
from tests.unit.test_synthetic_data.factories import make_ingest_credential, make_secrets
from tests.unit.test_synthetic_data.test_credential.credential_cases import (
    REFUSED_UNTIL_CASES,
    STREAM_CREDENTIAL_CASES,
    STREAM_CREDENTIAL_NONE_CASES,
    WANTED,
    RefusedUntilCase,
    StreamCredentialCase,
    StreamCredentialNoneCase,
)


def _hold(
    *, credential: IngestCredential, registry: OrgRegistry, secrets: DfeSecrets, token: str | None
) -> None:
    """Store *credential*, and *token* as its token where one is given."""
    registry.ingest_credentials.put(credential=credential)
    if token is not None:
        store_token(credential=credential, secrets=secrets, token=token)


def _name_primary(*, registry: OrgRegistry) -> None:
    """Name acme the primary org."""
    present(registry.home).set(actor="test", name="acme")


@pytest.fixture
def registry(tmp_path: Path) -> OrgRegistry:
    """An org registry holding acme and beta, with no primary org named."""
    registry = make_org_registry(
        home=make_home_org_store(config_dir=tmp_path), path=tmp_path / "orgs"
    )
    registry.create(name="acme")
    registry.create(name="beta")
    return registry


@pytest.fixture
def secrets(tmp_path: Path) -> DfeSecrets:
    """A file secrets backend."""
    return make_secrets(path=tmp_path / "secrets")


class TestStreamCredential:
    @pytest.mark.parametrize(
        "case", STREAM_CREDENTIAL_CASES, ids=[case["id"] for case in STREAM_CREDENTIAL_CASES]
    )
    def test_matches_expected(
        self, registry: OrgRegistry, secrets: DfeSecrets, case: StreamCredentialCase
    ):
        _name_primary(registry=registry)
        _hold(
            credential=case["stored"],
            registry=registry,
            secrets=secrets,
            token=case["stored_token"],
        )
        result = present(stream_credential(registry=registry, secrets=secrets))
        held = present(registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL))
        assert (
            result.minted,
            held.content(),
            secrets.get_or_none(credential_secret_path(credential=held)),
            secrets.get_or_none(credential_secret_path(credential=case["stored"])),
        ) == (case["expected_minted"], WANTED, result.token, case["expected_old_token"])

    def test_mints_with_none_stored(self, registry: OrgRegistry, secrets: DfeSecrets):
        _name_primary(registry=registry)
        result = present(stream_credential(registry=registry, secrets=secrets))
        held = present(registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL))
        assert (
            result.minted,
            held.content(),
            secrets.get_or_none(credential_secret_path(credential=held)),
        ) == (True, WANTED, result.token)

    @pytest.mark.parametrize(
        "case", REFUSED_UNTIL_CASES, ids=[case["id"] for case in REFUSED_UNTIL_CASES]
    )
    def test_refused_until(
        self, registry: OrgRegistry, secrets: DfeSecrets, case: RefusedUntilCase
    ):
        _name_primary(registry=registry)
        _hold(
            credential=make_ingest_credential(
                name=SYNTHETIC_DATA_CREDENTIAL, org="acme", rotated_at=case["rotated_at"]
            ),
            registry=registry,
            secrets=secrets,
            token="held-token",
        )
        result = present(stream_credential(registry=registry, secrets=secrets))
        assert result.refused_until == case["expected_refused_until"]

    @pytest.mark.parametrize(
        "case",
        STREAM_CREDENTIAL_NONE_CASES,
        ids=[case["id"] for case in STREAM_CREDENTIAL_NONE_CASES],
    )
    def test_none(
        self,
        registry: OrgRegistry,
        secrets: DfeSecrets,
        tmp_path: Path,
        case: StreamCredentialNoneCase,
    ):
        path = tmp_path / PLAIN_FILE
        path.parent.mkdir(parents=True)
        path.write_text(case["doc"], encoding="utf-8")
        assert stream_credential(registry=registry, secrets=secrets) is None

    def test_secrets_backend_refuses(self, registry: OrgRegistry, tmp_path: Path):
        _name_primary(registry=registry)
        occupied = tmp_path / "occupied"
        occupied.write_text("", encoding="utf-8")
        refusing = make_secrets(path=occupied / "secrets")
        assert (
            stream_credential(registry=registry, secrets=refusing),
            registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL),
        ) == (None, None)
