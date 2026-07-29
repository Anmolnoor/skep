"""Voice as a channel-layer capability (v53-F6, ADR 0031).

TTS renders the Queen's REPLY TEXT to audio after the turn — the model
never sees or produces audio. Providers, honestly labeled:

- ``none``   — the default. Nothing renders, nothing egresses.
- ``piper``  — LOCAL neural TTS (the ``piper`` CLI). Nothing leaves the
  machine.
- ``edge``   — Microsoft's CLOUD service (the ``edge-tts`` package). No
  API key does not mean no network: every rendered reply's text is sent
  to Microsoft. The setting description says so; choosing it is choosing
  that egress.
- ``openai`` — OpenAI's CLOUD TTS API (key required). Same egress truth.

Config-gated, not operator-policy-gated: TTS is channel infrastructure
(like the Discord API calls that deliver the text itself), recorded as a
deliberate boundary in ADR 0031. Providers are optional dependencies —
a missing package is a clean logged skip, never a crash.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger("skep.voice")

TTS_PROVIDER_SETTING = "tts_provider"
TTS_PROVIDERS: tuple[str, ...] = ("none", "piper", "edge", "openai")
# One spoken reply is a message, not a podcast.
TTS_TEXT_CAP = 1_500
AUDIO_CACHE_DIR = "audio-cache"

PROVIDER_EGRESS_NOTES: dict[str, str] = {
    "none": "voice off (default)",
    "piper": "local — nothing leaves this machine",
    "edge": "CLOUD (Microsoft) — every rendered reply's text leaves this machine",
    "openai": "CLOUD (OpenAI) — every rendered reply's text leaves this machine",
}


def audio_cache_dir(home: Path) -> Path:
    return home / AUDIO_CACHE_DIR


def render_tts(home: Path, text: str, *, provider: str) -> Path | None:
    """Render ``text`` to an audio file in the cache, or None (logged skip).

    Failure is always None, never an exception — voice is a delivery
    garnish and must not corrupt chat or scheduler state (the outbound
    push posture).
    """
    text = text.strip()[:TTS_TEXT_CAP]
    if not text or provider in ("", "none"):
        return None
    if provider not in TTS_PROVIDERS:
        logger.warning("unknown tts provider %r", provider)
        return None
    directory = audio_cache_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if provider == "piper":
            return _render_piper(directory, text)
        if provider == "edge":
            return _render_edge(directory, text)
        return _render_openai(directory, text)
    except Exception:
        logger.warning("tts render failed (provider=%s)", provider, exc_info=True)
        return None


def _render_piper(directory: Path, text: str) -> Path | None:
    if shutil.which("piper") is None:
        logger.warning("tts_provider=piper but the piper CLI is not installed")
        return None
    target = directory / f"{uuid.uuid4().hex}.wav"
    proc = subprocess.run(
        ["piper", "--output_file", str(target)],
        input=text,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0 or not target.is_file():
        logger.warning("piper render failed: %s", proc.stderr.strip()[:200])
        return None
    return target


def _render_edge(directory: Path, text: str) -> Path | None:
    try:
        import asyncio

        import edge_tts  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("tts_provider=edge but edge-tts is not installed (extras: voice)")
        return None
    target = directory / f"{uuid.uuid4().hex}.mp3"
    # Cloud egress happens HERE: the text goes to Microsoft's endpoint.
    asyncio.run(edge_tts.Communicate(text).save(str(target)))
    return target if target.is_file() else None


def _render_openai(directory: Path, text: str) -> Path | None:
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("tts_provider=openai but the openai package is not installed")
        return None
    target = directory / f"{uuid.uuid4().hex}.mp3"
    client = OpenAI()
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text
    ) as response:
        response.stream_to_file(str(target))
    return target if target.is_file() else None
