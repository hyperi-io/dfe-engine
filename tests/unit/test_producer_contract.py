#  Project:      dfe-engine
#  File:         tests/unit/test_producer_contract.py
#  Purpose:      Tests for how the cross-repo contract tests find what a producer ships
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""How the cross-repo contract tests find what a producer ships.

A checkout wins, an unreadable public contract fails in CI and skips elsewhere, a private
one GitHub hides skips everywhere, a token stays on GitHub, and every pin is a release.
The last test reads GitHub and warns when a producer has released a change to a file the
engine still pins at an older release.
"""

import dataclasses
import http.server
import io
import re
import tarfile
import threading

import pytest

from tests.support import producer_contract
from tests.support.producer_contract import (
    DFE_LOADER,
    PRODUCERS,
    SCALO_RS,
    ContractPinWarning,
    ContractUnreadableError,
    Producer,
    check_pin,
    fetch_at,
    hidden_private_reason,
    in_ci,
    producer_file,
    producer_tree,
)

# Nothing listens on the discard port, so a fetch fails at once without leaving the host.
_UNREACHABLE = "http://127.0.0.1:9"
_CEL = SCALO_RS.files[0]
_PUBLIC = Producer(
    repo="public-probe", ref="v0.0.0", dir_env="PUBLIC_PROBE_DIR", files=(_CEL,), private=False
)
_PRIVATE = dataclasses.replace(
    _PUBLIC, repo="private-probe", dir_env="PRIVATE_PROBE_DIR", private=True
)


@pytest.fixture
def no_checkout_named(monkeypatch):
    for producer in (SCALO_RS, _PUBLIC, _PRIVATE):
        monkeypatch.delenv(producer.dir_env, raising=False)
    # One refused connection says as much as three, without the backoff between them.
    monkeypatch.setattr(producer_contract, "_ATTEMPTS", 1)


@pytest.fixture
def outside_ci(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def in_a_ci_run(monkeypatch, outside_ci):
    monkeypatch.setenv("CI", "true")


@pytest.fixture
def local_server():
    """Start a server on a free local port answering every GET with one status.

    Yields a function taking the status and body and returning the base URL plus the
    ``Authorization`` header each request carried.
    """
    servers: list[http.server.HTTPServer] = []

    def start(status: int, body: bytes = b"") -> tuple[str, list[str | None]]:
        seen: list[str | None] = []

        class _Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Answer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", seen

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _write(root, body: bytes):
    copy = root / _CEL
    copy.parent.mkdir(parents=True)
    copy.write_bytes(body)
    return copy


_CHARTS = "helm/charts"


def _archive(files: dict[str, bytes]) -> bytes:
    """A tar.gz shaped like GitHub's: every path under one repo-and-ref directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"public-probe-v0.0.0/{name}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


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


@pytest.mark.usefixtures("no_checkout_named", "in_a_ci_run")
def test_a_public_producer_github_hides_still_fails_in_ci(tmp_path, local_server):
    base_url, _ = local_server(404)
    with pytest.raises(pytest.fail.Exception, match=r"public-probe@v0\.0\.0 .* HTTP 404"):
        producer_file(_PUBLIC, _CEL, checkouts=tmp_path, base_url=base_url)


@pytest.mark.usefixtures("no_checkout_named", "in_a_ci_run")
def test_a_private_producer_github_hides_skips_even_in_ci(tmp_path, local_server):
    base_url, _ = local_server(404)
    with pytest.raises(pytest.skip.Exception) as skipped:
        producer_file(_PRIVATE, _CEL, checkouts=tmp_path, base_url=base_url)
    assert skipped.value.msg == (
        "private-probe is private until GA; set GH_TOKEN with read on it, or it runs once "
        "the repo is public (dfe-engine #553)"
    )


@pytest.mark.usefixtures("no_checkout_named", "in_a_ci_run")
def test_a_private_producer_that_cannot_connect_still_fails_in_ci(tmp_path):
    # Only GitHub's 404 means "private"; a refused connection is a real failure.
    with pytest.raises(pytest.fail.Exception, match="ConnectError"):
        producer_file(_PRIVATE, _CEL, checkouts=tmp_path, base_url=_UNREACHABLE)


def test_the_dfe_loader_skip_says_why_and_when_it_ends():
    assert hidden_private_reason(DFE_LOADER, ContractUnreadableError("hidden", 404)) == (
        "dfe-loader is private until GA; set GH_TOKEN with read on it, or it runs once the "
        "repo is public (dfe-engine #553)"
    )


