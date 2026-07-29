"""v16 Step 1: the channel adapter contract.

Channels (Discord/Telegram/Slack) are *entrances only* — they route into the
exact same Queen chat -> action -> approval -> audit flow as the web UI and can
never bypass the trust engine. This module is the shared, platform-agnostic
contract and — the security core — the confirmation gating:

- Unknown identities fail closed.
- Channel confirmation is an ALLOW-LIST of low-risk action classes. Shell-command
  approvals and policy changes can NEVER be confirmed from any channel, whatever
  the identity or ``channel_can_confirm`` — a compromised chat account must not be
  able to approve arbitrary commands. Those get a "review in the web UI" link.
- The v19-F1 multi-command approval payload renders as a list, not one string.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

CHANNELS: frozenset[str] = frozenset({"discord", "telegram", "slack"})

# The ONLY action classes a channel may confirm (fail-closed allow-list). Shell
# approvals, policy changes, patch landings, git/PR mutations, etc. are
# deliberately absent — they are web-UI-only, whatever the config says.
# v66-F2: read_url (a supervisor-side scoped network read, audited by the v52
# operator-policy layer) and start_research (dispatch_run for the read-only
# researcher caste — same envelope, same gates) join the low-risk set; the
# field pain was a Discord chat locked behind web-UI round-trips for every
# read_url card (the #2 carded class on the operator's store).
CHANNEL_CONFIRMABLE_ACTIONS: frozenset[str] = frozenset(
    {"dispatch_run", "scheduled_result_ack", "read_url", "start_research"}
)


@dataclass(frozen=True)
class ChannelIdentity:
    channel: str
    identity_id: str  # platform user/chat/workspace id


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    identity: ChannelIdentity
    text: str
    session_key: str  # stable per (channel, conversation) -> one chat session
    # v44-F9: raw image payloads the transport already downloaded (size/type
    # gated at ingest); run_channel_turn persists them onto the user message.
    attachments: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class ChannelConfirmationCapability:
    channel_can_confirm: bool = False


@dataclass(frozen=True)
class ChannelSessionBinding:
    channel: str
    identity_id: str
    chat_id: str
    # v78-F6: the messenger-side thread anchor (Slack thread_ts) outbound
    # pushes reply under; None = top-level sends, exactly as before.
    thread_ref: str | None = None


@dataclass(frozen=True)
class ChannelDeliveryResult:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ChannelConfirmationDecision:
    allowed: bool
    reason: str


def identity_allowlisted(identity: ChannelIdentity, allowlist: Sequence[str]) -> bool:
    """Fail closed: an identity is admitted only if explicitly allow-listed."""
    return identity.identity_id in set(allowlist)


def channel_confirmation_decision(
    *,
    action_class: str,
    channel_can_confirm: bool,
    identity_allowlisted: bool,
) -> ChannelConfirmationDecision:
    """Whether a pending action may be confirmed from a channel.

    Fail-closed allow-list: the action class must be one of the low-risk classes,
    the channel must be configured to confirm, and the identity must be
    allow-listed. Shell/policy/patch approvals are never in the allow-list, so
    they are never confirmable from a channel — they route to the web UI.
    """
    if action_class not in CHANNEL_CONFIRMABLE_ACTIONS:
        return ChannelConfirmationDecision(
            False, "channel.confirm.denied.web_ui_only_action_class"
        )
    if not channel_can_confirm:
        return ChannelConfirmationDecision(
            False, "channel.confirm.denied.channel_not_confirm_enabled"
        )
    if not identity_allowlisted:
        return ChannelConfirmationDecision(
            False, "channel.confirm.denied.identity_not_allowlisted"
        )
    return ChannelConfirmationDecision(True, "channel.confirm.allowed.low_risk_action")


@dataclass(frozen=True)
class ConfirmCard:
    action_class: str
    commands: tuple[str, ...]
    web_ui_only: bool


def render_confirm_card(
    action_class: str, commands: Sequence[Sequence[str]] = ()
) -> ConfirmCard:
    """A platform-neutral confirm-card view. Renders the v19-F1 multi-command
    payload as a LIST of command strings (never a single joined string)."""
    from shlex import join as _join

    command_lines = tuple(_join([str(part) for part in command]) for command in commands)
    return ConfirmCard(
        action_class=action_class,
        commands=command_lines,
        web_ui_only=action_class not in CHANNEL_CONFIRMABLE_ACTIONS,
    )


# v78-F2: the shared state-emoji vocabulary — applied ONCE at the source
# (run_terminal_text prefixes its line), so every consumer (web transcript,
# all three channels, the scheduler funnel) inherits it from one place. On a
# phone lock screen "run 0f3a… worker_crashed" and "run 0f3a… completed" read
# identically until opened; the glyph is the tell.
STATE_EMOJI: dict[str, str] = {
    "completed": "🟢",
    "failed": "🔴",
    "rejected": "🔴",
    "worker_crashed": "🔴",
    "worker_timeout": "🔴",
    "running": "🟡",
    "dispatched": "🟡",
    "pending_approval": "🟡",
}


def state_emoji(state: str) -> str:
    return STATE_EMOJI.get(state, "⚪")


# v78-F1: the per-channel volume dial. Filters DELIVERY only — the chat row
# and the web UI record everything whatever the level; it can silence pushes,
# never allow anything. "approvals" delivers only pushes classified
# action-needed (pending gate, unlanded patch, G10 disagreement, resumable
# crash); "none" delivers nothing.
NOTIFICATION_LEVELS: tuple[str, ...] = ("all", "approvals", "none")


@dataclass(frozen=True)
class ChannelConfig:
    channel: str
    enabled: bool = False
    channel_can_confirm: bool = False
    allowed_identities: tuple[str, ...] = ()
    # v44-F1 (Discord routing parity). All three default to today's behavior.
    # require_mention: guild messages in an allow-listed channel need a bot
    # @mention (threads skep created and DMs are exempt). auto_thread: a routed
    # guild mention spawns a thread and the conversation moves there.
    # allowed_users: when non-empty, the AUTHOR must also be listed (fail
    # closed) — and a DM from a listed user is admitted without its channel id
    # being pre-allowlisted (you can't know a DM channel id in advance).
    require_mention: bool = False
    auto_thread: bool = False
    allowed_users: tuple[str, ...] = ()
    # v78-F1: "all" preserves pre-v78 behavior byte-for-byte.
    notification_level: str = "all"


# -- secrets (v16 Step 2): 0600 files beside the serve token, never in GET -----
# v26-F1: a channel may need more than one secret (Slack: bot token + signing
# secret) — ``part`` names the extra ones; the default stays byte-compatible.

# Channels with a wired live transport. Telegram long-polls and Slack receives
# signed webhooks (v26); Discord runs a gateway websocket (v37-F4) — messages,
# reactions, AND button interactions arrive over the one socket, so the
# Ed25519 interactions webhook was never needed.
LIVE_CHANNELS: frozenset[str] = frozenset({"telegram", "slack", "discord"})


def channel_secret_path(home: Path, channel: str, part: str | None = None) -> Path:
    name = channel if part is None else f"{channel}-{part}"
    return home / f"{name}-secret"


def channel_secret_env(channel: str, part: str | None = None) -> str:
    name = channel if part is None else f"{channel}_{part}"
    return f"SKEP_{name.upper()}_SECRET"


def resolve_channel_secret(home: Path, channel: str, part: str | None = None) -> str | None:
    env = os.environ.get(channel_secret_env(channel, part), "").strip()
    if env:
        return env
    path = channel_secret_path(home, channel, part)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def store_channel_secret(home: Path, channel: str, value: str, part: str | None = None) -> None:
    """Persist (or, for an empty value, remove) a channel secret — 0600."""
    path = channel_secret_path(home, channel, part)
    if not value:
        path.unlink(missing_ok=True)
        return
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def adapter_ready(config: ChannelConfig, secret: str | None) -> bool:
    """An adapter runs only when enabled AND its secret is present — a missing
    secret disables the adapter cleanly (no half-configured channel)."""
    return config.enabled and bool(secret)


def channel_actor(identity: ChannelIdentity) -> str:
    """The audit actor string for an action confirmed from a channel — the
    approval records WHO (which channel identity) confirmed it, not a generic
    'channel' (v16 Step 6)."""
    return f"channel:{identity.channel}:{identity.identity_id}"


def channel_origin_metadata(message: ChannelMessage) -> dict[str, str]:
    """Origin metadata to attach to a channel-originated run/session. It is pure
    evidence: origin never changes the policy answer — a channel run goes through
    the same run_task/dispatch path and gets the same decision as a web/CLI run."""
    return {
        "origin": "channel",
        "channel": message.channel,
        "identity": message.identity.identity_id,
        "session_key": message.session_key,
    }


@dataclass(frozen=True)
class NormalizedInbound:
    """The result of an adapter parsing a platform payload: either a routable
    message, or a rejection (unknown identity fails closed)."""

    message: ChannelMessage | None
    rejected_reason: str | None = None
    audit: dict[str, str] = field(default_factory=dict)
