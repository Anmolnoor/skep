"""The Queen's own model (v6 Stage A): LLM provider config + chat clients.

Chat answers come from here. In v8, the default in-repo coding worker can also
bootstrap from this same assistant config when no explicit worker ``profile.json``
is present. Config (base URL + default model) lives in the settings table like
every other UI-editable knob (A5). The API key is the one deliberate exception
to the G2 "names only" posture: it lives in a 0600 file beside the serve token,
never in SQLite and never in any GET response; the ``SKEP_LLM_API_KEY`` env var
wins when set.

The default client speaks the native Ollama API, which covers both Ollama
Cloud (``https://ollama.com`` + bearer key) and a local daemon
(``http://localhost:11434``, no key). v7 adds an OpenAI-compatible protocol
adapter at this boundary so the rest of chat sees the same message/tool shape.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, get_args
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..store import RunStore

if TYPE_CHECKING:
    from ..providers import ProviderProfile

LLMProtocol = Literal["ollama", "openai-compat", "anthropic", "openai-responses", "bedrock"]
_PROTOCOL_VALUES: tuple[str, ...] = get_args(LLMProtocol)
# v108-F1: the ONE registry->serve protocol bridge. The registry spells
# protocols with underscores (providers.PROVIDER_PROTOCOLS); the wire uses
# this Literal. This map lived in ticker.py with only two entries, so an
# anthropic registry profile could never probe healthy and routing skipped
# it forever. test_protocol_vocabulary pins every derived surface to it.
REGISTRY_PROTOCOLS: dict[str, LLMProtocol] = {
    "ollama": "ollama",
    "openai_compat": "openai-compat",
    "anthropic": "anthropic",
    "openai_responses": "openai-responses",
    "bedrock": "bedrock",
}
LLM_BASE_URL = "llm_base_url"
LLM_DEFAULT_MODEL = "llm_default_model"
LLM_PROTOCOL = "llm_protocol"
# v44-F9: whether the assistant model accepts images. Off by default — with a
# non-vision model the chat degrades honestly to "[image attached: ...]".
LLM_VISION = "llm_vision"
# v56-F1 (ADR 0037): the context window we ASK ollama for. Without it ollama
# silently truncates at its own tiny default (2048/4096) — under skep's ~14k
# token fixed floor (tool specs + system block) the Queen was blind from
# turn one. openai-compat servers manage their own window and ignore this.
LLM_NUM_CTX = "llm_num_ctx"
DEFAULT_NUM_CTX = 16384
_MIN_NUM_CTX = 1024
# v74-F2: the model's real context length, detected at config-save time and
# cached per model. The auto value is capped: window_chars = num_ctx * 4
# drives how much history is REPLAYED every round, so an uncapped 256k
# auto-window quietly multiplies per-turn cost and local-ollama VRAM. The
# operator's explicit LLM_NUM_CTX override goes as high as they like.
MODEL_CTX_PREFIX = "llm_model_ctx:"
AUTO_NUM_CTX_CAP = 65536
# Current Claude models all carry >= 200k; the Messages API has no
# per-model context endpoint, so a static floor is the honest answer.
_ANTHROPIC_CTX_FLOOR = 200_000
# v74-F3: how the chat advertises tools. 'indexed' (default) sends the
# categorized index in the prompt plus full schemas for the core set and
# any described-active tools; 'full' restores the whole registry every
# round — the one-flip escape hatch if a small Queen cannot work the index.
TOOL_DELIVERY_SETTING = "llm_tool_delivery"
TOOL_DELIVERIES = ("indexed", "full")
DEFAULT_LLM_PROTOCOL: LLMProtocol = "ollama"
SECRET_FILE = "llm-secret"
SECRET_ENV = "SKEP_LLM_API_KEY"


class OllamaError(Exception):
    """The upstream LLM API was unreachable, refused us, or spoke garbage.

    ``status`` carries the HTTP status when one was seen (v73-F1) — the chat
    turn loop uses it to tell a rejected request (4xx: shrink and retry once)
    from a transient failure (retry identical) without parsing the message.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_api_key(home: Path) -> str | None:
    env = os.environ.get(SECRET_ENV, "").strip()
    if env:
        return env
    path = home / SECRET_FILE
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def store_api_key(home: Path, value: str) -> None:
    """Persist (or, for an empty value, remove) the key — 0600, like the token."""
    path = home / SECRET_FILE
    if not value:
        path.unlink(missing_ok=True)
        return
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


# v108-F4: per-profile keys. One llm-secret could not serve a multi-provider
# registry — chat, workers, and probes all collapsed onto it, so a second
# provider's credential had nowhere to live. Each profile gets its own 0600
# file beside llm-secret (the ADR 0019 exception extended per profile,
# ADR 0051). Resolution order everywhere: the profile's NAMED env var → its
# own file → the legacy llm-secret (v19-F9 compatibility) — the last only for
# the endpoint it was saved for (v119-F1, legacy_api_key).
_PROVIDER_SECRET_PREFIX = "llm-secret-"


def provider_secret_path(home: Path, provider_id: str) -> Path:
    # provider_id is slug-validated at the registry write path, so it cannot
    # traverse out of home.
    return home / f"{_PROVIDER_SECRET_PREFIX}{provider_id}"


