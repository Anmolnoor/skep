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
  SKEP_MAIL_PASSWORD   password; when unset, read from <SKEP_HOME>/supervisor/mail-secret
                       (0600, the llm-secret pattern — never SQLite, never an argument)

Run: python -m skep.mcp_servers.mail
"""

from __future__ import annotations

import email
import email.message
import imaplib
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any

_BODY_CAP = 8_000
_LIST_CAP = 20

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
    secret = _home() / "supervisor" / "mail-secret"
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
                return payload.decode(charset, errors="replace")[:_BODY_CAP]
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
            raw = next(
                (part[1] for part in fetched if isinstance(part, tuple)), b""
            )
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


def main() -> None:
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
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
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
