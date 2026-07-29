"""The in-process scheduler ticker (v5 Stage D / A4).

``skep tick`` is cron-shaped, and cron does not translate into a container.
The ticker is the same ``run_due`` call on a timer thread owned by the serve
process: killable on shutdown, never two ticks concurrently (one thread, one
sequential loop), and it re-reads its interval every cycle so a policy edit
takes effect without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..providers import ProviderProfile

if TYPE_CHECKING:
    from .llm import LLMProtocol
from ..scheduler import run_due, run_provider_health_checks
from ..store import RunStore
from .settings import (
    CARD_TIMEOUT_SECONDS,
    DEFAULT_CARD_TIMEOUT,
    DEFAULT_TICKER_INTERVAL,
    TICKER_INTERVAL_SECONDS,
    ConfigHolder,
    _stored_int,
)

logger = logging.getLogger("skep.serve")

# v59-F6: how often the ticker re-probes provider health. Before this wiring
# run_provider_health_checks had no production caller at all — provider_health
# stayed empty forever and every dispatch routed on "no_healthy_provider".
PROVIDER_HEALTH_INTERVAL_SECONDS = "provider_health_interval_seconds"
DEFAULT_PROVIDER_HEALTH_INTERVAL = 300

# Registry protocol → serve list_models protocol; probes support these two.
_PROBE_PROTOCOLS: dict[str, LLMProtocol] = {"ollama": "ollama", "openai_compat": "openai-compat"}


def _probe_list_models(home: Path) -> Callable[[ProviderProfile], list[str]]:
    """Production ``list_models`` for health checks: the profile's endpoint,
    its env credential when named, the daemon's llm-secret otherwise."""
    from .llm import list_models, resolve_api_key

    def _list(profile: ProviderProfile) -> list[str]:
        protocol = _PROBE_PROTOCOLS.get(profile.protocol)
        if protocol is None:
            raise RuntimeError(f"health probe not supported for protocol {profile.protocol!r}")
        if profile.api_key_env:
            api_key: str | None = os.environ.get(profile.api_key_env) or None
        else:
            api_key = resolve_api_key(home)
        return list_models(profile.base_url, api_key, protocol=protocol)

    return _list