def store_provider_api_key(home: Path, provider_id: str, value: str) -> None:
    """Persist (or, for an empty value, remove) one profile's key — 0600."""
    path = provider_secret_path(home, provider_id)
    if not value:
        path.unlink(missing_ok=True)
        return
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _codex_login_key(base_url: str) -> str:
    """v118-F1 (ADR 0063): the one credential a chatgpt.com profile has — the
    operator's Codex login, read and never refreshed. Never the profile's key
    file, ``llm-secret`` or an env var: an ollama key must never reach
    chatgpt.com. Raises OllamaError naming the fix."""
    from ..codex_login import (
        CHATGPT_HOST,
        QUEEN_LOGIN_NAME,
        QUEEN_REFRESH_HINT,
        queen_access_token,
    )

    # v118-R2: the token leaves only over TLS to the exact host — never
    # http://, a subdomain or a trailing-dot spelling of it.
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != CHATGPT_HOST:
        raise OllamaError(
            f"your {QUEEN_LOGIN_NAME} is only sent to https://{CHATGPT_HOST} — "
            f"set the base URL to https://{CHATGPT_HOST}/backend-api/codex"
        )
    try:
        return queen_access_token()
    except ValueError as exc:
        problem = str(exc)
        if "codex login" not in problem:
            problem += f" — {QUEEN_REFRESH_HINT}"
        raise OllamaError(f"your {QUEEN_LOGIN_NAME} cannot be used: {problem}") from exc


def _login_refused(base_url: str, status: int) -> str:
    """v118-F1: what a 401 from chatgpt.com means and how to fix it (I9)."""
    from ..codex_login import QUEEN_LOGIN_NAME, QUEEN_REFRESH_HINT, uses_codex_login

    if status != 401 or not uses_codex_login(base_url):
        return ""
    return f" — your {QUEEN_LOGIN_NAME} was refused: {QUEEN_REFRESH_HINT}"


def chatgpt_login_view() -> dict[str, Any]:
    """v118-F1: what a key-presence view says for a chatgpt.com profile —
    the login's presence, plan and expiry, never a value (I8)."""
    from ..codex_login import login_status

    status = login_status()
    return {
        "present": status.present,
        "plan": status.plan,
        "expires_at": status.expires_at.isoformat() if status.expires_at else None,
        "problem": status.problem,
    }


def resolve_provider_api_key(
    home: Path, profile: ProviderProfile, store: RunStore | None = None
) -> str | None:
    from ..codex_login import uses_codex_login

    if uses_codex_login(profile.base_url):
        return _codex_login_key(profile.base_url)
    key = _raw_provider_api_key(home, profile, store)
    if key is None:
        return None
    from ..providers import provider_host

    if provider_host(profile.base_url) == "api.githubcopilot.com":
        # v108-F7: a Copilot endpoint takes a short-lived bearer, not the
        # GitHub token itself — exchange (and cache) it transparently.
        from .llm_copilot import resolve_copilot_bearer

        return resolve_copilot_bearer(key)
    return key


def worker_key_profile(store: RunStore, endpoint: str) -> ProviderProfile | None:
    """The registry profile whose credential a worker dialing ``endpoint``
    uses: the active one — or, while the Queen is on the ChatGPT plan (v118-R1),
    the profile registered for that endpoint, since workers kept it. None when
    the endpoint matches neither (a diverged config never borrows a key)."""
    from ..codex_login import uses_codex_login

    want = endpoint.strip().rstrip("/")
    profile = store.active_provider_profile()
    if profile is not None and uses_codex_login(profile.base_url):
        profile = next(
            (
                p
                for p in store.list_provider_profiles()
                if not uses_codex_login(p.base_url) and p.base_url.rstrip("/") == want
            ),
            None,
        )
    if profile is None or profile.base_url.rstrip("/") != want:
        return None
    return profile


def _raw_provider_api_key(
    home: Path, profile: ProviderProfile, store: RunStore | None = None
) -> str | None:
    if profile.api_key_env:
        env = os.environ.get(profile.api_key_env, "").strip()
        if env:
            return env
    path = provider_secret_path(home, profile.provider_id)
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return legacy_api_key(home, profile.base_url, store)


def legacy_key_endpoint(home: Path, store: RunStore | None) -> str:
    """v119-F1: the endpoint llm-secret was saved for — the saved
    LLM_BASE_URL; with none saved, or the Queen on the ChatGPT plan (ADR 0063:
    the saved key is the workers'), the workers' profile.json endpoint. The
    sources migrate_legacy_provider seeds the ``default`` profile from, in
    the same order. ``home`` is the supervisor home."""
    from ..codex_login import uses_codex_login

    saved = store.get_setting(LLM_BASE_URL) if store is not None else None
    if isinstance(saved, str) and saved.strip() and not uses_codex_login(saved):
        return saved.strip().rstrip("/")
    from skep.profile import load_profile, profile_path

    endpoint = ""
    if profile_path(home.parent).is_file():
        with contextlib.suppress(OSError, ValueError):
            endpoint = load_profile(home.parent).provider.endpoint or ""
    return endpoint.strip().rstrip("/")


