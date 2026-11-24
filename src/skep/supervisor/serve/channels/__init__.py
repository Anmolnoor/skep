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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...codex_login import AUTH_SOURCE, CARD_LINE

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
    # v130-F7: the platform-authenticated sender (already in allowed_users);
    # channel commands use it (/whoami, the admin check).
    user_id: str = ""


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


def user_allowlisted(user_id: str, config: ChannelConfig) -> bool:
    """ES (B114): authority belongs to a PERSON, not a room. Every channel
    checks the author of every message and verdict against
    ``allowed_users``; an empty list admits nobody (fail closed — a channel
    id names everyone in it). ``skep doctor`` warns on an empty list."""
    return bool(user_id) and user_id in set(config.allowed_users)


def channel_admin(user_id: str, config: ChannelConfig) -> bool:
    """v130-F5 (ADR 0067): the person the operator named as this channel's
    admin — listed in ``admin_users`` AND still in ``allowed_users``."""
    return user_allowlisted(user_id, config) and user_id in set(config.admin_users)


def with_admins(
    config: ChannelConfig, users: Sequence[str], *, naming: bool = False
) -> ChannelConfig:
    """v130-F5: ``config`` with these admins — each must already be an allowed
    user (I9: the refusal names the list). ``naming`` (the operator is naming
    admins in this very request) turns confirming on, since an admin who
    cannot confirm is no admin; an explicit switch-off later is respected.
    Shared by the REST and CLI faces; the model has no face for it (an admin
    is the operator's own act, like ``trust``)."""
    from dataclasses import replace

    admins = tuple(dict.fromkeys(u.strip() for u in users if u.strip()))
    outside = [u for u in admins if u not in set(config.allowed_users)]
    if outside:
        raise ValueError(
            f"admin users must be allowed users first: {', '.join(outside)} not in allowed_users "
            f"({', '.join(config.allowed_users) or 'empty'})"
        )
    can_confirm = config.channel_can_confirm or (naming and bool(admins))
    return replace(config, admin_users=admins, channel_can_confirm=can_confirm)


def channel_confirmation_decision(
    *,
    action_class: str,
    channel_can_confirm: bool,
    identity_allowlisted: bool,
    admin: bool = False,
) -> ChannelConfirmationDecision:
    """Whether a pending action may be confirmed from a channel.

    Fail-closed allow-list: the action class must be one of the low-risk classes,
    the channel must be configured to confirm, and the identity must be
    allow-listed. Shell/policy/patch approvals are never in the allow-list, so
    they route to the web UI — unless the presser is the channel's admin
    (v130-F5, ADR 0067), who may confirm any class.
    """
    if action_class not in CHANNEL_CONFIRMABLE_ACTIONS and not admin:
        return ChannelConfirmationDecision(False, "channel.confirm.denied.web_ui_only_action_class")
    if not channel_can_confirm:
        return ChannelConfirmationDecision(
            False, "channel.confirm.denied.channel_not_confirm_enabled"
        )
    if not identity_allowlisted:
        return ChannelConfirmationDecision(False, "channel.confirm.denied.identity_not_allowlisted")
    if action_class not in CHANNEL_CONFIRMABLE_ACTIONS:
        return ChannelConfirmationDecision(True, "channel.confirm.allowed.admin")
    return ChannelConfirmationDecision(True, "channel.confirm.allowed.low_risk_action")


@dataclass(frozen=True)
class ConfirmCard:
    action_class: str
    commands: tuple[str, ...]
    web_ui_only: bool
    # P4-2 (ADR 0059 D6): plain lines from the server's card — a HIGH card's
    # "needs the web UI" and why, the chat's taint, every folded item.
    notes: tuple[str, ...] = ()
    high: bool = False
    too_long: bool = False  # a fold no messenger can show whole: web UI for everyone


# A messenger lists a folded card's items in full or not at all — Discord caps
# a message at 25 fields of 1024 characters, Telegram at 4096 characters. A
# fold past these limits is web-UI-only (and ``chat.verdict_events`` refuses
# a channel confirm of it): no reaction may approve an item nobody could read.
MESSENGER_FOLD_ITEMS = 15
MESSENGER_FOLD_CHARS = 2500
MESSENGER_ITEM_CHARS = 1000


