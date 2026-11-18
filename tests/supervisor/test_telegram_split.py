"""v122-F6: a long Telegram message is split, not lost.

Telegram rejects a text over 4096 characters, and both the MarkdownV2 and the
plain attempt failed the same way, so a long summary vanished."""

from __future__ import annotations

from typing import Any

from skep.supervisor.serve.channels.runtime import send_telegram_markdown

KEYBOARD: dict[str, object] = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}


def _recorder() -> tuple[list[tuple[str, Any, Any]], Any]:
    sent: list[tuple[str, Any, Any]] = []

    def send(
        token: str, chat_id: str, text: str, reply_markup: Any = None, parse_mode: Any = None
    ) -> bool:
        if len(text) > 4096:
            return False  # what Telegram does
        sent.append((text, reply_markup, parse_mode))
        return True

    return sent, send


def test_a_long_message_goes_in_ordered_chunks_with_the_keyboard_last() -> None:
    paragraphs = [f"para {i} " + "x" * 2990 for i in range(3)]
    sent, send = _recorder()
    assert send_telegram_markdown(send, "tok", "7", "\n\n".join(paragraphs), KEYBOARD)
    assert [text for text, _, _ in sent] == paragraphs
    assert [markup for _, markup, _ in sent] == [None, None, KEYBOARD]
    assert all(mode == "MarkdownV2" for _, _, mode in sent)


def test_a_line_longer_than_a_chunk_is_cut_and_nothing_is_lost() -> None:
    text = "y" * 9000
    sent, send = _recorder()
    assert send_telegram_markdown(send, "tok", "7", text)
    assert len(sent) == 3 and "".join(t for t, _, _ in sent) == text


def test_a_chunk_that_escaping_pushes_past_the_limit_resends_plain() -> None:
    dots = "." * 3000  # each "." escapes to "\\." — 6000 chars in MarkdownV2
    sent, send = _recorder()
    assert send_telegram_markdown(send, "tok", "7", dots)
    assert sent == [(dots, None, None)]


def test_one_failed_chunk_reports_failure_and_the_rest_still_go() -> None:
    sent: list[str] = []

    def send(token: str, chat_id: str, text: str, *_a: Any, **_kw: Any) -> bool:
        sent.append(text[:1])
        return not text.startswith("b")  # both attempts of the second chunk fail

    assert not send_telegram_markdown(send, "tok", "7", "a" * 3500 + "b" * 3500 + "c" * 100)
    assert sent == ["a", "b", "b", "c"]


def test_a_trailing_newline_never_sends_an_empty_last_chunk_with_the_keyboard() -> None:
    sent, send = _recorder()
    assert send_telegram_markdown(send, "tok", "7", "y" * 3500 + "\n", KEYBOARD)
    assert len(sent) == 1 and sent[0][0].strip() and sent[0][1] == KEYBOARD
    sent, send = _recorder()
    # a run of spaces with no break in it is hard-cut into a whitespace-only chunk
    assert send_telegram_markdown(
        send, "tok", "7", "a" * 3400 + "\n" + " " * 3600 + "\nb", KEYBOARD
    )
    assert [text.strip()[:1] for text, _, _ in sent] == ["a", "b"]
    assert sent[-1][1] == KEYBOARD


def test_after_a_failed_chunk_the_rest_go_without_the_keyboard() -> None:
    """A card with a gap must not carry live Confirm/Deny."""
    markups: list[Any] = []

    def send(
        token: str, chat_id: str, text: str, reply_markup: Any = None, parse_mode: Any = None
    ) -> bool:
        markups.append(reply_markup)
        return not text.startswith("a")  # the first chunk fails both ways

    assert not send_telegram_markdown(send, "tok", "7", "a" * 3500 + "b" * 100, KEYBOARD)
    assert markups == [None, None, None]  # a fails twice; b goes once, bare