def legacy_api_key(home: Path, endpoint: str, store: RunStore | None = None) -> str | None:
    """v119-F1: llm-secret / SKEP_LLM_API_KEY, for its own endpoint only. It
    was the fallback for EVERY profile, so the health sweep sent the main key
    to each keyless profile's host every five minutes. The same URL match
    resolve_active_api_key and worker_key_profile use; anywhere else gets no
    key (a local server needs none; a remote one fails, naming the fix)."""
    if endpoint.strip().rstrip("/") != legacy_key_endpoint(home, store):
        return None
    return resolve_api_key(home)


def legacy_key_withheld(home: Path, profile: ProviderProfile, store: RunStore) -> str | None:
    """v119-F1: why a profile is called without a key, naming the fix (I9) —
    None when it has a credential, needs none from skep, or none exists."""
    from ..codex_login import uses_codex_login

    if uses_codex_login(profile.base_url) or resolve_api_key(home) is None:
        return None
    if _raw_provider_api_key(home, profile, store) is not None:
        return None
    owner = legacy_key_endpoint(home, store) or "the saved endpoint"
    return (
        f"provider {profile.provider_id!r} has no key of its own, and the main key "
        f"(llm-secret) is only sent to {owner}, so it is called without one — give it "
        f"its own key: `skep provider set-key {profile.provider_id}`"
    )


def resolve_active_api_key(
    store: RunStore, home: Path, *, base_url: str | None = None
) -> str | None:
    """The key for the ACTIVE registry profile — what chat and the model
    listing authenticate with. ``base_url`` guards divergence: when the
    saved llm_* settings no longer match the active profile (a manual PUT
    /api/llm/config), the profile's credential must not leak onto a
    different endpoint, so the legacy single secret applies instead."""
    from ..codex_login import uses_codex_login

    if base_url is not None and uses_codex_login(base_url):
        return _codex_login_key(base_url)  # v118-F1: whichever profile is active
    active = store.active_provider_profile()
    if active is None:
        return resolve_api_key(home)
    if base_url is not None and active.base_url.rstrip("/") != base_url.strip().rstrip("/"):
        return resolve_api_key(home)
    return resolve_provider_api_key(home, active, store)


def llm_config_view(store: RunStore, home: Path) -> dict[str, Any]:
    base_url = store.get_setting(LLM_BASE_URL)
    # v14: seed the provider registry from legacy config on first use (side
    # effect; the registry is surfaced through the dedicated provider views, so
    # the public /api/llm/config shape stays stable).
    from ..providers import migrate_legacy_provider

    migrate_legacy_provider(store, home)
    num_ctx, num_ctx_source = resolved_num_ctx(store)
    from ..codex_login import uses_codex_login

    # v118-F1: on chatgpt.com no API key is in play — the login is (I8).
    codex = isinstance(base_url, str) and uses_codex_login(base_url)
    return {
        "configured": bool(base_url),
        "base_url": base_url,
        "default_model": store.get_setting(LLM_DEFAULT_MODEL),
        "protocol": _protocol(store.get_setting(LLM_PROTOCOL)),
        "api_key_set": resolve_api_key(home) is not None,
        **({"chatgpt_login": chatgpt_login_view()} if codex else {}),
        "vision": store.get_setting(LLM_VISION) is True,
        "num_ctx": num_ctx,
        # v74-F2: which rule set the window, so the UI and doctor can say (I8).
        "num_ctx_source": num_ctx_source,
        "tool_delivery": tool_delivery(store),
    }


def tool_delivery(store: RunStore) -> str:
    value = store.get_setting(TOOL_DELIVERY_SETTING)
    return str(value) if value in TOOL_DELIVERIES else "indexed"


def _valid_ctx(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= _MIN_NUM_CTX


def resolved_num_ctx(store: RunStore, model: str | None = None) -> tuple[int, str]:
    """(window tokens, source) for ``model`` (default: the configured one).

    v74-F2 resolution order: explicit operator override → detected model
    context capped at the auto-ceiling → DEFAULT_NUM_CTX. The dial wins;
    the auto is conservative. v82-F1: a loopback ollama daemon never
    auto-follows the model — there num_ctx is pre-allocated KV-cache RAM,
    so even a cached detection from an earlier remote config stays inert.
    """
    value = store.get_setting(LLM_NUM_CTX)
    if _valid_ctx(value):
        return int(value), "override"
    name = model or store.get_setting(LLM_DEFAULT_MODEL)
    if isinstance(name, str) and name and not _loopback_ollama(store):
        detected = store.get_setting(MODEL_CTX_PREFIX + name)
        if _valid_ctx(detected):
            return min(int(detected), AUTO_NUM_CTX_CAP), "detected"
    return DEFAULT_NUM_CTX, "default"


def _is_loopback(base_url: Any) -> bool:
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        host = (httpx.URL(base_url).host or "").lower()
    except httpx.InvalidURL:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def _loopback_ollama(store: RunStore) -> bool:
    """v82-F1: on a local daemon the window is the operator's call, never
    auto-matched — requesting a 128k+ num_ctx there allocates that much
    KV cache in RAM and can OOM the machine the Queen runs on."""
    return _protocol(store.get_setting(LLM_PROTOCOL)) == "ollama" and _is_loopback(
        store.get_setting(LLM_BASE_URL)
    )


def chat_num_ctx(store: RunStore, model: str | None = None) -> int:
    """The context window requested from ollama (v56-F1); bounded, defaulted."""
    return resolved_num_ctx(store, model)[0]


def detect_model_ctx(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 5.0,
) -> int | None:
    """The model's real context length, or None — detection never breaks a save.

    ollama reports it via POST /api/show under an architecture-prefixed
    ``model_info`` key (``llama.context_length``, ``qwen3.context_length``, …);
    the ``.context_length`` suffix is the stable part. openai-compat and
    bedrock have no standard endpoint — they fall through to None.
    """
    if protocol == "anthropic":
        return _ANTHROPIC_CTX_FLOOR
    if protocol != "ollama":
        # openai-compat and openai-responses (v108-F5) have no such endpoint.
        return None
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/show",
            headers=_headers(api_key),
            json={"model": model},
            timeout=timeout,
        )
        response.raise_for_status()
        info = response.json().get("model_info") or {}
    except (httpx.HTTPError, json.JSONDecodeError, AttributeError):
        return None
    for key, value in info.items() if isinstance(info, dict) else ():
        if str(key).endswith(".context_length") and _valid_ctx(value):
            return int(value)
    return None


