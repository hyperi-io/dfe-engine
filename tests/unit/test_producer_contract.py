#  Project:      dfe-engine
#  File:         tests/unit/test_producer_contract.py
#  Purpose:      Tests for how the cross-repo contract tests find what a producer ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""How the cross-repo contract tests find what a producer ships.

A checkout wins, an unreadable contract fails in CI and skips elsewhere, and every pin
is a release. The last test reads GitHub and warns when a producer has released a change
to a file the engine still pins at an older release.
"""

import re
import warnings

import pytest

from tests.support import producer_contract
from tests.support.producer_contract import (
    PRODUCERS,
    SCALO_RS,
    ContractPinWarning,
    ContractUnreadableError,
    Producer,
    fetch_at,
    in_ci,
    latest_release,
    producer_file,
)

# Nothing listens on the discard port, so a fetch fails at once without leaving the host.
_UNREACHABLE = "http://127.0.0.1:9"
_CEL = SCALO_RS.files[0]


@pytest.fixture
def no_checkout_named(monkeypatch):
    monkeypatch.delenv(SCALO_RS.dir_env, raising=False)
    # One refused connection says as much as three, without the backoff between them.
    monkeypatch.setattr(producer_contract, "_ATTEMPTS", 1)


@pytest.fixture
def outside_ci(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _write(root, body: bytes):
    copy = root / _CEL
    copy.parent.mkdir(parents=True)
    copy.write_bytes(body)
    return copy


@pytest.mark.usefixtures("no_checkout_named")
def test_a_checkout_beside_this_one_wins_over_the_pin(tmp_path):
    copy = _write(tmp_path / "scalo-rs", b"uncommitted")
    found = producer_file(SCALO_RS, _CEL, checkouts=tmp_path, base_url=_UNREACHABLE)
    assert found.data == b"uncommitted"
    assert found.origin == str(copy)


def test_the_named_checkout_wins_over_the_one_beside_this_one(tmp_path, monkeypatch):
    _write(tmp_path / "scalo-rs", b"beside")
    _write(tmp_path / "elsewhere", b"named")
    monkeypatch.setenv(SCALO_RS.dir_env, str(tmp_path / "elsewhere"))
    found = producer_file(SCALO_RS, _CEL, checkouts=tmp_path, base_url=_UNREACHABLE)
    assert found.data == b"named"


@pytest.mark.usefixtures("no_checkout_named", "outside_ci")
@pytest.mark.parametrize(
    ("name", "value"), [("CI", "true"), ("CI", "1"), ("GITHUB_ACTIONS", "true")]
)
def test_an_unreadable_contract_fails_in_ci(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(pytest.fail.Exception, match=r"scalo-rs@v\S+ \S+ could not be read"):
        producer_file(SCALO_RS, _CEL, checkouts=tmp_path, base_url=_UNREACHABLE)


@pytest.mark.usefixtures("no_checkout_named", "outside_ci")
@pytest.mark.parametrize("value", ["", "false", "0"])
def test_an_unreadable_contract_skips_outside_ci(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CI", value)
    assert not in_ci()
    with pytest.raises(pytest.skip.Exception, match="SCALO_RS_DIR"):
        producer_file(SCALO_RS, _CEL, checkouts=tmp_path, base_url=_UNREACHABLE)


def test_a_file_the_producer_does_not_declare_is_refused():
    with pytest.raises(ValueError, match=r"does not declare 'src/lib\.rs'"):
        producer_file(SCALO_RS, "src/lib.rs")


@pytest.mark.parametrize("producer", PRODUCERS, ids=lambda producer: producer.repo)
def test_every_pin_is_a_release_tag(producer: Producer):
    # A branch compares against whatever merged last, which is no pin at all.
    assert re.fullmatch(r"v\d+\.\d+\.\d+", producer.ref), (
        f"{producer.repo} is pinned to {producer.ref!r}, not a release tag"
    )


@pytest.mark.parametrize("producer", PRODUCERS, ids=lambda producer: producer.repo)
def test_a_pin_behind_a_contract_change_is_reported(producer: Producer):
    """Warn when a producer's latest release changed a file the engine still pins older.

    A release that leaves every pinned file alone needs no bump, so it passes quietly. A
    check that cannot reach GitHub says so rather than passing as current.
    """
    moved: list[str] = []
    try:
        latest = latest_release(producer)
        if latest != producer.ref:
            pinned = {path: fetch_at(producer.repo, producer.ref, path) for path in producer.files}
            moved = [
                path
                for path in producer.files
                if fetch_at(producer.repo, latest, path) != pinned[path]
            ]
    except ContractUnreadableError as exc:
        if not in_ci():
            pytest.skip(f"could not check {producer.repo}'s pin: {exc}")
        warnings.warn(
            ContractPinWarning(f"{producer.repo}'s pin {producer.ref} could not be checked: {exc}"),
            stacklevel=1,
        )
        return
    if moved:
        warnings.warn(
            ContractPinWarning(
                f"{producer.repo} {latest} changed {', '.join(moved)} since the pinned "
                f"{producer.ref}; move the pin in tests/support/producer_contract.py once the "
                "suite ships it"
            ),
            stacklevel=1,
        )
