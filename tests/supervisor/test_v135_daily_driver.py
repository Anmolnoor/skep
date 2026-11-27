"""v135 Wave 3 (ADR 0070 Part 1): a morning ritual that works and keeps its slot.

F4: a Telegram or Discord turn stores the same ``skill_hint`` the web route
stores (Phase 06 known limit). F5: the walled ``fetch_brief.py`` step reads
tech news (HN, RSS, Atom) inside its 4,000-character cap. F6: "run now" keeps
tomorrow's slot. F7: a daily schedule with a timezone keeps its wall-clock
time across daylight-saving changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.seed_skills import load_seed_skills
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import serve_client
from .fake_ollama import FakeOllama

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

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _fake_web(monkeypatch: pytest.MonkeyPatch, module: object, pages: dict[str, str]) -> list[str]:
    import urllib.error

    asked: list[str] = []

    def urlopen(url: str, timeout: float) -> _Body:
        asked.append(url)
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return _Body(body.encode())
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)  # type: ignore[attr-defined]
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