def refresh_model_ctx(store: RunStore, home: Path, model: str) -> None:
    """Detect + cache ``model``'s context length. Runs where the model
    changes (config save, set_assistant_model) — never per turn."""
    base_url = store.get_setting(LLM_BASE_URL)
    if not (isinstance(base_url, str) and base_url and model):
        return
    protocol = _protocol(store.get_setting(LLM_PROTOCOL))
    if protocol == "ollama" and _is_loopback(base_url):
        return  # v82-F1: local RAM — never probed or auto-matched
    from ..codex_login import uses_codex_login

    if uses_codex_login(base_url):
        return  # v118-F1: no context endpoint, and llm-secret never goes there
    detected = detect_model_ctx(
        base_url,
        resolve_api_key(home),
        model=model,
        protocol=protocol,
    )
    if detected is not None:
        store.set_setting(MODEL_CTX_PREFIX + model, detected)


def _headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def openai_style_prefix(base_url: str) -> str:
    """v108-F11: where an OpenAI-style API's path root lives. A bare host
    gets the conventional ``/v1`` appended (``api.openai.com`` →
    ``api.openai.com/v1``); a base that already carries a path IS the prefix,
    verbatim (``openrouter.ai/api/v1``, ``api.z.ai/api/paas/v4``) — the
    OpenAI-SDK convention operators know. Before this rule the client
    hardcoded ``/v1``, so a path-carrying provider was unreachable under
    either spelling."""
    trimmed = base_url.rstrip("/")
    if urlparse(trimmed).path in ("", "/"):
        return trimmed + "/v1"
    return trimmed


def _protocol(value: Any | None) -> LLMProtocol:
    # Unknown values fall back (never raise): legacy settings rows predate the
    # vocabulary and must keep the daemon bootable. New-protocol additions grow
    # the Literal, and this guard follows automatically.
    return value if value in _PROTOCOL_VALUES else DEFAULT_LLM_PROTOCOL


def list_models(
    base_url: str,
    api_key: str | None,
    *,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 10.0,
) -> list[str]:
    if protocol in ("openai-compat", "openai-responses"):
        # v108-F5: the Responses API ships the same GET /v1/models listing.
        return _list_openai_models(base_url, api_key, timeout=timeout)
    if protocol == "anthropic":
        return _list_anthropic_models(base_url, api_key, timeout=timeout)
    if protocol == "bedrock":
        from .llm_bedrock import list_bedrock_models  # local import: llm_bedrock imports us

        return list_bedrock_models(base_url, api_key, timeout=timeout)
    return _list_ollama_models(base_url, api_key, timeout=timeout)


def _list_ollama_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/api/tags", headers=_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("name") or entry.get("model") for entry in payload.get("models", [])]
    return [str(name) for name in names if name]


# v118-F2: chatgpt.com lists models only for a client version it knows — a
# compatibility filter on the listing, not an identity header.
CODEX_CLIENT_VERSION = "0.169.0"


