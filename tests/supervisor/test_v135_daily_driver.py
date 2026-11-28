"""v135 Wave 3 (ADR 0070 Part 1): a morning ritual that works and keeps its slot.

F4: a Telegram or Discord turn stores the same ``skill_hint`` the web route
stores (Phase 06 known limit). F5: the walled ``fetch_brief.py`` step reads
tech news (HN, RSS, Atom) inside its 4,000-character cap. F6: "run now" keeps
tomorrow's slot. F7: a daily schedule with a timezone keeps its wall-clock
time across daylight-saving changes.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.scheduler import make_schedule, next_run_after, run_due
from skep.supervisor.seed_skills import load_seed_skills
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.store import ScheduleRecord

from .conftest import serve_client
from .fake_ollama import FakeOllama

NEW_SCHEDULE_COLUMNS = ("resume_slot", "timezone", "wall_time")


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


# -- F4: skill hints on channel turns ------------------------------------------


@dataclass
class ChannelRig:
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama


@pytest.fixture()
def channel_rig(config: SupervisorConfig) -> Iterator[ChannelRig]:
    ollama = FakeOllama(api_key=None).start()
    client = serve_client(config)
    client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "m"})
    store = RunStore(config.db_path)
    load_seed_skills(store)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder), home=config.home
    )
    yield ChannelRig(store, engine, ollama)
    store.close()
    ollama.stop()


@pytest.mark.parametrize("channel", ["telegram", "discord"])
def test_a_channel_turn_stores_the_skill_hint_like_the_web(
    channel_rig: ChannelRig, channel: str
) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn
    from skep.supervisor.serve.chat import skill_hint

    text = "Can you merge these two PDFs for me?"
    message = ChannelMessage(
        channel=channel,
        identity=ChannelIdentity(channel=channel, identity_id="u1"),
        text=text,
        session_key=f"{channel}:t1",
    )
    channel_rig.ollama.script_reply("sure")

    run_channel_turn(channel_rig.engine, message, web_ui_url="http://x")

    hint = skill_hint(channel_rig.store, text)
    assert hint  # a seed matches this text
    (chat,) = channel_rig.store.list_chats()
    (user,) = [m for m in channel_rig.store.chat_messages(chat.chat_id) if m.role == "user"]
    assert user.skill_hint == hint
    assert user.content == f"{text}\n\n{hint}"  # the web route's exact shape


def test_a_channel_turn_with_no_matching_skill_stores_no_hint(channel_rig: ChannelRig) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    message = ChannelMessage(
        channel="telegram",
        identity=ChannelIdentity(channel="telegram", identity_id="u1"),
        text="hi",
        session_key="telegram:t2",
    )
    channel_rig.ollama.script_reply("hello")

    run_channel_turn(channel_rig.engine, message, web_ui_url="http://x")

    (chat,) = channel_rig.store.list_chats()
    (user,) = [m for m in channel_rig.store.chat_messages(chat.chat_id) if m.role == "user"]
    assert user.skill_hint is None and user.content == "hi"


# -- F5: feeds in the morning-ritual script -------------------------------------

HN_JSON = (
    '{"hits": ['
    '{"title": "Rust 2.0 ships", "url": "https://example.com/rust", "points": 512,'
    ' "objectID": "1"},'
    '{"title": "Ask HN: what do you self-host?", "url": null, "points": 90, "objectID": "2"}'
    "]}"
)
RSS_XML = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>'
    "<item><title>Python 3.15 released</title><link>https://blog.example/py</link></item>"
    "<item><title>  Gardening\n tips </title><link>https://blog.example/garden</link></item>"
    "</channel></rss>"
)
ATOM_XML = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>'
    '<entry><title>Kernel 7.4 notes</title><link rel="replies" href="https://a.example/c"/>'
    '<link href="https://a.example/kernel"/></entry>'
    "</feed>"
)


def _brief_module() -> object:
    from .test_example_skills import _fetch_brief

    return _fetch_brief()


class _Body:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, limit: int = -1) -> bytes:
        chunk, self.body = (self.body, b"") if limit < 0 else (self.body[:limit], self.body[limit:])
        return chunk


def _fake_web(monkeypatch: pytest.MonkeyPatch, module: object, pages: dict[str, str]) -> list[str]:
    import urllib.error

    asked: list[str] = []

    def urlopen(url: str, timeout: float) -> _Body:
        asked.append(url)
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return _Body(body.encode())
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(module._OPENER, "open", urlopen)  # type: ignore[attr-defined]
    return asked


def test_fetch_brief_reads_hn_rss_and_atom(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    asked = _fake_web(
        monkeypatch,
        module,
        {
            "https://hn.algolia.com/": HN_JSON,
            "https://blog.example/rss": RSS_XML,
            "https://a.example/atom": ATOM_XML,
        },
    )

    code = module.main(  # type: ignore[attr-defined]
        ["--hn", "5", "--rss", "https://blog.example/rss", "--rss", "https://a.example/atom"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "- Rust 2.0 ships (512 points) — https://example.com/rust" in out
    assert "— https://news.ycombinator.com/item?id=2" in out  # an Ask HN has no url
    assert "- Python 3.15 released — https://blog.example/py" in out
    assert "- Gardening tips — https://blog.example/garden" in out  # whitespace folded
    assert "- Kernel 7.4 notes — https://a.example/kernel" in out  # the alternate link
    assert "## Feed blog.example" in out and "## Feed a.example" in out
    assert asked[0].startswith("https://hn.algolia.com/api/v1/search?tags=front_page")
    assert "hitsPerPage=5" in asked[0]


def test_fetch_brief_topics_keep_only_matching_titles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://hn.algolia.com/": HN_JSON, "https://b/": RSS_XML})

    code = module.main(  # type: ignore[attr-defined]
        ["--hn", "5", "--rss", "https://b/rss", "--topic", "RUST", "--topic", "python"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Rust 2.0 ships" in out and "Python 3.15 released" in out
    assert "self-host" not in out and "Gardening" not in out


def test_fetch_brief_survives_one_dead_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://blog.example/rss": RSS_XML})

    code = module.main(  # type: ignore[attr-defined]
        ["--hn", "3", "--rss", "https://blog.example/rss", "--rss", "https://dead.example/x"]
    )

    out = capsys.readouterr().out
    assert code == 1  # schedule health shows the failure
    assert "## Hacker News\n(unavailable: URLError:" in out
    assert "## Feed dead.example\n(unavailable: URLError:" in out
    assert "- Python 3.15 released — https://blog.example/py" in out  # the rest arrive
    for line in out.splitlines():
        if line.startswith("(unavailable"):
            assert line.endswith(")")  # one line per dead source


def test_fetch_brief_stays_inside_the_script_cap_with_huge_feeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.scheduler import SCRIPT_OUTPUT_CAP

    module = _brief_module()
    long_items = "".join(
        f"<item><title>{'x' * 900} {n}</title><link>https://f.example/{n}</link></item>"
        for n in range(40)
    )
    feed = f"<rss><channel>{long_items}</channel></rss>"
    _fake_web(monkeypatch, module, {"https://f.example/": feed, "https://g.example/": feed})
    digest = "---\nFrom: a@example.com\nSubject: hi\nDate: today\n> body"
    from skep.mcp_servers import mail

    monkeypatch.setattr(mail, "digest", lambda count: digest)

    code = module.main(  # type: ignore[attr-defined]
        ["--rss", "https://f.example/a", "--rss", "https://g.example/b", "--mail", "3"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert module.OUTPUT_CAP == SCRIPT_OUTPUT_CAP  # type: ignore[attr-defined]
    assert len(out) <= SCRIPT_OUTPUT_CAP
    assert out.index("## Feed f.example") < out.index("## Feed g.example") < out.index("## Mail")
    assert out.rstrip().endswith(digest)  # the short mail section is never squeezed out
    assert out.count("https://f.example/") <= module.ITEM_CAP  # type: ignore[attr-defined]
    assert "…" in out  # a clipped section says so


def test_fetch_brief_caps_the_feed_read_and_refuses_non_web_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _brief_module()
    huge = "<rss>" + " " * (module.FETCH_CAP_BYTES + 10) + "</rss>"  # type: ignore[attr-defined]
    _fake_web(monkeypatch, module, {"https://big.example/": huge})
    with pytest.raises(ValueError, match="KiB"):
        module.feed("https://big.example/rss", ())  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="http"):
        module.feed("file:///etc/passwd", ())  # type: ignore[attr-defined]


def _long_feed(host: str, items: int = 40) -> str:
    body = "".join(
        f"<item><title>{'x' * 900} {n}</title><link>https://{host}/{n}</link></item>"
        for n in range(items)
    )
    return f"<rss><channel>{body}</channel></rss>"


def test_fetch_brief_a_clipped_final_block_fits_the_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://f.example/": _long_feed("f.example")})
    assert module.main(["--rss", "https://f.example/a"]) == 0  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert len(out) <= module.OUTPUT_CAP  # type: ignore[attr-defined]
    assert out.rstrip("\n").endswith(" …")


@pytest.mark.parametrize("feeds", [3, 60, 400])
def test_fetch_brief_never_passes_the_cap_however_many_feeds(
    feeds: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://": _long_feed("f.example")})
    argv = [arg for n in range(feeds) for arg in ("--rss", f"https://h{n}.example/rss")]
    assert module.main(argv) == 0  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert len(out) <= module.OUTPUT_CAP  # type: ignore[attr-defined]
    if feeds == 400:
        assert out.rstrip("\n").splitlines()[-1] == module.CAP_NOTE  # type: ignore[attr-defined]


def test_fetch_brief_a_malformed_feed_url_fails_only_its_own_section(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://blog.example/rss": RSS_XML})
    code = module.main(  # type: ignore[attr-defined]
        ["--rss", "https://[bad/rss", "--rss", "https://blog.example/rss"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "## Feed (malformed URL)\n(unavailable: ValueError:" in out
    assert "- Python 3.15 released — https://blog.example/py" in out


def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)


class _Trickle(http.server.BaseHTTPRequestHandler):
    """Loopback only: /rss answers at once; /headers never finishes its
    header block and /body never finishes a chunk-size line, one byte at a
    time, each well inside the socket timeout."""

    def do_GET(self) -> None:
        if self.path == "/rss":
            body = RSS_XML.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        head = b"HTTP/1.1 200 OK\r\n"
        if self.path == "/body":
            head += b"Transfer-Encoding: chunked\r\n\r\n"
        try:
            self.wfile.write(head)
            for _ in range(200):  # gives up after ~10 s
                self.wfile.write(b"X" if self.path == "/headers" else b"0")
                time.sleep(0.05)
        except OSError:
            return  # the reader hung up

    def log_message(self, *args: object) -> None:
        return None


@pytest.mark.parametrize("slow", ["/headers", "/body"])
def test_fetch_brief_a_trickling_source_costs_only_its_share(
    slow: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_proxy(monkeypatch)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        module = _brief_module()
        monkeypatch.setattr(module, "TIME_BUDGET", 1.0)
        began = time.monotonic()
        code = module.main(["--rss", base + slow, "--rss", base + "/rss"])  # type: ignore[attr-defined]
        spent = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert code == 1
    assert "(unavailable: TimeShareSpent: the source did not finish in its time share" in out
    assert "- Python 3.15 released" in out  # time was left for the next source
    assert spent < 2.5  # the first source's half of 1 s, not the trickle's 10 s


def _slow_imap(listener: socket.socket, logout_delay: float) -> None:
    """Loopback only: greets, answers CAPABILITY and LOGIN, never answers
    EXAMINE, and answers LOGOUT only after ``logout_delay``."""
    conn, _ = listener.accept()
    with conn, conn.makefile("rb") as lines:
        conn.sendall(b"* OK ready\r\n")
        for line in lines:
            tag, command = line.split()[:2]
            if command == b"CAPABILITY":
                conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
            elif command == b"LOGIN":
                conn.sendall(tag + b" OK logged in\r\n")
            elif command == b"LOGOUT":
                time.sleep(logout_delay)
                conn.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
                return


def test_fetch_brief_a_mail_source_that_times_out_closes_without_logout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review r3 (C): the alarm fires inside an IMAP command; the one-shot
    alarm is spent, so a LOGOUT round trip would wait on the server."""
    import imaplib

    from skep.mcp_servers import mail

    logout_exit = imaplib.IMAP4.__exit__
    listener = socket.create_server(("127.0.0.1", 0))
    server = threading.Thread(target=_slow_imap, args=(listener, 2.0), daemon=True)
    server.start()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, **_k: imaplib.IMAP4(host, port))
    monkeypatch.setattr(mail, "resolve_password", lambda: "pw")
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("SKEP_MAIL_IMAP_PORT", str(listener.getsockname()[1]))
    monkeypatch.setenv("SKEP_MAIL_USER", "me")
    try:
        module = _brief_module()
        monkeypatch.setattr(module, "TIME_BUDGET", 0.3)
        began = time.monotonic()
        code = module.main(["--mail", "3"])  # type: ignore[attr-defined]
        spent = time.monotonic() - began
    finally:
        listener.close()
    out = capsys.readouterr().out
    assert code == 1
    assert "## Mail\n(unavailable: TimeShareSpent" in out
    assert spent < 1.5  # its 0.3 s share, not the 2 s LOGOUT reply
    assert imaplib.IMAP4.__exit__ is logout_exit  # put back for every other caller