class Ticker(threading.Thread):
    """Call ``run_due`` every interval until stopped."""

    def __init__(
        self, holder: ConfigHolder, store: RunStore, runner: object | None = None
    ) -> None:
        super().__init__(name="serve-ticker", daemon=True)
        self._holder = holder
        self._store = store
        # v83-F5: the Dispatcher, so 'prompt' schedules can run a read-only
        # Queen turn at tick time. None (tests, CLI-shaped callers) keeps the
        # ticker engine-free and prompt ticks fail honestly in run_due.
        self._runner = runner
        self._stop_event = threading.Event()
        # v59-F6: 0.0 → the first tick probes immediately (fresh serve start).
        self._last_health_probe = 0.0
        # v72-F3: last known health per provider, for once-per-transition
        # pushes. In-memory on purpose: a restart re-alarms only while the
        # provider is STILL down — a fresh reminder, not a duplicate.
        self._provider_ok: dict[str, bool] = {}

    def _interval(self) -> float:
        value = self._store.get_setting(TICKER_INTERVAL_SECONDS)
        if isinstance(value, int) and value >= 1:
            return float(value)
        return float(DEFAULT_TICKER_INTERVAL)

    def _notify(self, chat_id: str, text: str, kind: str = "info") -> None:
        # v44-F2: scheduled messages bound to a messenger chat get pushed out
        # to that messenger. Lazy import keeps the supervisor core (scheduler,
        # CLI tick) transport-free. kind (v78-F1) threads the delivery
        # classification to the notification_level gate.
        from .channels.outbound import push_to_chat_channel

        push_to_chat_channel(self._store, self._holder.current.home, chat_id, text, kind=kind)

    def _expire_cards(self) -> None:
        """v54-F1 (ADR 0032): auto-DENY proposed cards older than the timeout.

        Deny only, never confirm — the model never holds the trigger (ADR 0019);
        a timeout is the human not pulling it, and the safe default is to not
        execute. ``card_timeout_seconds = 0`` disables the sweep.
        """
        timeout = _stored_int(
            self._store.get_setting(CARD_TIMEOUT_SECONDS), DEFAULT_CARD_TIMEOUT, minimum=0
        )
        if timeout <= 0:
            return
        payload = {
            "ok": False,
            "denied": True,
            "note": "auto-denied: card timed out",
            "auto": True,
        }
        for card in self._store.pending_cards_older_than(timeout):
            # v87-F2: a gate mirror has no timeout of its own — the question
            # lives in the approvals ledger until the operator answers (ADR
            # 0038), and the supersede reconciliation is its only other exit.
            if card.source == "gate":
                continue
            # v63-F2: a card whose underlying review/run already resolved
            # through another surface records the truth, never "timed out".
            note = self._store.card_resolution_elsewhere(card)
            if note is not None:
                superseded = {"ok": True, "superseded": True, "note": note}
                try:
                    self._store.resolve_chat_action(
                        card.action_id, status="superseded", result=superseded
                    )
                except (KeyError, ValueError):
                    continue  # resolved concurrently — that verdict stands
                self._store.add_chat_message(
                    card.chat_id,
                    role="tool",
                    tool_name=card.tool,
                    content=json.dumps(superseded, ensure_ascii=True),
                )
                continue
            try:
                self._store.resolve_chat_action(card.action_id, status="denied", result=payload)
            except (KeyError, ValueError):
                continue  # resolved concurrently — that verdict stands
            # Same transcript shape as a manual deny: the model sees the denial
            # in history and responds on the user's next message (no SSE here —
            # the ticker has no ChatEngine, and an unasked continuation would
            # surprise).
            self._store.add_chat_message(
                card.chat_id,
                role="tool",
                tool_name=card.tool,
                content=json.dumps(payload, ensure_ascii=True),
            )
            self._notify(
                card.chat_id,
                f"⏰ card auto-denied: {card.tool} — timed out after {timeout}s",
            )

    def _probe_provider_health(self) -> None:
        """v59-F6: record real provider health on an interval so routing has
        data — the checks existed since v14 but nothing ever called them.
        ``provider_health_interval_seconds = 0`` disables the sweep."""
        interval = _stored_int(
            self._store.get_setting(PROVIDER_HEALTH_INTERVAL_SECONDS),
            DEFAULT_PROVIDER_HEALTH_INTERVAL,
            minimum=0,
        )
        if interval <= 0:
            return
        now = time.monotonic()
        if self._last_health_probe and now - self._last_health_probe < interval:
            return
        self._last_health_probe = now
        home = self._holder.current.home
        for health in run_provider_health_checks(
            self._store, list_models=_probe_list_models(home)
        ):
            ok = bool(health.reachable and health.model_found)
            if not ok:
                logger.warning(
                    "provider %r unhealthy: %s", health.provider_id, health.error
                )
            self._push_provider_transition(health.provider_id, ok=ok, error=health.error)

    def _push_provider_transition(
        self, provider_id: str, *, ok: bool, error: str | None
    ) -> None:
        """v72-F3 (R5): a dead Queen provider is "come to me" news — push ONCE
        per healthy→unhealthy transition (never per probe; an alarm fires for
        an actual failure, once — I8), and once more when it recovers."""
        previous = self._provider_ok.get(provider_id)
        self._provider_ok[provider_id] = ok
        if previous is ok or (previous is None and ok):
            return
        if ok:
            text = f"provider {provider_id!r} is healthy again"
        else:
            detail = f": {error}" if error else ""
            text = f"⚠ provider {provider_id!r} is unhealthy{detail} — chats and runs may fail"
        chat_id = self._store.latest_channel_chat()
        if chat_id is not None:
            self._store.add_chat_message(chat_id, role="assistant", content=text)
            self._notify(chat_id, text)
        else:  # no messenger has ever bound a chat — the durable note is the record
            self._store.create_note(text, actor="provider-health")

    def _prompt_turn(self) -> Callable[..., tuple[str, bool]] | None:
        """v83-F5: the run_due hook for 'prompt' schedules — a read-only Queen
        turn in the bound chat. Lazy import keeps the core scheduler
        transport-free; no runner → None → the tick fails honestly."""
        if self._runner is None:
            return None
        from .chat import run_scheduled_prompt
        from .jobs import Dispatcher

        runner = self._runner
        assert isinstance(runner, Dispatcher)

        def _turn(schedule: object, chained: str | None) -> tuple[str, bool]:
            return run_scheduled_prompt(
                self._store,
                self._holder,
                runner,
                self._holder.current.home,
                schedule,
                chained,
            )

        return _turn

    def run(self) -> None:
        # wait() doubles as the kill switch: it returns True the moment stop()
        # sets the event, so shutdown never waits out a full interval.
        while not self._stop_event.wait(self._interval()):
            try:
                # v54-F1: stale-card sweep before schedules; a broken sweep
                # must not break ticking.
                self._expire_cards()
            except Exception:
                logger.exception("card timeout sweep failed")
            try:
                # v59-F6: provider health rides the tick (throttled inside).
                self._probe_provider_health()
            except Exception:
                logger.exception("provider health sweep failed")
            try:
                # v71-F5: observations expire on the tick — the fluid memory
                # lane earns its no-proposal write by never outliving its TTL.
                from ..memory import OBSERVATION_TTL_DAYS

                expired = self._store.expire_observations(ttl_days=OBSERVATION_TTL_DAYS)
                if expired:
                    logger.info("expired %d observation(s)", len(expired))
            except Exception:
                logger.exception("observation TTL sweep failed")
            try:
                # v72-F4: the harvest — observation-shaped chat lines and run
                # terminals feed the lane the TTL sweep above empties.
                from ..observe import harvest_observations

                for harvested in harvest_observations(self._store):
                    logger.info("harvested observation: %s", harvested)
            except Exception:
                logger.exception("observation harvest sweep failed")
            try:
                results = run_due(
                    store=self._store,
                    config=self._holder.current,
                    notify=self._notify,
                    prompt_turn=self._prompt_turn(),
                )
            except Exception:  # one broken tick must never kill the daemon
                logger.exception("scheduler tick failed")
                continue
            for result in results:
                logger.info("tick ran %r: %s (%s)", result.name, result.task_id, result.state)
            try:
                # v53-F1: the conversation-skill observer rides the tick —
                # opt-in (checked inside), heuristic-only, never in the
                # request path. A broken sweep must not break ticking.
                from ..observe import observe_conversations

                for draft in observe_conversations(self._store):
                    logger.info("observer proposed skill draft %r", draft)
            except Exception:
                logger.exception("conversation-skill sweep failed")

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)