def fold_fits_a_messenger(items: Sequence[str]) -> bool:
    """Can a messenger list every item of this fold, whole?"""
    return (
        len(items) <= MESSENGER_FOLD_ITEMS
        and sum(len(item) for item in items) <= MESSENGER_FOLD_CHARS
        and all(len(item) <= MESSENGER_ITEM_CHARS for item in items)
    )


def forge_lines(forged: Mapping[str, Any]) -> list[str]:
    """Review R1 (K6, I6, I8): everything a promote_tool confirm binds —
    the full source and lock digests, the approved hosts, every wheel pin
    and the source diff — as plain lines."""
    if "error" in forged:
        return [f"it cannot be installed as shown: {forged['error']}"]
    lock, hosts = forged.get("lock_sha256"), forged.get("approved_hosts")
    lines = [
        f"source sha256: {forged.get('source_sha256')}",
        f"lock sha256: {lock}" if lock else "lock: none (stdlib only, no venv)",
        f"network: {_hosts(hosts) if hosts else 'none (deny-all)'}",
    ]
    locked = [str(pin) for pin in forged.get("locked") or ()]
    if locked:
        lines.append(f"{len(locked)} hash-pinned wheel(s):")
        lines.extend(f"- {pin}" for pin in locked)
    diff = str(forged.get("diff") or "")
    lines.append("source diff:" if diff else "source diff: none (it installs the copy it runs)")
    lines.extend(diff.splitlines())
    return lines


def forge_fits_a_messenger(forged: Mapping[str, Any]) -> bool:
    """Can a messenger show a promote_tool card's whole binding?"""
    return sum(len(line) + 1 for line in forge_lines(forged)) <= MESSENGER_FOLD_CHARS


def card_notes(
    card: Mapping[str, Any] | None, *, messenger: bool = True, admin: bool = False
) -> tuple[str, ...]:
    """The lines a messenger shows from the server's card (``card_summary``):
    a HIGH card says it needs the web UI and why, then the taint line; a
    folded card lists every item it would approve — on a messenger only when
    it fits whole (``fold_fits_a_messenger``); the CLI (``messenger`` False)
    always lists them. Review R1 (K6): the risk line, and a promote_tool
    card's whole binding (``forge_lines``) — on a messenger only when it
    fits whole, else a pointer to the web UI or the CLI."""
    if not card:
        return ()
    notes: list[str] = []
    if card.get("tier") == "high" and admin:  # v130-F5
        notes.append(
            f"HIGH risk: {card.get('high') or 'see the web UI'}. Only this channel's admin "
            "can confirm it here."
        )
    elif card.get("tier") == "high":
        notes.append(
            f"HIGH risk: {card.get('high') or 'see the web UI'}. No messenger can confirm "
            "this: open the skep web UI or the CLI."
        )
    if card.get("resolves_to"):  # W38: where a linked path really reads
        notes.append(f"the path is a link: it resolves to {card['resolves_to']}")
    if card.get("taint"):
        notes.append(str(card["taint"]))
    if card.get("risk"):
        notes.append(f"risk: {card['risk']}")
    forged = card.get("forge")
    if isinstance(forged, Mapping):
        if messenger and not forge_fits_a_messenger(forged):
            notes.append(
                "this tool's source digest, lock, hosts and source diff are too long to show "
                "here: open the skep web UI or the CLI (skep chat) to read them whole and "
                "confirm there."
            )
        else:
            notes.extend(forge_lines(forged))
    manifest = card.get("manifest")
    if isinstance(manifest, Mapping):  # P4-2: what the dispatched run gets
        notes.extend(manifest_lines(manifest))
    schedule = card.get("schedule")
    if isinstance(schedule, Mapping):  # P4-2: what a script schedule really does
        notes.extend(schedule_lines(schedule))
    items = card.get("items")
    if isinstance(items, list) and len(items) > 1:
        lines = [str(item) for item in items]
        if messenger and not fold_fits_a_messenger(lines):
            notes.append(
                f"{len(items)} items, too many or too long to list here: open the skep "
                "web UI to read every one and confirm them there."
            )
        else:
            notes.append(f"{len(items)} items, one approval covers exactly these:")
            notes.extend(f"- {line}" for line in lines)
    return tuple(notes)


def _hosts(values: Any) -> str:
    return ", ".join(str(v) for v in values) if values else "none"