def _imap_one_message(listener: socket.socket, logout_delay: float) -> None:
    """Loopback only: a mailbox with one message; LOGOUT answers late."""
    body = b"From: a@example.org\r\nSubject: hi\r\n\r\nhello\r\n"
    conn, _ = listener.accept()
    with conn, conn.makefile("rb") as lines:
        conn.sendall(b"* OK ready\r\n")
        for line in lines:
            words = line.split()
            tag, command = words[0], words[1]
            if command == b"CAPABILITY":
                conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
            elif command == b"LOGOUT":
                time.sleep(logout_delay)
                conn.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
                return
            elif command == b"EXAMINE":
                conn.sendall(b"* 1 EXISTS\r\n" + tag + b" OK [READ-ONLY] done\r\n")
            elif command == b"UID" and words[2] == b"SEARCH":
                conn.sendall(b"* SEARCH 1\r\n" + tag + b" OK done\r\n")
            elif command == b"UID" and words[2] == b"FETCH":
                head = b"* 1 FETCH (UID 1 BODY[] {%d}\r\n" % len(body)
                conn.sendall(head + body + b")\r\n" + tag + b" OK done\r\n")
            else:
                conn.sendall(tag + b" OK done\r\n")


def test_fetch_brief_a_time_share_spent_while_parsing_mail_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review r4 (C): the alarm can fire while a fetched message is parsed.
    ``_digest_block`` caught it as "one bad message", the digest then left
    its ``with`` block normally, and LOGOUT waited with no timer left."""
    import email
    import imaplib

    from skep.mcp_servers import mail

    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=_imap_one_message, args=(listener, 2.0), daemon=True).start()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, **_k: imaplib.IMAP4(host, port))
    monkeypatch.setattr(mail, "resolve_password", lambda: "pw")
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SKEP_MAIL_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("SKEP_MAIL_IMAP_PORT", str(listener.getsockname()[1]))
    monkeypatch.setenv("SKEP_MAIL_USER", "me")
    module = _brief_module()

    def spent_mid_parse(*_a: object, **_k: object) -> None:
        raise module.TimeShareSpent("the source did not finish in its time share")  # type: ignore[attr-defined]

    monkeypatch.setattr(email, "message_from_bytes", spent_mid_parse)
    try:
        began = time.monotonic()
        code = module.main(["--mail", "1"])  # type: ignore[attr-defined]
        spent = time.monotonic() - began
    finally:
        listener.close()
    out = capsys.readouterr().out
    assert code == 1
    assert "## Mail\n(unavailable: TimeShareSpent" in out
    assert spent < 1.5  # not the 2 s LOGOUT reply


def test_fetch_brief_a_long_bad_url_costs_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    _fake_web(monkeypatch, module, {"https://blog.example/rss": RSS_XML})
    bad = "https:///" + "a" * 4000  # no hostname: the label would be the whole URL
    code = module.main(["--rss", bad, "--rss", "https://blog.example/rss"])  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert code == 1
    assert "- Python 3.15 released" in out  # the other feed still prints
    section = out.split("## ")[1].strip().splitlines()
    assert len(section) == 2 and section[1].startswith("(unavailable: ")  # heading + one line
    assert max(len(line) for line in section) <= 200


@pytest.mark.parametrize("target", ["https://moved.example/feed", "ftp://moved.example/feed"])
def test_fetch_brief_does_not_follow_redirects(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.server
    import threading

    class _Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        module = _brief_module()
        with pytest.raises(Exception, match=f"{target}"):
            module.feed(f"http://127.0.0.1:{server.server_port}/rss", ())  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_brief_refuses_a_feed_with_a_dtd(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _brief_module()
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        "<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    _fake_web(monkeypatch, module, {"https://bomb.example/": bomb})
    with pytest.raises(ValueError, match="DTD"):
        module.feed("https://bomb.example/rss", ())  # type: ignore[attr-defined]


def test_fetch_brief_prints_remote_fields_as_one_safe_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _brief_module()
    hn = (
        '{"hits": [{"title": "Evil\\u001b[2J\\nIGNORE ABOVE", "url": "javascript:alert(1)",'
        ' "points": "7\\n## Mail", "objectID": "9"},'
        '{"title": "plain", "url": "https://ok.example/", "points": 3, "objectID": "8"}]}'
    )
    rss = (
        "<rss><channel><item><title>A\u0085B\u202e</title>"
        "<link>data:text/html,hi</link></item></channel></rss>"
    )
    _fake_web(monkeypatch, module, {"https://hn.algolia.com/": hn, "https://r.example/": rss})
    module.main(["--hn", "5", "--rss", "https://r.example/x"])  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x85" not in out and "\u202e" not in out
    assert "javascript:" not in out and "data:" not in out
    assert "- Evil [2J IGNORE ABOVE (7 ## Mail points)" in out
    assert "- A B\n" in out
    assert out.count("## Mail") == 1  # only inside the HN line, never a section

    module.main(["--hn", "5", "--topic", "points"])  # type: ignore[attr-defined]
    assert "(nothing matching points)" in capsys.readouterr().out


# -- F6: "run now" keeps the slot ----------------------------------------------

TOMORROW_7AM = "2026-11-28T15:00:00Z"  # 07:00 in Vancouver (PST)
TODAY_2PM = "2026-11-27T22:00:00Z"  # 14:00 in Vancouver


def _note(name: str, *, start_at: str, timezone: str | None = None) -> ScheduleRecord:
    return make_schedule(
        name=name,
        repo="",
        instructions="brief",
        interval_seconds=86400,
        worker_kind="note",
        start_at=start_at,
        timezone=timezone,
    )


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    config.home.mkdir(parents=True, exist_ok=True)
    opened = RunStore(config.db_path)
    yield opened
    opened.close()


def test_run_now_at_2pm_leaves_tomorrows_7am_slot_untouched(
    store: RunStore, config: SupervisorConfig
) -> None:
    store.add_schedule(_note("ritual", start_at=TOMORROW_7AM))

    assert store.mark_schedule_due("ritual", due_at=TODAY_2PM)
    marked = store.get_schedule("ritual")
    assert marked is not None
    assert marked.next_run_at == TODAY_2PM and marked.resume_slot == TOMORROW_7AM

    # the ticker is still the one dispatcher: run_due fires it, nothing else did
    results = run_due(store=store, config=config, now="2026-11-27T22:00:30Z")

    assert [(r.name, r.state) for r in results] == [("ritual", "note_posted")]
    ran = store.get_schedule("ritual")
    assert ran is not None
    assert ran.last_run_at == "2026-11-27T22:00:30Z"
    assert ran.next_run_at == TOMORROW_7AM  # not 14:00 tomorrow
    assert ran.resume_slot is None
    assert run_due(store=store, config=config, now="2026-11-27T22:01:00Z") == []


def test_run_now_twice_before_the_tick_keeps_the_first_saved_slot(store: RunStore) -> None:
    store.add_schedule(_note("ritual", start_at=TOMORROW_7AM))
    store.mark_schedule_due("ritual", due_at=TODAY_2PM)
    store.mark_schedule_due("ritual", due_at="2026-11-27T22:00:05Z")
    marked = store.get_schedule("ritual")
    assert marked is not None and marked.resume_slot == TOMORROW_7AM


def test_a_saved_slot_that_passed_before_the_tick_keeps_its_time_of_day(
    store: RunStore, config: SupervisorConfig
) -> None:
    """The serve was down from 14:00 until 08:00 the next day: the 07:00 slot
    has passed, so v122-F5's rule fires once and keeps 07:00, not 08:00."""
    store.add_schedule(_note("ritual", start_at=TOMORROW_7AM))
    store.mark_schedule_due("ritual", due_at=TODAY_2PM)

    run_due(store=store, config=config, now="2026-11-28T16:00:00Z")

    ran = store.get_schedule("ritual")
    assert ran is not None
    assert ran.next_run_at == "2026-11-29T15:00:00Z" and ran.resume_slot is None


