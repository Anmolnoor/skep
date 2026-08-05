"""Run store, approval queue, and audit tables.

Built from durable-state concepts: run records with append-only state
transitions, a FIFO HITL approval queue with actor + timestamp on resolution,
and an event table deduplicated on
``event_id`` and ordered on ``seq`` (spec §4 idempotency). Single writer, SQLite
WAL (G4: one worker in v1; the v3 entry gate revisits concurrency).
"""

from __future__ import annotations

import contextlib
import functools
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from skep.worker_contract import TERMINAL_STATES, CodingWorkerResult, CodingWorkerTask, Event

from .memory import (
    MemoryError,
    MemoryItem,
    MemoryProposal,
    MemorySource,
    require_transition,
    validate_memory_class,
    validate_proposal_state,
    validate_source_kind,
)
from .nodes import Node, validate_node
from .providers import ProviderHealth, ProviderProfile, validate_provider_profile
from .serve.channels import (
    CHANNELS,
    NOTIFICATION_LEVELS,
    ChannelConfig,
    ChannelSessionBinding,
)
from .skills import SkillCandidate
from .templates import TemplateParam, WorkflowTemplate, template_from_dict, template_to_dict


def _created_transition_detail(task: CodingWorkerTask) -> str | None:
    detail: dict[str, Any] = {}
    if task.project_context is not None:
        detail["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        detail["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    if task.landing_decision is not None:
        detail["landing_decision"] = task.landing_decision.model_dump(mode="json")
    if not detail:
        return None
    return json.dumps(detail, ensure_ascii=True)


def _locked[F: Callable[..., Any]](method: F) -> F:
    """Serialize a RunStore method on its connection lock (G4 single writer).

    v3 dispatches workers in parallel (Stage F), so several threads call into one
    shared store. SQLite WAL permits concurrent readers but a Python ``Connection``
    is not safe to use from multiple threads at once; we therefore funnel every
    operation through a single connection guarded by one re-entrant lock — the
    literal "single writer process" of decision G4. The lock is held only for the
    brief duration of each statement/transaction, never across a worker's runtime.
    """

    @functools.wraps(method)
    def wrapper(self: RunStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(F, wrapper)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    task_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    ref TEXT,
    workspace TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'sandbox',
    instructions TEXT NOT NULL,
    state TEXT NOT NULL,
    summary TEXT,
    verification_outcome TEXT,
    verification_details TEXT,
    worker_version TEXT,
    manifest_fingerprint TEXT,
    resume_of TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    base_commit TEXT,
    worker_kind TEXT,
    coding_engine TEXT
);
CREATE TABLE IF NOT EXISTS run_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events (task_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    audit_path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    purpose TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    review_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_note TEXT,
    commands_json TEXT,
    -- v30: the branch an apply_patch approval actually landed on (skep/<task_id>
    -- by default, an operator-named branch, or a project integration branch).
    -- Reported as applied_branch instead of the old hardcoded skep/<task_id>.
    landing_branch TEXT,
    -- v40-F8 (v36-F4): the policy rule that routed this gate,
    -- "<template>/<rule_id>" (or "auto/<rule>" for auto-approvals).
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status, requested_at);
CREATE TABLE IF NOT EXISTS approval_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT UNIQUE,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    reason TEXT NOT NULL,
    instructions_snippet TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    template_name TEXT,
    approved_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    task_outcome TEXT,
    remembered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_approval_ledger_repo ON approval_ledger (repo_path, id);
CREATE INDEX IF NOT EXISTS idx_approval_ledger_task ON approval_ledger (task_id, id);
CREATE TABLE IF NOT EXISTS reverifications (
    task_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    worker_outcome TEXT,
    confirmed INTEGER NOT NULL,
    commands_json TEXT NOT NULL,
    exit_codes_json TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_usage (
    task_id TEXT PRIMARY KEY,
    provider_calls INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at TEXT NOT NULL
);
-- v69-F4 (R12a): operator steering notes sent into a RUNNING react loop.
-- Input, never authority: a note resolves no card, approval, or gate.
CREATE TABLE IF NOT EXISTS run_steering (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_steering_task ON run_steering (task_id, id);
CREATE TABLE IF NOT EXISTS schedules (
    name TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ref TEXT,
    worker_kind TEXT NOT NULL,
    instructions TEXT NOT NULL,
    network_json TEXT NOT NULL,
    env_allow_json TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    last_task_id TEXT,
    last_state TEXT,
    -- v3.5: a schedule may bind to a template; tick re-instantiates it live.
    template_name TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    -- v43-F6: 'note' schedules created from a chat deliver into that chat.
    chat_id TEXT,
    -- v44-F2: a one-shot schedule disables itself after its first fire.
    once INTEGER NOT NULL DEFAULT 0,
    -- v53-F5: cron context chaining — B reads A's last stored output.
    chain TEXT,
    last_output TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (enabled, next_run_at);
CREATE TABLE IF NOT EXISTS schedule_health (
    name TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_reason TEXT,
    disabled_reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_name TEXT NOT NULL,
    task_id TEXT,
    state TEXT NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedule_health_events
    ON schedule_health_events (schedule_name, id);
CREATE TABLE IF NOT EXISTS templates (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    worker_kind TEXT NOT NULL,
    instructions TEXT NOT NULL,
    params_json TEXT NOT NULL,
    repo TEXT,
    ref TEXT,
    network_json TEXT NOT NULL,
    env_allow_json TEXT NOT NULL,
    shell_allow_json TEXT NOT NULL DEFAULT '[]',
    allow_git_mutation INTEGER NOT NULL DEFAULT 0,
    wall_clock_seconds INTEGER NOT NULL,
    max_iterations INTEGER NOT NULL,
    max_actions INTEGER NOT NULL,
    max_provider_calls INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    -- v4: 'user' (hand-authored, v3.5) or 'learned' (generated + human-approved).
    provenance TEXT NOT NULL DEFAULT 'user'
);
-- v4: learned-skill candidates in the draft -> tested -> approved pipeline. They
-- live here, NOT in `templates`: a draft/tested candidate is deliberately outside
-- the registry, so it cannot be run or scheduled until a human approves it (which
-- inserts its recipe into `templates`). The recipe is an opaque JSON payload at this
-- stage; the explicit columns are the governance evidence.
CREATE TABLE IF NOT EXISTS skill_candidates (
    name TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    status TEXT NOT NULL,
    template_json TEXT NOT NULL,
    source_task_ids_json TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    test_task_id TEXT,
    test_outcome TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    registry_name TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON skill_candidates (status, created_at);
-- v5: UI-editable supervisor settings (A5). Generic key/value JSON; the serve
-- layer owns the keys and rebuilds the frozen SupervisorConfig on every write.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_policies (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL,
    phase TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    pack_name TEXT,
    pack_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_bindings (
    project_id TEXT NOT NULL,
    binding_kind TEXT NOT NULL,
    binding_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (binding_kind, binding_value)
);
CREATE INDEX IF NOT EXISTS idx_project_bindings_project
ON project_bindings (project_id, binding_kind);
-- v6: chat sessions with the Queen's own model. Messages are the durable
-- transcript, including the model's tool traffic (tool_calls_json on an
-- assistant turn, tool_name on a 'tool' result row).
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'web'
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    thinking TEXT,
    tool_calls_json TEXT,
    tool_name TEXT,
    -- v106-F4 (v101-F15): WHICH call a tool result answers. Position is not a
    -- link once cards resolve out of emission order — two same-named calls
    -- resolved in reverse reported the operator's verdicts inverted (I6/I8).
    tool_call_id TEXT,
    created_at TEXT NOT NULL,
    -- v44-F9: image attachments (stored file names under chat-attachments/).
    attachments_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages (chat_id, id);
-- v51-F1: FTS5 over the transcript (external-content: chat_messages is the
-- source of truth; triggers keep the index in sync — messages are append-only
-- plus whole-chat deletes, so insert/delete triggers cover every write path).
CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(
    content, content='chat_messages', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chat_messages_fts_insert
AFTER INSERT ON chat_messages BEGIN
    INSERT INTO chat_fts (rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS chat_messages_fts_delete
AFTER DELETE ON chat_messages BEGIN
    INSERT INTO chat_fts (chat_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
-- v6 Stage D: mutations the chat model proposed, awaiting a human verdict in
-- the chat itself — the conversational analogue of the approval queue. The
-- model never executes these; a confirmed action runs under actor 'chat-user'.
CREATE TABLE IF NOT EXISTS chat_actions (
    action_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    -- v25-F1: who proposed it — 'assistant' (the model) or 'operator' (a typed
    -- /command). Operator actions resolve without the model ever seeing them.
    source TEXT NOT NULL DEFAULT 'assistant',
    -- v106-F4 (v101-F15): the model's own call id. A card resolves minutes
    -- after its call, in a different request — the id must survive on the
    -- action row so the result row can carry it back.
    tool_call_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_actions_pending ON chat_actions (chat_id, status);
-- v7 Stage B: inert notes and tasks the assistant may append to freely; due
-- dates/deletes are gated at the chat-tool layer because they change behavior
-- or destroy user data.
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks (status, due_at);
CREATE TABLE IF NOT EXISTS note_task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_proposals (
    proposal_id TEXT PRIMARY KEY,
    memory_class TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL,
    actor TEXT NOT NULL,
    rationale TEXT,
    project_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_proposals_state ON memory_proposals (state, created_at);
CREATE TABLE IF NOT EXISTS memory_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_sources_proposal ON memory_sources (proposal_id);
CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    memory_class TEXT NOT NULL,
    content TEXT NOT NULL,
    project_id TEXT,
    proposal_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_items_active ON memory_items (active, project_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, memory_id UNINDEXED);
CREATE TABLE IF NOT EXISTS provider_profiles (
    provider_id TEXT PRIMARY KEY,
    protocol TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    allowed_network_hosts_json TEXT NOT NULL DEFAULT '[]',
    cost_class TEXT NOT NULL DEFAULT 'local',
    fallback_order INTEGER NOT NULL DEFAULT 0,
    api_key_env TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    model_found INTEGER NOT NULL,
    latency_ms INTEGER,
    error TEXT,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_health ON provider_health (provider_id, id);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    kind TEXT NOT NULL,
    trust_tier TEXT NOT NULL,
    allowed_capabilities_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_configs (
    channel TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    channel_can_confirm INTEGER NOT NULL DEFAULT 0,
    allowed_identities_json TEXT NOT NULL DEFAULT '[]',
    notification_level TEXT NOT NULL DEFAULT 'all',
    updated_at TEXT NOT NULL
);
-- v26-F2: one messenger conversation = one durable chat session. The session
-- key is the adapter's stable per-conversation key ("telegram:42").
CREATE TABLE IF NOT EXISTS channel_sessions (
    session_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- v78-F6: the messenger-side anchor outbound pushes thread under (Slack
    -- thread_ts today; named generically — Telegram reply ids could reuse it).
    thread_ref TEXT
);
-- v44-F3: inbound webhook subscriptions (GitHub/generic CI -> a bound chat).
-- The per-subscription secret is a 0600 file beside the serve token, never a row.
CREATE TABLE IF NOT EXISTS webhooks (
    name TEXT PRIMARY KEY,
    template TEXT NOT NULL,
    chat_id TEXT,
    created_at TEXT NOT NULL
);
-- v74-F6: skep's own token tally per provider request (ollama reports
-- prompt_eval_count/eval_count on the final chunk). ollama.com exposes NO
-- account usage API, so this local count is the closest honest approximation
-- of the 5h/weekly windows; rows older than 8 days are pruned on write.
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL
);
-- v83-F8: operator-started background processes (start_process). The row is
-- the record; liveness is reconciled against the real pid on every read so
-- the table never shows a false "running" (I8). Output tees to log_path.
CREATE TABLE IF NOT EXISTS processes (
    proc_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    cwd TEXT,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    log_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# v14: a schedule tick "succeeds" only when its run completed. Everything else —
# failed, rejected, worker crash/timeout, policy_blocked, dispatch_error — is a
# health failure that increments the consecutive-failure count. A 'note'
# schedule's tick succeeds by posting its note ("note_posted") — no run exists.
# v44-F4: a 'script' tick succeeds when the command exits 0 ("script_ran").
# v47-F6: a 'digest' tick succeeds by posting its summary ("digest_posted").
# v83-F5: a 'prompt' tick succeeds when the read-only Queen turn replied.
SCHEDULE_SUCCESS_STATES: frozenset[str] = frozenset(
    {"completed", "note_posted", "script_ran", "digest_posted", "prompt_posted"}
)
_SCHEDULE_HEALTH_WINDOW = 20


def schedule_state_ok(state: str) -> bool:
    return state in SCHEDULE_SUCCESS_STATES


_TERMINAL_STATE_VALUES = {state.value for state in TERMINAL_STATES}
_INSTRUCTIONS_SNIPPET_CHARS = 200
_SHELL_APPROVAL_PREFIX = "shell.run requires approval for command: "


@dataclass(frozen=True)
class RunRecord:
    task_id: str
    trace_id: str
    repo: str
    ref: str | None
    workspace: str | None
    execution_mode: str
    instructions: str
    state: str
    summary: str | None
    verification_outcome: str | None
    verification_details: str | None
    worker_version: str | None
    manifest_fingerprint: str | None
    resume_of: str | None
    created_at: str
    updated_at: str
    base_commit: str | None = None
    # v101-F4: old rows keep NULL and every surface renders NULL as absent
    # rather than guessing which caste or engine ran.
    worker_kind: str | None = None
    coding_engine: str | None = None


@dataclass(frozen=True)
class ReverifyRecord:
    task_id: str
    outcome: str
    worker_outcome: str | None
    confirmed: bool
    commands: list[str]
    exit_codes: list[int]
    detail: str
    created_at: str


@dataclass(frozen=True)
class UsageRecord:
    task_id: str
    provider_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class ApprovalRecord:
    review_id: str
    task_id: str
    action: str
    reason: str
    status: str
    requested_at: str
    resolved_at: str | None
    resolved_by: str | None
    resolution_note: str | None
    landing_branch: str | None = None
    decided_by: str | None = None


@dataclass(frozen=True)
class ApprovalLedgerRecord:
    id: int
    review_id: str | None
    task_id: str
    action: str
    resource: str
    reason: str
    instructions_snippet: str
    repo_path: str
    template_name: str | None
    approved_at: str
    approved_by: str
    task_outcome: str | None
    remembered: bool


@dataclass(frozen=True)
class ChatRecord:
    chat_id: str
    title: str
    model: str | None  # None = the configured default, resolved at send time
    created_at: str
    updated_at: str
    source: str = "web"  # v44-F1: which face opened the chat ('web', 'terminal', channel name)
    # v44-F10: a style preamble name ('concise'...) or 'custom:<text>'; None = default voice.
    personality: str | None = None
    # v56-F2 (ADR 0037): deterministic digest of compacted-away turns; the
    # transcript store stays complete — only the model replay is bounded.
    context_summary: str | None = None
    compacted_through: int = 0  # last message id folded into the summary
    # v56-F4: the project this chat last worked on — lets project-scoped
    # memory ride the prompt. Set by dispatch/workon, never required.
    project_id: str | None = None
    # v73-F1: the replay budget (chars) that last got past the provider's own
    # request wall after a 4xx — replay and compaction stay under
    # min(num_ctx budget, this). None = the provider has never pushed back.
    provider_ceiling_chars: int | None = None
    # v74-F3: tools this chat described via describe_tools — their full
    # schemas ride the advertised tools array from then on. Advertisement
    # only, never permission (the executor accepts any registered tool).
    active_tools: list[str] | None = None


@dataclass(frozen=True)
class ChatMessageRecord:
    id: int
    chat_id: str
    role: str  # 'user' | 'assistant' | 'tool'
    content: str
    thinking: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_name: str | None
    created_at: str
    # v44-F9: stored image file names (under home/chat-attachments/<chat_id>/).
    attachments: list[str] | None = None
    # v106-F4 (v101-F15): which call this tool result answers. NULL on rows
    # older than the column — those replay by position, as they always did.
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ChatSearchHit:
    """One FTS5 match over the durable transcript (v51-F1)."""

    chat_id: str
    chat_title: str
    message_id: int
    role: str
    created_at: str
    snippet: str
    # v84-F8 (I8): the chat's source rides every hit so imported words are
    # distinguishable from the operator's own at the moment they are trusted.
    source: str = "web"


@dataclass(frozen=True)
class ChatActionRecord:
    action_id: str
    chat_id: str
    tool: str
    args: dict[str, Any]
    status: str  # 'proposed' | 'confirmed' | 'denied' | 'superseded' (v63-F2)
    result: Any | None
    created_at: str
    resolved_at: str | None
    source: str = "assistant"  # 'assistant' (model-proposed) | 'operator' (/command)
    decided_by: str | None = None  # v40-F8: the routing policy decision, when any
    tool_call_id: str | None = None  # v106-F4: the model's call id, for the result row


@dataclass(frozen=True)
class NoteRecord:
    note_id: str
    content: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    title: str
    status: str
    due_at: str | None
    due: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class NoteTaskEventRecord:
    id: int
    kind: str
    item_id: str
    action: str
    actor: str
    detail: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ScheduleRecord:
    name: str
    repo: str
    ref: str | None
    worker_kind: str
    instructions: str
    network: list[str]
    env_allowlist: list[str]
    interval_seconds: int
    enabled: bool
    created_at: str
    last_run_at: str | None
    next_run_at: str
    last_task_id: str | None
    last_state: str | None
    # v3.5: when set, this schedule is bound to a template; ``instructions`` and the
    # other knobs above are a display snapshot taken at add time, while ``tick``
    # re-instantiates the live template with ``params`` (so template edits + the
    # template's budget take effect). A direct schedule leaves both at their defaults.
    template_name: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    # v53-F5: chain names another schedule whose last stored output is
    # injected as context before this one runs; last_output is what THIS
    # schedule most recently produced (capped, synchronous castes only).
    chain: str | None = None
    last_output: str | None = None
    # v43-F6: a 'note' schedule created from a chat delivers into that chat;
    # None means the tick posts an inert note instead.
    chat_id: str | None = None
    # v44-F2: one-shot — the tick disables the schedule after its first fire.
    once: bool = False


@dataclass(frozen=True)
class ProcessRecord:
    """v83-F8: one operator-started background process."""

    proc_id: str
    command: str
    cwd: str | None
    pid: int
    status: str  # running | stopped | dead
    exit_code: int | None
    log_path: str
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class WebhookRecord:
    """v44-F3: one inbound webhook subscription. ``chat_id`` is the delivery
    target (None → inert note); the secret lives in a 0600 file, never here."""

    name: str
    template: str
    chat_id: str | None
    created_at: str


@dataclass(frozen=True)
class ScheduleHealth:
    """v14: a schedule's operational health, composed for CLI/UI views."""

    name: str
    enabled: bool
    project_context: dict[str, Any] | None
    last_task_id: str | None
    last_state: str | None
    last_failure_reason: str | None
    consecutive_failures: int
    success_rate: float | None  # over the recent window; None with no history
    window_size: int
    next_run_at: str
    disabled_reason: str | None


@dataclass(frozen=True)
class ProjectPolicyRecord:
    project_id: str
    name: str
    strategy: str
    phase: str
    policy: dict[str, Any]
    created_at: str
    updated_at: str
    pack_name: str | None = None
    pack_version: str | None = None


@dataclass(frozen=True)
class ProjectBindingRecord:
    project_id: str
    binding_kind: str
    binding_value: str
    created_at: str


class RunStore:
    """Single-writer SQLite store for runs, events, artifacts, and approvals."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # G4: SQLite-WAL, single writer. The connection is shared across dispatch
        # threads (check_same_thread=False) and serialized by self._lock; WAL keeps
        # readers (a concurrent `skep status`, in another process) non-blocking, and
        # busy_timeout makes a cross-process writer wait its turn instead of raising.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # v51-F1: sniff BEFORE the schema runs — an external-content FTS table
        # reads through to chat_messages, so it never *looks* empty afterwards.
        chat_fts_is_new = not self._conn.execute(
            "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_fts')"
        ).fetchone()[0]
        self._conn.executescript(_SCHEMA)
        self._migrate(rebuild_chat_fts=chat_fts_is_new)
        self._conn.commit()

    def _migrate(self, *, rebuild_chat_fts: bool = False) -> None:
        """Add columns introduced after a table first shipped (CREATE IF NOT EXISTS
        never alters an existing table). Idempotent: each ADD is guarded by a
        column check. v3.5 adds the template binding to ``schedules``; v4 adds
        ``provenance`` to ``templates`` (an existing v3.5 DB has the table already)."""
        schedule_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(schedules)")}
        if "template_name" not in schedule_columns:
            self._conn.execute("ALTER TABLE schedules ADD COLUMN template_name TEXT")
        if "params_json" not in schedule_columns:
            self._conn.execute(
                "ALTER TABLE schedules ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "last_state" not in schedule_columns:
            self._conn.execute("ALTER TABLE schedules ADD COLUMN last_state TEXT")
        if "chat_id" not in schedule_columns:
            # v43-F6: note schedules deliver into the chat that created them.
            self._conn.execute("ALTER TABLE schedules ADD COLUMN chat_id TEXT")
        if "once" not in schedule_columns:
            # v44-F2: one-shot reminders ("remind me tomorrow at 9am", once).
            self._conn.execute("ALTER TABLE schedules ADD COLUMN once INTEGER NOT NULL DEFAULT 0")
        if "chain" not in schedule_columns:
            # v53-F5: cron context chaining — B reads A's last output.
            self._conn.execute("ALTER TABLE schedules ADD COLUMN chain TEXT")
            self._conn.execute("ALTER TABLE schedules ADD COLUMN last_output TEXT")
        channel_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(channel_configs)")
        }
        if "require_mention" not in channel_columns:
            # v44-F1: Discord routing parity — mention gating, auto-threads,
            # and a user-level allowlist on top of the channel one.
            self._conn.execute(
                "ALTER TABLE channel_configs ADD COLUMN require_mention INTEGER NOT NULL DEFAULT 0"
            )
        if "auto_thread" not in channel_columns:
            self._conn.execute(
                "ALTER TABLE channel_configs ADD COLUMN auto_thread INTEGER NOT NULL DEFAULT 0"
            )
        if "allowed_users_json" not in channel_columns:
            self._conn.execute(
                "ALTER TABLE channel_configs ADD COLUMN allowed_users_json"
                " TEXT NOT NULL DEFAULT '[]'"
            )
        if "notification_level" not in channel_columns:
            # v78-F1: per-channel delivery volume; 'all' = today's behavior.
            self._conn.execute(
                "ALTER TABLE channel_configs ADD COLUMN notification_level"
                " TEXT NOT NULL DEFAULT 'all'"
            )
        session_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(channel_sessions)")
        }
        if "thread_ref" not in session_columns:
            # v78-F6: outbound pushes thread under the operator's latest message.
            self._conn.execute("ALTER TABLE channel_sessions ADD COLUMN thread_ref TEXT")
        message_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(chat_messages)")}
        if "attachments_json" not in message_columns:
            # v44-F9: image input in chat.
            self._conn.execute("ALTER TABLE chat_messages ADD COLUMN attachments_json TEXT")
        template_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(templates)")}
        if "provenance" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN provenance TEXT NOT NULL DEFAULT 'user'"
            )
        if "shell_allow_json" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN shell_allow_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "allow_git_mutation" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN allow_git_mutation INTEGER NOT NULL DEFAULT 0"
            )
        run_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(runs)")}
        if "workspace" not in run_columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN workspace TEXT")
        if "execution_mode" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'sandbox'"
            )
        if "base_commit" not in run_columns:
            # v81-F3: the commit the run's patch was generated against.
            self._conn.execute("ALTER TABLE runs ADD COLUMN base_commit TEXT")
        if "worker_kind" not in run_columns:
            # v101-F4: WHICH caste ran. With one caste this was a footnote; with
            # nine it is a hole in the record (I8) — "which worker ran?" could
            # only be answered by finding the task envelope on disk.
            self._conn.execute("ALTER TABLE runs ADD COLUMN worker_kind TEXT")
        if "coding_engine" not in run_columns:
            # v101-F4: and which AGENT edited the repo — builtin or a CLI engine.
            # Resolved at dispatch (policy_resolver) and then discarded.
            self._conn.execute("ALTER TABLE runs ADD COLUMN coding_engine TEXT")
        chat_message_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(chat_messages)")
        }
        if "thinking" not in chat_message_columns:
            self._conn.execute("ALTER TABLE chat_messages ADD COLUMN thinking TEXT")
        if "tool_call_id" not in chat_message_columns:
            # v106-F4 (v101-F15): pair a tool result with ITS call, not with
            # whatever position it landed in. Old rows keep NULL — replay falls
            # back to arrival order for them, never for new rows (I11).
            self._conn.execute("ALTER TABLE chat_messages ADD COLUMN tool_call_id TEXT")
        project_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(project_policies)")
        }
        if "pack_name" not in project_columns:
            self._conn.execute("ALTER TABLE project_policies ADD COLUMN pack_name TEXT")
        if "pack_version" not in project_columns:
            self._conn.execute("ALTER TABLE project_policies ADD COLUMN pack_version TEXT")
        approval_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(approvals)")}
        if "commands_json" not in approval_columns:
            # v19-F1: the full command list a batch approval grants.
            self._conn.execute("ALTER TABLE approvals ADD COLUMN commands_json TEXT")
        if "landing_branch" not in approval_columns:
            # v30: the branch a landing actually applied on.
            self._conn.execute("ALTER TABLE approvals ADD COLUMN landing_branch TEXT")
        if "decided_by" not in approval_columns:
            # v40-F8: the policy rule that routed this gate.
            self._conn.execute("ALTER TABLE approvals ADD COLUMN decided_by TEXT")
        action_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(chat_actions)")}
        if "source" not in action_columns:
            # v25-F1: operator-typed /commands audit beside model proposals.
            self._conn.execute(
                "ALTER TABLE chat_actions ADD COLUMN source TEXT NOT NULL DEFAULT 'assistant'"
            )
        if "decided_by" not in action_columns:
            # v40-F8: the policy decision that routed a confirm card, when any.
            self._conn.execute("ALTER TABLE chat_actions ADD COLUMN decided_by TEXT")
        if "tool_call_id" not in action_columns:
            # v106-F4 (v101-F15): a card resolves in a later request — the
            # call id survives on the action row so the result row carries it.
            self._conn.execute("ALTER TABLE chat_actions ADD COLUMN tool_call_id TEXT")
        chat_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(chats)")}
        if "personality" not in chat_columns:
            # v44-F10: per-chat style preamble.
            self._conn.execute("ALTER TABLE chats ADD COLUMN personality TEXT")
        if "context_summary" not in chat_columns:
            # v56-F2: bounded model replay — digest + cursor; store stays complete.
            self._conn.execute("ALTER TABLE chats ADD COLUMN context_summary TEXT")
            self._conn.execute(
                "ALTER TABLE chats ADD COLUMN compacted_through INTEGER NOT NULL DEFAULT 0"
            )
        if "project_id" not in chat_columns:
            # v56-F4: which project the chat works on — project memory follows.
            self._conn.execute("ALTER TABLE chats ADD COLUMN project_id TEXT")
        if "provider_ceiling_chars" not in chat_columns:
            # v73-F1: the discovered per-chat provider request ceiling.
            self._conn.execute("ALTER TABLE chats ADD COLUMN provider_ceiling_chars INTEGER")
        if "active_tools_json" not in chat_columns:
            # v74-F3: tools described via describe_tools, advertised in full.
            self._conn.execute("ALTER TABLE chats ADD COLUMN active_tools_json TEXT")
        if "source" not in chat_columns:
            # v44-F1: which face opened the chat — 'web', 'terminal', or a channel name.
            self._conn.execute("ALTER TABLE chats ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
            self._conn.execute(
                "UPDATE chats SET source = (SELECT channel FROM channel_sessions"
                " WHERE channel_sessions.chat_id = chats.chat_id)"
                " WHERE chat_id IN (SELECT chat_id FROM channel_sessions)"
            )
            # ponytail: title heuristic mops up pre-migration terminal chats only
            self._conn.execute(
                "UPDATE chats SET source = 'terminal'"
                " WHERE source = 'web' AND title LIKE 'terminal 20__-__-__ __:__'"
            )
        # v51-F1: a store that predates chat_fts has transcripts the triggers
        # never saw — rebuild the index once from chat_messages on first open.
        if rebuild_chat_fts:
            self._conn.execute("INSERT INTO chat_fts (chat_fts) VALUES ('rebuild')")

    @_locked
    def checkpoint(self) -> None:
        """v19-F10: truncate the WAL so it does not grow unbounded during an idle
        session. Best-effort — a busy WAL is left for the next opportunity."""
        with contextlib.suppress(sqlite3.OperationalError):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @_locked
    def close(self) -> None:
        self._conn.close()

    # -- runs ---------------------------------------------------------------

    @_locked
    def create_run(
        self,
        task: CodingWorkerTask,
        *,
        repo: Path,
        ref: str | None,
        execution_mode: str,
        base_commit: str | None = None,
        coding_engine: str | None = None,
    ) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (task_id, trace_id, repo, ref, workspace, execution_mode,"
            " instructions, state, resume_of, created_at, updated_at, base_commit,"
            " worker_kind, coding_engine)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.trace_id,
                str(repo),
                ref,
                task.workspace,
                execution_mode,
                task.instructions,
                "created",
                task.resume_of,
                now,
                now,
                base_commit,
                task.worker_kind,
                coding_engine or None,
            ),
        )
        self._record_transition(task.task_id, "created", _created_transition_detail(task))
        self._conn.commit()

    @_locked
    def transition(self, task_id: str, state: str, detail: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET state = ?, updated_at = ? WHERE task_id = ?",
            (state, _now(), task_id),
        )
        self._record_transition(task_id, state, detail)
        if state in _TERMINAL_STATE_VALUES:
            self._update_ledger_outcome(task_id, state)
        self._conn.commit()

    def _record_transition(self, task_id: str, state: str, detail: str | None) -> None:
        self._conn.execute(
            "INSERT INTO run_transitions (task_id, state, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, state, detail, _now()),
        )

    @_locked
    def record_result(self, task_id: str, result: CodingWorkerResult) -> None:
        self._conn.execute(
            "UPDATE runs SET summary = ?, verification_outcome = ?, verification_details = ?,"
            " updated_at = ? WHERE task_id = ?",
            (
                result.summary,
                result.verification.outcome.value,
                result.verification.details,
                _now(),
                task_id,
            ),
        )
        self._conn.executemany(
            "INSERT INTO commands (task_id, command, exit_code, purpose) VALUES (?, ?, ?, ?)",
            [(task_id, c.command, c.exit_code, c.purpose) for c in result.commands],
        )
        self._conn.commit()

    @_locked
    def set_worker_identity(self, task_id: str, *, version: str, fingerprint: str) -> None:
        """G7: every run record carries worker version + manifest fingerprint."""
        self._conn.execute(
            "UPDATE runs SET worker_version = ?, manifest_fingerprint = ?, updated_at = ?"
            " WHERE task_id = ?",
            (version, fingerprint, _now(), task_id),
        )
        self._conn.commit()

    @_locked
    def get_run(self, task_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT task_id, trace_id, repo, ref, workspace, execution_mode, instructions, state,"
            " summary,"
            " verification_outcome, verification_details, worker_version,"
            " manifest_fingerprint, resume_of, created_at, updated_at, base_commit,"
            " worker_kind, coding_engine"
            " FROM runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return RunRecord(*row) if row is not None else None

    @_locked
    def runs_with_states(self, states: Sequence[str]) -> list[RunRecord]:
        """v59-F10: the startup recovery sweep's view — runs left in a
        non-terminal state by a supervisor death."""
        placeholders = ", ".join("?" for _ in states)
        rows = self._conn.execute(
            "SELECT task_id, trace_id, repo, ref, workspace, execution_mode, instructions,"
            " state, summary, verification_outcome, verification_details, worker_version,"
            " manifest_fingerprint, resume_of, created_at, updated_at, base_commit,"
            " worker_kind, coding_engine"
            f" FROM runs WHERE state IN ({placeholders}) ORDER BY created_at",
            tuple(states),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    @_locked
    def task_ids_with_prefix(self, prefix: str) -> list[str]:
        """v59-F8: resolve a truncated task id — LIKE with escaped wildcards."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            r"SELECT task_id FROM runs WHERE task_id LIKE ? ESCAPE '\' ORDER BY task_id",
            (f"{escaped}%",),
        ).fetchall()
        return [row[0] for row in rows]

    @_locked
    def recent_runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT task_id, trace_id, repo, ref, workspace, execution_mode, instructions, state,"
            " summary,"
            " verification_outcome, verification_details, worker_version,"
            " manifest_fingerprint, resume_of, created_at, updated_at, base_commit,"
            " worker_kind, coding_engine"
            " FROM runs ORDER BY created_at DESC, task_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    @_locked
    def pending_runs_before(self, cutoff_iso: str) -> list[RunRecord]:
        """Runs stuck in ``pending_approval`` whose ``updated_at`` is < cutoff (v20-F6).

        Timestamps are the sortable ``%Y-%m-%dT%H:%M:%SZ`` form, so the string
        comparison orders correctly. Used by ``skep doctor`` to surface stale
        gates with a deny command.
        """
        rows = self._conn.execute(
            "SELECT task_id, trace_id, repo, ref, workspace, execution_mode, instructions, state,"
            " summary,"
            " verification_outcome, verification_details, worker_version,"
            " manifest_fingerprint, resume_of, created_at, updated_at, base_commit,"
            " worker_kind, coding_engine"
            " FROM runs WHERE state = 'pending_approval' AND updated_at < ?"
            " ORDER BY updated_at ASC, task_id ASC",
            (cutoff_iso,),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    @_locked
    def transitions_for(self, task_id: str) -> list[tuple[str, str | None, str]]:
        rows = self._conn.execute(
            "SELECT state, detail, created_at FROM run_transitions WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    # -- events (audit, spec §4 idempotency) ---------------------------------

    @_locked
    def ingest_events(self, events: list[Event]) -> int:
        """Insert events deduplicated on event_id; returns how many were new."""
        added = 0
        for event in events:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO events (event_id, task_id, trace_id, contract_version,"
                " seq, ts, type, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.task_id,
                    event.trace_id,
                    event.contract_version,
                    event.seq,
                    event.ts,
                    event.type.value,
                    json.dumps(event.payload, ensure_ascii=True),
                ),
            )
            added += cursor.rowcount
        self._conn.commit()
        return added

    @_locked
    def events_for(self, task_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT event_id, task_id, trace_id, contract_version, seq, ts, type, payload_json"
            " FROM events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        return [
            Event.model_validate(
                {
                    "event_id": row[0],
                    "task_id": row[1],
                    "trace_id": row[2],
                    "contract_version": row[3],
                    "seq": row[4],
                    "ts": row[5],
                    "type": row[6],
                    "payload": json.loads(row[7]),
                }
            )
            for row in rows
        ]

    # -- artifacts ------------------------------------------------------------

    @_locked
    def add_artifact(self, task_id: str, *, kind: str, audit_path: Path, sha256: str) -> None:
        self._conn.execute(
            "INSERT INTO artifacts (task_id, kind, audit_path, sha256) VALUES (?, ?, ?, ?)",
            (task_id, kind, str(audit_path), sha256),
        )
        self._conn.commit()

    @_locked
    def artifacts_for(self, task_id: str) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT kind, audit_path, sha256 FROM artifacts WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    @_locked
    def commands_for(self, task_id: str) -> list[tuple[str, int, str]]:
        rows = self._conn.execute(
            "SELECT command, exit_code, purpose FROM commands WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    # -- re-verification (G10: evidence over claims) --------------------------

    @_locked
    def record_reverification(
        self,
        task_id: str,
        *,
        outcome: str,
        worker_outcome: str | None,
        confirmed: bool,
        commands: list[str],
        exit_codes: list[int],
        detail: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO reverifications (task_id, outcome, worker_outcome,"
            " confirmed, commands_json, exit_codes_json, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                outcome,
                worker_outcome,
                1 if confirmed else 0,
                json.dumps(commands),
                json.dumps(exit_codes),
                detail,
                _now(),
            ),
        )
        self._conn.commit()

    @_locked
    def reverification_for(self, task_id: str) -> ReverifyRecord | None:
        row = self._conn.execute(
            "SELECT task_id, outcome, worker_outcome, confirmed, commands_json,"
            " exit_codes_json, detail, created_at FROM reverifications WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return ReverifyRecord(
            task_id=row[0],
            outcome=row[1],
            worker_outcome=row[2],
            confirmed=bool(row[3]),
            commands=list(json.loads(row[4])),
            exit_codes=list(json.loads(row[5])),
            detail=row[6],
            created_at=row[7],
        )

    # -- usage accounting (G8: cost is answerable) ----------------------------

    @_locked
    def record_usage(
        self,
        task_id: str,
        *,
        provider_calls: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO task_usage (task_id, provider_calls, input_tokens,"
            " output_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, provider_calls, input_tokens, output_tokens, cost_usd, _now()),
        )
        self._conn.commit()

    @_locked
    def usage_for(self, task_id: str) -> UsageRecord | None:
        row = self._conn.execute(
            "SELECT task_id, provider_calls, input_tokens, output_tokens, cost_usd"
            " FROM task_usage WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return UsageRecord(*row) if row is not None else None

    @_locked
    def usage_totals(self) -> UsageRecord:
        """Aggregate usage across every run — answers per-day/repo cost (G8)."""
        row = self._conn.execute(
            "SELECT SUM(provider_calls), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)"
            " FROM task_usage"
        ).fetchone()
        return UsageRecord(
            task_id="*",
            provider_calls=row[0],
            input_tokens=row[1],
            output_tokens=row[2],
            cost_usd=row[3],
        )

    # -- approval queue (carve-out: HITL flow) --------------------------------

    @_locked
    def enqueue_approval(
        self,
        task_id: str,
        *,
        action: str,
        reason: str,
        commands: list[list[str]] | None = None,
        decided_by: str | None = None,
    ) -> str:
        review_id = str(uuid.uuid4())
        commands_json = json.dumps(commands) if commands else None
        self._conn.execute(
            "INSERT INTO approvals"
            " (review_id, task_id, action, reason, status, requested_at, commands_json,"
            " decided_by)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (review_id, task_id, action, reason, _now(), commands_json, decided_by),
        )
        self._conn.commit()
        return review_id

    @_locked
    def approval_commands(self, review_id: str) -> list[list[str]] | None:
        """The batch-approval command list stored with an approval (v19-F1)."""
        row = self._conn.execute(
            "SELECT commands_json FROM approvals WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        commands: list[list[str]] = []
        for entry in data:
            if isinstance(entry, list) and all(isinstance(part, str) for part in entry):
                commands.append([str(part) for part in entry])
        return commands or None

    @_locked
    def resolve_approval(
        self,
        review_id: str,
        *,
        approved: bool,
        actor: str,
        note: str | None = None,
        remembered: bool = False,
        landing_branch: str | None = None,
    ) -> None:
        """Record the verdict with actor + timestamp; resolution is final.

        v30: ``landing_branch`` records where an apply_patch approval actually
        landed (default skep/<task_id>, a named branch, or an integration
        branch) so the applied-branch view is accurate for every landing path.
        """
        row = self._conn.execute(
            "SELECT status FROM approvals WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no approval {review_id!r}")
        if row[0] != "pending":
            raise ValueError(f"approval {review_id!r} already resolved as {row[0]!r}")
        resolved_at = _now()
        self._conn.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, resolved_by = ?,"
            " resolution_note = ?, landing_branch = ? WHERE review_id = ?",
            (
                "approved" if approved else "denied",
                resolved_at,
                actor,
                note,
                landing_branch,
                review_id,
            ),
        )
        if approved:
            self._record_approval_ledger_for_review(
                review_id,
                approved_at=resolved_at,
                approved_by=actor,
                remembered=remembered,
            )
        else:
            # v48-F3: denying the gate of a suspended run is a terminal
            # verdict. Every deny path routes through here; without this the
            # run sat in pending_approval forever and doctor kept flagging it
            # stale. The approval row is already 'denied', so the NOT EXISTS
            # counts only OTHER still-pending gates for the task.
            row = self._conn.execute(
                "SELECT a.task_id FROM approvals a JOIN runs r ON r.task_id = a.task_id"
                " WHERE a.review_id = ? AND r.state = 'pending_approval'"
                " AND NOT EXISTS (SELECT 1 FROM approvals p"
                "                 WHERE p.task_id = a.task_id AND p.status = 'pending')",
                (review_id,),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE runs SET state = 'rejected', updated_at = ? WHERE task_id = ?",
                    (resolved_at, row[0]),
                )
                self._record_transition(row[0], "rejected", f"gate denied by {actor}")
                self._update_ledger_outcome(row[0], "rejected")
        # v63-F2: a resolution reached elsewhere (review CLI, approvals API)
        # reconciles the chat's proposed cards for the SAME decision — without
        # this they sat until the v54-F1 sweep recorded "timed out" for work
        # that actually shipped, and the model read that lie in history.
        self._supersede_cards_for_resolution(
            review_id,
            approved=approved,
            actor=actor,
            landing_branch=landing_branch,
            resolved_at=resolved_at,
        )
        self._conn.commit()

    def _supersede_cards_for_resolution(
        self,
        review_id: str,
        *,
        approved: bool,
        actor: str,
        landing_branch: str | None,
        resolved_at: str,
    ) -> None:
        """Resolve proposed cards that were asking the question just answered:
        approve_review/deny_review by review id, land_run by the review's task.
        Same transcript shape as a manual resolve (a tool row), so history
        tells the truth instead of a later timeout-deny."""
        task_row = self._conn.execute(
            "SELECT task_id FROM approvals WHERE review_id = ?", (review_id,)
        ).fetchone()
        task_id = str(task_row[0]) if task_row is not None else ""
        verdict = f"approved by {actor}" if approved else f"denied by {actor}"
        note = f"resolved elsewhere: {verdict}"
        if approved and landing_branch:
            note = f"{note}, applied on {landing_branch}"
        payload = {"ok": True, "superseded": True, "note": note}
        rows = self._conn.execute(
            "SELECT action_id, chat_id, tool, args_json FROM chat_actions"
            " WHERE status = 'proposed'"
            " AND tool IN ('approve_review', 'deny_review', 'land_run')"
        ).fetchall()
        for action_id, chat_id, tool, args_json in rows:
            try:
                args = json.loads(args_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(args, dict):
                continue
            if tool == "land_run":
                matches = bool(task_id) and str(args.get("task_id", "")) == task_id
            else:
                matches = str(args.get("review_id", "")) == review_id
            if not matches:
                continue
            self._conn.execute(
                "UPDATE chat_actions SET status = 'superseded', result_json = ?,"
                " resolved_at = ? WHERE action_id = ?",
                (json.dumps(payload, ensure_ascii=True), resolved_at, action_id),
            )
            self.add_chat_message(
                str(chat_id),
                role="tool",
                tool_name=str(tool),
                content=json.dumps(payload, ensure_ascii=True),
            )

    @_locked
    def get_approval(self, review_id: str) -> ApprovalRecord | None:
        row = self._conn.execute(
            "SELECT review_id, task_id, action, reason, status, requested_at, resolved_at,"
            " resolved_by, resolution_note, landing_branch, decided_by"
            " FROM approvals WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        return None if row is None else ApprovalRecord(*row)

    @_locked
    def pending_approvals(self) -> list[ApprovalRecord]:
        rows = self._conn.execute(
            "SELECT review_id, task_id, action, reason, status, requested_at, resolved_at,"
            " resolved_by, resolution_note, landing_branch, decided_by"
            " FROM approvals WHERE status = 'pending'"
            " ORDER BY requested_at",
        ).fetchall()
        return [ApprovalRecord(*row) for row in rows]

    @_locked
    def resolved_approvals(self, limit: int = 10) -> list[ApprovalRecord]:
        """v79-F2: the recently-resolved tail of the queue, newest first (I13).

        Approvals are a ledger, not a moment — a verdict resolved by another
        actor (the web UI, auto:verified-patch) must stay visible to the chat
        instead of silently vanishing from the pending queue (field test
        2026-07-21: "there is no approval here")."""
        rows = self._conn.execute(
            "SELECT review_id, task_id, action, reason, status, requested_at, resolved_at,"
            " resolved_by, resolution_note, landing_branch, decided_by"
            " FROM approvals WHERE status != 'pending'"
            " ORDER BY resolved_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [ApprovalRecord(*row) for row in rows]

    @_locked
    def resumed_as_for(self, task_id: str) -> str | None:
        """v79-F2: the forward resume pointer — the latest run that resumed this
        one. Approved gates resume under a NEW task_id; without this pointer the
        chat has to rediscover the successor by scanning list_runs."""
        row = self._conn.execute(
            "SELECT task_id FROM runs WHERE resume_of = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    @_locked
    def pending_gate_workspaces(self) -> list[str]:
        """Workspaces of pending_approval runs whose gate approval is unresolved.

        These worktrees are preserved for in-place resume; once the approval
        resolves (or the run terminates) they fall out of this set and the
        next orphan sweep reclaims them.
        """
        rows = self._conn.execute(
            "SELECT r.workspace FROM runs r"
            " WHERE r.state = 'pending_approval' AND r.workspace IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM approvals a"
            "  WHERE a.task_id = r.task_id AND a.status = 'pending')",
        ).fetchall()
        return [str(row[0]) for row in rows]

    @_locked
    def active_run_workspaces(self) -> list[str]:
        """Workspaces of runs still executing (created/dispatched/running).

        v19-F8: a superseded predecessor drops out of pending_gate_workspaces the
        moment it is transitioned, so the successor (which may reuse the same
        worktree) must be kept alive here until it reaches a terminal state.
        """
        rows = self._conn.execute(
            "SELECT workspace FROM runs"
            " WHERE workspace IS NOT NULL"
            " AND state IN ('created', 'dispatched', 'running')",
        ).fetchall()
        return [str(row[0]) for row in rows]

    @_locked
    def approvals_for(self, task_id: str) -> list[ApprovalRecord]:
        rows = self._conn.execute(
            "SELECT review_id, task_id, action, reason, status, requested_at, resolved_at,"
            " resolved_by, resolution_note, landing_branch, decided_by"
            " FROM approvals WHERE task_id = ?"
            " ORDER BY requested_at",
            (task_id,),
        ).fetchall()
        return [ApprovalRecord(*row) for row in rows]

    # -- approval ledger (approval-to-template foundation) --------------------

    @staticmethod
    def _row_to_ledger(row: tuple[object, ...]) -> ApprovalLedgerRecord:
        return ApprovalLedgerRecord(
            id=cast(int, row[0]),
            review_id=None if row[1] is None else str(row[1]),
            task_id=str(row[2]),
            action=str(row[3]),
            resource=str(row[4]),
            reason=str(row[5]),
            instructions_snippet=str(row[6]),
            repo_path=str(row[7]),
            template_name=None if row[8] is None else str(row[8]),
            approved_at=str(row[9]),
            approved_by=str(row[10]),
            task_outcome=None if row[11] is None else str(row[11]),
            remembered=bool(row[12]),
        )

    def _approval_resource(self, task_id: str, action: str, reason: str) -> str:
        rows = self._conn.execute(
            "SELECT payload_json FROM events WHERE task_id = ? AND type = 'approval.requested'"
            " ORDER BY seq DESC",
            (task_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row[0]))
            if payload.get("action") != action:
                continue
            decision = payload.get("decision")
            if isinstance(decision, dict):
                detail = decision.get("detail")
                if isinstance(detail, str) and detail.strip():
                    return detail.strip()
        if action == "shell.run" and reason.startswith(_SHELL_APPROVAL_PREFIX):
            command = reason[len(_SHELL_APPROVAL_PREFIX) :].strip()
            if command:
                return command
        return action

    def _record_approval_ledger_for_review(
        self,
        review_id: str,
        *,
        approved_at: str,
        approved_by: str,
        remembered: bool,
    ) -> int:
        row = self._conn.execute(
            "SELECT a.task_id, a.action, a.reason, r.instructions, r.repo, r.state"
            " FROM approvals a JOIN runs r ON r.task_id = a.task_id"
            " WHERE a.review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no approval {review_id!r}")
        task_id = str(row[0])
        action = str(row[1])
        reason = str(row[2])
        return self._record_approval_ledger(
            review_id=review_id,
            task_id=task_id,
            action=action,
            resource=self._approval_resource(task_id, action, reason),
            reason=reason,
            instructions=str(row[3]),
            repo_path=str(row[4]),
            task_outcome=str(row[5]),
            approved_at=approved_at,
            approved_by=approved_by,
            remembered=remembered,
            template_name=None,
        )

    def _record_approval_ledger(
        self,
        *,
        review_id: str | None,
        task_id: str,
        action: str,
        resource: str,
        reason: str,
        instructions: str,
        repo_path: str,
        task_outcome: str | None,
        approved_at: str,
        approved_by: str,
        remembered: bool,
        template_name: str | None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO approval_ledger (review_id, task_id, action, resource, reason,"
            " instructions_snippet, repo_path, template_name, approved_at, approved_by,"
            " task_outcome, remembered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_id,
                task_id,
                action,
                resource,
                reason,
                instructions[:_INSTRUCTIONS_SNIPPET_CHARS],
                repo_path,
                template_name,
                approved_at,
                approved_by,
                task_outcome,
                1 if remembered else 0,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("approval ledger insert did not return a row id")
        return cursor.lastrowid

    @_locked
    def record_approval_ledger(
        self,
        *,
        task_id: str,
        action: str,
        resource: str,
        reason: str,
        approved_by: str,
        approved_at: str | None = None,
        remembered: bool = False,
        review_id: str | None = None,
        template_name: str | None = None,
    ) -> int:
        run = self.get_run(task_id)
        if run is None:
            raise KeyError(f"no run {task_id!r}")
        entry_id = self._record_approval_ledger(
            review_id=review_id,
            task_id=task_id,
            action=action,
            resource=resource,
            reason=reason,
            instructions=run.instructions,
            repo_path=run.repo,
            task_outcome=run.state,
            approved_at=approved_at or _now(),
            approved_by=approved_by,
            remembered=remembered,
            template_name=template_name,
        )
        self._conn.commit()
        return entry_id

    def _update_ledger_outcome(self, task_id: str, outcome: str) -> None:
        self._conn.execute(
            "UPDATE approval_ledger SET task_outcome = ? WHERE task_id = ?",
            (outcome, task_id),
        )

    @_locked
    def update_ledger_outcome(self, task_id: str, outcome: str) -> None:
        self._update_ledger_outcome(task_id, outcome)
        self._conn.commit()

    @_locked
    def ledger_for_repo(self, repo_path: Path | str) -> list[ApprovalLedgerRecord]:
        rows = self._conn.execute(
            "SELECT id, review_id, task_id, action, resource, reason, instructions_snippet,"
            " repo_path, template_name, approved_at, approved_by, task_outcome, remembered"
            " FROM approval_ledger WHERE repo_path = ? ORDER BY id",
            (str(repo_path),),
        ).fetchall()
        return [self._row_to_ledger(row) for row in rows]

    @_locked
    def ledger_for_pattern(self, instructions_snippet: str) -> list[ApprovalLedgerRecord]:
        rows = self._conn.execute(
            "SELECT id, review_id, task_id, action, resource, reason, instructions_snippet,"
            " repo_path, template_name, approved_at, approved_by, task_outcome, remembered"
            " FROM approval_ledger WHERE instructions_snippet LIKE ? ORDER BY id",
            (f"%{instructions_snippet[:_INSTRUCTIONS_SNIPPET_CHARS]}%",),
        ).fetchall()
        return [self._row_to_ledger(row) for row in rows]

    # -- recurring schedules (Stage E: Queen-scheduled tasks) -----------------

    @staticmethod
    def _row_to_schedule(row: tuple[object, ...]) -> ScheduleRecord:
        return ScheduleRecord(
            name=str(row[0]),
            repo=str(row[1]),
            ref=None if row[2] is None else str(row[2]),
            worker_kind=str(row[3]),
            instructions=str(row[4]),
            network=list(json.loads(str(row[5]))),
            env_allowlist=list(json.loads(str(row[6]))),
            interval_seconds=int(row[7]),  # type: ignore[call-overload]
            enabled=bool(row[8]),
            created_at=str(row[9]),
            last_run_at=None if row[10] is None else str(row[10]),
            next_run_at=str(row[11]),
            last_task_id=None if row[12] is None else str(row[12]),
            last_state=None if row[13] is None else str(row[13]),
            template_name=None if row[14] is None else str(row[14]),
            params=dict(json.loads(str(row[15]))) if row[15] is not None else {},
            chat_id=None if row[16] is None else str(row[16]),
            once=bool(row[17]),
            chain=None if row[18] is None else str(row[18]),
            last_output=None if row[19] is None else str(row[19]),
        )

    _SCHEDULE_COLS = (
        "name, repo, ref, worker_kind, instructions, network_json, env_allow_json,"
        " interval_seconds, enabled, created_at, last_run_at, next_run_at, last_task_id,"
        " last_state, template_name, params_json, chat_id, once, chain, last_output"
    )

    @_locked
    def add_schedule(self, schedule: ScheduleRecord) -> None:
        """Insert or replace a schedule by name."""
        self._conn.execute(
            f"INSERT OR REPLACE INTO schedules ({self._SCHEDULE_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule.name,
                schedule.repo,
                schedule.ref,
                schedule.worker_kind,
                schedule.instructions,
                json.dumps(schedule.network),
                json.dumps(schedule.env_allowlist),
                schedule.interval_seconds,
                1 if schedule.enabled else 0,
                schedule.created_at,
                schedule.last_run_at,
                schedule.next_run_at,
                schedule.last_task_id,
                schedule.last_state,
                schedule.template_name,
                json.dumps(schedule.params),
                schedule.chat_id,
                1 if schedule.once else 0,
                schedule.chain,
                schedule.last_output,
            ),
        )
        self._conn.commit()

    @_locked
    def record_schedule_output(self, name: str, output: str) -> None:
        """v53-F5: persist a tick's synchronous output for chained consumers."""
        self._conn.execute(
            "UPDATE schedules SET last_output = ? WHERE name = ?", (output[:4096], name)
        )
        self._conn.commit()

    @_locked
    def get_schedule(self, name: str) -> ScheduleRecord | None:
        row = self._conn.execute(
            f"SELECT {self._SCHEDULE_COLS} FROM schedules WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_schedule(row) if row is not None else None

    @_locked
    def list_schedules(self) -> list[ScheduleRecord]:
        rows = self._conn.execute(
            f"SELECT {self._SCHEDULE_COLS} FROM schedules ORDER BY name"
        ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    @_locked
    def due_schedules(self, now: str) -> list[ScheduleRecord]:
        """Enabled schedules whose next_run_at has arrived (RFC3339 UTC, lexical-safe)."""
        rows = self._conn.execute(
            f"SELECT {self._SCHEDULE_COLS} FROM schedules"
            " WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
            (now,),
        ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    @_locked
    def remove_schedule(self, name: str) -> bool:
        cursor = self._conn.execute("DELETE FROM schedules WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def set_schedule_enabled(self, name: str, *, enabled: bool) -> bool:
        cursor = self._conn.execute(
            "UPDATE schedules SET enabled = ? WHERE name = ?", (1 if enabled else 0, name)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def mark_schedule_due(self, name: str, *, due_at: str) -> bool:
        """v70-F5: run-now moves WHEN, never HOW — the ticker stays the only
        dispatcher; this just makes the schedule due on its next tick."""
        cursor = self._conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE name = ?", (due_at, name)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def mark_schedule_ran(
        self, name: str, *, ran_at: str, next_run_at: str, task_id: str | None, state: str
    ) -> None:
        self._conn.execute(
            "UPDATE schedules SET last_run_at = ?, next_run_at = ?, "
            "last_task_id = ?, last_state = ?"
            " WHERE name = ?",
            (ran_at, next_run_at, task_id, state, name),
        )
        self._conn.commit()

    # -- schedule health (v14) -------------------------------------------------

    @_locked
    def record_schedule_outcome(
        self, name: str, *, task_id: str | None, state: str, reason: str | None = None
    ) -> None:
        """Record one tick's outcome for health tracking. Success resets the
        consecutive-failure count; failure increments it and records the reason.
        Also appends a health event for the success-rate window."""
        ok = schedule_state_ok(state)
        now = _now()
        row = self._conn.execute(
            "SELECT consecutive_failures FROM schedule_health WHERE name = ?", (name,)
        ).fetchone()
        prior = int(row[0]) if row is not None else 0
        if ok:
            consecutive = 0
            failure_reason = None
        else:
            consecutive = prior + 1
            failure_reason = reason or state
        self._conn.execute(
            "INSERT INTO schedule_health (name, consecutive_failures, last_failure_reason,"
            " updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET consecutive_failures = excluded.consecutive_failures,"
            " last_failure_reason = excluded.last_failure_reason, updated_at = excluded.updated_at",
            (name, consecutive, failure_reason, now),
        )
        self._conn.execute(
            "INSERT INTO schedule_health_events (schedule_name, task_id, state, ok, reason,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, task_id, state, 1 if ok else 0, failure_reason, now),
        )
        self._conn.commit()

    @_locked
    def set_schedule_disabled_reason(self, name: str, reason: str | None) -> None:
        """Record why a schedule was auto-disabled (v14 Step 2 uses this)."""
        now = _now()
        self._conn.execute(
            "INSERT INTO schedule_health (name, disabled_reason, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
            " disabled_reason = excluded.disabled_reason, updated_at = excluded.updated_at",
            (name, reason, now),
        )
        self._conn.commit()

    def _schedule_project_context(self, repo: str) -> dict[str, Any] | None:
        project = self.project_for_binding("repo_path", repo)
        if project is None:
            return None
        return {
            "project_id": project.project_id,
            "name": project.name,
            "strategy": project.strategy,
            "phase": project.phase,
        }

    def _schedule_health_for(self, schedule: ScheduleRecord) -> ScheduleHealth:
        row = self._conn.execute(
            "SELECT consecutive_failures, last_failure_reason, disabled_reason"
            " FROM schedule_health WHERE name = ?",
            (schedule.name,),
        ).fetchone()
        consecutive = int(row[0]) if row is not None else 0
        last_failure_reason = None if row is None or row[1] is None else str(row[1])
        disabled_reason = None if row is None or row[2] is None else str(row[2])
        events = self._conn.execute(
            "SELECT ok FROM schedule_health_events WHERE schedule_name = ?"
            " ORDER BY id DESC LIMIT ?",
            (schedule.name, _SCHEDULE_HEALTH_WINDOW),
        ).fetchall()
        window = len(events)
        success_rate = sum(int(e[0]) for e in events) / window if window else None
        return ScheduleHealth(
            name=schedule.name,
            enabled=schedule.enabled,
            project_context=self._schedule_project_context(schedule.repo),
            last_task_id=schedule.last_task_id,
            last_state=schedule.last_state,
            last_failure_reason=last_failure_reason,
            consecutive_failures=consecutive,
            success_rate=success_rate,
            window_size=window,
            next_run_at=schedule.next_run_at,
            disabled_reason=disabled_reason,
        )

    @_locked
    def schedule_for_task(self, task_id: str) -> str | None:
        """The schedule a run originated from, if any (v14 Step 2: schedule origin)."""
        row = self._conn.execute(
            "SELECT schedule_name FROM schedule_health_events WHERE task_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    @_locked
    def schedule_health(self, name: str) -> ScheduleHealth | None:
        schedule = self.get_schedule(name)
        return None if schedule is None else self._schedule_health_for(schedule)

    @_locked
    def list_schedule_health(self) -> list[ScheduleHealth]:
        return [self._schedule_health_for(schedule) for schedule in self.list_schedules()]

    # -- workflow templates (v3.5: user-authored task recipes) ----------------

    _TEMPLATE_COLS = (
        "name, description, worker_kind, instructions, params_json, repo, ref,"
        " network_json, env_allow_json, shell_allow_json, allow_git_mutation,"
        " wall_clock_seconds, max_iterations, max_actions, max_provider_calls, created_at,"
        " provenance"
    )

    @staticmethod
    def _row_to_template(row: tuple[object, ...]) -> WorkflowTemplate:
        params = tuple(
            TemplateParam(
                name=str(p["name"]),
                description=str(p.get("description", "")),
                default=None if p.get("default") is None else str(p["default"]),
            )
            for p in json.loads(str(row[4]))
        )
        return WorkflowTemplate(
            name=str(row[0]),
            description=str(row[1]),
            worker_kind=str(row[2]),
            instructions=str(row[3]),
            params=params,
            repo=None if row[5] is None else str(row[5]),
            ref=None if row[6] is None else str(row[6]),
            network=tuple(json.loads(str(row[7]))),
            env_allowlist=tuple(json.loads(str(row[8]))),
            shell_allowlist=tuple(tuple(command) for command in json.loads(str(row[9]))),
            allow_git_mutation=bool(row[10]),
            wall_clock_seconds=int(row[11]),  # type: ignore[call-overload]
            max_iterations=int(row[12]),  # type: ignore[call-overload]
            max_actions=int(row[13]),  # type: ignore[call-overload]
            max_provider_calls=int(row[14]),  # type: ignore[call-overload]
            created_at=str(row[15]),
            provenance=str(row[16]),
        )

    @_locked
    def add_template(self, template: WorkflowTemplate) -> None:
        """Insert or replace a template by name (single writer, G4)."""
        params_json = json.dumps(
            [
                {"name": p.name, "description": p.description, "default": p.default}
                for p in template.params
            ]
        )
        self._conn.execute(
            f"INSERT OR REPLACE INTO templates ({self._TEMPLATE_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                template.name,
                template.description,
                template.worker_kind,
                template.instructions,
                params_json,
                template.repo,
                template.ref,
                json.dumps(list(template.network)),
                json.dumps(list(template.env_allowlist)),
                json.dumps([list(command) for command in template.shell_allowlist]),
                1 if template.allow_git_mutation else 0,
                template.wall_clock_seconds,
                template.max_iterations,
                template.max_actions,
                template.max_provider_calls,
                template.created_at or _now(),
                template.provenance,
            ),
        )
        self._conn.commit()

    @_locked
    def get_template(self, name: str) -> WorkflowTemplate | None:
        row = self._conn.execute(
            f"SELECT {self._TEMPLATE_COLS} FROM templates WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_template(row) if row is not None else None

    @_locked
    def list_templates(self) -> list[WorkflowTemplate]:
        rows = self._conn.execute(
            f"SELECT {self._TEMPLATE_COLS} FROM templates ORDER BY name"
        ).fetchall()
        return [self._row_to_template(row) for row in rows]

    @_locked
    def remove_template(self, name: str) -> bool:
        cursor = self._conn.execute("DELETE FROM templates WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def rename_template(self, old_name: str, new_name: str) -> bool:
        if old_name == new_name:
            row = self._conn.execute(
                "SELECT 1 FROM templates WHERE name = ?", (old_name,)
            ).fetchone()
            return row is not None
        cursor = self._conn.execute(
            "UPDATE templates SET name = ? WHERE name = ?"
            " AND NOT EXISTS (SELECT 1 FROM templates WHERE name = ?)",
            (new_name, old_name, new_name),
        )
        if cursor.rowcount:
            self._conn.execute(
                "UPDATE schedules SET template_name = ? WHERE template_name = ?",
                (new_name, old_name),
            )
        self._conn.commit()
        return cursor.rowcount > 0

    # -- learned-skill candidates (v4: the draft -> tested -> approved pipeline) ----

    _CANDIDATE_COLS = (
        "name, signature, status, template_json, source_task_ids_json, occurrences,"
        " test_task_id, test_outcome, decided_by, decided_at, decision_note,"
        " registry_name, created_at"
    )

    @staticmethod
    def _row_to_candidate(row: tuple[object, ...]) -> SkillCandidate:
        return SkillCandidate(
            name=str(row[0]),
            signature=str(row[1]),
            status=str(row[2]),
            template=template_from_dict(json.loads(str(row[3]))),
            source_task_ids=tuple(json.loads(str(row[4]))),
            occurrences=int(row[5]),  # type: ignore[call-overload]
            test_task_id=None if row[6] is None else str(row[6]),
            test_outcome=None if row[7] is None else str(row[7]),
            decided_by=None if row[8] is None else str(row[8]),
            decided_at=None if row[9] is None else str(row[9]),
            decision_note=None if row[10] is None else str(row[10]),
            registry_name=None if row[11] is None else str(row[11]),
            created_at=str(row[12]),
        )

    @_locked
    def add_candidate(self, candidate: SkillCandidate) -> None:
        """Insert or replace a candidate by name (also used to record a transition).

        Candidate names are content-addressed (``learned-<caste>-<sig8>``), so a
        re-proposal of an already-known recipe never reaches here — the propose layer
        filters by signature first — and a replace only ever carries a status change.
        """
        self._conn.execute(
            f"INSERT OR REPLACE INTO skill_candidates ({self._CANDIDATE_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.name,
                candidate.signature,
                candidate.status,
                json.dumps(template_to_dict(candidate.template)),
                json.dumps(list(candidate.source_task_ids)),
                candidate.occurrences,
                candidate.test_task_id,
                candidate.test_outcome,
                candidate.decided_by,
                candidate.decided_at,
                candidate.decision_note,
                candidate.registry_name,
                candidate.created_at or _now(),
            ),
        )
        self._conn.commit()

    @_locked
    def get_candidate(self, name: str) -> SkillCandidate | None:
        row = self._conn.execute(
            f"SELECT {self._CANDIDATE_COLS} FROM skill_candidates WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_candidate(row) if row is not None else None

    @_locked
    def list_candidates(self) -> list[SkillCandidate]:
        rows = self._conn.execute(
            f"SELECT {self._CANDIDATE_COLS} FROM skill_candidates ORDER BY created_at DESC, name"
        ).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    @_locked
    def remove_candidate(self, name: str) -> bool:
        cursor = self._conn.execute("DELETE FROM skill_candidates WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    # -- settings (v5) --------------------------------------------------------

    @_locked
    def get_setting(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    @_locked
    def set_setting(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=True), _now()),
        )
        self._conn.commit()

    @_locked
    def all_settings(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT key, value_json FROM settings").fetchall()
        return {str(key): json.loads(value) for key, value in rows}

    # -- llm usage tally (v74-F6) ---------------------------------------------
    # ollama.com has no account usage API (ollama/ollama#15663); this local
    # count of skep's own requests is the closest honest approximation.

    _USAGE_RETENTION_DAYS = 8

    @_locked
    def record_llm_usage(self, *, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self._USAGE_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._conn.execute("DELETE FROM llm_usage WHERE created_at < ?", (cutoff,))
        self._conn.execute(
            "INSERT INTO llm_usage (created_at, model, prompt_tokens, completion_tokens)"
            " VALUES (?, ?, ?, ?)",
            (_now(), model, prompt_tokens, completion_tokens),
        )
        self._conn.commit()

    @_locked
    def llm_usage_totals(self, *, hours: float) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0),"
            " COALESCE(SUM(completion_tokens), 0)"
            " FROM llm_usage WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        return {
            "requests": int(row[0]),
            "prompt_tokens": int(row[1]),
            "completion_tokens": int(row[2]),
            "total_tokens": int(row[1]) + int(row[2]),
        }

    # -- project policies (vx Stage A) ----------------------------------------

    _PROJECT_POLICY_COLS = (
        "project_id, name, strategy, phase, policy_json, pack_name, pack_version, "
        "created_at, updated_at"
    )

    @staticmethod
    def _row_to_project_policy(row: tuple[object, ...]) -> ProjectPolicyRecord:
        return ProjectPolicyRecord(
            project_id=str(row[0]),
            name=str(row[1]),
            strategy=str(row[2]),
            phase=str(row[3]),
            policy=dict(json.loads(str(row[4]))),
            pack_name=None if row[5] is None else str(row[5]),
            pack_version=None if row[6] is None else str(row[6]),
            created_at=str(row[7]),
            updated_at=str(row[8]),
        )

    @staticmethod
    def _row_to_project_binding(row: tuple[object, ...]) -> ProjectBindingRecord:
        return ProjectBindingRecord(
            project_id=str(row[0]),
            binding_kind=str(row[1]),
            binding_value=str(row[2]),
            created_at=str(row[3]),
        )

    @_locked
    def add_project_policy(
        self,
        *,
        project_id: str,
        name: str,
        strategy: str,
        phase: str,
        policy: dict[str, Any],
        pack_name: str | None = None,
        pack_version: str | None = None,
    ) -> None:
        from .projects import validate_project_definition

        definition = validate_project_definition(
            project_id=project_id,
            name=name,
            strategy=strategy,
            phase=phase,
            policy=policy,
            bindings=[],
            pack_name=pack_name,
            pack_version=pack_version,
        )
        project_id = definition.project_id
        name = definition.name
        strategy = definition.strategy
        phase = definition.phase
        policy = definition.policy
        existing = self.get_project_policy(project_id)
        created_at = existing.created_at if existing is not None else _now()
        self._conn.execute(
            f"INSERT OR REPLACE INTO project_policies ({self._PROJECT_POLICY_COLS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                name,
                strategy,
                phase,
                json.dumps(policy, ensure_ascii=True, sort_keys=True),
                definition.pack_name,
                definition.pack_version,
                created_at,
                _now(),
            ),
        )
        self._conn.commit()

    @_locked
    def get_project_policy(self, project_id: str) -> ProjectPolicyRecord | None:
        row = self._conn.execute(
            f"SELECT {self._PROJECT_POLICY_COLS} FROM project_policies WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return self._row_to_project_policy(row) if row is not None else None

    @_locked
    def list_project_policies(self) -> list[ProjectPolicyRecord]:
        rows = self._conn.execute(
            f"SELECT {self._PROJECT_POLICY_COLS} FROM project_policies ORDER BY name, project_id"
        ).fetchall()
        return [self._row_to_project_policy(row) for row in rows]

    @_locked
    def add_project_binding(
        self, *, project_id: str, binding_kind: str, binding_value: str
    ) -> None:
        from .projects import ProjectBinding, validate_project_binding

        binding = validate_project_binding(ProjectBinding(kind=binding_kind, value=binding_value))
        self._conn.execute(
            "INSERT OR REPLACE INTO project_bindings"
            " (project_id, binding_kind, binding_value, created_at)"
            " VALUES (?, ?, ?, ?)",
            (project_id, binding.kind, binding.value, _now()),
        )
        self._conn.commit()

    @_locked
    def project_bindings(self, project_id: str) -> list[ProjectBindingRecord]:
        rows = self._conn.execute(
            "SELECT project_id, binding_kind, binding_value, created_at"
            " FROM project_bindings WHERE project_id = ? ORDER BY binding_kind, binding_value",
            (project_id,),
        ).fetchall()
        return [self._row_to_project_binding(row) for row in rows]

    @_locked
    def remove_project_bindings(self, project_id: str) -> None:
        self._conn.execute("DELETE FROM project_bindings WHERE project_id = ?", (project_id,))
        self._conn.commit()

    @_locked
    def project_for_binding(
        self, binding_kind: str, binding_value: str
    ) -> ProjectPolicyRecord | None:
        row = self._conn.execute(
            "SELECT p.project_id, p.name, p.strategy, p.phase,"
            " p.policy_json, p.pack_name, p.pack_version, p.created_at, p.updated_at"
            " FROM project_bindings b"
            " JOIN project_policies p ON p.project_id = b.project_id"
            " WHERE b.binding_kind = ? AND b.binding_value = ?",
            (binding_kind, binding_value),
        ).fetchone()
        return self._row_to_project_policy(row) if row is not None else None

    @_locked
    def remove_project_policy(self, project_id: str) -> bool:
        self._conn.execute("DELETE FROM project_bindings WHERE project_id = ?", (project_id,))
        deleted = self._conn.execute(
            "DELETE FROM project_policies WHERE project_id = ?", (project_id,)
        ).rowcount
        self._conn.commit()
        return bool(deleted)

    # -- notes + tasks (v7 Stage B) ------------------------------------------

    @staticmethod
    def _row_to_note(row: tuple[object, ...]) -> NoteRecord:
        return NoteRecord(
            note_id=str(row[0]),
            content=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
        )

    @staticmethod
    def _row_to_task(row: tuple[object, ...]) -> TaskRecord:
        status = str(row[2])
        due_at = None if row[3] is None else str(row[3])
        return TaskRecord(
            task_id=str(row[0]),
            title=str(row[1]),
            status=status,
            due_at=due_at,
            due=bool(status == "todo" and due_at is not None and due_at <= _now()),
            created_at=str(row[4]),
            updated_at=str(row[5]),
        )

    def _record_note_task_event(
        self, *, kind: str, item_id: str, action: str, actor: str, detail: dict[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO note_task_events (kind, item_id, action, actor, detail_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, item_id, action, actor, json.dumps(detail, ensure_ascii=True), _now()),
        )

    @_locked
    def create_note(self, content: str, *, actor: str) -> NoteRecord:
        now = _now()
        note = NoteRecord(str(uuid.uuid4()), content, now, now)
        self._conn.execute(
            "INSERT INTO notes (note_id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (note.note_id, note.content, note.created_at, note.updated_at),
        )
        self._record_note_task_event(
            kind="note", item_id=note.note_id, action="created", actor=actor, detail={}
        )
        self._conn.commit()
        return note

    @_locked
    def list_notes(self) -> list[NoteRecord]:
        rows = self._conn.execute(
            "SELECT note_id, content, created_at, updated_at FROM notes"
            " ORDER BY updated_at DESC, note_id"
        ).fetchall()
        return [self._row_to_note(row) for row in rows]

    @_locked
    def get_note(self, note_id: str) -> NoteRecord | None:
        row = self._conn.execute(
            "SELECT note_id, content, created_at, updated_at FROM notes WHERE note_id = ?",
            (note_id,),
        ).fetchone()
        return self._row_to_note(row) if row is not None else None

    @_locked
    def update_note(self, note_id: str, *, content: str, actor: str) -> NoteRecord | None:
        now = _now()
        cursor = self._conn.execute(
            "UPDATE notes SET content = ?, updated_at = ? WHERE note_id = ?",
            (content, now, note_id),
        )
        if cursor.rowcount == 0:
            self._conn.commit()
            return None
        self._record_note_task_event(
            kind="note", item_id=note_id, action="updated", actor=actor, detail={}
        )
        self._conn.commit()
        return self.get_note(note_id)

    @_locked
    def delete_note(self, note_id: str, *, actor: str) -> bool:
        cursor = self._conn.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
        removed = cursor.rowcount > 0
        if removed:
            self._record_note_task_event(
                kind="note", item_id=note_id, action="deleted", actor=actor, detail={}
            )
        self._conn.commit()
        return removed

    @_locked
    def create_task(self, title: str, *, actor: str, due_at: str | None = None) -> TaskRecord:
        now = _now()
        task_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO tasks (task_id, title, status, due_at, created_at, updated_at)"
            " VALUES (?, ?, 'todo', ?, ?, ?)",
            (task_id, title, due_at, now, now),
        )
        self._record_note_task_event(
            kind="task", item_id=task_id, action="created", actor=actor, detail={}
        )
        self._conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"created task {task_id!r} disappeared")
        return task

    @_locked
    def list_tasks(self) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT task_id, title, status, due_at, created_at, updated_at FROM tasks"
            " ORDER BY status, due_at IS NULL, due_at, updated_at DESC, task_id"
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    @_locked
    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT task_id, title, status, due_at, created_at, updated_at"
            " FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._row_to_task(row) if row is not None else None

    @_locked
    def update_task(
        self,
        task_id: str,
        *,
        title: str,
        status: str,
        due_at: str | None,
        actor: str,
        action: str = "updated",
    ) -> TaskRecord | None:
        now = _now()
        cursor = self._conn.execute(
            "UPDATE tasks SET title = ?, status = ?, due_at = ?, updated_at = ? WHERE task_id = ?",
            (title, status, due_at, now, task_id),
        )
        if cursor.rowcount == 0:
            self._conn.commit()
            return None
        self._record_note_task_event(
            kind="task",
            item_id=task_id,
            action=action,
            actor=actor,
            detail={"status": status, "due_at": due_at},
        )
        self._conn.commit()
        return self.get_task(task_id)

    @_locked
    def delete_task(self, task_id: str, *, actor: str) -> bool:
        cursor = self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        removed = cursor.rowcount > 0
        if removed:
            self._record_note_task_event(
                kind="task", item_id=task_id, action="deleted", actor=actor, detail={}
            )
        self._conn.commit()
        return removed

    @_locked
    def note_task_events(self) -> list[NoteTaskEventRecord]:
        rows = self._conn.execute(
            "SELECT id, kind, item_id, action, actor, detail_json, created_at"
            " FROM note_task_events ORDER BY id"
        ).fetchall()
        return [
            NoteTaskEventRecord(
                id=int(row[0]),
                kind=str(row[1]),
                item_id=str(row[2]),
                action=str(row[3]),
                actor=str(row[4]),
                detail=dict(json.loads(str(row[5]))),
                created_at=str(row[6]),
            )
            for row in rows
        ]

    # -- curated memory (v13): proposals, durable items, FTS search ------------

    def _memory_proposal_sources(self, proposal_id: str) -> tuple[MemorySource, ...]:
        rows = self._conn.execute(
            "SELECT source_kind, source_id FROM memory_sources WHERE proposal_id = ? ORDER BY id",
            (proposal_id,),
        ).fetchall()
        return tuple(MemorySource(kind=str(row[0]), source_id=str(row[1])) for row in rows)

    def _row_to_memory_proposal(self, row: Any) -> MemoryProposal:
        return MemoryProposal(
            proposal_id=str(row[0]),
            memory_class=str(row[1]),
            content=str(row[2]),
            state=str(row[3]),
            actor=str(row[4]),
            rationale=None if row[5] is None else str(row[5]),
            project_id=None if row[6] is None else str(row[6]),
            created_at=str(row[7]),
            updated_at=str(row[8]),
            decided_at=None if row[9] is None else str(row[9]),
            decided_by=None if row[10] is None else str(row[10]),
            decision_reason=None if row[11] is None else str(row[11]),
            sources=self._memory_proposal_sources(str(row[0])),
        )

    _MEMORY_PROPOSAL_COLS = (
        "proposal_id, memory_class, content, state, actor, rationale, project_id,"
        " created_at, updated_at, decided_at, decided_by, decision_reason"
    )

    @_locked
    def create_memory_proposal(
        self,
        *,
        memory_class: str,
        content: str,
        actor: str,
        state: str = "pending_review",
        rationale: str | None = None,
        project_id: str | None = None,
        sources: Sequence[MemorySource] = (),
    ) -> MemoryProposal:
        validate_memory_class(memory_class)
        validate_proposal_state(state)
        for source in sources:
            validate_source_kind(source.kind)
        proposal_id = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO memory_proposals (proposal_id, memory_class, content, state, actor,"
            " rationale, project_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, memory_class, content, state, actor, rationale, project_id, now, now),
        )
        for source in sources:
            self._conn.execute(
                "INSERT INTO memory_sources (proposal_id, source_kind, source_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (proposal_id, source.kind, source.source_id, now),
            )
        self._record_note_task_event(
            kind="memory_proposal",
            item_id=proposal_id,
            action="created",
            actor=actor,
            detail={"memory_class": memory_class, "state": state},
        )
        self._conn.commit()
        proposal = self.get_memory_proposal(proposal_id)
        if proposal is None:
            raise RuntimeError(f"created memory proposal {proposal_id!r} disappeared")
        return proposal

    @_locked
    def get_memory_proposal(self, proposal_id: str) -> MemoryProposal | None:
        row = self._conn.execute(
            f"SELECT {self._MEMORY_PROPOSAL_COLS} FROM memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return self._row_to_memory_proposal(row) if row is not None else None

    @_locked
    def list_memory_proposals(self, *, state: str | None = None) -> list[MemoryProposal]:
        if state is not None:
            validate_proposal_state(state)
            rows = self._conn.execute(
                f"SELECT {self._MEMORY_PROPOSAL_COLS} FROM memory_proposals"
                " WHERE state = ? ORDER BY created_at DESC, proposal_id",
                (state,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {self._MEMORY_PROPOSAL_COLS} FROM memory_proposals"
                " ORDER BY created_at DESC, proposal_id"
            ).fetchall()
        return [self._row_to_memory_proposal(row) for row in rows]

    def _row_to_memory_item(self, row: Any) -> MemoryItem:
        return MemoryItem(
            memory_id=str(row[0]),
            memory_class=str(row[1]),
            content=str(row[2]),
            project_id=None if row[3] is None else str(row[3]),
            proposal_id=None if row[4] is None else str(row[4]),
            active=bool(row[5]),
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )

    _MEMORY_ITEM_COLS = (
        "memory_id, memory_class, content, project_id, proposal_id, active, created_at, updated_at"
    )

    @_locked
    def add_memory_item(
        self,
        *,
        memory_class: str,
        content: str,
        actor: str,
        project_id: str | None = None,
        proposal_id: str | None = None,
    ) -> MemoryItem:
        """Insert a durable memory item directly. The governed path is a proposal
        approval (Step 4); this is the low-level write it and tests build on."""
        validate_memory_class(memory_class)
        memory_id = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO memory_items (memory_id, memory_class, content, project_id,"
            " proposal_id, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (memory_id, memory_class, content, project_id, proposal_id, now, now),
        )
        self._conn.execute(
            "INSERT INTO memory_fts (content, memory_id) VALUES (?, ?)",
            (content, memory_id),
        )
        self._record_note_task_event(
            kind="memory_item",
            item_id=memory_id,
            action="created",
            actor=actor,
            detail={"memory_class": memory_class, "project_id": project_id},
        )
        self._conn.commit()
        item = self.get_memory_item(memory_id)
        if item is None:
            raise RuntimeError(f"created memory item {memory_id!r} disappeared")
        return item

    @_locked
    def get_memory_item(self, memory_id: str) -> MemoryItem | None:
        row = self._conn.execute(
            f"SELECT {self._MEMORY_ITEM_COLS} FROM memory_items WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return self._row_to_memory_item(row) if row is not None else None

    @_locked
    def list_memory_items(
        self, *, project_id: str | None = None, include_forgotten: bool = False
    ) -> list[MemoryItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_forgotten:
            clauses.append("active = 1")
        if project_id is not None:
            # Project-scoped view also includes global (unscoped) memory.
            clauses.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {self._MEMORY_ITEM_COLS} FROM memory_items{where}"
            " ORDER BY created_at DESC, memory_id",
            tuple(params),
        ).fetchall()
        return [self._row_to_memory_item(row) for row in rows]

    @staticmethod
    def _fts_match(query: str) -> str | None:
        """Quote each token so arbitrary user text is a safe FTS5 MATCH."""
        tokens = [token for token in query.replace('"', " ").split() if token]
        if not tokens:
            return None
        return " ".join(f'"{token}"' for token in tokens)

    @_locked
    def search_memory(
        self, query: str, *, project_id: str | None = None, limit: int = 50
    ) -> list[MemoryItem]:
        match = self._fts_match(query)
        if match is None:
            return []
        rows = self._conn.execute(
            "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
        items: list[MemoryItem] = []
        for row in rows:
            item = self.get_memory_item(str(row[0]))
            if item is None or not item.active:
                continue
            if project_id is not None and item.project_id not in (None, project_id):
                continue
            items.append(item)
        return items

    @_locked
    def forget_memory_item(self, memory_id: str, *, actor: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE memory_items SET active = 0, updated_at = ? WHERE memory_id = ? AND active = 1",
            (_now(), memory_id),
        )
        forgotten = cursor.rowcount > 0
        if forgotten:
            self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            self._record_note_task_event(
                kind="memory_item", item_id=memory_id, action="forgotten", actor=actor, detail={}
            )
        self._conn.commit()
        return forgotten

    @_locked
    def expire_observations(self, *, ttl_days: int, actor: str = "ticker") -> list[str]:
        """v71-F5: sweep observation items past their TTL — deactivated with an
        'expired' event each, so the record says why the memory went away."""
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._conn.execute(
            "SELECT memory_id FROM memory_items WHERE memory_class = 'observation'"
            " AND active = 1 AND created_at < ?",
            (cutoff,),
        ).fetchall()
        expired = [str(row[0]) for row in rows]
        for memory_id in expired:
            self._conn.execute(
                "UPDATE memory_items SET active = 0, updated_at = ? WHERE memory_id = ?",
                (_now(), memory_id),
            )
            self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            self._record_note_task_event(
                kind="memory_item",
                item_id=memory_id,
                action="expired",
                actor=actor,
                detail={"ttl_days": ttl_days},
            )
        self._conn.commit()
        return expired

    @_locked
    def count_memory_items(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM memory_items"
        if active_only:
            sql += " WHERE active = 1"
        row = self._conn.execute(sql).fetchone()
        return int(row[0]) if row is not None else 0

    def _decide_proposal(
        self,
        proposal_id: str,
        *,
        target_state: str,
        actor: str,
        reason: str | None,
    ) -> MemoryProposal:
        proposal = self.get_memory_proposal(proposal_id)
        if proposal is None:
            raise MemoryError(f"no memory proposal {proposal_id!r}")
        require_transition(proposal.state, target_state)  # raises on illegal transition
        now = _now()
        self._conn.execute(
            "UPDATE memory_proposals SET state = ?, updated_at = ?, decided_at = ?,"
            " decided_by = ?, decision_reason = ? WHERE proposal_id = ?",
            (target_state, now, now, actor, reason, proposal_id),
        )
        self._record_note_task_event(
            kind="memory_proposal",
            item_id=proposal_id,
            action=target_state,
            actor=actor,
            detail={"reason": reason} if reason is not None else {},
        )
        updated = self.get_memory_proposal(proposal_id)
        assert updated is not None
        return updated

    @_locked
    def approve_memory_proposal(self, proposal_id: str, *, actor: str) -> MemoryItem:
        """Approve a pending proposal: promote it into a durable memory item.

        The only path a proposal becomes durable memory. Enforces the legal
        transition (only ``pending_review`` may be approved) and audit-records
        both the decision and the item creation."""
        proposal = self.get_memory_proposal(proposal_id)
        if proposal is None:
            raise MemoryError(f"no memory proposal {proposal_id!r}")
        require_transition(proposal.state, "approved")
        item = self.add_memory_item(
            memory_class=proposal.memory_class,
            content=proposal.content,
            actor=actor,
            project_id=proposal.project_id,
            proposal_id=proposal_id,
        )
        self._decide_proposal(proposal_id, target_state="approved", actor=actor, reason=None)
        self._conn.commit()
        return item

    @_locked
    def reject_memory_proposal(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> MemoryProposal:
        """Reject a proposal with a recorded reason. It never becomes memory."""
        proposal = self._decide_proposal(
            proposal_id, target_state="rejected", actor=actor, reason=reason
        )
        self._conn.commit()
        return proposal

    @_locked
    def request_memory_clarification(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> MemoryProposal:
        proposal = self._decide_proposal(
            proposal_id, target_state="needs_clarification", actor=actor, reason=reason
        )
        self._conn.commit()
        return proposal

    @_locked
    def resubmit_memory_proposal(self, proposal_id: str, *, actor: str) -> MemoryProposal:
        """Return a clarified proposal to the review queue."""
        proposal = self._decide_proposal(
            proposal_id, target_state="pending_review", actor=actor, reason=None
        )
        self._conn.commit()
        return proposal

    # -- provider profile registry (v14) ---------------------------------------

    _PROVIDER_COLS = (
        "provider_id, protocol, base_url, model, allowed_network_hosts_json, cost_class,"
        " fallback_order, api_key_env, active"
    )

    @staticmethod
    def _row_to_provider(row: Any) -> ProviderProfile:
        return ProviderProfile(
            provider_id=str(row[0]),
            protocol=str(row[1]),
            base_url=str(row[2]),
            model=str(row[3]),
            allowed_network_hosts=tuple(json.loads(str(row[4]))),
            cost_class=str(row[5]),
            fallback_order=int(row[6]),
            api_key_env=None if row[7] is None else str(row[7]),
            active=bool(row[8]),
        )

    @_locked
    def upsert_provider_profile(self, profile: ProviderProfile) -> ProviderProfile:
        normalized = validate_provider_profile(profile)
        now = _now()
        if normalized.active:
            self._conn.execute("UPDATE provider_profiles SET active = 0")
        self._conn.execute(
            "INSERT INTO provider_profiles (provider_id, protocol, base_url, model,"
            " allowed_network_hosts_json, cost_class, fallback_order, api_key_env, active,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider_id) DO UPDATE SET protocol = excluded.protocol,"
            " base_url = excluded.base_url, model = excluded.model,"
            " allowed_network_hosts_json = excluded.allowed_network_hosts_json,"
            " cost_class = excluded.cost_class, fallback_order = excluded.fallback_order,"
            " api_key_env = excluded.api_key_env, active = excluded.active,"
            " updated_at = excluded.updated_at",
            (
                normalized.provider_id,
                normalized.protocol,
                normalized.base_url,
                normalized.model,
                json.dumps(list(normalized.allowed_network_hosts)),
                normalized.cost_class,
                normalized.fallback_order,
                normalized.api_key_env,
                1 if normalized.active else 0,
                now,
                now,
            ),
        )
        self._conn.commit()
        return normalized

    @_locked
    def get_provider_profile(self, provider_id: str) -> ProviderProfile | None:
        row = self._conn.execute(
            f"SELECT {self._PROVIDER_COLS} FROM provider_profiles WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        return self._row_to_provider(row) if row is not None else None

    @_locked
    def list_provider_profiles(self) -> list[ProviderProfile]:
        rows = self._conn.execute(
            f"SELECT {self._PROVIDER_COLS} FROM provider_profiles"
            " ORDER BY fallback_order, provider_id"
        ).fetchall()
        return [self._row_to_provider(row) for row in rows]

    @_locked
    def active_provider_profile(self) -> ProviderProfile | None:
        row = self._conn.execute(
            f"SELECT {self._PROVIDER_COLS} FROM provider_profiles WHERE active = 1 LIMIT 1"
        ).fetchone()
        return self._row_to_provider(row) if row is not None else None

    @_locked
    def set_active_provider(self, provider_id: str) -> bool:
        if self.get_provider_profile(provider_id) is None:
            return False
        self._conn.execute("UPDATE provider_profiles SET active = 0")
        self._conn.execute(
            "UPDATE provider_profiles SET active = 1, updated_at = ? WHERE provider_id = ?",
            (_now(), provider_id),
        )
        self._conn.commit()
        return True

    @_locked
    def delete_provider_profile(self, provider_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM provider_profiles WHERE provider_id = ?", (provider_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_provider_health(row: Any) -> ProviderHealth:
        return ProviderHealth(
            provider_id=str(row[0]),
            reachable=bool(row[1]),
            model_found=bool(row[2]),
            latency_ms=None if row[3] is None else int(row[3]),
            error=None if row[4] is None else str(row[4]),
            checked_at=str(row[5]),
        )

    _PROVIDER_HEALTH_COLS = "provider_id, reachable, model_found, latency_ms, error, checked_at"

    @_locked
    def record_provider_health(self, health: ProviderHealth) -> None:
        self._conn.execute(
            "INSERT INTO provider_health (provider_id, reachable, model_found, latency_ms,"
            " error, checked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                health.provider_id,
                1 if health.reachable else 0,
                1 if health.model_found else 0,
                health.latency_ms,
                health.error,
                health.checked_at,
            ),
        )
        self._conn.commit()

    @_locked
    def latest_provider_health(self, provider_id: str) -> ProviderHealth | None:
        row = self._conn.execute(
            f"SELECT {self._PROVIDER_HEALTH_COLS} FROM provider_health"
            " WHERE provider_id = ? ORDER BY id DESC LIMIT 1",
            (provider_id,),
        ).fetchone()
        return self._row_to_provider_health(row) if row is not None else None

    @_locked
    def list_provider_health(self) -> list[ProviderHealth]:
        """The latest health record per provider."""
        rows = self._conn.execute(
            f"SELECT {self._PROVIDER_HEALTH_COLS} FROM provider_health ph"
            " WHERE id = (SELECT MAX(id) FROM provider_health WHERE provider_id = ph.provider_id)"
            " ORDER BY provider_id"
        ).fetchall()
        return [self._row_to_provider_health(row) for row in rows]

    # -- node registry (v15) ---------------------------------------------------

    _NODE_COLS = "node_id, name, host, kind, trust_tier, allowed_capabilities_json, metadata_json"

    @staticmethod
    def _row_to_node(row: Any) -> Node:
        return Node(
            node_id=str(row[0]),
            name=str(row[1]),
            host=str(row[2]),
            kind=str(row[3]),
            trust_tier=str(row[4]),
            allowed_capabilities=tuple(json.loads(str(row[5]))),
            metadata=dict(json.loads(str(row[6]))),
        )

    @_locked
    def upsert_node(self, node: Node) -> Node:
        normalized = validate_node(node)
        now = _now()
        self._conn.execute(
            "INSERT INTO nodes (node_id, name, host, kind, trust_tier,"
            " allowed_capabilities_json, metadata_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(node_id) DO UPDATE SET name = excluded.name, host = excluded.host,"
            " kind = excluded.kind, trust_tier = excluded.trust_tier,"
            " allowed_capabilities_json = excluded.allowed_capabilities_json,"
            " metadata_json = excluded.metadata_json, updated_at = excluded.updated_at",
            (
                normalized.node_id,
                normalized.name,
                normalized.host,
                normalized.kind,
                normalized.trust_tier,
                json.dumps(list(normalized.allowed_capabilities)),
                json.dumps(dict(normalized.metadata)),
                now,
                now,
            ),
        )
        self._conn.commit()
        return normalized

    @_locked
    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute(
            f"SELECT {self._NODE_COLS} FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row is not None else None

    @_locked
    def list_nodes(self) -> list[Node]:
        rows = self._conn.execute(
            f"SELECT {self._NODE_COLS} FROM nodes ORDER BY node_id"
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    @_locked
    def delete_node(self, node_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # -- channel configs (v16) -------------------------------------------------

    @staticmethod
    def _row_to_channel_config(row: Any) -> ChannelConfig:
        return ChannelConfig(
            channel=str(row[0]),
            enabled=bool(row[1]),
            channel_can_confirm=bool(row[2]),
            allowed_identities=tuple(json.loads(str(row[3]))),
            require_mention=bool(row[4]),
            auto_thread=bool(row[5]),
            allowed_users=tuple(json.loads(str(row[6]))),
            notification_level=str(row[7]),
        )

    _CHANNEL_CONFIG_COLUMNS = (
        "channel, enabled, channel_can_confirm, allowed_identities_json,"
        " require_mention, auto_thread, allowed_users_json, notification_level"
    )

    @_locked
    def upsert_channel_config(self, config: ChannelConfig) -> ChannelConfig:
        if config.channel not in CHANNELS:
            raise ValueError(
                f"unknown channel {config.channel!r}; expected one of {sorted(CHANNELS)!r}"
            )
        if config.notification_level not in NOTIFICATION_LEVELS:
            raise ValueError(
                f"unknown notification_level {config.notification_level!r};"
                f" expected one of {list(NOTIFICATION_LEVELS)!r}"
            )
        self._conn.execute(
            "INSERT INTO channel_configs (channel, enabled, channel_can_confirm,"
            " allowed_identities_json, require_mention, auto_thread, allowed_users_json,"
            " notification_level, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(channel) DO UPDATE SET enabled = excluded.enabled,"
            " channel_can_confirm = excluded.channel_can_confirm,"
            " allowed_identities_json = excluded.allowed_identities_json,"
            " require_mention = excluded.require_mention,"
            " auto_thread = excluded.auto_thread,"
            " allowed_users_json = excluded.allowed_users_json,"
            " notification_level = excluded.notification_level,"
            " updated_at = excluded.updated_at",
            (
                config.channel,
                1 if config.enabled else 0,
                1 if config.channel_can_confirm else 0,
                json.dumps(list(config.allowed_identities)),
                1 if config.require_mention else 0,
                1 if config.auto_thread else 0,
                json.dumps(list(config.allowed_users)),
                config.notification_level,
                _now(),
            ),
        )
        self._conn.commit()
        return config

    @_locked
    def get_channel_config(self, channel: str) -> ChannelConfig | None:
        row = self._conn.execute(
            f"SELECT {self._CHANNEL_CONFIG_COLUMNS} FROM channel_configs WHERE channel = ?",
            (channel,),
        ).fetchone()
        return self._row_to_channel_config(row) if row is not None else None

    # -- channel sessions (v26-F2): messenger conversation -> chat session ----

    @_locked
    def channel_session(self, session_key: str) -> ChannelSessionBinding | None:
        row = self._conn.execute(
            "SELECT channel, identity_id, chat_id, thread_ref FROM channel_sessions"
            " WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if row is None:
            return None
        return ChannelSessionBinding(
            channel=str(row[0]),
            identity_id=str(row[1]),
            chat_id=str(row[2]),
            thread_ref=str(row[3]) if row[3] is not None else None,
        )

    @_locked
    def set_channel_session_thread(self, session_key: str, thread_ref: str) -> None:
        """v78-F6: remember the messenger-side thread anchor for outbound
        pushes. Overwrites — the newest inbound message wins."""
        self._conn.execute(
            "UPDATE channel_sessions SET thread_ref = ? WHERE session_key = ?",
            (thread_ref, session_key),
        )
        self._conn.commit()

    @_locked
    def channel_binding_for_chat(self, chat_id: str) -> ChannelSessionBinding | None:
        """v44-F2: the reverse lookup — which messenger conversation (if any)
        a chat belongs to, so scheduled/system messages can be pushed OUT to
        it. The newest binding wins (a chat rebound to a fresh thread pushes
        there)."""
        row = self._conn.execute(
            "SELECT channel, identity_id, chat_id, thread_ref FROM channel_sessions"
            " WHERE chat_id = ? ORDER BY created_at DESC, session_key DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return ChannelSessionBinding(
            channel=str(row[0]),
            identity_id=str(row[1]),
            chat_id=str(row[2]),
            thread_ref=str(row[3]) if row[3] is not None else None,
        )

    # v107-F1: one predicate, two views. Preserved = crashed/timed-out/failed
    # with no successor (v72-F8 widened — a failed external-engine run's warm
    # tree is the resume value), plus completed runs whose re-verification is
    # absent (still running) or unconfirmed-with-a-patch (the evidence for
    # diagnose_run). A resume releases the tree; confirmed/patch-less
    # completions never enter the set (dispatch removes those directly).
    _PRESERVED_PREDICATE = (
        " FROM runs r"
        " WHERE r.workspace IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM runs s WHERE s.resume_of = r.task_id)"
        " AND (r.state IN ('worker_crashed', 'worker_timeout', 'failed')"
        "      OR (r.state = 'completed' AND EXISTS ("
        "            SELECT 1 FROM reverifications v WHERE v.task_id = r.task_id"
        "            AND v.confirmed = 0 AND v.outcome != 'not_applicable'))"
        "      OR (r.state = 'completed' AND NOT EXISTS ("
        "            SELECT 1 FROM reverifications v WHERE v.task_id = r.task_id)))"
    )

    @_locked
    def preserved_run_workspaces(self, *, max_age_seconds: float | None = None) -> list[str]:
        """Workspaces the orphan sweep must spare (fresh preserved trees)."""
        sql = "SELECT r.workspace" + self._PRESERVED_PREDICATE
        params: tuple[Any, ...] = ()
        if max_age_seconds is not None:
            cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            sql += " AND r.updated_at >= ?"
            params = (cutoff,)
        rows = self._conn.execute(sql, params).fetchall()
        return [str(row[0]) for row in rows]

    @_locked
    def expired_preserved_worktrees(self, *, max_age_seconds: float) -> list[tuple[str, str]]:
        """(repo, workspace) pairs past the preservation TTL — sweep targets."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = self._conn.execute(
            "SELECT r.repo, r.workspace" + self._PRESERVED_PREDICATE + " AND r.updated_at < ?",
            (cutoff,),
        ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    @_locked
    def preserved_resumable_runs(
        self,
        *,
        repo: str,
        states: Sequence[str],
        ref: str | None = None,
        max_age_seconds: float | None = None,
    ) -> list[tuple[str, str, str, str]]:
        """v109-F6: (task_id, state, workspace, updated_at) rows for this
        repo's preserved runs in the given resumable states, newest first —
        the dispatch surface's "a warm tree already exists" lookup. Same
        predicate as the sweep views (one source of truth); the state filter
        drops the completed-unconfirmed half, which diagnose_run serves but
        resume never will. The caller checks the tree still exists on disk."""
        placeholders = ", ".join("?" for _ in states)
        sql = (
            "SELECT r.task_id, r.state, r.workspace, r.updated_at"
            + self._PRESERVED_PREDICATE
            + f" AND r.repo = ? AND r.state IN ({placeholders})"
        )
        params: list[Any] = [repo, *states]
        if ref is not None:
            sql += " AND r.ref = ?"
            params.append(ref)
        if max_age_seconds is not None:
            cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            sql += " AND r.updated_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY r.updated_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    @_locked
    def latest_channel_chat(self) -> str | None:
        """v72-F3: the newest messenger-bound chat that still exists — the
        delivery target for system alarms that belong to no particular chat
        (provider health). None when no messenger has ever bound a chat."""
        row = self._conn.execute(
            "SELECT cs.chat_id FROM channel_sessions cs"
            " JOIN chats c ON c.chat_id = cs.chat_id"
            " ORDER BY cs.created_at DESC, cs.session_key DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    @_locked
    def bind_channel_session(
        self, *, session_key: str, channel: str, identity_id: str, chat_id: str
    ) -> ChannelSessionBinding:
        self._conn.execute(
            "INSERT INTO channel_sessions (session_key, channel, identity_id, chat_id,"
            " created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET chat_id = excluded.chat_id",
            (session_key, channel, identity_id, chat_id, _now()),
        )
        self._conn.commit()
        return ChannelSessionBinding(channel=channel, identity_id=identity_id, chat_id=chat_id)

    # -- webhooks (v44-F3): inbound event subscriptions ------------------------

    @_locked
    def add_webhook(self, *, name: str, template: str, chat_id: str | None) -> WebhookRecord:
        created = _now()
        self._conn.execute(
            "INSERT INTO webhooks (name, template, chat_id, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET template = excluded.template,"
            " chat_id = excluded.chat_id",
            (name, template, chat_id, created),
        )
        self._conn.commit()
        return WebhookRecord(name=name, template=template, chat_id=chat_id, created_at=created)

    @_locked
    def get_webhook(self, name: str) -> WebhookRecord | None:
        row = self._conn.execute(
            "SELECT name, template, chat_id, created_at FROM webhooks WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return WebhookRecord(
            name=str(row[0]),
            template=str(row[1]),
            chat_id=None if row[2] is None else str(row[2]),
            created_at=str(row[3]),
        )

    @_locked
    def list_webhooks(self) -> list[WebhookRecord]:
        rows = self._conn.execute(
            "SELECT name, template, chat_id, created_at FROM webhooks ORDER BY name"
        ).fetchall()
        return [
            WebhookRecord(
                name=str(row[0]),
                template=str(row[1]),
                chat_id=None if row[2] is None else str(row[2]),
                created_at=str(row[3]),
            )
            for row in rows
        ]

    @_locked
    def remove_webhook(self, name: str) -> bool:
        cursor = self._conn.execute("DELETE FROM webhooks WHERE name = ?", (name,))
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def list_channel_configs(self) -> list[ChannelConfig]:
        rows = self._conn.execute(
            f"SELECT {self._CHANNEL_CONFIG_COLUMNS} FROM channel_configs ORDER BY channel"
        ).fetchall()
        return [self._row_to_channel_config(row) for row in rows]

    # -- chats (v6: sessions with the Queen's own model) -----------------------

    @_locked
    def create_chat(self, *, title: str, model: str | None, source: str = "web") -> ChatRecord:
        now = _now()
        chat = ChatRecord(
            chat_id=str(uuid.uuid4()),
            title=title,
            model=model,
            created_at=now,
            updated_at=now,
            source=source,
        )
        self._conn.execute(
            "INSERT INTO chats (chat_id, title, model, created_at, updated_at, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chat.chat_id, chat.title, chat.model, chat.created_at, chat.updated_at, chat.source),
        )
        self._conn.commit()
        return chat

    _CHAT_COLUMNS = (
        "chat_id, title, model, created_at, updated_at, source, personality,"
        " context_summary, COALESCE(compacted_through, 0), project_id,"
        " provider_ceiling_chars, active_tools_json"
    )

    @staticmethod
    def _row_to_chat(row: Any) -> ChatRecord:
        record = ChatRecord(*row[:-1])
        if not row[-1]:
            return record
        return replace(record, active_tools=json.loads(row[-1]))

    @_locked
    def get_chat(self, chat_id: str) -> ChatRecord | None:
        row = self._conn.execute(
            f"SELECT {self._CHAT_COLUMNS} FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return self._row_to_chat(row) if row is not None else None

    @_locked
    def list_chats(self) -> list[ChatRecord]:
        rows = self._conn.execute(
            f"SELECT {self._CHAT_COLUMNS} FROM chats ORDER BY updated_at DESC, chat_id"
        ).fetchall()
        return [self._row_to_chat(row) for row in rows]

    @_locked
    def chats_with_messages_after(self, message_id: int) -> list[tuple[str, int]]:
        """v53-F1: (chat_id, newest message id) for chats with transcript rows
        past the observer's cursor — the sweep's work list."""
        rows = self._conn.execute(
            "SELECT chat_id, MAX(id) FROM chat_messages WHERE id > ? GROUP BY chat_id",
            (message_id,),
        ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    @_locked
    def chat_overviews(self, limit: int = 20) -> list[dict[str, Any]]:
        """v53-F3: recent chats for the browse tool — one row per chat with
        its message count, newest activity first."""
        rows = self._conn.execute(
            "SELECT c.chat_id, c.title, c.created_at, c.updated_at, c.source, COUNT(m.id)"
            " FROM chats c LEFT JOIN chat_messages m ON m.chat_id = c.chat_id"
            " GROUP BY c.chat_id ORDER BY c.updated_at DESC, c.chat_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "chat_id": str(row[0]),
                "title": str(row[1]),
                "created_at": str(row[2]),
                "updated_at": str(row[3]),
                "source": str(row[4]),
                "message_count": int(row[5]),
            }
            for row in rows
        ]

    @_locked
    def set_chat_title(self, chat_id: str, title: str) -> None:
        self._conn.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))
        self._conn.commit()

    @_locked
    def set_chat_personality(self, chat_id: str, personality: str | None) -> bool:
        cursor = self._conn.execute(
            "UPDATE chats SET personality = ? WHERE chat_id = ?", (personality, chat_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def set_chat_model(self, chat_id: str, model: str | None) -> bool:
        """v72-F1: per-chat model override; None falls back to the saved default."""
        cursor = self._conn.execute(
            "UPDATE chats SET model = ? WHERE chat_id = ?", (model, chat_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def set_chat_project(self, chat_id: str, project_id: str | None) -> bool:
        """v56-F4: remember the project this chat works on (last one wins).

        v96-F2: None clears it — the operator unbinding the composer selector.
        """
        cursor = self._conn.execute(
            "UPDATE chats SET project_id = ? WHERE chat_id = ?", (project_id, chat_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def add_chat_active_tools(self, chat_id: str, names: list[str]) -> bool:
        """v74-F3: merge described tools into the chat's active set (dedup,
        first-described order kept)."""
        row = self._conn.execute(
            "SELECT active_tools_json FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return False
        current = json.loads(row[0]) if row[0] else []
        merged = list(dict.fromkeys([*current, *names]))
        if merged == current:
            return True
        self._conn.execute(
            "UPDATE chats SET active_tools_json = ? WHERE chat_id = ?",
            (json.dumps(merged, ensure_ascii=True), chat_id),
        )
        self._conn.commit()
        return True

    @_locked
    def set_chat_provider_ceiling(self, chat_id: str, chars: int) -> bool:
        """v73-F1: persist the replay budget that got past a provider 4xx —
        the chat heals durably instead of shrinking every turn."""
        cursor = self._conn.execute(
            "UPDATE chats SET provider_ceiling_chars = ? WHERE chat_id = ?",
            (chars, chat_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def set_chat_context(self, chat_id: str, *, summary: str, compacted_through: int) -> bool:
        """v56-F2: advance the compaction cursor + digest. Replay-side only —
        chat_messages rows are never touched."""
        cursor = self._conn.execute(
            "UPDATE chats SET context_summary = ?, compacted_through = ? WHERE chat_id = ?",
            (summary, compacted_through, chat_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def remove_chat(self, chat_id: str) -> bool:
        self._conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        self._conn.execute("DELETE FROM chat_actions WHERE chat_id = ?", (chat_id,))
        cursor = self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    @_locked
    def add_chat_message(
        self,
        chat_id: str,
        *,
        role: str,
        content: str,
        thinking: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_name: str | None = None,
        attachments: list[str] | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        now = _now()
        cursor = self._conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, thinking, tool_calls_json,"
            " tool_name, tool_call_id, created_at, attachments_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                role,
                content,
                thinking,
                None if tool_calls is None else json.dumps(tool_calls, ensure_ascii=True),
                tool_name,
                tool_call_id,
                now,
                None if not attachments else json.dumps(attachments),
            ),
        )
        self._conn.execute("UPDATE chats SET updated_at = ? WHERE chat_id = ?", (now, chat_id))
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    _ACTION_COLS = (
        "action_id, chat_id, tool, args_json, status, result_json, created_at, resolved_at,"
        " source, decided_by, tool_call_id"
    )

    @staticmethod
    def _row_to_action(row: tuple[object, ...]) -> ChatActionRecord:
        return ChatActionRecord(
            action_id=str(row[0]),
            chat_id=str(row[1]),
            tool=str(row[2]),
            args=dict(json.loads(str(row[3]))),
            status=str(row[4]),
            result=None if row[5] is None else json.loads(str(row[5])),
            created_at=str(row[6]),
            resolved_at=None if row[7] is None else str(row[7]),
            source=str(row[8]),
            decided_by=None if row[9] is None else str(row[9]),
            tool_call_id=None if row[10] is None else str(row[10]),
        )

    @_locked
    def add_chat_action(
        self,
        chat_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        source: str = "assistant",
        decided_by: str | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        action_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO chat_actions"
            " (action_id, chat_id, tool, args_json, status, created_at, source, decided_by,"
            "  tool_call_id)"
            " VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?)",
            (
                action_id,
                chat_id,
                tool,
                json.dumps(args, ensure_ascii=True),
                _now(),
                source,
                decided_by,
                tool_call_id,
            ),
        )
        self._conn.commit()
        return action_id

    @_locked
    def record_resolved_chat_action(
        self,
        chat_id: str,
        *,
        tool: str,
        args: dict[str, Any],
        result: Any,
        source: str = "assistant",
        decided_by: str | None = None,
    ) -> str:
        """v61-F1: an auto-allowed mutation records its row born resolved.

        Auto-allowed mutations execute inside the turn with no card, so
        nothing landed in chat_actions and chat_for_task could not route
        their runs (the v59-F2/F3 notifications stayed silent for exactly
        the trusted dispatches). A single INSERT — never a transient
        'proposed' row the auto-deny sweep or the badge poll could see."""
        action_id = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO chat_actions"
            " (action_id, chat_id, tool, args_json, status, result_json,"
            "  created_at, resolved_at, source, decided_by)"
            " VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)",
            (
                action_id,
                chat_id,
                tool,
                json.dumps(args, ensure_ascii=True),
                json.dumps(result, ensure_ascii=True),
                now,
                now,
                source,
                decided_by,
            ),
        )
        self._conn.commit()
        return action_id

    @_locked
    def get_chat_action(self, action_id: str) -> ChatActionRecord | None:
        row = self._conn.execute(
            f"SELECT {self._ACTION_COLS} FROM chat_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return self._row_to_action(row) if row is not None else None

    @_locked
    def chat_actions(self, chat_id: str) -> list[ChatActionRecord]:
        rows = self._conn.execute(
            f"SELECT {self._ACTION_COLS} FROM chat_actions WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        return [self._row_to_action(row) for row in rows]

    @_locked
    def pending_chat_actions(self, chat_id: str) -> list[ChatActionRecord]:
        rows = self._conn.execute(
            f"SELECT {self._ACTION_COLS} FROM chat_actions"
            " WHERE chat_id = ? AND status = 'proposed' ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        return [self._row_to_action(row) for row in rows]

    @_locked
    def supersede_chat_action(self, action_id: str, *, note: str) -> None:
        """v109-F2: a newer proposal for the same subject replaces a pending
        card. Recorded exactly like the resolution-side supersede (v63-F2) —
        an honest terminal row plus a tool line in the transcript — never a
        silent delete. A card that is no longer 'proposed' is left alone."""
        row = self._conn.execute(
            "SELECT chat_id, tool, status FROM chat_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None or str(row[2]) != "proposed":
            return
        payload = {"ok": True, "superseded": True, "note": note}
        self._conn.execute(
            "UPDATE chat_actions SET status = 'superseded', result_json = ?,"
            " resolved_at = ? WHERE action_id = ?",
            (json.dumps(payload, ensure_ascii=True), _now(), action_id),
        )
        self.add_chat_message(
            str(row[0]),
            role="tool",
            tool_name=str(row[1]),
            content=json.dumps(payload, ensure_ascii=True),
        )
        self._conn.commit()

    @_locked
    def pending_cards_older_than(self, seconds: int) -> list[ChatActionRecord]:
        """v54-F1: proposed cards across ALL chats stale past the cutoff —
        the ticker's auto-deny sweep. ISO-Z timestamps compare lexically.

        v106-F6: the clock measures OPERATOR ABSENCE, not card age. The sweep
        exists because "a timeout is the human not pulling it" (ADR 0032) —
        but 15 cards auto-denied in one field day while the operator was
        actively typing in the owning chat. A card now expires only when both
        its creation AND the chat's last operator message are older than the
        cutoff; cards in an abandoned chat die exactly as before.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._conn.execute(
            f"SELECT {self._ACTION_COLS} FROM chat_actions a"
            " WHERE a.status = 'proposed' AND a.created_at < ?"
            " AND COALESCE((SELECT MAX(m.created_at) FROM chat_messages m"
            "               WHERE m.chat_id = a.chat_id AND m.role = 'user'), '') < ?",
            (cutoff, cutoff),
        ).fetchall()
        return [self._row_to_action(row) for row in rows]

    @_locked
    def add_run_steering(self, task_id: str, *, actor: str, text: str) -> int:
        """v69-F4: record an operator steering note for a running react run."""
        cursor = self._conn.execute(
            "INSERT INTO run_steering (task_id, actor, text, created_at) VALUES (?, ?, ?, ?)",
            (task_id, actor, text, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    @_locked
    def steering_for(self, task_id: str) -> list[tuple[str, str, str]]:
        """(actor, text, created_at) rows for a task, oldest first."""
        rows = self._conn.execute(
            "SELECT actor, text, created_at FROM run_steering WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    @_locked
    def card_resolution_elsewhere(self, card: ChatActionRecord) -> str | None:
        """v63-F2 (sweep belt): the honest note when a proposed card's
        underlying review/run already resolved through another surface —
        the sweep must never record "timed out" for work that shipped."""
        if card.tool in {"approve_review", "deny_review"}:
            review_id = str(card.args.get("review_id", ""))
            approval = self.get_approval(review_id) if review_id else None
            if approval is None or approval.status == "pending":
                return None
            note = f"resolved elsewhere: {approval.status} by {approval.resolved_by or 'unknown'}"
            if approval.status == "approved" and approval.landing_branch:
                note = f"{note}, applied on {approval.landing_branch}"
            return note
        if card.tool == "land_run":
            task_id = str(card.args.get("task_id", ""))
            if not task_id:
                return None
            for approval in self.approvals_for(task_id):
                if approval.status == "approved" and approval.landing_branch:
                    return (
                        "resolved elsewhere: approved by"
                        f" {approval.resolved_by or 'unknown'},"
                        f" applied on {approval.landing_branch}"
                    )
        return None

    @_locked
    def chat_for_task(self, task_id: str) -> str | None:
        """v43-F4: the chat whose action dispatched this run, if any — the
        dispatch result rides chat_actions.result_json with the task id.
        ponytail: LIKE over a small table; task ids are UUIDs, collision-free."""
        if not task_id:
            return None
        row = self._conn.execute(
            "SELECT chat_id FROM chat_actions WHERE result_json LIKE ?"
            " ORDER BY created_at DESC LIMIT 1",
            (f"%{task_id}%",),
        ).fetchone()
        return str(row[0]) if row is not None else None

    @_locked
    def resolve_chat_action(self, action_id: str, *, status: str, result: Any) -> None:
        """Record the verdict; like approvals, resolution is final.

        v63-F2, the one exception: confirming an approve_review/land_run card
        executes resolve_approval, whose reconciliation supersedes cards for
        that decision — including the card mid-confirm (both executors gate on
        'proposed' BEFORE executing, so no other path arrives here on a
        superseded row). The executed verdict is the truer record; every
        other transition stays final. v87-F2 widens the exception to 'denied'
        for the same reason: a gate mirror's Deny executes deny_review, whose
        reconciliation supersedes the very card mid-deny.
        """
        row = self._conn.execute(
            "SELECT status FROM chat_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no chat action {action_id!r}")
        if row[0] != "proposed" and not (
            row[0] == "superseded" and status in ("confirmed", "denied")
        ):
            raise ValueError(f"chat action {action_id!r} already resolved as {row[0]!r}")
        self._conn.execute(
            "UPDATE chat_actions SET status = ?, result_json = ?, resolved_at = ?"
            " WHERE action_id = ?",
            (status, json.dumps(result, ensure_ascii=True), _now(), action_id),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_chat_message(row: Any) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=int(row[0]),
            chat_id=str(row[1]),
            role=str(row[2]),
            content=str(row[3]),
            thinking=None if row[4] is None else str(row[4]),
            tool_calls=None if row[5] is None else list(json.loads(str(row[5]))),
            tool_name=None if row[6] is None else str(row[6]),
            created_at=str(row[7]),
            attachments=None if row[8] is None else list(json.loads(str(row[8]))),
            tool_call_id=None if row[9] is None else str(row[9]),
        )

    _CHAT_MESSAGE_COLS = (
        "id, chat_id, role, content, thinking, tool_calls_json, tool_name,"
        " created_at, attachments_json, tool_call_id"
    )

    @_locked
    def chat_messages(
        self, chat_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[ChatMessageRecord]:
        # v53-F3: limit/offset paginate the scroll tool; the default stays
        # "everything" for the transcript replay callers.
        rows = self._conn.execute(
            f"SELECT {self._CHAT_MESSAGE_COLS}"
            " FROM chat_messages WHERE chat_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (chat_id, -1 if limit is None else limit, offset),
        ).fetchall()
        return [self._row_to_chat_message(row) for row in rows]

    @_locked
    def chat_messages_after(self, after_id: int, *, limit: int = 50) -> list[ChatMessageRecord]:
        """v72-F4: the global (all-chats) scan the observation harvest sweeps —
        message ids are one monotonic sequence, so ``after_id`` is an exact
        watermark and a capped sweep resumes precisely where it stopped."""
        rows = self._conn.execute(
            f"SELECT {self._CHAT_MESSAGE_COLS} FROM chat_messages WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [self._row_to_chat_message(row) for row in rows]

    @_locked
    def chat_messages_around(
        self, chat_id: str, message_id: int, *, before: int = 10, after: int = 10
    ) -> list[ChatMessageRecord]:
        """v83-F3: the window around ONE message — the scroll a search hit
        needs. Ids are one global monotonic sequence, so a chat's ids are
        NOT contiguous: two bounded scans on the (chat_id, id) index give
        the exact window (anchor included when it exists in this chat)."""
        preceding = self._conn.execute(
            f"SELECT {self._CHAT_MESSAGE_COLS}"
            " FROM chat_messages WHERE chat_id = ? AND id <= ?"
            " ORDER BY id DESC LIMIT ?",
            (chat_id, message_id, before + 1),
        ).fetchall()
        following = self._conn.execute(
            f"SELECT {self._CHAT_MESSAGE_COLS}"
            " FROM chat_messages WHERE chat_id = ? AND id > ?"
            " ORDER BY id LIMIT ?",
            (chat_id, message_id, after),
        ).fetchall()
        rows = [*reversed(preceding), *following]
        return [self._row_to_chat_message(row) for row in rows]

    @_locked
    def search_chats(
        self, query: str, *, limit: int = 20, chat_id: str | None = None
    ) -> list[ChatSearchHit]:
        """v51-F1: FTS5 over the transcript, best match first. Only what a
        human said or read (user/assistant rows) — tool traffic is noise.
        v53-F3: ``chat_id`` scopes the search to one conversation."""
        match = self._fts_match(query)
        if match is None:
            return []
        scope = "" if chat_id is None else " AND m.chat_id = ?"
        params: list[Any] = [match]
        if chat_id is not None:
            params.append(chat_id)
        params.append(limit)
        rows = self._conn.execute(
            "SELECT m.chat_id, c.title, m.id, m.role, m.created_at,"
            " snippet(chat_fts, 0, '[', ']', ' … ', 12), c.source"
            " FROM chat_fts"
            " JOIN chat_messages m ON m.id = chat_fts.rowid"
            " JOIN chats c ON c.chat_id = m.chat_id"
            f" WHERE chat_fts MATCH ? AND m.role IN ('user', 'assistant'){scope}"
            # v84-F8 (A5): at equal relevance an imported transcript never
            # outranks the operator's own words.
            " ORDER BY rank, (c.source = 'hermes-import') LIMIT ?",
            params,
        ).fetchall()
        return [
            ChatSearchHit(
                chat_id=str(row[0]),
                chat_title=str(row[1]),
                message_id=int(row[2]),
                role=str(row[3]),
                created_at=str(row[4]),
                snippet=str(row[5]),
                source=str(row[6]),
            )
            for row in rows
        ]

    # -- v83-F8: background processes ---------------------------------------

    _PROCESS_COLS = "proc_id, command, cwd, pid, status, exit_code, log_path, started_at, ended_at"

    @staticmethod
    def _row_to_process(row: Any) -> ProcessRecord:
        return ProcessRecord(
            proc_id=str(row[0]),
            command=str(row[1]),
            cwd=None if row[2] is None else str(row[2]),
            pid=int(row[3]),
            status=str(row[4]),
            exit_code=None if row[5] is None else int(row[5]),
            log_path=str(row[6]),
            started_at=str(row[7]),
            ended_at=None if row[8] is None else str(row[8]),
        )

    @_locked
    def add_process(self, record: ProcessRecord) -> None:
        self._conn.execute(
            f"INSERT INTO processes ({self._PROCESS_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.proc_id,
                record.command,
                record.cwd,
                record.pid,
                record.status,
                record.exit_code,
                record.log_path,
                record.started_at,
                record.ended_at,
            ),
        )
        self._conn.commit()

    @_locked
    def list_processes(self) -> list[ProcessRecord]:
        rows = self._conn.execute(
            f"SELECT {self._PROCESS_COLS} FROM processes ORDER BY started_at DESC"
        ).fetchall()
        return [self._row_to_process(row) for row in rows]

    @_locked
    def get_process(self, proc_id: str) -> ProcessRecord | None:
        row = self._conn.execute(
            f"SELECT {self._PROCESS_COLS} FROM processes WHERE proc_id = ?", (proc_id,)
        ).fetchone()
        return None if row is None else self._row_to_process(row)

    @_locked
    def mark_process(self, proc_id: str, *, status: str, exit_code: int | None = None) -> None:
        self._conn.execute(
            "UPDATE processes SET status = ?, exit_code = ?, ended_at = ? WHERE proc_id = ?",
            (status, exit_code, _now(), proc_id),
        )
        self._conn.commit()
