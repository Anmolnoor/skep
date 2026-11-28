"""mail — first-party IMAP/SMTP MCP server (v72-F5), stdlib only.

Register with ``scope=email`` (v41-F3): ``list_recent``/``read_message``
classify as ``email/read`` and flow; ``send_message`` classifies as
``email/send`` and CARDS. This file holds no permission logic — the policy
engine decides (I5); it only talks to the operator's mail host.

Config (environment):
  SKEP_MAIL_IMAP_HOST  IMAP server (required for reads), port SKEP_MAIL_IMAP_PORT (993)
  SKEP_MAIL_SMTP_HOST  SMTP server (required for send), port SKEP_MAIL_SMTP_PORT (465)
  SKEP_MAIL_USER       login (also the default From)
  SKEP_MAIL_FROM       From override
  SKEP_MAIL_PASSWORD   password; when unset, read from SKEP_MAIL_PASSWORD_FILE, else from
                       <SKEP_HOME>/supervisor/mail-secret (0600, the llm-secret pattern —
                       never SQLite, never an argument). A script schedule runs walled,
                       where the skep home is masked: it names SKEP_MAIL_PASSWORD_FILE.
  HTTPS_PROXY          set by a script schedule's wall: the digest tunnels IMAP through it

Run: python -m skep.mcp_servers.mail
     python -m skep.mcp_servers.mail --digest N   (v122-F4: print the newest N
     messages for a script schedule, then exit — a read, same config)
"""

from __future__ import annotations

import argparse
import contextlib
import email
import email.message
import email.policy
import imaplib
import json
import os
import smtplib
import socket
import ssl
import sys
import urllib.parse
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

