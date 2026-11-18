"""v121-F1 (DD-1): every outbound messenger text turns link previews off.

A reply that carries a URL must never make the messenger fetch it to build a
preview — that fetch is an exfil path nobody confirmed (I5)."""

from __future__ import annotations

from typing import Any

import pytest

from skep.supervisor.serve.channels import runtime, telegram

_POST = "skep.supervisor.serve.channels.runtime.httpx.post"


class _Resp:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"ok": True}


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        calls.append(kwargs.get("json") or {})
        return _Resp()

    monkeypatch.setattr(_POST, fake_post)
    return calls


def test_telegram_send_disables_link_previews(posts: list[dict[str, Any]]) -> None:
    runtime._default_send("tok", "42", "see http://x.test/?q=notes")
    runtime._default_send("tok", "42", "see *x*", None, parse_mode="MarkdownV2")
    assert posts and all(p["link_preview_options"] == {"is_disabled": True} for p in posts)


def test_telegram_markdown_path_sends_both_attempts_without_previews(
    posts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = iter([400, 200])

    class _Flaky(_Resp):
        def __init__(self) -> None:
            self.status_code = next(statuses)

    def flaky_post(url: str, **kwargs: Any) -> _Flaky:
        posts.append(kwargs["json"])
        return _Flaky()

    monkeypatch.setattr(_POST, flaky_post)
    assert runtime.send_telegram_markdown(runtime._default_send, "tok", "42", "http://x.test")
    assert len(posts) == 2
    assert all(p["link_preview_options"] == {"is_disabled": True} for p in posts)


def test_telegram_markdown_v2_escapes_link_syntax() -> None:
    # The converter escapes [ ] ( ), so a model-written [label](url) is never a
    # clickable link entity — only the literal text the operator can read.
    out = telegram.to_markdown_v2("[label](http://x.test/?q=1)")
    assert out == r"\[label\]\(http://x\.test/?q\=1\)"


def test_slack_send_disables_unfurls(posts: list[dict[str, Any]]) -> None:
    runtime._default_slack_send("tok", "C1", runtime._text_blocks("http://x.test"))
    runtime._default_slack_send("tok", "C1", runtime._text_blocks("hi"), thread_ts="1.2")
    assert len(posts) == 2
    assert all(p["unfurl_links"] is False and p["unfurl_media"] is False for p in posts)


def test_discord_text_payloads_suppress_embeds(posts: list[dict[str, Any]]) -> None:
    runtime._default_discord_send("tok", "C1", {"content": "http://x.test"})
    runtime._default_discord_ack("iid", "itok", "http://x.test")
    runtime._default_discord_followup("app", "itok", "http://x.test")
    send, ack, followup = posts
    assert send["flags"] == 4
    assert ack["type"] == 4 and ack["data"]["flags"] == 4
    assert followup["flags"] == 4


def test_discord_payload_with_our_embeds_keeps_them_and_wraps_urls(
    posts: list[dict[str, Any]],
) -> None:
    payload: dict[str, object] = {
        "content": "run done: http://x.test/a and <https://y.test/b>, see https://z.test/c",
        "embeds": [{"title": "run"}],
    }
    runtime._default_discord_send("tok", "C1", payload)
    (sent,) = posts
    assert "flags" not in sent
    assert sent["embeds"] == [{"title": "run"}]
    assert sent["content"] == (
        "run done: <http://x.test/a> and <https://y.test/b>, see <https://z.test/c>"
    )
    # The caller's payload is not mutated.
    assert str(payload["content"]).startswith("run done: http://")


def test_discord_card_without_content_is_sent_as_is(posts: list[dict[str, Any]]) -> None:
    card: dict[str, object] = {"embeds": [{"title": "Confirm x?"}], "components": []}
    runtime._default_discord_send("tok", "C1", card)
    assert posts == [card]