def _list_openai_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    from ..codex_login import uses_codex_login

    codex = uses_codex_login(base_url)
    try:
        response = httpx.get(
            f"{openai_style_prefix(base_url)}/models",
            params={"client_version": CODEX_CLIENT_VERSION} if codex else None,
            headers=_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise OllamaError(f"{status} from {base_url}" + _login_refused(base_url, status)) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    if codex:  # v118-F2: {"models": [{"slug": ...}]}
        return [str(e["slug"]) for e in payload.get("models", []) if e.get("slug")]
    names = [entry.get("id") or entry.get("name") for entry in payload.get("data", [])]
    return [str(name) for name in names if name]


def probe_completion(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 20.0,
) -> None:
    """v115-F5: one authenticated completion — the probe a dead key cannot pass.

    A model list is public on ollama.com: the key that 401'd every chat turn
    passed ``list_models`` and doctor said "provider ready". This reads the
    first streamed chunk of a one-word prompt and closes the stream (a
    token's worth), raising OllamaError with the provider's words on refusal.
    """
    stream = chat_stream(
        base_url,
        api_key,
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        protocol=protocol,
        timeout=timeout,
    )
    try:
        next(stream, None)
    finally:
        # chat_stream is a generator function (typed as its Iterator face):
        # closing it aborts the HTTP stream after the first chunk.
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 300.0,
    num_ctx: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield protocol-normalized chunks for one streamed chat call."""
    if protocol == "openai-compat":
        # openai-compat servers size their own context; num_ctx is ollama-only.
        yield from _openai_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    if protocol == "anthropic":
        # anthropic sizes its own context too; num_ctx is ollama-only.
        yield from _anthropic_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    if protocol == "openai-responses":
        # v108-F5: the Responses API sizes its own context; num_ctx is
        # ollama-only. Imported here — llm_responses imports this module.
        from .llm_responses import responses_chat_stream

        yield from responses_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    if protocol == "bedrock":
        # bedrock sizes its own context too; num_ctx is ollama-only.
        from .llm_bedrock import bedrock_chat_stream  # local import: llm_bedrock imports us

        yield from bedrock_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    yield from _ollama_chat_stream(
        base_url,
        api_key,
        model=model,
        messages=messages,
        tools=tools,
        timeout=timeout,
        num_ctx=num_ctx,
    )


# v48-F1: ollama.com intermittently 404s streaming chat requests that succeed
# on the next attempt (field test 2026-07-15 — every worker plan run failed on
# it). Retry transient statuses when OPENING the stream; the status check
# precedes any yielded line, so a retry can never duplicate streamed output.
_TRANSIENT_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})
_STREAM_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.5


def _error_detail(response: httpx.Response) -> str:
    """ES-F7: the provider's own words from an error body — ollama's
    ``{"error": "..."}``, the OpenAI/Anthropic ``{"error": {"message"}}``,
    Bedrock's ``{"message"}`` — capped; plain text when it is not JSON."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:300]
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    # v118-F2: chatgpt.com answers {"detail": "..."}.
    return str(error or payload.get("message") or payload.get("detail") or "")[:300]


def _open_stream_lines(
    url: str,
    base_url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> Iterator[str]:
    for attempt in range(1, _STREAM_ATTEMPTS + 1):
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=body,
            timeout=httpx.Timeout(timeout, connect=10.0),
        ) as response:
            if response.status_code != 200:
                response.read()
                detail = _error_detail(response)
                # ES-F7 (B36): a 404 that names a missing model is the model
                # name, not a v48-F1 flake — retrying only hides it.
                lowered = detail.lower()
                missing_model = (
                    response.status_code == 404 and "model" in lowered and "not found" in lowered
                )
                if (
                    response.status_code in _TRANSIENT_STATUSES
                    and attempt < _STREAM_ATTEMPTS
                    and not missing_model
                ):
                    time.sleep(_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise OllamaError(
                    f"{response.status_code} from {base_url}"
                    + (f": {detail}" if detail else "")
                    + _login_refused(base_url, response.status_code),
                    status=response.status_code,
                )
            yield from response.iter_lines()
            return


def _ollama_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
    num_ctx: int | None = None,
) -> Iterator[dict[str, Any]]:
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    if num_ctx is not None:
        body["options"] = {"num_ctx": num_ctx}
    try:
        for line in _open_stream_lines(
            f"{base_url.rstrip('/')}/api/chat",
            base_url,
            headers=_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            if line.strip():
                chunk = json.loads(line)
                if isinstance(chunk, dict) and chunk.get("error"):
                    # ES-F6 (B24): a failure after the 200 arrives as an
                    # {"error"} line; passed through, it read as an empty
                    # round and drew a stall nudge instead of the provider's
                    # own words (I8).
                    raise OllamaError(str(chunk["error"]))
                yield chunk
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


def _final_openai_tool_calls(calls: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index in sorted(calls):
        call = calls[index]
        arguments: Any = call["arguments"]
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = None
        # ES-F8 (B35): text that is not a JSON object stays the raw string —
        # flattening it to {} ran the call with no arguments, silently. The
        # chat answers a string with an error row the model can act on.
        if isinstance(parsed, dict):
            arguments = parsed
        final: dict[str, Any] = {"function": {"name": call["name"], "arguments": arguments}}
        # v106-F4 (v101-F15): the provider's call id survives normalization —
        # it is the only durable link between a call and its result once
        # cards resolve out of emission order.
        if call.get("id"):
            final["id"] = call["id"]
        normalized.append(final)
    return normalized


def _openai_delta_thinking(delta: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if value:
            return str(value)
    return ""


# v44-F9: base64 prefixes → data-URI mime (the Ollama shape carries bare b64;
# OpenAI-compat wants typed content parts).
_B64_MIME_PREFIXES = (
    ("iVBOR", "image/png"),
    ("/9j/", "image/jpeg"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)


def _openai_image_message(message: dict[str, Any]) -> dict[str, Any]:
    images = message.get("images")
    if message.get("role") != "user" or not isinstance(images, list) or not images:
        return message
    parts: list[dict[str, Any]] = [{"type": "text", "text": str(message.get("content") or "")}]
    for b64 in images:
        encoded = str(b64)
        mime = next(
            (m for prefix, m in _B64_MIME_PREFIXES if encoded.startswith(prefix)), "image/png"
        )
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return {"role": "user", "content": parts}


def _call_arguments_json(function: dict[str, Any]) -> str:
    """History rows carry parsed arguments; the wire wants a JSON string."""
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        return arguments or "{}"
    return json.dumps(arguments or {}, separators=(",", ":"))


def _openai_wire_message(
    message: dict[str, Any], index: int, unanswered: list[str]
) -> dict[str, Any]:
    """Ollama-shaped history row → /v1/chat/completions message.

    History stores tool calls Ollama-native (``arguments`` a dict, no
    ``type``); chat/completions 400s on that ("arguments of type string"),
    which broke every preset chat after its first tool call. New dicts only —
    the stored rows stay Ollama-native for the other protocols.

    ES-F3: native ollama stores calls with no id, and chat/completions needs
    one on every call and a ``tool_call_id`` on every result. Id-less calls
    get a synthesized id queued in ``unanswered``; id-less results take them
    in order — the anthropic FIFO rule for pre-id rows, where results follow
    their call in emission order.
    """
    role = message.get("role")
    if role == "assistant" and message.get("tool_calls"):
        calls = []
        for position, call in enumerate(message["tool_calls"]):
            function = call.get("function") or {}
            wire: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": _call_arguments_json(function),
                },
            }
            wire["id"] = call.get("id") or f"call_skep_{index}_{position}"
            if not call.get("id"):
                unanswered.append(wire["id"])
            calls.append(wire)
        return {**message, "tool_calls": calls}
    if role == "tool":
        # tool_name is Ollama-native; chat/completions pairs by tool_call_id.
        result = {k: v for k, v in message.items() if k != "tool_name"}
        if not result.get("tool_call_id") and unanswered:
            result["tool_call_id"] = unanswered.pop(0)
        return result
    return _openai_image_message(message)


def _openai_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    unanswered: list[str] = []
    messages = [_openai_wire_message(m, i, unanswered) for i, m in enumerate(messages)]
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    pending_calls: dict[int, dict[str, str]] = {}
    try:
        for line in _open_stream_lines(
            f"{openai_style_prefix(base_url)}/chat/completions",
            base_url,
            headers=_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            stripped = line.strip()
            if not stripped or not stripped.startswith("data:"):
                continue
            data = stripped.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            payload = json.loads(data)
            error = payload.get("error")
            if error:
                # ES-F14 (B90): the ES-F6 rule for openai-compat — a failure
                # after the 200 rides the stream; skipped, a partial reply
                # read as complete and an error-only one as a stall (I8).
                detail = error.get("message") if isinstance(error, dict) else None
                raise OllamaError(str(detail or error))
            for choice in payload.get("choices", []):
                delta = choice.get("delta") or {}
                thinking = _openai_delta_thinking(delta)
                if thinking:
                    yield {"message": {"role": "assistant", "thinking": thinking}}
                content = str(delta.get("content") or "")
                if content:
                    yield {"message": {"role": "assistant", "content": content}}
                for tool_call in delta.get("tool_calls") or []:
                    index = int(tool_call.get("index") or 0)
                    current = pending_calls.setdefault(index, {"name": "", "arguments": ""})
                    if tool_call.get("id"):
                        current["id"] = str(tool_call["id"])
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        current["name"] = str(function["name"])
                    if "arguments" in function:
                        current["arguments"] += str(function.get("arguments") or "")
                if choice.get("finish_reason") == "tool_calls" and pending_calls:
                    yield {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": _final_openai_tool_calls(pending_calls),
                        }
                    }
                    pending_calls = {}
        if pending_calls:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": _final_openai_tool_calls(pending_calls),
                }
            }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


# -- anthropic (v72-F1) -------------------------------------------------------
# The Messages API speaks a different shape at both ends; everything is
# translated at this boundary so the rest of chat (and the worker planner,
# which reuses chat_stream) keeps seeing the one normalized chunk format.

ANTHROPIC_VERSION = "2023-06-01"
# ponytail: fixed output ceiling; make it a setting if a use case ever hits it.
_ANTHROPIC_MAX_TOKENS = 8192


def _anthropic_headers(api_key: str | None) -> dict[str, str]:
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _list_anthropic_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/models",
            headers=_anthropic_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("id") or entry.get("display_name") for entry in payload.get("data", [])]
    return [str(name) for name in names if name]