def _grant_lines(manifest: Mapping[str, Any]) -> list[str]:
    """W13: the task grants beyond hosts, commands and writes. A key a card
    from before W13 did not record is left out, never guessed."""
    lines: list[str] = []
    if "read" in manifest:
        lines.append(f"may read: {_hosts(manifest.get('read'))}")
    if "env_allowlist" in manifest:
        lines.append(f"environment: {_hosts(manifest.get('env_allowlist'))}")
    if manifest.get("allow_git_mutation"):
        lines.append("git: may commit and branch in its own worktree")
    if manifest.get("allowed_plugin_risks"):
        lines.append(f"plugin risks allowed: {_hosts(manifest['allowed_plugin_risks'])}")
    if "allowed_tools" in manifest:
        tools = manifest.get("allowed_tools")
        lines.append(f"tools: {'all its tools' if tools is None else _hosts(tools)}")
    if manifest.get("auto_apply_verified_patch"):
        branch = manifest.get("auto_apply_branch") or "a new skep/<task> branch"
        lines.append(f"auto-apply: a re-verified patch lands on {branch} without asking")
    return lines


def manifest_lines(manifest: Mapping[str, Any]) -> list[str]:
    """A dispatch card's manifest as plain lines (``actions.dispatch_manifest``).
    A long shell list is counted, with the first few named. D23: a batch's
    ``members`` are listed run by run."""
    members = manifest.get("members")
    if isinstance(members, list):
        return [
            line
            for index, member in enumerate(members, 1)
            for line in [f"run {index} of {len(members)}:", *manifest_lines(member)]
        ]
    if manifest.get("error"):
        return [f"the run's envelope could not be compiled: {manifest['error']}"]
    prefixes = [" ".join(map(str, p)) for p in manifest.get("shell_allowlist") or []]
    shown = ", ".join(prefixes[:8]) + (f" (+{len(prefixes) - 8} more)" if len(prefixes) > 8 else "")
    lines = [
        f"hosts: {_hosts(manifest.get('network'))}",
        *([str(manifest["network_note"])] if manifest.get("network_note") else []),  # D10
        *(
            [f"adds to the project's hosts for this run: {_hosts(manifest['network_added'])}"]
            if manifest.get("network_added")
            else []
        ),
        *(
            [f"loses the project's own hosts for this run: {_hosts(manifest['network_dropped'])}"]
            if manifest.get("network_dropped")
            else []
        ),
        f"model provider: {_hosts(manifest.get('provider_hosts'))}",
        *(
            [f"loopback ports: {', '.join(map(str, manifest['loopback_ports'])) or 'closed'}"]
            if "loopback_ports" in manifest
            else []
        ),
        f"commands it may run: {shown or 'none'}",
        f"may write: {_hosts(manifest.get('write'))}",
        *_grant_lines(manifest),
        f"engine: {manifest.get('engine')} · mode: {manifest.get('execution_mode')}"
        f" · verify: {manifest.get('verify_command') or 'the worker picks'}",
        *([f"login: {CARD_LINE}"] if manifest.get("engine_auth") == AUTH_SOURCE else []),
    ]
    asks = [
        *(manifest.get("network_ask") or []),
        *(" ".join(map(str, p)) for p in manifest.get("shell_ask") or []),
    ]
    if asks:
        lines.append(
            "approving this also approves, for this run only: " + ", ".join(map(str, asks))
        )
    if manifest.get("chain"):
        lines.append("delegated by: " + " -> ".join(map(str, manifest["chain"])))
    if manifest.get("taint"):
        lines.append("from a chat that has: " + " + ".join(map(str, manifest["taint"])))
    return lines


def schedule_lines(facts: Mapping[str, Any]) -> list[str]:
    """The schedule card's facts as plain lines (``cards.schedule_facts``)."""
    lines: list[str] = []
    if facts.get("command"):
        lines.append(f"command: {facts['command']}")
    if facts.get("every"):
        lines.append(f"every: {facts['every']}")
    if facts.get("next"):
        lines.append("next runs: " + ", ".join(str(t) for t in facts["next"]))
    if facts.get("error"):
        lines.append(f"will not save: {facts['error']}")
    if "env" in facts:
        lines.append("environment: " + (", ".join(facts["env"]) or "(empty)"))
    if facts.get("chain"):
        lines.append("reads the output of: " + " <- ".join(str(c) for c in facts["chain"]))
    if facts.get("replaces"):
        lines.append(f"replaces {facts['replaces']}")
    if facts.get("runs"):
        lines.append(str(facts["runs"]))
    return lines