def test_only_the_repos_private_until_ga_may_skip_in_ci():
    # Marking a public producer private would let its contract skip in CI unnoticed.
    private = {producer.repo for producer in PRODUCERS if producer.private}
    assert private == {"dfe-loader", "dfe-transform-vrl"}


def test_a_token_is_never_sent_off_github(monkeypatch, local_server):
    base_url, seen = local_server(200, b"served")
    monkeypatch.setenv("GITHUB_TOKEN", "held-for-github")
    assert fetch_at("scalo-rs", "v0.0.0-token-probe", _CEL, base_url) == b"served"
    assert seen == [None]


@pytest.mark.usefixtures("no_checkout_named")
def test_a_tree_in_a_checkout_beside_this_one_wins_over_the_pin(tmp_path):
    (tmp_path / "public-probe" / _CHARTS / "app").mkdir(parents=True)
    found = producer_tree(
        _PUBLIC, _CHARTS, tmp_path / "dest", checkouts=tmp_path, base_url=_UNREACHABLE
    )
    assert found == tmp_path / "public-probe" / _CHARTS


@pytest.mark.usefixtures("no_checkout_named")
def test_a_tree_comes_from_the_archive_at_the_pin_without_its_siblings(tmp_path, local_server):
    base_url, _ = local_server(
        200, _archive({f"{_CHARTS}/app/Chart.yaml": b"name: app", "docs/README.md": b"docs"})
    )
    dest = tmp_path / "dest"
    found = producer_tree(_PUBLIC, _CHARTS, dest, checkouts=tmp_path, base_url=base_url)
    assert found == dest / _CHARTS
    assert (found / "app" / "Chart.yaml").read_bytes() == b"name: app"
    assert not (dest / "docs").exists()


@pytest.mark.usefixtures("no_checkout_named", "in_a_ci_run")
def test_an_unreadable_tree_fails_in_ci(tmp_path):
    with pytest.raises(pytest.fail.Exception, match=r"public-probe@v0\.0\.0 helm/charts could not"):
        producer_tree(
            _PUBLIC, _CHARTS, tmp_path / "dest", checkouts=tmp_path, base_url=_UNREACHABLE
        )


@pytest.mark.usefixtures("no_checkout_named")
def test_an_archive_without_the_tree_fails(tmp_path, local_server):
    base_url, _ = local_server(200, _archive({"docs/README.md": b"docs"}))
    with pytest.raises(pytest.fail.Exception, match=r"public-probe@v0\.0\.0 has no helm/charts"):
        producer_tree(_PUBLIC, _CHARTS, tmp_path / "dest", checkouts=tmp_path, base_url=base_url)


def test_a_file_the_producer_does_not_declare_is_refused():
    with pytest.raises(ValueError, match=r"does not declare 'src/lib\.rs'"):
        producer_file(SCALO_RS, "src/lib.rs")


@pytest.mark.parametrize("producer", PRODUCERS, ids=lambda producer: producer.repo)
def test_every_pin_is_a_release_tag(producer: Producer):
    # A branch compares against whatever merged last, which is no pin at all.
    assert re.fullmatch(r"v\d+\.\d+\.\d+", producer.ref), (
        f"{producer.repo} is pinned to {producer.ref!r}, not a release tag"
    )


@pytest.mark.usefixtures("in_a_ci_run")
def test_a_private_pin_check_github_hides_stays_quiet(local_server, recwarn):
    base_url, _ = local_server(404)
    with pytest.raises(pytest.skip.Exception, match="private-probe is private until GA"):
        check_pin(_PRIVATE, api_base=base_url, base_url=base_url)
    assert not [w for w in recwarn if issubclass(w.category, ContractPinWarning)]


@pytest.mark.usefixtures("in_a_ci_run")
def test_a_public_pin_check_that_cannot_run_warns_in_ci(local_server):
    base_url, _ = local_server(404)
    with pytest.warns(ContractPinWarning, match="public-probe's pin v0.0.0 could not be checked"):
        check_pin(_PUBLIC, api_base=base_url, base_url=base_url)


@pytest.mark.parametrize("producer", PRODUCERS, ids=lambda producer: producer.repo)
def test_a_pin_behind_a_contract_change_is_reported(producer: Producer):
    check_pin(producer)
