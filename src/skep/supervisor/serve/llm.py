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

import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, cast, get_args
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..store import RunStore

if TYPE_CHECKING:
    from ..providers import ProviderHealth, ProviderProfile

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
# v74-F2: the model's real context length, detected at config-save time.
# Phase 06 (6.1): cached per (provider_id, base_url, model) — the v74 key was
# the bare model name (``llm_model_ctx:<model>``), so two endpoints serving
# one slug shared a window. Those rows name no endpoint and are never read.
# The auto value is capped: window_chars = num_ctx * 4 drives how much
# history is REPLAYED every round, so an uncapped 256k auto-window quietly
# multiplies per-turn cost and local-ollama VRAM. The operator's explicit
# LLM_NUM_CTX override goes as high as they like.
MODEL_WINDOW_PREFIX = "llm_model_window:"
AUTO_NUM_CTX_CAP = 65536
ContextSource = Literal["override", "provider-api", "catalog", "default"]
# v74-F3: how the chat advertises tools. 'indexed' (default) sends the
# categorized index in the prompt plus full schemas for the core set and
# any described-active tools; 'full' restores the whole registry every
# round — the one-flip escape hatch if a small Queen cannot work the index.
TOOL_DELIVERY_SETTING = "llm_tool_delivery"
TOOL_DELIVERIES = ("indexed", "full")
DEFAULT_LLM_PROTOCOL: LLMProtocol = "ollama"
SECRET_FILE = "llm-secret"
SECRET_ENV = "SKEP_LLM_API_KEY"
# Phase 05 R1: the endpoint llm-secret was saved for, pinned the moment the
# active endpoint first moves. Since Phase 05 LLM_BASE_URL mirrors the active
# profile, so "llm-secret belongs to LLM_BASE_URL" (v119-F1) handed the old
# key to every newly activated host.
LLM_SECRET_ENDPOINT = "llm_secret_endpoint"
# R3 (H1): SKEP_LLM_API_KEY's own owner. Sharing the file's marker let a
# save or clear of llm-secret move (or drop) the env key's owner with it.
LLM_ENV_KEY_ENDPOINT = "llm_env_key_endpoint"


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
    return wire_key(profile.base_url, _raw_provider_api_key(home, profile, store))


def wire_key(base_url: str, key: str | None) -> str | None:
    """A raw credential as ``base_url`` takes it — saved or pasted alike
    (Phase 05 R2, F10: the Web probe sent a pasted GitHub token raw). v108-F7:
    a Copilot endpoint takes a short-lived bearer, not the GitHub token
    itself — exchanged (and cached) transparently. Raises OllamaError when
    the exchange is refused."""
    from ..providers import provider_host
    from .llm_copilot import COPILOT_HOST, resolve_copilot_bearer

    if key and provider_host(base_url) == COPILOT_HOST:
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


def legacy_key_endpoint(
    home: Path, store: RunStore | None, marker: str = LLM_SECRET_ENDPOINT
) -> str:
    """v119-F1: the endpoint llm-secret (R3: or, with ``marker`` =
    LLM_ENV_KEY_ENDPOINT, SKEP_LLM_API_KEY) belongs to — its pinned owner;
    unpinned, the saved LLM_BASE_URL; with none saved, or the Queen on the
    ChatGPT plan (ADR 0063: the saved key is the workers'), the workers'
    profile.json endpoint. The sources migrate_legacy_provider seeds the
    ``default`` profile from, in the same order. ``home`` is the supervisor
    home."""
    from ..codex_login import uses_codex_login

    pinned = store.get_setting(marker) if store is not None else None
    if isinstance(pinned, str) and pinned.strip():
        return pinned.strip().rstrip("/")
    saved = store.get_setting(LLM_BASE_URL) if store is not None else None
    if isinstance(saved, str) and saved.strip() and not uses_codex_login(saved):
        return saved.strip().rstrip("/")
    from skep.profile import load_profile, profile_path

    endpoint = ""
    if profile_path(home.parent).is_file():
        with contextlib.suppress(OSError, ValueError):
            endpoint = load_profile(home.parent).provider.endpoint or ""
    return endpoint.strip().rstrip("/")


def pin_legacy_key(store: RunStore, home: Path) -> None:
    """Phase 05 R1: before the active endpoint moves, record the endpoint
    llm-secret belongs to, so it never follows the switch. Idempotent; a key
    saved before any endpoint keeps belonging to the first one saved. R2
    (F4): SKEP_LLM_API_KEY too — R3 (H1): under its own marker, so nothing
    a save or clear of llm-secret does moves it."""
    for marker, key in _legacy_values(home):
        if key and not store.get_setting(marker):
            owner = legacy_key_endpoint(home, store, marker)
            if owner:
                store.set_setting(marker, owner)


def _legacy_values(home: Path) -> tuple[tuple[str, str | None], ...]:
    """(owner marker, key) of SKEP_LLM_API_KEY, then of llm-secret — R3
    (H1): each has its own owner; the env key first, as it always shadowed
    the file."""
    path = home / SECRET_FILE
    held = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    return (
        (LLM_ENV_KEY_ENDPOINT, os.environ.get(SECRET_ENV, "").strip() or None),
        (LLM_SECRET_ENDPOINT, held or None),
    )


def legacy_api_key(
    home: Path, endpoint: str, store: RunStore | None = None, *, unowned: bool = False
) -> str | None:
    """v119-F1: SKEP_LLM_API_KEY / llm-secret, each for its own endpoint
    only. It was the fallback for EVERY profile, so the health sweep sent
    the main key to each keyless profile's host every five minutes. The same
    URL match resolve_active_api_key and worker_key_profile use; anywhere
    else gets no key (a local server needs none; a remote one fails, naming
    the fix). ``unowned``: a key no endpoint owns yet also counts — it is
    the first one saved's (pin_legacy_key)."""
    want = endpoint.strip().rstrip("/")
    owners = (want, "") if unowned else (want,)
    for marker, key in _legacy_values(home):
        if key and legacy_key_endpoint(home, store, marker) in owners:
            return key
    return None