def _anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _anthropic_call_arguments(function: dict[str, Any]) -> dict[str, Any]:
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(str(arguments or "") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _anthropic_payload(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Ollama-shaped history → Messages API body.

    v106-F4 (v101-F15): when the history carries real call ids (every row
    written since the tool_call_id column), each tool result pairs with ITS
    call. The synthesized-id FIFO remains only for older, id-less rows —
    there the engine appended results immediately after the calling message,
    so arrival order is call order.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    counter = 0
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "")
            if text:
                system_parts.append(text)
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = str(message.get("content") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                counter += 1
                call_id = str(call.get("id") or "") or f"call_{counter}"
                pending_ids.append(call_id)
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": str(function.get("name") or ""),
                        "input": _anthropic_call_arguments(function),
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            content = str(message.get("content") or "")
            result_id = str(message.get("tool_call_id") or "")
            if result_id and result_id in pending_ids:
                # v106-F4: exact pairing — the id names the call regardless of
                # the order the operator resolved the cards in.
                pending_ids.remove(result_id)
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": result_id,
                    "content": content,
                }
            elif pending_ids:
                # FIFO fallback for pre-column rows (NULL id) only — never the
                # primary mechanism again (the inverted-verdicts field test).
                block = {
                    "type": "tool_result",
                    "tool_use_id": pending_ids.pop(0),
                    "content": content,
                }
            else:  # an orphan result (its call was budgeted out) degrades to text
                block = {"type": "text", "text": f"[tool result] {content}"}
            converted.append({"role": "user", "content": [block]})
            continue
        blocks = []
        images = message.get("images")
        if isinstance(images, list):
            for b64 in images:
                encoded = str(b64)
                mime = next(
                    (m for prefix, m in _B64_MIME_PREFIXES if encoded.startswith(prefix)),
                    "image/png",
                )
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": encoded},
                    }
                )
        text = str(message.get("content") or "")
        if text:
            blocks.append({"type": "text", "text": text})
        if blocks:
            converted.append({"role": "user", "content": blocks})
    merged: list[dict[str, Any]] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append(message)
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "."}]})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": merged,
        "stream": True,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    converted_tools = _anthropic_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
    return body