def render_confirm_card(
    action_class: str,
    commands: Sequence[Sequence[str]] = (),
    card: Mapping[str, Any] | None = None,
    *,
    admin: bool = False,
) -> ConfirmCard:
    """A platform-neutral confirm-card view. Renders the v19-F1 multi-command
    payload as a LIST of command strings (never a single joined string).
    P4-2: a HIGH ``card`` is web-UI-only whatever its class (the server
    refuses a channel confirm of it too: ``chat.verdict_events``). v130-F5:
    on a channel with an admin (``admin``) every class and HIGH card carries
    buttons — the gate and the server let only the admin's press through."""
    from shlex import join as _join

    command_lines = tuple(_join([str(part) for part in command]) for command in commands)
    high = bool(card) and card.get("tier") == "high"  # type: ignore[union-attr]
    items = [str(item) for item in ((card or {}).get("items") or [])]
    forged = (card or {}).get("forge")
    too_long = (len(items) > 1 and not fold_fits_a_messenger(items)) or (
        isinstance(forged, Mapping) and not forge_fits_a_messenger(forged)
    )
    web_ui_only = too_long or (
        not admin and (action_class not in CHANNEL_CONFIRMABLE_ACTIONS or high)
    )
    headline = (card or {}).get("headline")
    # v129-F5: a card the phone may confirm shows what it acts on (a
    # read_url's target): no blind approvals. A web-only card keeps its
    # headline at the desk; a fold lists its items in the notes.
    lead = (str(headline),) if headline and not web_ui_only and len(items) <= 1 else ()
    return ConfirmCard(
        action_class=action_class,
        commands=command_lines,
        web_ui_only=web_ui_only,
        notes=lead + card_notes(card, admin=admin),
        high=high,
        too_long=too_long,
    )


def confirm_off_line(channel: str, view: ConfirmCard, config: ChannelConfig) -> str | None:
    """v129-F5 (I9): a pointer that is one setting away from buttons says so
    — "never from a phone" and "you turned it off" must read differently.
    None for a card that is web-UI-only by design, or when buttons show."""
    if view.web_ui_only or config.channel_can_confirm:
        return None
    return (
        f"Confirming from {channel.capitalize()} is off for this channel (channel_can_confirm). "
        "Turn it on in the web UI's channel settings to confirm cards like this here."
    )


def admin_nudge_line(channel: str, view: ConfirmCard, config: ChannelConfig) -> str | None:
    """v130-F5 (I9): a web-only card on a channel with no admin says how to get
    every card here — named once, set only from the desk (never the channel)."""
    if not view.web_ui_only or view.too_long or config.admin_users:
        return None
    who = config.allowed_users[0] if len(config.allowed_users) == 1 else "<your user id>"
    return (
        f"To approve every card from {channel.capitalize()}, make yourself this channel's "
        f"admin: `skep channel admin {channel} {who}` or Settings, Channels, Admin users."
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
    # allowed_users: the AUTHOR must be listed, on every channel (ES/B114:
    # empty = nobody; fail closed) — and a Discord DM from a listed user is
    # admitted without its channel id being pre-allowlisted.
    require_mention: bool = False
    auto_thread: bool = False
    allowed_users: tuple[str, ...] = ()
    # v78-F1: "all" preserves pre-v78 behavior byte-for-byte.
    notification_level: str = "all"
    # v130-F5 (ADR 0067): people (a subset of allowed_users) who may confirm
    # every card from this channel. Set only from the web UI, REST or CLI.
    admin_users: tuple[str, ...] = ()


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


def channel_actor(identity: ChannelIdentity, admin_user: str | None = None) -> str:
    """The audit actor string for an action confirmed from a channel — the
    approval records WHO (which channel identity) confirmed it, not a generic
    'channel' (v16 Step 6). v130-F5: an admin's press names the admin."""
    actor = f"channel:{identity.channel}:{identity.identity_id}"
    return f"{actor}:admin:{admin_user}" if admin_user else actor


def channel_admin_actor(actor: str, config_for: Any) -> bool:
    """v130-F5: does ``actor`` name a person who is this channel's admin NOW?
    The server re-asks on every confirm (``config_for(channel)`` reads the
    stored config), so a name removed since the press counts for nothing."""
    parts = actor.split(":")
    if len(parts) != 5 or parts[0] != "channel" or parts[3] != "admin":
        return False
    config = config_for(parts[1])
    return config is not None and channel_admin(parts[4], config)


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