def legacy_key_withheld(home: Path, profile: ProviderProfile, store: RunStore) -> str | None:
    """v119-F1: why a profile is called without a key, naming the fix (I9) —
    None when it has a credential, needs none from skep, or none exists."""
    from ..codex_login import uses_codex_login

    owners = [
        legacy_key_endpoint(home, store, marker) or "the saved endpoint"
        for marker, key in _legacy_values(home)
        if key
    ]
    if uses_codex_login(profile.base_url) or not owners:
        return None
    if _raw_provider_api_key(home, profile, store) is not None:
        return None
    owner = " and ".join(dict.fromkeys(owners))
    return (
        f"provider {profile.provider_id!r} has no key of its own, and the main key "
        f"(llm-secret / {SECRET_ENV}) is only sent to {owner}, so it is called without "
        f"one — give it its own key: `skep provider set-key {profile.provider_id}`"
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
    if base_url is None:
        return (
            resolve_api_key(home)
            if active is None
            else resolve_provider_api_key(home, active, store)
        )
    return saved_key_for(store, home, base_url)


def saved_key_for(store: RunStore, home: Path, base_url: str) -> str | None:
    """Phase 05 R1: the saved credential that belongs to ``base_url`` — the
    registered profile for that endpoint (the active one first), else
    llm-secret only if it was saved for it. Anything else gets no key: a key
    never travels to an endpoint it was not saved for (P2)."""
    owner = _registered_for(store, base_url)
    if owner is not None:
        return resolve_provider_api_key(home, owner, store)
    return legacy_api_key(home, base_url.strip().rstrip("/"), store)


def _registered_for(store: RunStore, endpoint: str) -> ProviderProfile | None:
    """The registry profile for ``endpoint`` — the active one first."""
    want = endpoint.strip().rstrip("/")
    return next(
        (
            p
            for p in sorted(store.list_provider_profiles(), key=lambda p: not p.active)
            if p.base_url.rstrip("/") == want
        ),
        None,
    )


def assistant_llm(store: RunStore) -> tuple[str | None, str | None, LLMProtocol]:
    """Phase 05 (5.1): THE read of the assistant's provider — (base_url,
    model, wire protocol) of the ACTIVE registry profile. The legacy llm_*
    settings are its mirrors; they are read only while no profile is active
    (a first save still staging, or a home whose active profile was removed —
    ``remove_provider`` keeps the Queen on what it spoke). Chat, setup,
    channels, the model list and builtin workers all read through here."""
    active = store.active_provider_profile()
    if active is not None and active.protocol in REGISTRY_PROTOCOLS:
        return active.base_url, active.model, REGISTRY_PROTOCOLS[active.protocol]
    base_url, model = store.get_setting(LLM_BASE_URL), store.get_setting(LLM_DEFAULT_MODEL)
    return (
        base_url if isinstance(base_url, str) and base_url else None,
        model if isinstance(model, str) and model else None,
        _protocol(store.get_setting(LLM_PROTOCOL)),
    )


def _assistant_key_set(store: RunStore, home: Path) -> bool:
    """Whether the active profile has a credential — the one chat sends.
    R3 (H7): on the ChatGPT login a key here is the workers' (ADR 0063), so
    it is whether THEIR endpoint has one, never whether llm-secret exists."""
    from ..codex_login import uses_codex_login

    active = store.active_provider_profile()
    if active is None:
        return resolve_api_key(home) is not None
    if uses_codex_login(active.base_url):
        endpoint = _workers_provider(home)[0]
        holder = _registered_for(store, endpoint)
        key = (
            _raw_provider_api_key(home, holder, store)
            if holder is not None
            else legacy_api_key(home, endpoint, store)
        )
        return key is not None  # an unowned key is the first endpoint's (pin_legacy_key)
    return _raw_provider_api_key(home, active, store) is not None


def llm_config_view(store: RunStore, home: Path) -> dict[str, Any]:
    # v14: seed the provider registry from legacy config on first use (side
    # effect; the registry is surfaced through the dedicated provider views, so
    # the public /api/llm/config shape stays stable). Phase 05: and, once,
    # adopt settings that drifted from it — then read the registry.
    from ..providers import migrate_legacy_provider

    migrate_legacy_provider(store, home)
    base_url, model, protocol = assistant_llm(store)
    window = resolve_context_window(store)
    from ..codex_login import uses_codex_login

    # v118-F1: on chatgpt.com no API key is in play — the login is (I8).
    codex = isinstance(base_url, str) and uses_codex_login(base_url)
    return {
        "configured": bool(base_url),
        "base_url": base_url,
        "default_model": model,
        "protocol": protocol,
        "api_key_set": _assistant_key_set(store, home),
        **({"chatgpt_login": chatgpt_login_view()} if codex else {}),
        "vision": store.get_setting(LLM_VISION) is True,
        "num_ctx": window.replay_cap,
        # v74-F2: which rule set the window, so the UI and doctor can say (I8).
        "num_ctx_source": window.source,
        # Phase 06 (6.1): the model's real limit, even where num_ctx is capped
        "model_window": window.model_window,
        "tool_delivery": tool_delivery(store),
    }


def tool_delivery(store: RunStore) -> str:
    value = store.get_setting(TOOL_DELIVERY_SETTING)
    return str(value) if value in TOOL_DELIVERIES else "indexed"


def _valid_ctx(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= _MIN_NUM_CTX


@dataclasses.dataclass(frozen=True)
class ContextWindow:
    """Phase 06 (6.1): the model's real window at its endpoint, apart from
    what skep replays per turn. ``source`` names what set the window (I8)."""

    model_window: int
    replay_cap: int
    source: ContextSource
    max_output: int | None = None


def context_cache_key(provider_id: str, base_url: str, model: str) -> str:
    return MODEL_WINDOW_PREFIX + json.dumps([provider_id, base_url.strip().rstrip("/"), model])


def _context_identity(
    store: RunStore, profile: ProviderProfile | None
) -> tuple[str, str | None, str | None, LLMProtocol]:
    """(provider_id, base_url, model, protocol) the window belongs to: the
    given profile, else the assistant's (the active one, or the legacy
    settings while none is active — provider_id "")."""
    if profile is not None:
        protocol = REGISTRY_PROTOCOLS.get(profile.protocol, DEFAULT_LLM_PROTOCOL)
        return profile.provider_id, profile.base_url, profile.model, protocol
    active = store.active_provider_profile()
    base_url, model, protocol = assistant_llm(store)
    return (active.provider_id if active else ""), base_url, model, protocol


def resolve_context_window(
    store: RunStore, profile: ProviderProfile | None = None, model: str | None = None
) -> ContextWindow:
    """Phase 06 (6.1): THE context-window read — override > provider-api >
    catalog > default. Reads the store only, never the network, so it is
    safe on every chat turn. ``profile`` defaults to the assistant's;
    ``model`` to that profile's (a chat's pinned model passes its own).

    replay_cap is the window capped at AUTO_NUM_CTX_CAP — or, on a loopback
    ollama, at DEFAULT_NUM_CTX: there num_ctx is pre-allocated KV-cache RAM
    (v82-F1), so the real max shows in model_window and nothing more is
    requested. The operator's dial is uncapped, loopback or not."""
    value = store.get_setting(LLM_NUM_CTX)
    if _valid_ctx(value):
        return ContextWindow(int(value), int(value), "override")
    from ..provider_presets import MODEL_CONTEXT_WINDOWS

    provider_id, base_url, default_model, protocol = _context_identity(store, profile)
    name = model or default_model
    window, max_output = DEFAULT_NUM_CTX, None
    source: ContextSource = "default"
    cached = (
        store.get_setting(context_cache_key(provider_id, base_url, name))
        if (base_url and name)
        else None
    )
    if isinstance(cached, dict) and _valid_ctx(cached.get("window")):
        window, source = int(cached["window"]), "provider-api"
        max_output = cached.get("max_output") if _valid_ctx(cached.get("max_output")) else None
    elif name in MODEL_CONTEXT_WINDOWS:
        # R1 (F4): exact ids only — the anthropic wire carries third-party
        # presets too, so no protocol-wide 200k guess (I8)
        window, source = MODEL_CONTEXT_WINDOWS[name], "catalog"
    loopback = protocol == "ollama" and _is_loopback(base_url)
    cap = DEFAULT_NUM_CTX if loopback else AUTO_NUM_CTX_CAP
    return ContextWindow(window, min(window, cap), source, max_output)


def resolved_num_ctx(store: RunStore, model: str | None = None) -> tuple[int, str]:
    """(replay tokens, source) for ``model`` (default: the configured one)."""
    window = resolve_context_window(store, model=model)
    return window.replay_cap, window.source


def _is_loopback(base_url: Any) -> bool:
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        host = (httpx.URL(base_url).host or "").lower()
    except httpx.InvalidURL:
        return False
    if host == "localhost":
        return True
    try:  # R3 (Sol R2-1): a loopback LITERAL — a DNS name like 127.evil.example is remote
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def chat_num_ctx(store: RunStore, model: str | None = None) -> int:
    """The context window requested from ollama (v56-F1); bounded, defaulted."""
    return resolved_num_ctx(store, model)[0]


_WINDOW_FIELDS = ("context_length", "max_model_len", "max_context_length")
_UNREACHABLE = object()


def _http_body(
    method: str, url: str, *, headers: dict[str, str], json: Any, timeout: float
) -> tuple[bool, bytes]:
    """(is an HTTP error, body), read under ONE wall-clock deadline that
    covers connect, the response headers and the body — R4 (Sol R3-2):
    httpx's timeout is per read, so a host trickling header bytes kept the
    worker alive past the deadline (forever, if it never ended them). The
    async read is cancelled at the deadline and its connection dropped."""

    async def read() -> tuple[bool, bytes]:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(method, url, headers=headers, json=json) as response,
        ):
            if response.is_error:
                return True, b""
            return False, b"".join([chunk async for chunk in response.aiter_bytes()])

    try:
        return asyncio.run(asyncio.wait_for(read(), timeout))
    except TimeoutError as exc:
        raise httpx.ReadTimeout("context probe deadline") from exc


def _fetch_json(url: str, api_key: str | None, deadline: float, body: Any = None) -> Any:
    """The body (a POST of ``body`` when given), None on an HTTP, decoding,
    URL or JSON error, or _UNREACHABLE when the host did not answer within
    what is left of ``deadline`` (R1 F10: one budget for every probe, and a
    host that timed out is not asked again). R2 (G5): a bad gzip body or URL
    is a miss. R4: the read itself stops at the deadline (``_http_body``)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _UNREACHABLE
    try:
        failed, data = _http_body(
            "POST" if body is not None else "GET",
            url,
            headers=_headers(api_key),
            json=body,
            timeout=remaining,
        )
    except httpx.TransportError:
        return _UNREACHABLE
    except (httpx.HTTPError, httpx.InvalidURL):
        return None
    if failed:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _rows(body: Any, key: str) -> list[dict[str, Any]]:
    """R1 (F9): the dict rows under ``key`` — any other shape is no rows."""
    rows = body.get(key) if isinstance(body, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _lmstudio_window(body: Any, model: str) -> int | None:
    """R1 (F5): LM Studio's native GET /api/v1/models — a loaded instance's
    config.context_length (what a request may use) when ``model`` names the
    instance or its model, else the model's max_context_length."""
    for row in _rows(body, "models"):
        instances = _rows(row, "loaded_instances")
        named = [i for i in instances if i.get("id") == model]
        if not named and row.get("key") != model:
            continue
        for instance in named or instances:
            config = instance.get("config")
            n_ctx = config.get("context_length") if isinstance(config, dict) else None
            if _valid_ctx(n_ctx):
                return int(n_ctx)
        top = row.get("max_context_length")
        return int(top) if _valid_ctx(top) else None
    return None


def _openai_style_window(
    base_url: str, api_key: str | None, model: str, deadline: float, lmstudio: bool
) -> tuple[int, int | None] | None:
    """The model's row in GET <prefix>/models — OpenRouter's context_length
    (+ top_provider.max_completion_tokens), vLLM's max_model_len — else LM
    Studio's native GET /api/v1/models, else llama.cpp's GET /props n_ctx.

    R2 (G1, I11): those two live outside the configured prefix, so the key
    goes there only for a local server whose API sits at the host root (one
    port on this machine is one server) — the native listing also for an LM
    Studio preset profile. Elsewhere <base>/tenant/api/v1/models may be
    another service on the same gateway."""
    prefix = openai_style_prefix(base_url)
    root = prefix.removesuffix("/v1")
    local_root = _is_loopback(base_url) and urlparse(root).path in ("", "/")
    listing = _fetch_json(f"{prefix}/models", api_key, deadline)
    if listing is _UNREACHABLE:
        return None
    entry = next((r for r in _rows(listing, "data") if r.get("id") == model), {})
    top = entry.get("top_provider")
    out = top.get("max_completion_tokens") if isinstance(top, dict) else None
    for field in _WINDOW_FIELDS:
        if _valid_ctx(entry.get(field)):
            return int(entry[field]), (int(out) if _valid_ctx(out) else None)
    if not (local_root or lmstudio):
        return None
    native = _fetch_json(f"{root}/api/v1/models", api_key, deadline)
    if native is _UNREACHABLE:
        return None
    lmstudio_window = _lmstudio_window(native, model)
    if lmstudio_window is not None:
        return lmstudio_window, None
    if not local_root:
        return None
    props = _fetch_json(f"{root}/props", api_key, deadline)
    settings = props.get("default_generation_settings") if isinstance(props, dict) else None
    n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
    return (int(n_ctx), None) if _valid_ctx(n_ctx) else None


def detect_model_ctx(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 3.0,
    lmstudio: bool = False,
) -> tuple[int, int | None] | None:
    """(window, max output) from the endpoint's own API, or None — detection
    never breaks a save; the whole probe is bounded by ``timeout``, wall
    clock (R2, G3: the probe thread is abandoned at the deadline).

    ollama reports it via POST /api/show under an architecture-prefixed
    ``model_info`` key (``llama.context_length``, ``qwen3.context_length``, …);
    the ``.context_length`` suffix is the stable part. OpenAI-style servers
    answer through their model listing (Phase 06). anthropic and bedrock
    have no such endpoint — the catalog answers for them.
    """
    deadline = time.monotonic() + timeout
    found: list[tuple[int, int | None] | None] = []

    def probe() -> None:
        if protocol in ("openai-compat", "openai-responses"):
            found.append(_openai_style_window(base_url, api_key, model, deadline, lmstudio))
        elif protocol == "ollama":
            found.append(_ollama_window(base_url, api_key, model, deadline))

    worker = threading.Thread(target=probe, name="skep-context-detect", daemon=True)
    worker.start()
    worker.join(timeout)
    return found[0] if found else None


def _ollama_window(
    base_url: str, api_key: str | None, model: str, deadline: float
) -> tuple[int, int | None] | None:
    body = _fetch_json(f"{base_url.rstrip('/')}/api/show", api_key, deadline, {"model": model})
    info = body.get("model_info") if isinstance(body, dict) else None
    for key, value in info.items() if isinstance(info, dict) else ():
        if str(key).endswith(".context_length") and _valid_ctx(value):
            return int(value), None
    return None


# R2 (G4): the newest refresh per cache key — an older probe still in flight
# (the startup one, under a key or protocol since replaced) never publishes.
_CTX_LOCK = threading.Lock()
_CTX_GENERATION: dict[str, int] = {}


def refresh_model_ctx(
    store: RunStore, home: Path, model: str, *, live: Callable[[], RunStore] | None = None
) -> None:
    """Probe + cache ``model``'s window at the assistant's endpoint. Runs
    where the profile or model changes (config save, provider use,
    set_assistant_model) — never per turn. A failed probe drops this
    identity's row, so the read falls to catalog/default, never stale.
    R3 (Sol R2-2): published only if the live configuration (``live()`` —
    the startup probe's staged view re-read — else ``store``) still has the
    identity and protocol this probe captured."""
    identity = _context_identity(store, None)
    provider_id, base_url, _model, protocol = identity
    if not (base_url and model):
        return
    from ..codex_login import uses_codex_login

    if uses_codex_login(base_url):
        return  # v118-F1: no context endpoint, and llm-secret never goes there
    key = context_cache_key(provider_id, base_url, model)
    with _CTX_LOCK:
        generation = _CTX_GENERATION[key] = _CTX_GENERATION.get(key, 0) + 1
    active = store.active_provider_profile()
    # R3 (Sol R3-1): the native-path permission belongs to the endpoint probed
    lmstudio = (
        active is not None
        and active.source == "preset:lmstudio"
        and active.base_url.rstrip("/") == base_url.rstrip("/")
    )
    try:
        # Phase 05 R1: the key saved for this endpoint, or none (P2)
        api_key = resolve_active_api_key(store, home, base_url=base_url)
    except OllamaError:  # R1 (F9): a refused credential (Copilot exchange) is a miss
        detected = None
    else:
        detected = detect_model_ctx(
            base_url,
            api_key,
            model=model,
            protocol=protocol,
            lmstudio=lmstudio,
        )
    with _CTX_LOCK:
        if _CTX_GENERATION[key] != generation:
            return
        if _context_identity(live() if live else store, None) != identity:
            return  # the configuration moved while this probe ran
        if detected is None:
            store.set_setting(key, None)
        else:
            store.set_setting(key, {"window": detected[0], "max_output": detected[1]})


def refresh_missing_model_ctx(store: RunStore, home: Path) -> None:
    """R1 (F8): serve startup probes the active window once when it has no
    provider-keyed row (an upgraded home has only v74 bare-name rows).
    R2 (G2): keyed as Settings will read it — the legacy adoption applied in
    memory (Phase 05's staged view), never written here."""
    view = _staged_view(store, home)
    provider_id, base_url, model, _protocol = _context_identity(view, None)
    if (
        base_url
        and model
        and store.get_setting(context_cache_key(provider_id, base_url, model)) is None
    ):
        refresh_model_ctx(view, home, model, live=lambda: _staged_view(store, home))


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


def _scrubbed(exc: OllamaError, api_key: str | None) -> OllamaError:
    """Phase 05 R2 (F1, I11): a provider error safe to print anywhere — the
    key it may quote back ("Incorrect API key provided: ...") and any other
    credential shape masked; the HTTP status kept for classification."""
    from ..policy_v3 import redact

    text = str(exc).replace(api_key, "***") if api_key else str(exc)
    return OllamaError(redact(text), status=exc.status)


def list_models(
    base_url: str,
    api_key: str | None,
    *,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 10.0,
) -> list[str]:
    try:
        return _list_models(base_url, api_key, protocol=protocol, timeout=timeout)
    except OllamaError as exc:
        raise _scrubbed(exc, api_key) from None


def _list_models(
    base_url: str, api_key: str | None, *, protocol: LLMProtocol, timeout: float
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
        raise OllamaError(
            f"{exc.response.status_code} from {base_url}", status=exc.response.status_code
        ) from exc
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
        raise OllamaError(
            f"{status} from {base_url}" + _login_refused(base_url, status), status=status
        ) from exc
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
    passed ``list_models`` and doctor said "provider ready". This asks a
    one-word prompt for one output token, reads until the first real answer
    and closes the stream, raising OllamaError with the provider's words on
    refusal — and when the stream ends with no completion in it (Phase 05
    R1: an empty or metadata-only 200 is not a working provider).
    """
    stream = chat_stream(
        base_url,
        api_key,
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        protocol=protocol,
        timeout=timeout,
        max_tokens=1,
    )
    try:
        for chunk in stream:
            message = chunk.get("message") or {}
            if any(message.get(k) for k in ("content", "thinking", "tool_calls")):
                return
            if chunk.get("done") and chunk.get("done_reason") == "length":
                # ollama hit the 1-token cap: the model generated (R2, F13:
                # an empty "stop" is no completion)
                return
        raise OllamaError(f"{base_url} answered the probe with no completion")
    except OllamaError as exc:
        raise _scrubbed(exc, api_key) from None
    finally:
        # chat_stream is a generator function (typed as its Iterator face):
        # closing it aborts the HTTP stream after the first answer.
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
    max_tokens: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield protocol-normalized chunks for one streamed chat call.

    ``max_tokens`` caps the output where the wire has a portable cap
    (ollama num_predict, anthropic max_tokens, bedrock maxTokens). The two
    OpenAI shapes take none: OpenAI's reasoning models refuse ``max_tokens``
    and spend ``max_completion_tokens``/``max_output_tokens`` on hidden
    reasoning, so a tiny cap answers a valid key with an empty completion."""
    if protocol == "openai-compat":
        # openai-compat servers size their own context; num_ctx is ollama-only.
        yield from _openai_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    if protocol == "anthropic":
        # anthropic sizes its own context too; num_ctx is ollama-only.
        yield from _anthropic_chat_stream(
            base_url,
            api_key,
            model=model,
            messages=messages,
            tools=tools,
            timeout=timeout,
            max_tokens=max_tokens,
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
            base_url,
            api_key,
            model=model,
            messages=messages,
            tools=tools,
            timeout=timeout,
            max_tokens=max_tokens,
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
        max_tokens=max_tokens,
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
    max_tokens: int | None = None,
) -> Iterator[dict[str, Any]]:
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    options = {k: v for k, v in (("num_ctx", num_ctx), ("num_predict", max_tokens)) if v}
    if options:
        body["options"] = options
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
        raise OllamaError(
            f"{exc.response.status_code} from {base_url}", status=exc.response.status_code
        ) from exc
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
    max_tokens: int | None = None,
) -> Iterator[dict[str, Any]]:
    body = _anthropic_payload(model=model, messages=messages, tools=tools)
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
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
    default_model: str | None = None  # Phase 05: the model the 1-token probe asks


def _registry_protocol(protocol: LLMProtocol) -> str:
    return next(key for key, value in REGISTRY_PROTOCOLS.items() if value == protocol)


def probe_candidate(
    store: RunStore,
    home: Path,
    *,
    base_url: str,
    model: str | None,
    protocol: LLMProtocol,
    api_key: str | None,
    provider_id: str | None = None,
) -> ProviderHealth:
    """Phase 05 (5.1): the staged live probe of a candidate — its model list
    and one 1-token completion with ITS key, before anything is saved
    (``providers.check_provider_health``). With no model named yet, the
    first one the provider lists takes the token. Returns the ProviderHealth
    (its error carries no key: live_probes scrubs it); writes nothing."""
    from datetime import UTC, datetime

    from ..providers import ProviderProfile, check_provider_health, live_probes

    active = store.active_provider_profile()
    candidate = ProviderProfile(
        provider_id=provider_id or (active.provider_id if active is not None else "default"),
        protocol=_registry_protocol(protocol),
        base_url=base_url,
        model=model or "",
    )
    probes = live_probes(home, store, key_for=lambda _p: api_key)
    listed: list[str] = []

    def _list(profile: ProviderProfile) -> list[str]:
        listed[:] = probes["list_models"](profile)
        return listed

    def _probe(profile: ProviderProfile) -> None:
        named = profile.model or (listed[0] if listed else "")
        probes["probe"](dataclasses.replace(profile, model=named))

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return check_provider_health(candidate, list_models=_list, probe=_probe, now=now)


_UNCHANGED = "nothing changed: the saved provider and key are as they were"


def _probe_or_refuse(
    store: RunStore,
    home: Path,
    *,
    base_url: str,
    model: str | None,
    protocol: LLMProtocol,
    api_key: str | None,
    provider_id: str | None = None,
) -> str:
    """Probe a candidate; raise ProviderError (nothing written yet) when it
    fails. Returns the model the probe asked — the named one, or the first
    the endpoint lists."""
    from ..providers import ProviderError

    health = probe_candidate(
        store,
        home,
        base_url=base_url,
        model=model,
        protocol=protocol,
        api_key=api_key,
        provider_id=provider_id,
    )
    if not health.ok:
        raise ProviderError(f"{health.error} — {_UNCHANGED}")
    if model:
        return model
    if not health.models:  # R2 (F14): nothing to name, so the operator must
        raise ProviderError(f"{base_url} lists no models — name the model to use; {_UNCHANGED}")
    return health.models[0]


# What a provider write touches in sqlite besides the registry rows.
_ROLLBACK_SETTINGS = (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    LLM_VISION,
    LLM_NUM_CTX,
    TOOL_DELIVERY_SETTING,
    LLM_SECRET_ENDPOINT,
    LLM_ENV_KEY_ENDPOINT,  # R4 (Sol R4-2): pin_legacy_key writes it too
)
# Phase 05 R2 (F5): provider commits are serialized; a save stages and probes
# outside this lock. ponytail: one in-process lock — the CLI committing beside
# serve is another process, which only the staged-state re-check sees.
_PROVIDER_WRITES = threading.RLock()
ProviderState = tuple[list[Any], dict[str, Any], dict[Path, bytes]]


def _state_files(home: Path) -> list[Path]:
    return [
        home.parent / "profile.json",
        home / SECRET_FILE,
        *home.glob(f"{_PROVIDER_SECRET_PREFIX}*"),
    ]


def provider_state(store: RunStore, home: Path) -> ProviderState:
    """What a provider write touches: the registry rows, the llm_* settings,
    the key files and profile.json. ``home`` is the supervisor home."""
    from ..providers import LLM_REGISTRY_CANONICAL

    return (
        store.list_provider_profiles(),
        {key: store.get_setting(key) for key in (*_ROLLBACK_SETTINGS, LLM_REGISTRY_CANONICAL)},
        {path: path.read_bytes() for path in _state_files(home) if path.is_file()},
    )


def _restore(store: RunStore, home: Path, state: ProviderState) -> None:
    rows, settings, saved = state
    kept = {row.provider_id for row in rows}
    for profile in store.list_provider_profiles():
        if profile.provider_id not in kept:
            store.delete_provider_profile(profile.provider_id)
    # only what differs is put back: a row, setting or file that matches is
    # left untouched (timestamps too)
    current = {row.provider_id: row for row in store.list_provider_profiles()}
    for row in sorted(rows, key=lambda row: row.active):  # the active one last
        if current.get(row.provider_id) != row:
            store.upsert_provider_profile(row)
    for key, value in settings.items():
        if store.get_setting(key) == value:
            continue
        if value is None:  # absent before: deleted, not a JSON null row
            store.delete_setting(key)
        else:
            store.set_setting(key, value)
    for path in _state_files(home):
        if path not in saved:
            path.unlink(missing_ok=True)
    for path, data in saved.items():
        if path.is_file() and path.read_bytes() == data:
            continue
        path.write_bytes(data)
        if path.name.startswith(SECRET_FILE):
            path.chmod(0o600)


@contextlib.contextmanager
def provider_rollback(
    store: RunStore, home: Path, *, expect: ProviderState | None = None
) -> Iterator[None]:
    """Phase 05 R1: a provider write lands whole or not at all. The registry
    rows, the llm_* settings, the key files and profile.json are snapshotted
    and put back if anything raises — an interrupt included. (L2's onboard
    rollback, shared by every write.) ``home`` is the supervisor home.

    R2 (F5): commits are serialized, and the snapshot is taken only here,
    after the probe — a slow refusal can never put back a save that landed
    meanwhile. ``expect`` is the state a save was staged and probed on: if it
    moved, ProviderConflict, and nothing is written."""
    from ..providers import ProviderConflict

    with _PROVIDER_WRITES:
        state = provider_state(store, home)
        if expect is not None and state != expect:
            raise ProviderConflict(
                "the provider settings changed while this save was being tested — nothing "
                "was saved: reload them and save again"
            )
        try:
            yield
        except BaseException:
            _restore(store, home, state)
            raise


class _AdoptedView:
    """Phase 05 R2 (F6): the store as a save stages on it — the legacy
    adoption (``providers.legacy_adoption``) applied in memory, never
    written; every other read goes to the store. The commit writes it."""

    def __init__(self, store: RunStore, rows: list[ProviderProfile]) -> None:
        self._store = store
        profiles = {p.provider_id: p for p in store.list_provider_profiles()}
        for row in rows:
            if row.active:
                profiles = {k: dataclasses.replace(p, active=False) for k, p in profiles.items()}
            profiles[row.provider_id] = row
        self._profiles = sorted(profiles.values(), key=lambda p: (p.fallback_order, p.provider_id))

    def list_provider_profiles(self) -> list[ProviderProfile]:
        return list(self._profiles)

    def active_provider_profile(self) -> ProviderProfile | None:
        return next((p for p in self._profiles if p.active), None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _staged_view(store: RunStore, home: Path) -> RunStore:
    from ..providers import legacy_adoption

    rows, _canonical = legacy_adoption(store, home)
    # ponytail: duck-typed — staging reads profiles and settings, never writes
    return cast("RunStore", _AdoptedView(store, rows)) if rows else store


def _endpoint_profile(
    store: RunStore,
    base_url: str,
    protocol: LLMProtocol | None,
    fallback: LLMProtocol,
    model: str | None,
) -> tuple[ProviderProfile, bool]:
    """(profile, registered) for a new endpoint: the registered one for that
    endpoint (its credential belongs there; its own protocol unless one is
    named), else a new keyless one with its own cost class and host list
    (``providers.endpoint_profile``)."""
    from ..providers import endpoint_profile

    existing = next(
        (p for p in store.list_provider_profiles() if p.base_url.rstrip("/") == base_url),
        None,
    )
    if existing is None:
        fresh = endpoint_profile(
            store,
            base_url=base_url,
            protocol=_registry_protocol(protocol or fallback),
            model=model or "",
            source="assistant-settings",
        )
        return fresh, False
    named = _registry_protocol(protocol) if protocol else existing.protocol
    return dataclasses.replace(existing, model=model or existing.model, protocol=named), True


def _committed_key(
    store: RunStore,
    home: Path,
    target: str,
    profile: ProviderProfile | None,
    registered: bool,
    api_key: str | None,
) -> str | None:
    """Phase 05 R2 (F7/F10): the credential ``target`` resolves once this save
    lands, as the wire takes it — the probe sends exactly this."""
    from ..codex_login import uses_codex_login

    if uses_codex_login(target):
        return _codex_login_key(target)  # v118-F1: the Codex login, never a key
    if api_key:
        return wire_key(target, api_key)  # the replacement — the old one is never resolved
    if api_key == "":  # this save clears the key: only the env vars are left —
        # the one the profile names, else SKEP_LLM_API_KEY if pinned here (R4, R2-6)
        env = profile.api_key_env if profile is not None else None
        named = os.environ.get(env, "").strip() if env else ""
        return wire_key(target, named or _env_key_for(home, target, store))
    if registered and profile is not None:
        return resolve_provider_api_key(home, profile, store)
    # a new endpoint has no key — but a legacy key no endpoint owns yet
    # belongs to the first one saved (pin_legacy_key)
    # R4 (Sol R4-1): as the wire takes it (a Copilot GitHub token exchanged)
    return wire_key(target, legacy_api_key(home, target, store, unowned=True))


def _env_key_for(home: Path, target: str, store: RunStore) -> str | None:
    """SKEP_LLM_API_KEY when ``target`` owns it — clearing a saved key never
    clears the environment's."""
    marker, key = _legacy_values(home)[0]
    owner = legacy_key_endpoint(home, store, marker)
    return key if key and owner == target.strip().rstrip("/") else None


def save_assistant_llm(
    store: RunStore,
    home: Path,
    *,
    base_url: str | None = None,
    model: str | None = None,
    protocol: LLMProtocol | None = None,
    api_key: str | None = None,
    settings: dict[str, Any] | None = None,
) -> None:
    """Phase 05 (5.1): THE write of the assistant's provider/model — Web
    Settings and the Queen's model dial land here, ``use_provider`` (CLI
    ``skep provider``, onboard) activates a profile. The active registry
    profile is written; the llm_* settings and profile.json are mirrored from
    it. With no active profile the settings stage the save, and a complete
    config on an empty registry seeds the ``default`` profile. ``home`` is
    the supervisor home; ``settings`` (Web Settings' other knobs) land in
    the same commit.

    Phase 05 R1: a credential stays with its endpoint. A new endpoint is
    its own profile (the registered one for it, or a new keyless one with
    its own cost class and hosts) — probed, and only then activated; the old
    profile and its key stay as they were. A non-empty ``api_key`` is probed
    against the endpoint it is saved for before anything is written (with
    the Queen on the ChatGPT login, the workers' endpoint), and one with no
    endpoint is refused. An empty one clears the active profile's key.

    R2: the probe sends the protocol and credential the save commits (F7);
    an upgraded home's adoption is staged in memory and written only with a
    save that passed (F6); staging and probing happen outside any snapshot,
    then the commit re-checks what it was staged on (F5). Raises
    ProviderError (ProviderConflict when staged-on state moved), having
    written nothing."""
    from ..codex_login import uses_codex_login
    from ..providers import ProviderError, migrate_legacy_provider, provider_host

    if base_url and provider_host(base_url) is None:
        raise ProviderError(f"base_url must be a valid http(s) URL, got {base_url!r}")
    staged = provider_state(store, home)
    view = _staged_view(store, home)  # P9/F6: adopted for staging, written only on commit
    active = view.active_provider_profile()
    saved_url, saved_model, saved_protocol = assistant_llm(view)
    target = (base_url or saved_url or "").strip().rstrip("/")
    wire = protocol or saved_protocol
    if api_key and not target:
        raise ProviderError(
            "save a base URL first — a key is tested against the endpoint it is for, "
            "and none is configured"
        )
    moved = active is not None and target != active.base_url.rstrip("/")
    # R3 (H3): nothing active on a non-empty registry (the active profile was
    # removed) — the save activates its target's profile, probed like a
    # switch; a bare clear only clears (it sends nothing anywhere)
    orphaned = (
        active is None
        and bool(target)
        and bool(view.list_provider_profiles())
        and (api_key != "" or (base_url, model, protocol) != (None, None, None))
    )
    same = target == (saved_url or "").rstrip("/")  # P8: the saved model only for its endpoint
    candidate, registered = (
        _endpoint_profile(view, target, protocol, wire, model or (saved_model if same else None))
        if moved or orphaned
        else (None, False)
    )
    codex = uses_codex_login(target)
    switched = (
        active is not None
        and protocol is not None
        and _registry_protocol(protocol) != active.protocol
    )
    # F2: the workers' endpoint, resolved once — probed and saved against
    workers = _workers_provider(home) if codex and api_key is not None else ("", "", "")
    # stage + probe: nothing below this block runs for a refused candidate
    try:
        if candidate is not None:
            model = _probe_or_refuse(
                view,
                home,
                base_url=target,
                model=model or candidate.model or None,
                protocol=REGISTRY_PROTOCOLS[candidate.protocol],
                api_key=_committed_key(view, home, target, candidate, registered, api_key),
                provider_id=candidate.provider_id,
            )
        elif (api_key and not codex) or switched:
            _probe_or_refuse(
                view,
                home,
                base_url=target,
                model=model or (saved_model if same else None),
                protocol=wire,
                api_key=_committed_key(view, home, target, active, active is not None, api_key),
            )
        if api_key and codex:
            _probe_workers_key(view, home, workers, api_key)
    except OllamaError as exc:  # an unusable Codex login, a refused token exchange
        raise ProviderError(f"{exc} — {_UNCHANGED}") from exc
    with provider_rollback(store, home, expect=staged):
        migrate_legacy_provider(store, home)  # F6: the staged adoption lands with the save
        pin_legacy_key(store, home)  # before LLM_BASE_URL follows the switch
        if candidate is not None:
            store.upsert_provider_profile(
                dataclasses.replace(candidate, model=model or candidate.model, active=True)
            )
        elif active is None:
            for key, value in ((LLM_BASE_URL, base_url), (LLM_DEFAULT_MODEL, model)):
                if value is not None:
                    store.set_setting(key, value or None)
            if protocol is not None:
                store.set_setting(LLM_PROTOCOL, protocol)
            migrate_legacy_provider(store, home)
        else:
            updated = dataclasses.replace(
                active,
                model=model or active.model,
                protocol=_registry_protocol(protocol) if protocol else active.protocol,
            )
            if updated != active:
                store.upsert_provider_profile(updated)
        if api_key is not None:  # saved for the endpoint it was probed against
            store_assistant_key(store, home, api_key, endpoint=workers[0] if codex else target)
        for key, value in (settings or {}).items():
            store.set_setting(key, value)
        _write_through_profile(store, home.parent)


def _workers_provider(home: Path) -> tuple[str, str, str]:
    """(endpoint, model, protocol name) of the provider workers keep while
    the Queen is on the ChatGPT login (ADR 0063: profile.json); empty when
    there is none."""
    from skep.profile import load_profile, profile_path

    if profile_path(home.parent).is_file():
        with contextlib.suppress(OSError, ValueError):
            provider = load_profile(home.parent).provider
            return (provider.endpoint or "").strip().rstrip("/"), provider.model, provider.name
    return "", "", ""


def _probe_workers_key(
    store: RunStore, home: Path, workers: tuple[str, str, str], api_key: str
) -> None:
    """P5: with the Queen on the ChatGPT login a saved key is the WORKERS'
    (ADR 0063) — probed against the provider they keep, the same endpoint
    ``store_assistant_key`` then saves it for (F2)."""
    from ..providers import ProviderError

    endpoint, model, name = workers
    if not (endpoint and model):
        raise ProviderError(
            "the assistant runs on your ChatGPT plan login, so a key here is the workers' — "
            "and workers have no provider to test it against: make another provider active "
            "once (`skep provider use <id>`)"
        )
    _probe_or_refuse(
        store,
        home,
        base_url=endpoint,
        model=model,
        protocol=_protocol(name),
        api_key=wire_key(endpoint, api_key),
    )


def _write_legacy(store: RunStore, home: Path, endpoint: str, value: str) -> None:
    """llm-secret := ``value``, owned by ``endpoint``; empty clears the key
    and its owner marker together (R2, F2: a cleared key left it stale)."""
    store_api_key(home, value)
    if value and endpoint:
        store.set_setting(LLM_SECRET_ENDPOINT, endpoint)
    else:
        store.delete_setting(LLM_SECRET_ENDPOINT)


def _keep_legacy_owner(store: RunStore, home: Path, endpoint: str) -> bool:
    """Before llm-secret is rewritten for ``endpoint``: a key it holds for
    ANOTHER endpoint is first kept in that endpoint's own profile file (Rule
    Zero; a file it already has wins). False when that endpoint has no
    profile to keep it in."""
    owner = legacy_key_endpoint(home, store)
    path = home / SECRET_FILE
    held = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not (held and owner and owner != endpoint):
        return True
    holder = _registered_for(store, owner)
    if holder is None:
        return False
    if not provider_secret_path(home, holder.provider_id).is_file():
        store_provider_api_key(home, holder.provider_id, held)
    return True


def _save_profile_key(store: RunStore, home: Path, profile: ProviderProfile, value: str) -> None:
    """``profile``'s own 0600 key file. A non-empty key clears the
    ``api_key_env`` that would shadow it (P7: the env var is read first, so
    a revoked one beat the key just tested) — in profile.json too when the
    profile is active (R2, F9: doctor still blocked on the cleared env var).
    For the ACTIVE profile, llm-secret mirrors it — never overwriting a key
    another endpoint owns unless that key is first kept in its owner's own
    file (Rule Zero)."""
    store_provider_api_key(home, profile.provider_id, value)
    if value and profile.api_key_env:
        store.upsert_provider_profile(dataclasses.replace(profile, api_key_env=None))
        if profile.active:
            _write_through_profile(store, home.parent)
    if not profile.active:
        return
    mine = profile.base_url.rstrip("/")
    if _keep_legacy_owner(store, home, mine):
        _write_legacy(store, home, mine, value)


def _save_endpoint_key(store: RunStore, home: Path, endpoint: str, value: str) -> None:
    """Phase 05 R2 (F2): a key saved for ``endpoint`` — the one it was probed
    against: the workers' while the Queen is on the ChatGPT login; R3 (H3)
    the save's target while nothing is active. Its registered profile's own
    file; llm-secret only when that endpoint has no profile, never
    overwriting a key another endpoint owns. Empty clears its key — and
    llm-secret only if it is that endpoint's."""
    from ..providers import ProviderError

    if value:
        _clear_workers_env(home, endpoint)
    registered = _registered_for(store, endpoint) if endpoint else None
    if registered is not None:
        _save_profile_key(store, home, registered, value)
        if value:
            return
    if not value:
        if legacy_key_endpoint(home, store) in ("", endpoint):
            _write_legacy(store, home, endpoint, "")
        return
    if not _keep_legacy_owner(store, home, endpoint):
        raise ProviderError(
            f"llm-secret holds the key for {legacy_key_endpoint(home, store)}, which has no "
            "provider profile to keep it in — register that endpoint first (`skep provider "
            f"add`), then save this key — {_UNCHANGED}"
        )
    _write_legacy(store, home, endpoint, value)


def _clear_workers_env(home: Path, endpoint: str) -> None:
    """R3 (H4): profile.json's endpoint just got a saved key — the env var
    it names would shadow it in every worker (read first), so it goes in the
    same commit. ``home`` is the supervisor home."""
    from skep.profile import load_profile, profile_path, run_personal_setup

    if not (endpoint and profile_path(home.parent).is_file()):
        return
    try:
        provider = load_profile(home.parent).provider
    except (OSError, ValueError):
        return  # unreadable: no worker resolved an endpoint from it
    if provider.api_key_env and (provider.endpoint or "").strip().rstrip("/") == endpoint:
        run_personal_setup(
            home.parent,
            provider=provider.name,
            model=provider.model,
            endpoint=provider.endpoint,
            api_key_env=None,
        )


def store_assistant_key(store: RunStore, home: Path, value: str, *, endpoint: str = "") -> None:
    """Phase 05 (5.1): a key saved from Settings is the ACTIVE profile's —
    its own 0600 file, which ``resolve_active_api_key`` reads first (a key
    saved only to llm-secret lost to an older per-profile file) — with
    ``llm-secret`` kept as the legacy mirror. Empty clears both. Otherwise
    it is ``endpoint``'s, the one the caller probed it against, resolved
    once: with the Queen on the ChatGPT login the WORKERS' (R2, F2); with
    nothing active, the save's target (R3, H3 — never llm-secret's old
    owner)."""
    from ..codex_login import uses_codex_login

    active = store.active_provider_profile()
    if active is not None and not uses_codex_login(active.base_url):
        _save_profile_key(store, home, active, value)
    else:
        _save_endpoint_key(store, home, endpoint, value)


def _write_through_profile(store: RunStore, personal_home: Path) -> None:
    """v19-F9: mirror the assistant's provider into ``profile.json`` — and,
    Phase 05, into the llm_* settings: with an active registry profile it is
    the source and both are mirrors written from it; with none, the staged
    settings are mirrored into profile.json as before.

    Only writes a complete config (base URL + model). ``api_key_env`` is None:
    the daemon manages its own secret in ``supervisor/llm-secret``, and
    ``_provider_check`` treats a missing ``api_key_env`` as "no key required".
    """
    from skep.profile import run_personal_setup

    active = store.active_provider_profile()
    if active is not None and active.protocol in REGISTRY_PROTOCOLS:
        wire = REGISTRY_PROTOCOLS[active.protocol]
        for key, value in (
            (LLM_BASE_URL, active.base_url),
            (LLM_DEFAULT_MODEL, active.model),
            (LLM_PROTOCOL, wire),
        ):
            if store.get_setting(key) != value:
                store.set_setting(key, value)
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
    # v115-F5: the field registry said deepseek-v4-pro while chat ran
    # glm-5.3. Phase 05 closes it from the other side: the registry's active
    # profile is the source (save_assistant_llm) and these are its mirrors.


def add_llm_routes(app: FastAPI, *, run_store: RunStore, home: Path) -> None:
    @app.get("/api/llm/config")
    def get_llm_config() -> dict[str, Any]:
        return llm_config_view(run_store, home)

    @app.put("/api/llm/config")
    def put_llm_config(body: LlmConfigUpdate) -> dict[str, Any]:
        from ..providers import ProviderConflict, ProviderError, migrate_legacy_provider

        # Phase 05 R1 (P6): the whole request is checked before anything is
        # written, then it lands whole — a failure part-way puts it all back.
        if body.num_ctx is not None and body.num_ctx != 0 and body.num_ctx < _MIN_NUM_CTX:
            raise HTTPException(
                status_code=400, detail=f"num_ctx must be >= {_MIN_NUM_CTX} (0 = auto)"
            )
        base_url = body.base_url.strip().rstrip("/") if body.base_url is not None else None
        model = body.default_model.strip() if body.default_model is not None else None
        key = body.api_key.strip() if body.api_key is not None else None
        settings: dict[str, Any] = {}
        if body.vision is not None:
            settings[LLM_VISION] = body.vision
        if body.num_ctx is not None:
            # v74-F2: 0 clears the override — back to auto (detected/default).
            settings[LLM_NUM_CTX] = body.num_ctx or None
        if body.tool_delivery is not None:
            settings[TOOL_DELIVERY_SETTING] = body.tool_delivery
        provider = (base_url, model, body.protocol, key) != (None, None, None, None)
        try:
            if provider:
                # a new key, endpoint or protocol is probed before any write
                # (I8) — outside any snapshot (R2, F5); the knobs land with it
                save_assistant_llm(
                    run_store,
                    home,
                    base_url=base_url,
                    model=model,
                    protocol=body.protocol,
                    api_key=key,
                    settings=settings,
                )
            else:
                with provider_rollback(run_store, home):
                    # P9: an upgraded home adopts what the Queen spoke before any mirror write
                    migrate_legacy_provider(run_store, home)
                    for name, value in settings.items():
                        run_store.set_setting(name, value)
                    # v19-F9: profile.json follows, so `skep doctor` agrees with the daemon
                    _write_through_profile(run_store, home.parent)
        except ProviderConflict as exc:  # R2 (F5): staged on state that moved
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProviderError as exc:  # P9: a bad URL or a refused probe is the caller's
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"the save failed ({exc}) — {_UNCHANGED}"
            ) from exc
        bare_clear = key == "" and (base_url, model, body.protocol) == (None, None, None)
        if provider and not bare_clear:
            # v74-F2: detection runs where the provider changes — R1 (F8): a
            # key or protocol too; failure is silent here (never breaks a
            # save) and visible in num_ctx_source (I8). A bare key clear sends
            # nothing (Phase 05 R3): there is no new credential to read with.
            current = assistant_llm(run_store)[1]
            if current:
                refresh_model_ctx(run_store, home, current)
        return llm_config_view(run_store, home)

    @app.post("/api/llm/test")
    def test_llm(body: LlmTestRequest) -> dict[str, Any]:
        """Always 200: the verdict is the payload, so the UI shows it inline."""
        saved_url, saved_model, saved_protocol = assistant_llm(run_store)
        base_url = (body.base_url or "").strip().rstrip("/") or saved_url
        if not base_url:
            return {"ok": False, "detail": "no base URL configured"}
        protocol = body.protocol or saved_protocol
        same = base_url.rstrip("/") == (saved_url or "").rstrip("/")
        try:
            from ..codex_login import uses_codex_login

            # v118-F1: chatgpt.com gets the Codex login, never a pasted or saved key.
            # P2: a saved key only for the endpoint it was saved for.
            pasted = (body.api_key or "").strip()
            api_key = (
                _codex_login_key(base_url)
                if uses_codex_login(base_url)
                # R2 (F10): a pasted key as the wire takes it (Copilot: exchanged)
                else wire_key(base_url, pasted)
                if pasted
                else saved_key_for(run_store, home, base_url)
            )
        except OllamaError as exc:
            return {"ok": False, "detail": str(exc)}
        # Phase 05 (5.1): the listing AND one 1-token completion — the probe
        # a dead key cannot pass (ollama.com lists models to anyone). P8: the
        # saved model only for its own endpoint; another lists its own.
        health = probe_candidate(
            run_store,
            home,
            base_url=base_url,
            model=(body.default_model or "").strip() or (saved_model if same else None),
            protocol=protocol,
            api_key=api_key,
        )
        if not health.ok:
            return {"ok": False, "detail": health.error, "http_status": health.http_status}
        return {"ok": True, "models": len(health.models)}

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
        base_url, _model, protocol = assistant_llm(run_store)
        if not base_url:
            raise HTTPException(status_code=409, detail="configure the LLM base URL first")
        try:
            # v108-F4: the active profile's own credential, when settings match it.
            api_key = resolve_active_api_key(run_store, home, base_url=str(base_url))
            return {"models": list_models(str(base_url), api_key, protocol=protocol)}
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