def _anthropic_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    body = _anthropic_payload(model=model, messages=messages, tools=tools)
    pending_calls: dict[int, dict[str, str]] = {}
    try:
        for line in _open_stream_lines(
            f"{base_url.rstrip('/')}/v1/messages",
            base_url,
            headers=_anthropic_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped.removeprefix("data:").strip()
            if not data:
                continue
            payload = json.loads(data)
            kind = payload.get("type")
            if kind == "content_block_start":
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    index = int(payload.get("index") or 0)
                    pending_calls[index] = {"name": str(block.get("name") or ""), "arguments": ""}
            elif kind == "content_block_delta":
                delta = payload.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        yield {"message": {"role": "assistant", "content": text}}
                elif delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking") or "")
                    if thinking:
                        yield {"message": {"role": "assistant", "thinking": thinking}}
                elif delta_type == "input_json_delta":
                    index = int(payload.get("index") or 0)
                    if index in pending_calls:
                        pending_calls[index]["arguments"] += str(delta.get("partial_json") or "")
            elif kind == "error":
                error = payload.get("error") or {}
                raise OllamaError(str(error.get("message") or "anthropic stream error"))
            elif kind == "message_stop" and pending_calls:
                yield {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": _final_openai_tool_calls(pending_calls),
                    }
                }
                pending_calls = {}
        if pending_calls:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": _final_openai_tool_calls(pending_calls),
                }
            }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


# -- routes -------------------------------------------------------------------


class LlmConfigUpdate(BaseModel):
    """Only provided fields are written; an empty api_key clears the stored one."""

    base_url: str | None = None
    default_model: str | None = None
    protocol: LLMProtocol | None = None
    api_key: str | None = None
    vision: bool | None = None  # v44-F9: the model accepts image input
    num_ctx: int | None = None  # v56-F1: requested ollama context window; 0 = auto (v74-F2)
    tool_delivery: Literal["indexed", "full"] | None = None  # v74-F3: the escape hatch


class LlmTestRequest(BaseModel):
    """Optional overrides so the UI can probe before saving."""

    base_url: str | None = None
    protocol: LLMProtocol | None = None
    api_key: str | None = None


def _write_through_profile(store: RunStore, personal_home: Path) -> None:
    """v19-F9: mirror the sqlite provider settings into ``profile.json``.

    Only writes a complete config (base URL + model). ``api_key_env`` is None:
    the daemon manages its own secret in ``supervisor/llm-secret``, and
    ``_provider_check`` treats a missing ``api_key_env`` as "no key required".
    """
    from skep.profile import run_personal_setup

    base_url = store.get_setting(LLM_BASE_URL)
    model = store.get_setting(LLM_DEFAULT_MODEL)
    protocol = store.get_setting(LLM_PROTOCOL)
    if not (isinstance(base_url, str) and base_url.strip()):
        return
    if not (isinstance(model, str) and model.strip()):
        return
    from ..codex_login import uses_codex_login

    # v118-F4 (ADR 0063): profile.json is the WORKERS' provider. The ChatGPT
    # plan login is the Queen's alone, so workers keep the one they had.
    if not uses_codex_login(base_url):
        run_personal_setup(
            personal_home,
            provider=_protocol(protocol),
            model=model.strip(),
            endpoint=base_url.strip(),
            api_key_env=None,
        )
    # v115-F5: the registry's ACTIVE profile is the third copy of this truth
    # — routing and `skep provider health` read it. It was seeded once
    # (migrate_legacy_provider) and never followed the settings: the field
    # registry said deepseek-v4-pro while chat ran glm-5.3, so routing
    # logged no_healthy_provider for every run. One writer, mirrored here.
    active = store.active_provider_profile()
    if active is not None:
        serve_protocol = _protocol(protocol)
        registry_protocol = next(
            (key for key, value in REGISTRY_PROTOCOLS.items() if value == serve_protocol),
            active.protocol,
        )
        mirrored = dataclasses.replace(
            active, base_url=base_url.strip(), model=model.strip(), protocol=registry_protocol
        )
        if mirrored != active:
            store.upsert_provider_profile(mirrored)