_BODY_CAP = 8_000
_LIST_CAP = 20
# v122-F4: the digest fits a script schedule's output whole —
# skep.supervisor.scheduler.SCRIPT_OUTPUT_CAP (a test pins the two equal).
DIGEST_CAP = 4_000
_DIGEST_BODY_CAP = 500
_DIGEST_HEADER_CAP = 200

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_recent",
        "description": "List the newest messages in a mailbox: 'uid | date | from | "
        "subject' per line, newest first. Arguments: {limit?: int (default 10, max "
        "20), mailbox?: string (default INBOX)}. Example: {name: list_recent, "
        "arguments: {limit: 5}}. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "mailbox": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": True},  # B09: reads must say so
    },
    {
        "name": "read_message",
        "description": "Read ONE message's text body by uid (from list_recent). "
        "Arguments: {uid: string, mailbox?: string (default INBOX)}. Example: "
        "{name: read_message, arguments: {uid: '42'}}. Read-only; the body is "
        "capped at 8000 chars.",
        "inputSchema": {
            "type": "object",
            "properties": {"uid": {"type": "string"}, "mailbox": {"type": "string"}},
            "required": ["uid"],
        },
        "annotations": {"readOnlyHint": True},  # B09: reads must say so
    },
    {
        "name": "send_message",
        "description": "Send one plain-text email. Arguments: {to: string, subject: "
        "string, body: string}. Example: {name: send_message, arguments: {to: "
        "'a@b.c', subject: 'hi', body: '...'}}. This is a SEND — under the email "
        "scope it always requires operator confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check: exercises message "
        "composition and body extraction without any network. Reports whether "
        "mail config is present (non-fatal).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _home() -> Path:
    return Path(os.environ.get("SKEP_HOME") or "~/.skep").expanduser()


def resolve_password() -> str | None:
    env = os.environ.get("SKEP_MAIL_PASSWORD", "").strip()
    if env:
        return env
    named = os.environ.get("SKEP_MAIL_PASSWORD_FILE", "").strip()
    secret = Path(named) if named else _home() / "supervisor" / "mail-secret"
    if not secret.is_file():
        return None
    return secret.read_text(encoding="utf-8").strip() or None


def text_body(message: email.message.Message) -> str:
    """The first text/plain part, decoded and capped — honest when absent."""
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except LookupError:  # a charset Python does not know: read it as utf-8
                    text = payload.decode("utf-8", errors="replace")
                return text[:_BODY_CAP]
    return "[no text/plain part in this message]"


def compose(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def _config_error(*names: str) -> dict[str, Any] | None:
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if not missing:
        return None
    return {
        "content": "mail is not configured: set "
        + ", ".join(missing)
        + " in skep serve's environment (docs/assistant-tools.md)",
        "isError": True,
    }


def _decode_header_bytes(raw: object) -> str:
    return raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)


def list_recent(arguments: dict[str, Any]) -> dict[str, Any]:
    error = _config_error("SKEP_MAIL_IMAP_HOST", "SKEP_MAIL_USER")
    if error is not None:
        return error
    password = resolve_password()
    if password is None:
        return {
            "content": "no mail password: set SKEP_MAIL_PASSWORD or write "
            "<SKEP_HOME>/supervisor/mail-secret (0600)",
            "isError": True,
        }
    limit = min(int(arguments.get("limit") or 10), _LIST_CAP)
    mailbox = str(arguments.get("mailbox") or "INBOX")
    with imaplib.IMAP4_SSL(
        os.environ["SKEP_MAIL_IMAP_HOST"],
        int(os.environ.get("SKEP_MAIL_IMAP_PORT") or 993),
        ssl_context=ssl.create_default_context(),  # R2 (S6): verified, never the stdlib default
    ) as imap:
        imap.login(os.environ["SKEP_MAIL_USER"], password)
        imap.select(mailbox, readonly=True)
        _status, data = imap.uid("search", "ALL")
        uids = (data[0] or b"").split()
        lines: list[str] = []
        for uid in reversed(uids[-limit:]):
            _status, fetched = imap.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
            headers = email.message_from_bytes(raw if isinstance(raw, bytes) else b"")
            lines.append(
                f"{_decode_header_bytes(uid)} | {headers.get('Date', '?')} | "
                f"{headers.get('From', '?')} | {headers.get('Subject', '(no subject)')}"
            )
    return {"content": "\n".join(lines) or f"{mailbox} is empty"}


def read_message(arguments: dict[str, Any]) -> dict[str, Any]:
    if not str(arguments.get("uid") or "").strip():
        return {"content": "read_message needs {uid: string} from list_recent", "isError": True}
    error = _config_error("SKEP_MAIL_IMAP_HOST", "SKEP_MAIL_USER")
    if error is not None:
        return error
    password = resolve_password()
    if password is None:
        return {"content": "no mail password (see list_recent)", "isError": True}
    mailbox = str(arguments.get("mailbox") or "INBOX")
    with imaplib.IMAP4_SSL(
        os.environ["SKEP_MAIL_IMAP_HOST"],
        int(os.environ.get("SKEP_MAIL_IMAP_PORT") or 993),
        ssl_context=ssl.create_default_context(),  # R2 (S6): verified, never the stdlib default
    ) as imap:
        imap.login(os.environ["SKEP_MAIL_USER"], password)
        imap.select(mailbox, readonly=True)
        _status, fetched = imap.uid("fetch", str(arguments["uid"]), "(RFC822)")
        raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
        if not isinstance(raw, bytes):
            return {"content": f"no message with uid {arguments['uid']!r}", "isError": True}
        message = email.message_from_bytes(raw)
    return {
        "content": (
            f"From: {message.get('From', '?')}\nDate: {message.get('Date', '?')}\n"
            f"Subject: {message.get('Subject', '(no subject)')}\n\n{text_body(message)}"
        )
    }


def send_message(arguments: dict[str, Any]) -> dict[str, Any]:
    for field in ("to", "subject", "body"):
        if not str(arguments.get(field) or "").strip():
            return {
                "content": "send_message needs {to, subject, body}, all non-empty",
                "isError": True,
            }
    error = _config_error("SKEP_MAIL_SMTP_HOST", "SKEP_MAIL_USER")
    if error is not None:
        return error
    password = resolve_password()
    if password is None:
        return {"content": "no mail password (see list_recent)", "isError": True}
    sender = os.environ.get("SKEP_MAIL_FROM", "").strip() or os.environ["SKEP_MAIL_USER"]
    message = compose(
        sender, str(arguments["to"]), str(arguments["subject"]), str(arguments["body"])
    )
    with smtplib.SMTP_SSL(
        os.environ["SKEP_MAIL_SMTP_HOST"],
        int(os.environ.get("SKEP_MAIL_SMTP_PORT") or 465),
        context=ssl.create_default_context(),  # R2 (S6): certificate and name checked
    ) as smtp:
        smtp.login(os.environ["SKEP_MAIL_USER"], password)
        smtp.send_message(message)
    return {"content": f"sent to {arguments['to']}: {arguments['subject']}"}


def self_test(_arguments: dict[str, Any]) -> dict[str, Any]:
    """Offline by design — the forge-trial shape runs with NO network."""
    fixture = (
        b"From: probe@example.test\r\nTo: op@example.test\r\nSubject: probe\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nself-test body\r\n"
    )
    parsed = email.message_from_bytes(fixture)
    body = text_body(parsed)
    composed = compose("a@b.test", "c@d.test", "s", "self-test body")
    if "self-test body" not in body or composed["Subject"] != "s":
        return {"content": "message round-trip broken", "isError": True}
    configured = _config_error("SKEP_MAIL_IMAP_HOST", "SKEP_MAIL_SMTP_HOST", "SKEP_MAIL_USER")
    note = " (mail env not configured yet — reads/sends will say what to set)" if configured else ""
    return {"content": f"self_test passed: compose + body extraction intact{note}"}


def _one_line(value: object, cap: int = _DIGEST_HEADER_CAP) -> str:
    """A header value on one line: str.split() also splits the separators
    str.splitlines() breaks on, so no header can start a line of its own."""
    return " ".join(str(value).split())[:cap]


def _digest_block(raw: bytes) -> str:
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
        body = text_body(message).strip()
        if len(body) > _DIGEST_BODY_CAP:
            body = body[:_DIGEST_BODY_CAP] + " [...]"
        quoted = "\n".join("> " + line for line in body.splitlines())
        return (
            f"---\nFrom: {_one_line(message.get('From', '?'))}\n"
            f"Subject: {_one_line(message.get('Subject', '(no subject)'))}\n"
            f"Date: {_one_line(message.get('Date', '?'))}\n{quoted}"
        )
    except Exception as exc:  # one bad message never kills the digest
        reason = _one_line(f"{type(exc).__name__}: {exc}")
        return (
            "---\nFrom: (unreadable header)\nSubject: ?\nDate: ?\n"
            f"> (this message could not be read: {reason})"
        )


def proxy_tunnel(proxy: str, host: str, port: int, timeout: float | None) -> socket.socket:
    """Phase 09 R1 (S7): a script schedule runs walled, and its one way out is
    the FilteringProxy its HTTPS_PROXY names, which still admits only the
    schedule's hosts. IMAP has no proxy support, so open one CONNECT tunnel."""
    where = urllib.parse.urlsplit(proxy)
    sock = socket.create_connection((where.hostname or "127.0.0.1", where.port or 80), timeout)
    try:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                break
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode("latin-1")
        if status.split(" ")[1:2] != ["200"]:
            raise OSError(f"the proxy refused CONNECT {host}:{port}: {status or 'no reply'}")
    except BaseException:
        sock.close()
        raise
    return sock


class _ClosesOnCancel:
    """Mixin for the digest's IMAP connection: closed if it is cancelled
    while it connects. The stdlib constructor cleans up on Exception only; a
    caller's timer (a BaseException, the morning brief's time share) left
    the socket open."""

    def _tls(self, sock: socket.socket) -> socket.socket:
        imap: Any = self
        try:
            wrapped: socket.socket = imap.ssl_context.wrap_socket(sock, server_hostname=imap.host)
        except BaseException:
            sock.close()
            raise
        return wrapped

    def _connect(self) -> None:  # the wait for the server's greeting
        imap: Any = self
        try:
            super()._connect()  # type: ignore[misc]
        except Exception:
            raise  # the stdlib constructor shuts down on these itself
        except BaseException:
            with contextlib.suppress(OSError):
                imap.shutdown()
            raise


class _DirectIMAP4_SSL(_ClosesOnCancel, imaplib.IMAP4_SSL):
    def _create_socket(self, timeout: float | None = None) -> socket.socket:
        return self._tls(imaplib.IMAP4._create_socket(self, timeout))  # type: ignore[attr-defined]


class _ProxiedIMAP4_SSL(_ClosesOnCancel, imaplib.IMAP4_SSL):
    """IMAP4_SSL over ``proxy_tunnel``; TLS still runs end to end with the host."""

    def __init__(self, host: str, port: int, *, proxy: str) -> None:
        self._proxy = proxy
        super().__init__(host, port, ssl_context=ssl.create_default_context())

    def _create_socket(self, timeout: float | None = None) -> socket.socket:
        return self._tls(proxy_tunnel(self._proxy, self.host, self.port, timeout))


@contextlib.contextmanager
def _session(imap: imaplib.IMAP4) -> Iterator[imaplib.IMAP4]:
    """``with imap`` for the digest, except when it is cancelled: a caller's
    timer (a BaseException, e.g. the morning brief's time share) closes the
    socket without the LOGOUT round trip, which would wait on the server."""
    try:
        yield imap
    except Exception:
        imap.__exit__(None, None, None)
        raise
    except BaseException:
        with contextlib.suppress(OSError):
            imap.shutdown()
        raise
    imap.__exit__(None, None, None)


def digest(count: int, mailbox: str = "INBOX") -> str:
    """The newest ``count`` messages as blocks the v121 injection alarm
    parses: a ``---`` line, then ``From:``/``Subject:``/``Date:`` at column
    0, then the plain body with every line prefixed ``> `` (a body cannot
    forge a sender). Blocks that would pass DIGEST_CAP are dropped and
    counted. A read: EXAMINE and BODY.PEEK, so no flag changes."""
    password = resolve_password()
    if password is None:
        raise ValueError(
            "no mail password: a script schedule names a 0600 file outside ~/.skep "
            "inline (SKEP_MAIL_PASSWORD_FILE=/abs/path); elsewhere ~/.skep/supervisor/mail-secret"
        )
    host, port = (
        os.environ["SKEP_MAIL_IMAP_HOST"],
        int(os.environ.get("SKEP_MAIL_IMAP_PORT") or 993),
    )
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    connect = (
        _DirectIMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        if not proxy
        else _ProxiedIMAP4_SSL(host, port, proxy=proxy)
    )
    with _session(connect) as imap:
        imap.login(os.environ["SKEP_MAIL_USER"], password)
        imap.select(mailbox, readonly=True)
        _status, data = imap.uid("search", "ALL")
        uids = list(reversed((data[0] or b"").split()))[: max(min(count, _LIST_CAP), 0)]
        blocks: list[str] = []
        for uid in uids:
            _status, fetched = imap.uid("fetch", _decode_header_bytes(uid), "(BODY.PEEK[])")
            raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
            blocks.append(_digest_block(raw if isinstance(raw, bytes) else b""))
    if not blocks:
        return f"{mailbox} is empty"
    shown: list[str] = []
    for index, block in enumerate(blocks):
        trailer = (
            f"\n(+{len(blocks) - index - 1} more not shown)" if index + 1 < len(blocks) else ""
        )
        if len("\n".join([*shown, block])) + len(trailer) > DIGEST_CAP:
            break
        shown.append(block)
    out = "\n".join(shown)
    if len(shown) < len(blocks):
        out += f"\n(+{len(blocks) - len(shown)} more not shown)"
    return out


_HANDLERS = {
    "list_recent": list_recent,
    "read_message": read_message,
    "send_message": send_message,
    "self_test": self_test,
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {
            "content": f"no tool named {name!r}; tools: {', '.join(_HANDLERS)}",
            "isError": True,
        }
    return handler(arguments)


def _digest_main(count: int) -> None:
    missing = [n for n in ("SKEP_MAIL_IMAP_HOST", "SKEP_MAIL_USER") if not os.environ.get(n)]
    try:
        if missing:
            raise ValueError(
                f"mail is not configured: set {', '.join(missing)} inline before the command — "
                "a script schedule runs with a scrubbed env (docs/daily-driver-setup.md)"
            )
        print(digest(count))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    except (imaplib.IMAP4.error, OSError) as exc:
        print(
            f"mail pull failed: {_one_line(exc)} — check SKEP_MAIL_IMAP_HOST/PORT (and that "
            "the schedule's hosts name it), SKEP_MAIL_USER and the password file",
            file=sys.stderr,
        )
        sys.exit(2)


def _digest_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= _LIST_CAP:
        raise argparse.ArgumentTypeError(
            f"N must be between 1 and {_LIST_CAP} (got {count}); 5 fits the script output cap"
        )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m skep.mcp_servers.mail")
    parser.add_argument(
        "--digest", type=_digest_count, metavar="N", help="print the newest N (1-20) and exit"
    )
    args = parser.parse_args()
    if args.digest is not None:
        _digest_main(args.digest)
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "skep-mail", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(str(params.get("name")), params.get("arguments") or {})
            except Exception as exc:  # a tool error is a reply, never a crash
                result = {"content": f"{type(exc).__name__}: {exc}", "isError": True}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)


if __name__ == "__main__":
    main()
