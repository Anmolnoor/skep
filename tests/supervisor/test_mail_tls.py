"""Phase 09 R2 (S6): every mail TLS path verifies the server.

``imaplib.IMAP4_SSL`` and ``smtplib.SMTP_SSL`` with no context use
``ssl._create_stdlib_context()``: no certificate check, no hostname check.
The proxied digest made that a walled script's default. Each path now
passes ``ssl.create_default_context()``; a local TLS server (a CA minted
here with the openssl CLI) proves the login is never sent to a server
whose certificate or name does not verify."""

from __future__ import annotations

import imaplib
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.mcp_servers import mail


def _mint(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    """(ca.pem, leaf.pem, leaf.key): a CA and a leaf for DNS ``name``."""
    if shutil.which("openssl") is None:
        pytest.skip("no openssl CLI to mint a test CA")

    def run(*argv: str) -> None:
        subprocess.run(["openssl", *argv], cwd=tmp_path, check=True, capture_output=True)

    # the extensions Python 3.13's VERIFY_X509_STRICT wants (see test_provider_relay)
    (tmp_path / "caext").write_text(
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
    )
    (tmp_path / "ext").write_text(
        f"subjectAltName=DNS:{name}\nauthorityKeyIdentifier=keyid\nbasicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
    )
    try:
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=t-ca",
            "-keyout", "ca.key", "-out", "ca.csr")  # fmt: skip
        run("x509", "-req", "-in", "ca.csr", "-signkey", "ca.key", "-days", "1",
            "-extfile", "caext", "-out", "ca.pem")  # fmt: skip
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}",
            "-keyout", "srv.key", "-out", "srv.csr")  # fmt: skip
        run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "1", "-extfile", "ext", "-out", "srv.pem")  # fmt: skip
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"openssl could not mint a test CA: {exc.stderr!r}")
    return tmp_path / "ca.pem", tmp_path / "srv.pem", tmp_path / "srv.key"


class _Server:
    """A TLS IMAP/SMTP stand-in on loopback; with ``connect`` it first
    answers one CONNECT (the wall's proxy and the host in one). Records
    every command line the client sends after the handshake."""

    def __init__(self, cert: Path, key: Path, *, smtp: bool, connect: bool) -> None:
        self.lines: list[str] = []
        self.handshakes: list[str] = []
        self.done = threading.Event()  # one handshake decided (the client may raise first)
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(cert, key)
        self._smtp, self._connect = smtp, connect
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            if self._connect:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(1024)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            try:
                tls = self._tls.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                self.handshakes.append(f"failed: {exc}")
                self.done.set()
                return
            self.handshakes.append("ok")
            self.done.set()
            with tls:
                tls.sendall(b"220 ready\r\n" if self._smtp else b"* OK ready\r\n")
                reader = tls.makefile("rb")
                for raw in reader:
                    line = raw.decode().strip()
                    self.lines.append(line)
                    tag = line.split(" ", 1)[0]
                    if self._smtp:
                        ehlo = tag.upper() == "EHLO"
                        tls.sendall(b"250-hi\r\n250 AUTH PLAIN\r\n" if ehlo else b"535 no\r\n")
                    elif line.upper().endswith("CAPABILITY"):
                        tls.sendall(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n".encode())
                    else:
                        tls.sendall(f"{tag} NO no\r\n".encode())

    def close(self) -> None:
        self._sock.close()


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    made: list[_Server] = []

    def start(*, name: str = "mail.example.test", smtp: bool = False, connect: bool = False,
              trusted: bool = True) -> _Server:  # fmt: skip
        ca, cert, key = _mint(tmp_path, name)
        if trusted:
            monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        else:
            monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "no-roots.pem"))
        made.append(_Server(cert, key, smtp=smtp, connect=connect))
        return made[-1]

    monkeypatch.setenv("SKEP_MAIL_USER", "op@example.test")
    monkeypatch.setenv("SKEP_MAIL_PASSWORD", "pw-never-sent")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    yield start
    for one in made:
        one.close()


def _no_login(srv: _Server) -> None:
    assert srv.done.wait(5)
    assert srv.handshakes and srv.handshakes[0].startswith("failed"), srv.handshakes
    assert not any("pw-never-sent" in line or "LOGIN" in line.upper() for line in srv.lines)


def test_the_proxied_digest_logs_in_to_a_verified_host(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a trusted cert for the right name gets as far as LOGIN."""
    srv = server(connect=True)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "mail.example.test")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{srv.port}")
    with pytest.raises(imaplib.IMAP4.error):
        mail.digest(1)
    assert srv.handshakes == ["ok"] and any(" LOGIN " in line for line in srv.lines), srv.lines


def test_the_proxied_digest_refuses_an_untrusted_certificate(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = server(connect=True, trusted=False)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "mail.example.test")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{srv.port}")
    with pytest.raises(ssl.SSLCertVerificationError):
        mail.digest(1)
    _no_login(srv)


def test_the_proxied_digest_refuses_a_certificate_for_another_name(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = server(name="other.example.test", connect=True)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "mail.example.test")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{srv.port}")
    with pytest.raises(ssl.SSLCertVerificationError, match="match"):
        mail.digest(1)
    _no_login(srv)


@pytest.mark.parametrize("call", ["digest", "list_recent", "read_message"])
def test_every_direct_imap_path_refuses_an_unverified_server(
    server: Any, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    srv = server(trusted=False)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("SKEP_MAIL_IMAP_PORT", str(srv.port))
    with pytest.raises(ssl.SSLCertVerificationError):
        if call == "digest":
            mail.digest(1)
        else:
            getattr(mail, call)({"uid": "1"})
    _no_login(srv)


def test_smtp_refuses_an_unverified_server(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = server(smtp=True, trusted=False)
    monkeypatch.setenv("SKEP_MAIL_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SKEP_MAIL_SMTP_PORT", str(srv.port))
    with pytest.raises(ssl.SSLCertVerificationError):
        mail.send_message({"to": "a@example.test", "subject": "s", "body": "b"})
    _no_login(srv)