def add_llm_routes(app: FastAPI, *, run_store: RunStore, home: Path) -> None:
    @app.get("/api/llm/config")
    def get_llm_config() -> dict[str, Any]:
        return llm_config_view(run_store, home)

    @app.put("/api/llm/config")
    def put_llm_config(body: LlmConfigUpdate) -> dict[str, Any]:
        if body.base_url is not None:
            run_store.set_setting(LLM_BASE_URL, body.base_url.strip().rstrip("/") or None)
        if body.default_model is not None:
            run_store.set_setting(LLM_DEFAULT_MODEL, body.default_model.strip() or None)
        if body.protocol is not None:
            run_store.set_setting(LLM_PROTOCOL, body.protocol)
        if body.api_key is not None:
            store_api_key(home, body.api_key.strip())
        if body.vision is not None:
            run_store.set_setting(LLM_VISION, body.vision)
        if body.num_ctx is not None:
            if body.num_ctx == 0:
                # v74-F2: 0 clears the override — back to auto (detected/default).
                run_store.set_setting(LLM_NUM_CTX, None)
            elif body.num_ctx < _MIN_NUM_CTX:
                raise HTTPException(
                    status_code=400, detail=f"num_ctx must be >= {_MIN_NUM_CTX} (0 = auto)"
                )
            else:
                run_store.set_setting(LLM_NUM_CTX, body.num_ctx)
        if body.tool_delivery is not None:
            run_store.set_setting(TOOL_DELIVERY_SETTING, body.tool_delivery)
        if body.default_model is not None and body.default_model.strip():
            # v74-F2: detection runs where the model changes; failure is silent
            # here (never breaks a save) and visible in num_ctx_source (I8).
            refresh_model_ctx(run_store, home, body.default_model.strip())
        # v19-F9: write-through to the personal profile.json so the CLI view
        # (`skep doctor`) agrees with the daemon's sqlite settings. ``home`` is
        # the supervisor home; the profile lives one level up.
        _write_through_profile(run_store, home.parent)
        return llm_config_view(run_store, home)

    @app.post("/api/llm/test")
    def test_llm(body: LlmTestRequest) -> dict[str, Any]:
        """Always 200: the verdict is the payload, so the UI shows it inline."""
        base_url = (body.base_url or "").strip() or run_store.get_setting(LLM_BASE_URL)
        if not base_url:
            return {"ok": False, "detail": "no base URL configured"}
        protocol = body.protocol or _protocol(run_store.get_setting(LLM_PROTOCOL))
        try:
            from ..codex_login import uses_codex_login

            # v118-F1: chatgpt.com gets the Codex login, never a pasted or saved key.
            api_key = (
                _codex_login_key(str(base_url))
                if uses_codex_login(str(base_url))
                else (body.api_key or "").strip() or resolve_api_key(home)
            )
            models = list_models(str(base_url), api_key, protocol=protocol)
        except OllamaError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "models": len(models)}

    @app.get("/api/llm/usage")
    def get_llm_usage(chat_id: str | None = None) -> dict[str, Any]:
        """v74-F6: skep's own token tally over rolling windows shaped like
        ollama.com's session (5h) and weekly limits. Honest surface (I8):
        measured locally from this daemon's requests — the account meter at
        ollama.com/settings is authoritative and includes other clients."""
        return {
            "measured_locally": True,
            "authoritative_meter": "https://ollama.com/settings",
            "note": (
                "counted from skep's own requests (ollama reports token "
                "counts per call); other clients on the account are not seen"
            ),
            "last_5h": run_store.llm_usage_totals(hours=5),
            "last_7d": run_store.llm_usage_totals(hours=24 * 7),
            # W42b: one chat's share of the same windows
            **(
                {
                    "chat": {
                        "chat_id": chat_id,
                        "last_5h": run_store.llm_usage_totals(hours=5, chat_id=chat_id),
                        "last_7d": run_store.llm_usage_totals(hours=24 * 7, chat_id=chat_id),
                    }
                }
                if chat_id
                else {}
            ),
        }

    @app.get("/api/llm/models")
    def get_llm_models() -> dict[str, Any]:
        base_url = run_store.get_setting(LLM_BASE_URL)
        if not base_url:
            raise HTTPException(status_code=409, detail="configure the LLM base URL first")
        protocol = _protocol(run_store.get_setting(LLM_PROTOCOL))
        try:
            # v108-F4: the active profile's own credential, when settings match it.
            api_key = resolve_active_api_key(run_store, home, base_url=str(base_url))
            return {"models": list_models(str(base_url), api_key, protocol=protocol)}
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