def test_an_overdue_schedule_marked_due_saves_no_slot(
    store: RunStore, config: SupervisorConfig
) -> None:
    """ADR 0070 §3 saves a FUTURE slot only. An overdue one (the serve was
    down at 07:00) has no future slot to keep, so it behaves as before v135."""
    store.add_schedule(_note("ritual", start_at="2026-11-27T15:00:00Z"))  # 07:00 today, missed
    store.mark_schedule_due("ritual", due_at=TODAY_2PM)
    marked = store.get_schedule("ritual")
    assert marked is not None and marked.resume_slot is None

    run_due(store=store, config=config, now="2026-11-27T22:00:30Z")

    ran = store.get_schedule("ritual")
    assert ran is not None and ran.next_run_at == "2026-11-28T22:00:00Z"


def test_run_schedule_now_from_chat_keeps_the_slot(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    from .test_serve_chat_tools import chat_client

    client, chat_id = chat_client(config, ollama)
    seed = RunStore(config.db_path)
    try:
        seed.add_schedule(_note("ritual", start_at="2099-01-01T15:00:00Z"))
    finally:
        seed.close()
    ollama.script_tool_call("run_schedule_now", {"name": "ritual"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "run my ritual now"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("queued")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    (row,) = client.get("/api/schedules").json()["schedules"]
    assert row["resume_slot"] == "2099-01-01T15:00:00Z"
    reader = RunStore(config.db_path)
    try:
        run_due(store=reader, config=config)
        ran = reader.get_schedule("ritual")
    finally:
        reader.close()
    assert ran is not None and ran.next_run_at == "2099-01-01T15:00:00Z"


def _drop_columns(path: Path, columns: tuple[str, ...]) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    for column in columns:
        conn.execute(f"ALTER TABLE schedules DROP COLUMN {column}")
    conn.commit()
    conn.close()


def test_a_database_from_before_v135_migrates(config: SupervisorConfig) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    first = RunStore(config.db_path)
    first.add_schedule(_note("ritual", start_at=TOMORROW_7AM))
    first.close()
    _drop_columns(config.db_path, NEW_SCHEDULE_COLUMNS)

    reopened = RunStore(config.db_path)
    try:
        old = reopened.get_schedule("ritual")
        assert old is not None and old.next_run_at == TOMORROW_7AM
        assert old.resume_slot is None
        assert reopened.mark_schedule_due("ritual", due_at=TODAY_2PM)
        marked = reopened.get_schedule("ritual")
        assert marked is not None and marked.resume_slot == TOMORROW_7AM
    finally:
        reopened.close()


# review: a slot claimed before the side effect is never rewritten after it

TODAY_7AM = "2026-11-27T15:00:00Z"
TICK = "2026-11-27T15:00:00Z"
RUN_NOW = "2026-11-27T15:00:10Z"  # confirmed while the 07:00 run is still going


def _script(name: str = "ritual", *, once: bool = False) -> ScheduleRecord:
    return make_schedule(
        name=name, repo="", instructions="fetch-brief", interval_seconds=DAY,
        worker_kind="script", start_at=TODAY_7AM, once=once,
    )  # fmt: skip


def test_a_run_now_made_while_a_script_runs_fires_once_and_keeps_the_slot(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import scheduler

    store.add_schedule(_script())
    calls: list[str] = []

    def _script_run(command: str, **_kwargs: object) -> tuple[str, bool]:
        calls.append(command)
        if len(calls) == 1:
            assert store.mark_schedule_due("ritual", due_at=RUN_NOW)
        return ("", True)

    monkeypatch.setattr(scheduler, "run_schedule_script", _script_run)
    run_due(store=store, config=config, now=TICK)
    row = store.get_schedule("ritual")
    assert row is not None
    assert (row.next_run_at, row.resume_slot) == (RUN_NOW, TOMORROW_7AM)
    assert row.last_state == "script_ran"

    assert [r.name for r in run_due(store=store, config=config, now="2026-11-27T15:00:20Z")] == [
        "ritual"
    ]
    row = store.get_schedule("ritual")
    assert row is not None and (row.next_run_at, row.resume_slot) == (TOMORROW_7AM, None)
    assert run_due(store=store, config=config, now="2026-11-27T15:00:30Z") == []
    assert len(calls) == 2


def test_a_crash_after_a_run_now_keeps_the_saved_regular_slot(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import scheduler
    from skep.supervisor.scheduler import reconcile_interrupted_scripts

    store.add_schedule(_script())

    class _ServeDied(BaseException):
        pass

    def _dies(command: str, **_kwargs: object) -> tuple[str, bool]:
        assert store.mark_schedule_due("ritual", due_at=RUN_NOW)
        raise _ServeDied

    monkeypatch.setattr(scheduler, "run_schedule_script", _dies)
    with pytest.raises(_ServeDied):
        run_due(store=store, config=config, now=TICK)
    assert reconcile_interrupted_scripts(store) == ["ritual"]
    row = store.get_schedule("ritual")
    assert row is not None and (row.next_run_at, row.resume_slot) == (RUN_NOW, TOMORROW_7AM)

    monkeypatch.setattr(scheduler, "run_schedule_script", lambda *_a, **_k: ("", True))
    run_due(store=store, config=config, now="2026-11-27T15:00:20Z")
    row = store.get_schedule("ritual")
    assert row is not None and row.next_run_at == TOMORROW_7AM  # not 07:00:10 tomorrow


def test_an_interrupted_one_shot_script_does_not_fire_again(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import scheduler
    from skep.supervisor.scheduler import reconcile_interrupted_scripts

    store.add_schedule(_script(once=True))

    class _ServeDied(BaseException):
        pass

    def _dies(command: str, **_kwargs: object) -> tuple[str, bool]:
        raise _ServeDied

    monkeypatch.setattr(scheduler, "run_schedule_script", _dies)
    with pytest.raises(_ServeDied):
        run_due(store=store, config=config, now=TICK)
    reconcile_interrupted_scripts(store)
    row = store.get_schedule("ritual")
    assert row is not None and row.enabled is False
    assert run_due(store=store, config=config, now="2026-11-29T15:00:00Z") == []


def test_a_run_now_made_during_a_long_tick_survives_in_every_lane(
    store: RunStore, config: SupervisorConfig
) -> None:
    """The worker/prompt lanes write their bookkeeping after the side effect
    too: the slot is claimed first there as well, so a run-now made meanwhile
    is not erased by the tick's final write."""
    store.add_schedule(
        make_schedule(
            name="ritual", repo="", instructions="brief me", interval_seconds=DAY,
            worker_kind="prompt", start_at=TODAY_7AM,
        )
    )  # fmt: skip

    def _turn(schedule: object, chained: str | None) -> tuple[str, bool]:
        assert store.mark_schedule_due("ritual", due_at=RUN_NOW)
        return ("ok", True)

    run_due(store=store, config=config, now=TICK, prompt_turn=_turn)
    row = store.get_schedule("ritual")
    assert row is not None
    assert (row.next_run_at, row.resume_slot) == (RUN_NOW, TOMORROW_7AM)


# review r2 (A): the claim is conditional on the slot the tick read

OVERDUE_TICK = "2026-11-27T15:05:00Z"  # the 15:00 slot was missed by five minutes


def _race(store: RunStore, monkeypatch: pytest.MonkeyPatch, change: object) -> None:
    """``change`` lands after the tick's due-list read, before its claim."""
    read = store.due_schedules

    def due(now: str) -> list[ScheduleRecord]:
        stale = read(now)
        change()  # type: ignore[operator]
        return stale

    monkeypatch.setattr(store, "due_schedules", due)


def _posts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from skep.supervisor import scheduler

    posted: list[str] = []

    def deliver(_store: object, schedule: ScheduleRecord, *_a: object, **_k: object) -> None:
        posted.append(schedule.name)

    monkeypatch.setattr(scheduler, "_deliver_tick_text", deliver)
    return posted


def test_a_run_now_confirmed_after_the_read_is_not_overwritten_by_the_claim(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_schedule(_note("ritual", start_at=TODAY_7AM))  # fixed interval, overdue
    posted = _posts(monkeypatch)
    _race(store, monkeypatch, lambda: store.mark_schedule_due("ritual", due_at=OVERDUE_TICK))

    run_due(store=store, config=config, now=OVERDUE_TICK)

    row = store.get_schedule("ritual")
    assert row is not None
    assert (row.next_run_at, row.resume_slot) == ("2026-11-28T15:05:00Z", None)  # not 15:00
    assert posted == ["ritual"]  # one dispatch for the one claim


def test_a_run_now_after_the_read_keeps_the_saved_future_slot(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_schedule(_note("ritual", start_at=TOMORROW_7AM))
    store.mark_schedule_due("ritual", due_at=TODAY_2PM)
    posted = _posts(monkeypatch)
    _race(
        store, monkeypatch, lambda: store.mark_schedule_due("ritual", due_at="2026-11-27T22:00:20Z")
    )

    run_due(store=store, config=config, now="2026-11-27T22:00:30Z")

    row = store.get_schedule("ritual")
    assert row is not None and (row.next_run_at, row.resume_slot) == (TOMORROW_7AM, None)
    assert posted == ["ritual"]


def test_a_slot_moved_to_the_future_after_the_read_is_not_dispatched(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another tick claimed it first (or the operator moved it): nothing runs."""
    store.add_schedule(_note("ritual", start_at=TODAY_7AM))
    posted = _posts(monkeypatch)
    _race(store, monkeypatch, lambda: store.mark_schedule_due("ritual", due_at=TOMORROW_7AM))

    assert run_due(store=store, config=config, now=OVERDUE_TICK) == []

    row = store.get_schedule("ritual")
    assert row is not None and row.next_run_at == TOMORROW_7AM and row.last_run_at is None
    assert posted == []


# review r2 (N1): the claim sits right before the side effect


def _worker(tmp: Path) -> ScheduleRecord:
    return make_schedule(
        name="nightly", repo=tmp / "repo", instructions="x", interval_seconds=DAY,
        worker_kind="coding", start_at=TODAY_7AM,
    )  # fmt: skip


def test_a_crash_after_preflight_before_the_side_effect_leaves_the_schedule_due(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from skep.supervisor import scheduler

    class _ServeDied(BaseException):
        pass

    def _dies(*_a: object, **_k: object) -> None:
        raise _ServeDied

    store.add_schedule(_worker(tmp_path))
    monkeypatch.setattr(scheduler, "_uses_worker_provider", lambda *_a: False)
    monkeypatch.setattr(scheduler, "_external_gates", _dies)  # the last check before dispatch
    with pytest.raises(_ServeDied):
        run_due(store=store, config=config, now=TICK)

    row = store.get_schedule("nightly")
    assert row is not None and row.next_run_at == TODAY_7AM  # still due: nothing ran
    assert [r.name for r in store.due_schedules(TICK)] == ["nightly"]


@pytest.mark.parametrize("exit_", ["provider_blocked", "prompt_failed", "dispatch_error", "gate"])
def test_each_deliberate_no_dispatch_exit_advances_the_slot_once(
    exit_: str,
    store: RunStore,
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import dataclasses

    from skep import status
    from skep.supervisor import scheduler

    schedule = _worker(tmp_path)
    if exit_ == "provider_blocked":
        monkeypatch.setattr(scheduler, "_uses_worker_provider", lambda *_a: True)
        monkeypatch.setattr(
            status, "worker_provider_check", lambda *_a: {"kind": "auth", "detail": "401"}
        )
    else:
        monkeypatch.setattr(scheduler, "_uses_worker_provider", lambda *_a: False)
    if exit_ == "prompt_failed":
        schedule = dataclasses.replace(schedule, worker_kind="prompt", repo="")
    elif exit_ == "dispatch_error":
        schedule = dataclasses.replace(schedule, template_name="gone")
    elif exit_ == "gate":
        monkeypatch.setattr(
            scheduler, "_external_gates", lambda *_a, **_k: ("policy_blocked: no", None)
        )
    store.add_schedule(schedule)
    claim = store.claim_schedule_slot
    claims: list[object] = []

    def counted(*args: object, **kwargs: object) -> object:
        claims.append(args)
        return claim(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "claim_schedule_slot", counted)
    (result,) = run_due(store=store, config=config, now=TICK, dispatch=_never)

    assert result.state.startswith(exit_ if exit_ != "gate" else "policy_blocked")
    row = store.get_schedule("nightly")
    assert row is not None and row.next_run_at == TOMORROW_7AM and row.last_state == result.state
    assert len(claims) == 1


def _never(*_a: object, **_k: object) -> NoReturn:
    raise AssertionError("a no-dispatch exit dispatched")


# review r3: a claim needs an enabled schedule and the definition the tick read


def _texts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from skep.supervisor import scheduler

    posted: list[str] = []

    def deliver(_store: object, _schedule: object, text: str, *_a: object, **_k: object) -> None:
        posted.append(text)

    monkeypatch.setattr(scheduler, "_deliver_tick_text", deliver)
    return posted


def test_a_schedule_disabled_after_the_read_does_not_fire(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_schedule(_note("ritual", start_at=TODAY_7AM))
    posted = _texts(monkeypatch)
    _race(store, monkeypatch, lambda: store.set_schedule_enabled("ritual", enabled=False))

    assert run_due(store=store, config=config, now=TICK) == []

    row = store.get_schedule("ritual")
    assert row is not None and not row.enabled and row.next_run_at == TODAY_7AM
    health = store.schedule_health("ritual")
    assert row.last_run_at is None and health is not None and health.window_size == 0
    assert posted == []


def test_a_failed_claim_on_the_no_dispatch_path_records_nothing(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot prompt tick with no chat engine runs nothing; moved to a
    later slot meanwhile, it belongs to whoever moved it — not failed, not
    disabled by the stale tick."""
    store.add_schedule(
        make_schedule(
            name="ritual", repo="", instructions="brief me", interval_seconds=DAY,
            worker_kind="prompt", start_at=TODAY_7AM, once=True,
        )
    )  # fmt: skip
    _race(store, monkeypatch, lambda: store.mark_schedule_due("ritual", due_at=TOMORROW_7AM))

    assert run_due(store=store, config=config, now=TICK) == []

    row = store.get_schedule("ritual")
    assert row is not None and row.enabled and row.next_run_at == TOMORROW_7AM
    health = store.schedule_health("ritual")
    assert row.last_state is None and health is not None and health.window_size == 0


def test_a_schedule_replaced_after_the_read_runs_its_new_definition(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    store.add_schedule(_note("ritual", start_at=TODAY_7AM))  # overdue
    posted = _texts(monkeypatch)
    replaced = dataclasses.replace(
        _note("ritual", start_at="2026-11-27T15:04:00Z"), instructions="the new brief"
    )
    _race(store, monkeypatch, lambda: store.add_schedule(replaced))

    assert run_due(store=store, config=config, now=OVERDUE_TICK) == []  # left for the next tick
    assert posted == []
    row = store.get_schedule("ritual")
    assert row is not None and row.next_run_at == "2026-11-27T15:04:00Z"

    monkeypatch.undo()
    posted = _texts(monkeypatch)
    run_due(store=store, config=config, now=OVERDUE_TICK)
    assert posted == ["the new brief"]


def test_a_timezone_edited_after_the_read_leaves_the_slot_for_the_next_tick(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.scheduler import wall_clock

    store.add_schedule(_note("ritual", start_at=TODAY_7AM))
    posted = _texts(monkeypatch)
    _race(store, monkeypatch, lambda: store.set_schedule_timezone("ritual", VAN, wall_clock))

    assert run_due(store=store, config=config, now=TICK) == []
    assert posted == []
    row = store.get_schedule("ritual")
    assert row is not None and row.next_run_at == TODAY_7AM and row.timezone == VAN


# -- F7: wall-clock daily schedules ---------------------------------------------
# America/Vancouver's last transitions: fall back Sun 2 Nov 2025 02:00 PDT ->
# 01:00 PST; spring forward Sun 8 Mar 2026 02:00 PST -> 03:00 PDT. (tzdata
# 2026f keeps British Columbia on daylight time from then on, so later dates
# have no transition; these two are in every tzdata release.)

VAN = "America/Vancouver"
DAY = 86400


def test_a_7am_schedule_keeps_7am_across_spring_forward_and_fall_back() -> None:
    # 07:00 PST (15:00Z) -> 07:00 PDT (14:00Z); a fixed 24 h would say 08:00
    assert next_run_after("2026-03-07T15:00:00Z", DAY, timezone=VAN) == "2026-03-08T14:00:00Z"
    assert next_run_after("2026-03-07T15:00:00Z", DAY) == "2026-03-08T15:00:00Z"
    # 07:00 PDT (14:00Z) -> 07:00 PST (15:00Z)
    assert next_run_after("2025-11-01T14:00:00Z", DAY, timezone=VAN) == "2025-11-02T15:00:00Z"


def test_a_0230_schedule_fires_at_the_first_instant_after_the_gap_then_returns() -> None:
    """02:30 does not exist on 8 Mar: it fires at 03:00 PDT (10:00Z), the
    first valid instant, and the next day is 02:30 again — no drift."""
    gap_day = next_run_after("2026-03-07T10:30:00Z", DAY, timezone=VAN, wall_time="02:30:00")
    assert gap_day == "2026-03-08T10:00:00Z"
    after = next_run_after(gap_day, DAY, timezone=VAN, wall_time="02:30:00")
    assert after == "2026-03-09T09:30:00Z"  # 02:30 PDT


def test_a_0130_schedule_fires_once_on_the_fall_back_day_at_its_first_occurrence() -> None:
    first = next_run_after("2025-11-01T08:30:00Z", DAY, timezone=VAN, wall_time="01:30:00")
    assert first == "2025-11-02T08:30:00Z"  # 01:30 PDT, the first of the two
    # the tick that ran it moves on to the next day, never the 01:30 PST repeat
    after = next_run_after(
        first, DAY, timezone=VAN, wall_time="01:30:00", now="2025-11-02T08:30:05Z"
    )
    assert after == "2025-11-03T09:30:00Z"


def test_missed_wall_clock_slots_fire_once_and_keep_the_wall_time() -> None:
    """v122-F5 in wall-clock mode: down from 4 Mar to 10 Mar 08:00 PDT."""
    late = next_run_after("2026-03-04T15:00:00Z", DAY, timezone=VAN, now="2026-03-10T15:00:00Z")
    assert late == "2026-03-11T14:00:00Z"  # 07:00 PDT, once
    exact = next_run_after("2026-03-04T15:00:00Z", DAY, timezone=VAN, now="2026-03-10T14:00:00Z")
    assert exact == "2026-03-11T14:00:00Z"  # a slot equal to now has passed


def test_a_weekly_wall_clock_schedule_moves_by_calendar_weeks() -> None:
    assert next_run_after("2026-03-02T15:00:00Z", 7 * DAY, timezone=VAN) == "2026-03-09T14:00:00Z"


def test_no_timezone_is_byte_for_byte_the_fixed_interval() -> None:
    old = schedule_time_free_next_run_after
    for reference, interval, now in (
        ("2026-03-07T15:00:00Z", DAY, None),
        ("2026-03-07T10:30:00Z", DAY, "2026-03-14T00:00:00Z"),
        ("2025-11-01T08:30:00Z", 3600, "2025-11-02T08:30:00Z"),
        ("2026-01-01T00:00:00Z", 7 * DAY, "2026-01-01T00:00:00Z"),
    ):
        assert next_run_after(reference, interval, now=now) == old(reference, interval, now=now)
        assert next_run_after(reference, interval, now=now, timezone=None) == old(
            reference, interval, now=now
        )
    plain = _note("plain", start_at=TOMORROW_7AM)
    assert plain.timezone is None and plain.wall_time is None


def schedule_time_free_next_run_after(reference: str, interval: int, *, now: str | None) -> str:
    """next_run_after as it was before v135, verbatim."""
    from datetime import UTC, datetime, timedelta

    def parse(ts: str) -> datetime:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    step = timedelta(seconds=interval)
    slot = parse(reference) + step
    if now is not None:
        late = parse(now) - slot
        if late >= timedelta(0):
            slot += step * (late // step + 1)
    return slot.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_wall_clock_schedule_records_its_wall_time_from_the_first_slot() -> None:
    schedule = _note("brief", start_at="2026-03-07T02:30:00-08:00", timezone=VAN)
    assert schedule.timezone == VAN and schedule.wall_time == "02:30:00"
    assert schedule.next_run_at == "2026-03-07T10:30:00Z"


def test_an_explicit_start_at_in_the_gap_anchors_at_its_actual_local_time() -> None:
    """The documented rule: start_at is an absolute instant; 02:30-08:00 on
    the spring-forward day is 03:30 PDT, and that is the anchor."""
    made = _note("gap", start_at="2026-03-08T02:30:00-08:00", timezone=VAN)
    assert (made.next_run_at, made.wall_time) == ("2026-03-08T10:30:00Z", "03:30:00")


@pytest.mark.parametrize("bad", ["Mars/Olympus", "", "../etc/passwd", "PST8PDT/../x"])
def test_an_invalid_timezone_is_refused_with_a_valid_example(bad: str) -> None:
    with pytest.raises(ValueError, match="America/Vancouver"):
        _note("brief", start_at=TOMORROW_7AM, timezone=bad)


def test_a_timezone_needs_a_whole_day_interval() -> None:
    with pytest.raises(ValueError, match="whole number of days"):
        make_schedule(
            name="x", repo="", instructions="t", interval_seconds=6 * 3600,
            worker_kind="note", timezone=VAN,
        )  # fmt: skip


def test_the_tick_keeps_a_0230_schedule_on_its_wall_time_across_the_gap(
    store: RunStore, config: SupervisorConfig
) -> None:
    store.add_schedule(_note("night", start_at="2026-03-07T02:30:00-08:00", timezone=VAN))
    slots = []
    moment = "2026-03-07T10:30:00Z"
    for _ in range(3):
        run_due(store=store, config=config, now=moment)
        ran = store.get_schedule("night")
        assert ran is not None
        slots.append(moment := ran.next_run_at)
    assert slots == ["2026-03-08T10:00:00Z", "2026-03-09T09:30:00Z", "2026-03-10T09:30:00Z"]


def test_run_now_on_a_wall_clock_schedule_keeps_its_slot(
    store: RunStore, config: SupervisorConfig
) -> None:
    store.add_schedule(_note("ritual", start_at="2026-03-08T07:00:00-07:00", timezone=VAN))
    store.mark_schedule_due("ritual", due_at="2026-03-07T22:00:00Z")
    run_due(store=store, config=config, now="2026-03-07T22:00:30Z")
    ran = store.get_schedule("ritual")
    assert ran is not None and ran.next_run_at == "2026-03-08T14:00:00Z"
    run_due(store=store, config=config, now="2026-03-08T14:00:00Z")
    ran = store.get_schedule("ritual")
    assert ran is not None and ran.next_run_at == "2026-03-09T14:00:00Z"


# faces (ADR 0050): propose_schedule, POST/PATCH /api/schedules, skep schedule add


def test_propose_schedule_takes_a_timezone(config: SupervisorConfig, ollama: FakeOllama) -> None:
    from skep.supervisor.serve.tools import TOOL_SPECS

    from .test_serve_chat_tools import chat_client

    (spec,) = [s for s in TOOL_SPECS if s["function"]["name"] == "propose_schedule"]
    described = spec["function"]["parameters"]["properties"]["timezone"]["description"]
    assert "America/Vancouver" in described and "omit" in described.lower()

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {
            "name": "brief", "caste": "note", "every": "1d", "instructions": "good morning",
            "start_at": "2027-03-13T07:00:00+01:00", "timezone": "Europe/Berlin",
        },
    )  # fmt: skip
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "every morning at 7"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("done")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    (row,) = client.get("/api/schedules").json()["schedules"]
    assert row["timezone"] == "Europe/Berlin" and row["wall_time"] == "07:00:00"


def test_rest_create_and_patch_take_a_timezone(config: SupervisorConfig) -> None:
    client = serve_client(config)
    body = {"name": "brief", "caste": "note", "every": "1d", "instructions": "hi"}
    created = client.post("/api/schedules", json={**body, "timezone": VAN})
    assert created.status_code == 201, created.text
    assert created.json()["timezone"] == VAN

    refused = client.post("/api/schedules", json={**body, "name": "b2", "timezone": "Nope/No"})
    assert refused.status_code == 400 and "America/Vancouver" in refused.json()["detail"]

    plain = client.post("/api/schedules", json={**body, "name": "plain"}).json()
    assert plain["timezone"] is None
    patched = client.patch("/api/schedules/plain", json={"timezone": "Europe/Berlin"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["timezone"] == "Europe/Berlin"
    assert patched.json()["enabled"] is True  # untouched
    cleared = client.patch("/api/schedules/plain", json={"timezone": None}).json()
    assert cleared["timezone"] is None and cleared["wall_time"] is None
    bad = client.patch("/api/schedules/plain", json={"timezone": "Nope/No"})
    assert bad.status_code == 400 and "America/Vancouver" in bad.json()["detail"]
    toggled = client.patch("/api/schedules/plain", json={"enabled": False}).json()
    assert toggled["enabled"] is False and toggled["timezone"] is None
    assert client.patch("/api/schedules/plain", json={}).status_code == 400
    assert client.patch("/api/schedules/ghost", json={"timezone": VAN}).status_code == 404


def test_patch_with_the_same_timezone_keeps_the_wall_time_anchor(config: SupervisorConfig) -> None:
    """A 02:30 schedule waiting at its spring-forward fallback (03:00 PDT):
    re-sending its own timezone must not re-anchor it at 03:00."""
    client = serve_client(config)
    body = {"name": "night", "caste": "note", "every": "1d", "instructions": "hi"}
    made = client.post(
        "/api/schedules", json={**body, "start_at": "2026-03-07T02:30:00-08:00", "timezone": VAN}
    )
    assert made.status_code == 201, made.text
    store = RunStore(config.db_path)
    try:
        run_due(store=store, config=config, now="2026-03-07T10:30:00Z")
        waiting = store.get_schedule("night")
    finally:
        store.close()
    assert waiting is not None and waiting.next_run_at == "2026-03-08T10:00:00Z"  # 03:00 PDT

    patched = client.patch("/api/schedules/night", json={"timezone": VAN})
    assert patched.status_code == 200, patched.text
    assert patched.json()["wall_time"] == "02:30:00"


def test_patch_validates_the_timezone_against_the_row_it_writes(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name replacement between the route's read and its write gets
    no timezone checked against the old row's interval."""
    client = serve_client(config)
    body = {"name": "brief", "caste": "note", "every": "1d", "instructions": "hi"}
    assert client.post("/api/schedules", json=body).status_code == 201
    real_get = RunStore.get_schedule

    def get_then_replace(self: RunStore, name: str) -> ScheduleRecord | None:
        seen = real_get(self, name)
        monkeypatch.setattr(RunStore, "get_schedule", real_get)
        hourly = make_schedule(
            name=name, repo="", instructions="hi", interval_seconds=3600, worker_kind="note"
        )
        self.add_schedule(hourly)
        return seen

    monkeypatch.setattr(RunStore, "get_schedule", get_then_replace)
    refused = client.patch("/api/schedules/brief", json={"timezone": VAN})
    assert refused.status_code == 400 and "whole number of days" in refused.json()["detail"]
    store = RunStore(config.db_path)
    try:
        row = store.get_schedule("brief")
    finally:
        store.close()
    assert row is not None and row.interval_seconds == 3600 and row.timezone is None


def test_cli_schedule_add_takes_a_timezone(
    config: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.cli import main

    home = ["--home", str(config.home)]
    code = main(
        [*home, "schedule", "add", "brief", "good morning", "--caste", "note", "--every", "1d",
         "--start-at", "2027-03-13T07:00:00+01:00", "--timezone", "Europe/Berlin"]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "07:00:00 Europe/Berlin" in out
    assert main([*home, "schedule", "list"]) == 0
    assert "Europe/Berlin" in capsys.readouterr().out

    code = main(
        [*home, "schedule", "add", "bad", "x", "--caste", "note", "--every", "1d",
         "--timezone", "Nope/No"]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code != 0 and "America/Vancouver" in captured.out + captured.err


def test_the_card_preview_shows_wall_clock_fire_times() -> None:
    """The confirm card's next three fire times (cards.schedule_facts)
    follow the same rule. Berlin springs forward on 28 Mar 2027."""
    from skep.supervisor.scheduler import next_fire_times

    times = next_fire_times("2027-03-27T07:00:00+01:00", DAY, timezone="Europe/Berlin")
    assert times == ["2027-03-27T06:00:00Z", "2027-03-28T05:00:00Z", "2027-03-29T05:00:00Z"]
    assert next_fire_times("2027-03-27T07:00:00+01:00", DAY)[1] == "2027-03-28T06:00:00Z"
    with pytest.raises(ValueError, match="America/Vancouver"):
        next_fire_times(None, DAY, timezone="Nope/No")


def test_a_proposed_schedule_card_with_a_bad_timezone_says_why() -> None:
    from skep.supervisor.serve.cards import schedule_facts

    facts = schedule_facts(
        {"name": "b", "caste": "script", "every": "1d", "instructions": "x", "timezone": "No/Pe"}
    )
    assert facts is not None and "America/Vancouver" in facts["error"]
