#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_directory.py
#  Purpose:      Local HTTP(S) server answering directory API requests with the replies a test sets
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP(S) server answering directory API requests with the replies a test sets."""

import datetime
import ipaddress
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Replaced by the server origin in a reply's body and header values, so a next-page link can point back at the server.
BASE_PLACEHOLDER = "{base}"


@dataclass(frozen=True, slots=True)
class DirectoryReply:
    """One HTTP response the directory sends for a request target."""

    body: str
    # Name and value pairs, so one header can be sent on several lines.
    headers: list[tuple[str, str]] = field(default_factory=list)
    status: int = 200


def _ca_extensions(
    *, builder: x509.CertificateBuilder, key: ec.EllipticCurvePrivateKey
) -> x509.CertificateBuilder:
    """Add the extensions a strict verifier requires of a CA certificate."""
    usage = x509.KeyUsage(
        content_commitment=False,
        crl_sign=True,
        data_encipherment=False,
        decipher_only=False,
        digital_signature=False,
        encipher_only=False,
        key_agreement=False,
        key_cert_sign=True,
        key_encipherment=False,
    )
    return (
        builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )


def _handler_for(*, directory: LocalDirectory) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the replies of ``directory``."""

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            """Send the reply set for this request target, or a 404."""
            directory.requests.append(self.path)
            required = directory.required_authorization
            refused = required is not None and self.headers.get("Authorization") != required
            unknown = DirectoryReply(body="{}", status=404)
            reply = (
                DirectoryReply(body="{}", status=401)
                if refused
                else directory.replies.get(self.path, unknown)
            )
            encoded = reply.body.replace(BASE_PLACEHOLDER, directory.base_url).encode("utf-8")
            self.send_response(reply.status)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Content-Type", "application/json")
            for name, value in reply.headers:
                self.send_header(name, value.replace(BASE_PLACEHOLDER, directory.base_url))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            """Answer a directory API request."""
            self._reply()

        def do_POST(self) -> None:
            """Read the request body, then answer as for a GET (a token endpoint is a POST)."""
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply()

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


def _issue_certificates(*, tls_dir: Path) -> tuple[Path, Path, Path]:
    """Write a CA and a 127.0.0.1 server certificate signed by it, returning the CA, certificate and key files."""
    now = datetime.datetime.now(tz=datetime.UTC)
    ca_key = ec.generate_private_key(curve=ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dfe-engine test directory CA")])
    ca_builder = (
        x509.CertificateBuilder()
        .issuer_name(ca_name)
        .not_valid_after(now + datetime.timedelta(hours=1))
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .subject_name(ca_name)
    )
    ca = _ca_extensions(builder=ca_builder, key=ca_key).sign(
        algorithm=hashes.SHA256(), private_key=ca_key
    )
    key = ec.generate_private_key(curve=ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    certificate = (
        x509.CertificateBuilder()
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .issuer_name(ca_name)
        .not_valid_after(now + datetime.timedelta(hours=1))
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .subject_name(server_name)
        .sign(algorithm=hashes.SHA256(), private_key=ca_key)
    )
    tls_dir.mkdir(parents=True, exist_ok=True)
    ca_file = tls_dir / "ca.pem"
    certificate_file = tls_dir / "server.pem"
    key_file = tls_dir / "server-key.pem"
    ca_file.write_bytes(ca.public_bytes(encoding=serialization.Encoding.PEM))
    certificate_file.write_bytes(certificate.public_bytes(encoding=serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            encryption_algorithm=serialization.NoEncryption(),
            format=serialization.PrivateFormat.PKCS8,
        )
    )
    return ca_file, certificate_file, key_file


class LocalDirectory:
    """A directory API on 127.0.0.1 answering each request target from ``replies``, over TLS when given a ``tls_dir``.

    A client trusts the TLS server once ``ca_file`` is its CA bundle, for example through ``SSL_CERT_FILE``.
    """

    def __init__(self, *, tls_dir: Path | None) -> None:
        self.ca_file: Path | None = None
        self.replies: dict[str, DirectoryReply] = {}
        self.requests: list[str] = []
        # When set, a request whose Authorization header differs is answered 401.
        self.required_authorization: str | None = None
        self._scheme = "http"
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(directory=self))
        if tls_dir is not None:
            self.ca_file, certificate_file, key_file = _issue_certificates(tls_dir=tls_dir)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=certificate_file, keyfile=key_file)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            self._scheme = "https"
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``https://127.0.0.1:54321``."""
        return f"{self._scheme}://{self.host}"

    @property
    def host(self) -> str:
        """The server's host and port, such as ``127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        self._server.shutdown()
        self._server.server_close()
