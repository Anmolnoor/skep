/* skep — the face (v5 Stage F). No build step: ES modules + fetch + EventSource.
   Phase 10 10.1: the shared helpers live in js/ (dom, api, sse, store, diff);
   pages move to js/pages/ one at a time — this file still holds the rest. */
import { renderContextPanel } from "./room.js";
import { viewSettings } from "./settings.js";
import { installSidebar, refreshSidebar, renderThreads } from "./sidebar.js";
import { viewPolicies } from "./policies.js";
import { el, iconButton, notFound } from "./js/dom.js";
import { api, REAUTH, setToken, showLogin, token } from "./js/api.js";
import { liveSource, onceByEventId, streamSse } from "./js/sse.js";
import { renderDiff } from "./js/diff.js";
import { viewInbox } from "./js/pages/inbox.js";
import { forensicsSection, routeLive, viewReview } from "./js/pages/run-detail.js";
import { viewMemory, viewNotesTasks, viewTemplates } from "./js/pages/library.js";
import { viewSchedules } from "./js/pages/schedules.js";
import { providerShelf, stagedConnection } from "./js/pages/settings-provider.js";

// ---------- tiny view helpers ----------

// v130-F10: the working line's Stop. A direct POST — typed text queues behind
// the turn (v103-F1), so a typed /stop would arrive only after it ended.
function stopButton(chatId) {
  return el("button", {
    type: "button",
    class: "chat-stop",
    onclick: async () => {
      try { flash("ok", (await api("POST", `/api/chats/${chatId}/stop`)).detail); }
      catch (err) { flash("bad", err.message); }
    },
  }, "Stop");
}

function fmtTs(ts) { return ts ? ts.replace("T", " ").replace("Z", "") : "-"; }
function stateChip(state) { return el("span", { class: `state ${state}` }, state); }

// v101-F8: the phase badge is the shared .chip primitive with a tone. Both
// render sites went through the same longhand class pair before, and the
// stylesheet carried four near-identical tinted-background rules to serve them.
const PHASE_TONE = {
  build: "tone-accent",
  bootstrap: "tone-warn",
  maintain: "tone-info",
  publish_candidate: "tone-ok",
};
function phaseChip(phase) {
  return el("span", { class: `chip upper ${PHASE_TONE[phase] || "tone-muted"}` }, phase);
}

// v101-F10: every caste select is built from GET /api/workers, so a caste added
// to the registry is dispatchable from the UI with no edit here. Two hardcoded
// options made five of seven castes unreachable from Assign; the summary is the
// registry's own string, the same one the Queen's tool schema reads (F12), so
// the operator and the model are told the same thing in the same words.
function casteSelect(castes, selected = "coding") {
  const select = el("select", {},
    castes.map(c => el("option", { value: c.name, title: c.summary }, c.name)));
  select.value = selected;
  return select;
}

const casteSummary = (castes, name) =>
  castes.find(c => c.name === name)?.summary || "";

// ---------- v75-F1: shared view furniture (tabs, filters, time, sparkline) ----

// "in 4h 12m" / "2h ago" — relative time for schedules and cards. Callers
// that mean "overdue" for a past timestamp map that themselves.
function relativeTime(ts) {
  const date = ts ? new Date(ts) : null;
  if (!date || Number.isNaN(date.getTime())) return "-";
  let diff = date.getTime() - Date.now();
  const past = diff < 0;
  diff = Math.abs(diff);
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  const span = days > 0 ? `${days}d ${hours % 24}h`
    : hours > 0 ? `${hours}h ${mins % 60}m`
      : mins > 0 ? `${mins}m` : "<1m";
  return past ? `${span} ago` : `in ${span}`;
}

// Shared tab bar (Settings, Templates, Run detail). Panels persist across
// switches (hidden, never rebuilt) so a live SSE log keeps streaming while
// another tab is open; a tab's render() runs once, on first activation.
function buildTabBar(tabs, options = {}) {
  const content = el("div", { class: "tab-content" });
  const bar = el("div", { class: "tab-bar" });
  const panels = new Map();
  const buttons = new Map();
  const rendered = new Set();
  const activate = async (key) => {
    for (const [k, button] of buttons) button.classList.toggle("active", k === key);
    for (const [k, panel] of panels) panel.classList.toggle("hidden", k !== key);
    if (options.onActivate) options.onActivate(key);
    const tab = tabs.find(t => t.key === key);
    if (tab?.render && !rendered.has(key)) {
      rendered.add(key);
      try { await tab.render(panels.get(key)); }
      catch (e) { rendered.delete(key); flash("bad", e.message); }
    }
  };
  for (const tab of tabs) {
    const panel = el("div", { class: "tab-panel hidden" });
    panels.set(tab.key, panel);
    content.append(panel);
    const button = el("button", {
      type: "button",
      class: "tab-button",
      onclick: () => activate(tab.key),
    }, tab.label);
    buttons.set(tab.key, button);
    bar.append(button);
  }
  if (tabs.length) {
    const initial = options.initial && panels.has(options.initial)
      ? options.initial : tabs[0].key;
    activate(initial);
  }
  return { bar, content, panels, activate };
}

// Tab-style filter row with per-filter counts; onChange(key) re-renders.
function buildFilterBar(filters, counts, onChange) {
  const bar = el("div", { class: "filter-bar" });
  for (const f of filters) {
    const tab = el("button", {
      type: "button",
      class: `filter-tab${f === filters[0] ? " active" : ""}`,
      onclick: () => {
        for (const b of bar.children) b.classList.toggle("active", b === tab);
        onChange(f.key);
      },
    }, f.label, el("span", { class: "filter-count" }, String(counts[f.key] ?? 0)));
    bar.append(tab);
  }
  return bar;
}

// Tiny inline SVG bar sparkline — each value is pass/fail (ok/bad fill).
function buildSparkline(values) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "stat-sparkline");
  svg.setAttribute("viewBox", `0 0 ${Math.max(values.length, 1) * 4} 20`);
  svg.setAttribute("aria-hidden", "true");
  values.forEach((pass, i) => {
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", String(i * 4));
    rect.setAttribute("y", pass ? "2" : "9");
    rect.setAttribute("width", "3");
    rect.setAttribute("height", pass ? "18" : "11");
    rect.setAttribute("rx", "1");
    rect.setAttribute("class", pass ? "spark-ok" : "spark-bad");
    svg.append(rect);
  });
  return svg;
}

// Phase 05: `missing` is onboard's pending steps — the one setup truth.
const SETUP_MISSING_LABELS = {
  model: "model (provider and key)",
  workdir: "work folder",
  sandbox: "sandbox",
  template: "policy baseline",
  role: "role",
};

function setupMissingLabels(setup) {
  return (setup.missing || []).map(key => SETUP_MISSING_LABELS[key] || key);
}

function formatDecision(decision) {
  if (!decision || typeof decision !== "object") return "";
  const verdict = typeof decision.verdict === "string" ? decision.verdict : "";
  const reason = typeof decision.reason === "string" ? decision.reason : "";
  const detail = typeof decision.detail === "string" && decision.detail
    ? ` (${decision.detail})`
    : "";
  return verdict && reason ? `${verdict} ${reason}${detail}` : "";
}

function formatCompactDecision(decision, namespace) {
  if (!decision || typeof decision !== "object") return "";
  const verdict = typeof decision.verdict === "string" ? decision.verdict : "";
  let reason = typeof decision.reason === "string" ? decision.reason : "";
  const prefix = `${namespace}.`;
  if (reason.startsWith(prefix)) reason = reason.slice(prefix.length);
  return verdict && reason ? `${verdict} ${reason}` : "";
}

// first-run R3 (ADR 0055 D5): who delegated a run — roles.delegation_line's
// twin: "queen@operator → manager@builder → worker@builder · team <id>".
// R1: the role picker's options. A chat made before every chat recorded a role
// has none stored and runs as assistant (ADR 0059 decision #4) — say so.
function roleChoices(roles, legacyRoleLess) {
  return [...(legacyRoleLess ? [{ value: "", label: "role: assistant (none stored)" }] : []),
    ...roles.map(role => ({ value: role.name, label: `role: ${role.name}` }))];
}

// A new chat starts in the default workspace project (setup.py's
// DEFAULT_WORKSPACE_PROJECT_ID) when one is set up; the selector can still
// pick "no project" or any other project before the first message.
function newChatProject(projects) {
  return projects.find(p => p.project_id === "workspace") || null;
}

function delegationText(delegation) {
  if (!delegation || typeof delegation !== "object") return "";
  const chain = Array.isArray(delegation.chain) ? delegation.chain.map(String) : [];
  const worker = delegation.role ? `worker@${delegation.role}` : "worker";
  const line = [...(chain.length ? chain : ["operator"]), worker].join(" → ");
  return delegation.team ? `${line} · team ${delegation.team}` : line;
}

// first-run O4 (ADR 0056 D6): while first_run.json is active the server
// puts a one-line `why` on every verdict card and ends the turn with a
// `tally` event; dismissed, neither arrives and nothing renders.
function whyLine(d) {
  return d.why ? el("p", { class: "note card-why" }, `why: ${d.why}`) : null;
}

// v90-F3: a receipt — an action that ran WITHOUT asking because a grant the
// operator already gave covers it. Same headline and risk as the approval
// card would have shown, and deliberately no buttons: it is a record, not a
// decision. Silence here used to be indistinguishable from nothing happening.
// T3-2 (web twin of v115-F3): `decision` rides a denial too (v40-F10), and
// the receipt said "ran without asking" on every refusal. The kicker follows
// the verdict — the same set as AutonomyDecision.allows_execution().
// v112-F1 → P3-4 (ADR 0059 D4): the server says which keep choices a card can
// honour (card.keep); only a card with a real choice earns a keep selector —
// everything else approves once, and the UI never offers a lifetime the
// confirm would refuse (B32). A session keep holds in this chat only (B39).
const KEEP_LABELS = { once: "once", session: "this chat, this session", always: "always" };

function keepSelect(card) {
  const keeps = (card && Array.isArray(card.keep)) ? card.keep : [];
  if (keeps.length < 2) return null;
  return el("select", { class: "card-keep", title: "How long this approval holds" },
    ...keeps.map((k) => el("option",
      k === "session" ? { value: k, selected: "" } : { value: k }, KEEP_LABELS[k] || k)));
}

// P4-1 (ADR 0059 D5): a tainted chat's card says why in plain words (the
// server's card.taint), and a doubly tainted one is HIGH — it looks different.
function taintNote(card) {
  return card && card.taint ? el("p", { class: "note card-taint" }, card.taint) : null;
}

function cardClass(card) {
  return card && card.tier === "high" ? "confirm-card card-high" : "confirm-card";
}

// P4-2 (ADR 0059 D6): a HIGH card says so in words, not only with a red
// border — and why, and that it holds once and no messenger can confirm it.
function tierNote(card) {
  if (!card || card.tier !== "high") return null;
  return el("p", { class: "card-high-label" },
    el("strong", {}, "HIGH risk: "), card.high || "see details",
    ". It holds once, and only the web UI or the CLI can confirm it.");
}

// P4-2 (ADR 0059 D6): a folded card lists every call one approval covers —
// exactly what the server recorded; nothing beyond this list runs.
function foldItems(card) {
  if (!card || !Array.isArray(card.items) || card.items.length < 2) return null;
  return el("div", { class: "card-items" },
    el("p", { class: "note" }, `${card.items.length} items, one approval covers exactly these:`),
    el("ul", {}, ...card.items.map((item) => el("li", { class: "mono" }, String(item)))));
}

// L11 (ADR 0059 D3, V7): a delegated ask is approve-once for that run — the
// server refuses to remember it (409), so no "& remember" button offers it.
function offersRemember(approval) {
  return !(approval && approval.decision
    && approval.decision.reason === "capability.require_approval.delegated_ask");
}

// P4-2 (ADR 0059 D6): the dispatch manifest — what the run will get, as the
// server compiled it when the card was shown. Approving the card approves the
// listed asks for that run only; anything else the run asks for cards then.
function manifestNode(card) {
  const m = card && card.manifest;
  if (!m) return null;
  if (Array.isArray(m.members)) { // D23: a batch lists each run it starts
    return el("div", {}, ...m.members.flatMap((member, i) => [
      el("p", { class: "note" }, `Run ${i + 1} of ${m.members.length}:`),
      manifestNode({ manifest: member })]));
  }
  if (m.error) {
    return el("p", { class: "note card-manifest" }, `The run's envelope could not be compiled: ${m.error}`);
  }
  const list = (values) => (Array.isArray(values) && values.length
    ? values.map((v) => (Array.isArray(v) ? v.join(" ") : String(v))).join(", ") : "none");
  const row = (label, value) => el("div", { class: "arg-row" },
    el("span", { class: "arg-key" }, `${label}:`), el("span", { class: "arg-value mono" }, value));
  const asks = [...(m.network_ask || []), ...(m.shell_ask || [])];
  return el("div", { class: "card-manifest" },
    el("p", { class: "note" }, "What this run gets:"),
    row("hosts", list(m.network)),
    // D10: a role chain dropped the project's `*` — say so and why
    m.network_note ? el("p", { class: "note" }, String(m.network_note)) : null,
    // D5: a named network adds to the project's hosts; only [] drops them
    Array.isArray(m.network_added) && m.network_added.length
      ? row("adds to the project's hosts for this run", list(m.network_added)) : null,
    Array.isArray(m.network_dropped) && m.network_dropped.length
      ? row("loses the project's own hosts for this run", list(m.network_dropped)) : null,
    row("model provider", list(m.provider_hosts)),
    // D6: loopback is closed but for these ports; an older card has no key
    "loopback_ports" in m
      ? row("loopback ports", m.loopback_ports.length ? list(m.loopback_ports) : "closed") : null,
    row("commands it may run", list(m.shell_allowlist)),
    row("may write", list(m.write)),
    // W13: the other task grants; a key an older card did not record stays off
    "read" in m ? row("may read", list(m.read)) : null,
    "env_allowlist" in m ? row("environment", list(m.env_allowlist)) : null,
    m.allow_git_mutation ? row("git", "may commit and branch in its own worktree") : null,
    Array.isArray(m.allowed_plugin_risks) && m.allowed_plugin_risks.length
      ? row("plugin risks allowed", list(m.allowed_plugin_risks)) : null,
    "allowed_tools" in m
      ? row("tools", m.allowed_tools === null ? "all its tools" : list(m.allowed_tools)) : null,
    m.auto_apply_verified_patch
      ? row("auto-apply", `a re-verified patch lands on ${m.auto_apply_branch || "a new skep/<task> branch"} without asking`)
      : null,
    row("engine · mode", `${m.engine} · ${m.execution_mode}`),
    row("verify", m.verify_command || "the worker picks"),
    // ES-F124: whose login a codex run spends (the server decided it)
    m.engine_auth === "chatgpt_login" ? row("login", "uses your ChatGPT plan login (Codex)") : null,
    asks.length ? row("also approved, this run only", list(asks)) : null,
    Array.isArray(m.chain) && m.chain.length ? row("delegated by", m.chain.join(" → ")) : null,
    Array.isArray(m.taint) && m.taint.length ? row("from a chat that has", m.taint.join(" + ")) : null);
}

// P4-2 (ADR 0059 D6, F3 lesson 1): a script, chained or replacing schedule
// shows what it will really do — the server computed every line (the env the
// script gets, the scheduler's next fire times), the card only lists them.
function scheduleNode(card) {
  const s = card && card.schedule;
  if (!s) return null;
  const row = (label, value) => el("div", { class: "arg-row" },
    el("span", { class: "arg-key" }, `${label}:`), el("span", { class: "arg-value mono" }, value));
  return el("div", { class: "card-schedule" },
    s.command ? row("command", s.command) : null,
    s.every ? row("every", s.every) : null,
    Array.isArray(s.next) ? row("next runs", s.next.join(", ")) : null,
    s.error ? row("will not save", s.error) : null,
    Array.isArray(s.env) ? row("environment", s.env.join(", ") || "(empty)") : null,
    Array.isArray(s.chain) ? row("reads the output of", s.chain.join(" ← ")) : null,
    s.replaces ? row("replaces", s.replaces) : null,
    s.runs ? el("p", { class: "card-risk" }, el("strong", {}, s.runs)) : null);
}

// Phase 07 (7.2, I6): a promote_tool card shows what its confirm installs —
// the bound triple (source digest, lock digest, hosts), the hash-pinned wheels
// and the full source diff, all computed by the server when it was proposed.
function forgeNode(card) {
  const f = card && card.forge;
  if (!f) return null;
  const row = (label, value) => el("div", { class: "arg-row" },
    el("span", { class: "arg-key" }, `${label}:`), el("span", { class: "arg-value mono" }, value));
  if (f.error) return el("div", { class: "card-forge" }, row("cannot install", f.error));
  const hosts = Array.isArray(f.approved_hosts) && f.approved_hosts.length
    ? f.approved_hosts.join(", ") : "none (deny-all)";
  return el("div", { class: "card-forge" },
    row("source sha256", f.source_sha256),
    row("lock sha256", f.lock_sha256 || "none (stdlib only)"),
    row("network", hosts),
    Array.isArray(f.locked) && f.locked.length ? row("wheels", f.locked.join("\n")) : null,
    f.diff ? el("details", { class: "card-details" },
      el("summary", {}, "full source diff"), el("pre", { class: "mono" }, f.diff)) : null);
}

// Phase 08 (8.2, I8): a capture_skill card shows the full SKILL.md its
// confirm saves, drafted by the server when the card was proposed.
function skillNode(card) {
  const text = card && card.skill_md;
  return text ? el("pre", { class: "mono card-skill" }, text) : null;
}

// P4-1 (ADR 0059 D5): a run dispatched from a tainted chat says so on its card.
function taintChip(decision) {
  const bits = decision?.constraints?.chat_taint;
  if (!Array.isArray(bits) || !bits.length) return null;
  return el("span", { class: "chip", title: "the dispatching chat's taint" },
    `chat: ${bits.map(String).join(" + ")}`);
}

// P4-1 (B26, B37): a row you did not write says who did — a webhook, a
// script, a worker (`from webhook:ci`), or skep's own prompt (`skep`).
function authorLabel(message, fallback) {
  const author = message.provenance || "";
  if (message.role !== "user" || !author || author === "operator") return fallback;
  return author === "queen" ? "skep" : `from ${author}`;
}

// v128-F13: a user row skep wrote itself ("This is the run we were working
// on. Continue…") explains what woke the Queen — shown, but as a muted note,
// never as the operator's own bubble.
function isInternalNudge(message) {
  return message.role === "user" && message.provenance === "queen";
}

// What a keep would allow (card.keep_covers): each command of a compound line
// on its own, or a whole URL directory — said on the card, not only "always".
function keepCovers(card) {
  if (!card || !card.keep_covers || keepSelect(card) === null) return null;
  return el("p", { class: "card-keep-covers" }, card.keep_covers);
}

// P3-5 (V11): what a policy write would change, from the server's write-time
// check — the replay of recent decisions, the grants it would shadow, and what
// an "always" keep would flip (or why the card keeps only once).
function cardReplay(card) {
  if (!card) return null;
  const lines = [card.replay, ...(card.policy_warnings || []), card.keep_refused,
    keepSelect(card) === null ? null : card.keep_replay].filter(Boolean);
  if (!lines.length) return null;
  return el("div", { class: "card-replay" }, ...lines.map((line) => el("p", {}, line)));
}

function receiptNode(d) {
  const card = d.card || {};
  const decision = d.decision || {};
  const allowed = ["allow", "allow_with_constraints"].includes(decision.verdict);
  const covered = decision.detail || decision.reason;
  const denied = decision.reason
    ? `denied by ${decision.reason}${decision.detail ? ` (${decision.detail})` : ""}`
    : "denied by policy";
  return el("div", { class: "confirm-card receipt-card" },
    el("p", { class: "card-kicker" },
      allowed ? "Ran without asking — you approved this already" : "Denied — this did not run"),
    el("p", { class: "card-headline mono" }, card.headline || d.tool),
    card.risk ? el("p", { class: "card-risk" },
      el("span", { class: "card-risk-label" }, "Risk: "), card.risk) : null,
    !allowed ? el("p", { class: "card-purpose" }, denied)
      : covered ? el("p", { class: "card-purpose" }, `covered by ${covered}`) : null,
    whyLine(d),
    // v106-F11 (v90-F3): which tier of grant, given when.
    d.grant ? el("p", { class: "note" },
      `${d.grant.tier} grant · given ${d.grant.granted_at}`) : null);
}

function formatRunAutonomy(run) {
  if (!run || typeof run !== "object") return "-";
  const parts = [];
  const dispatch = formatCompactDecision(run.dispatch_decision, "dispatch");
  const landing = formatCompactDecision(run.landing_decision, "landing");
  if (dispatch) parts.push(`d:${dispatch}`);
  if (landing) parts.push(`l:${landing}`);
  return parts.join("  ") || "-";
}

function formatPolicyBlock(block) {
  if (!block || typeof block !== "object") return "";
  const parts = [];
  const decision = formatDecision(block.decision);
  if (decision) parts.push(`policy: ${decision}`);
  if (typeof block.command === "string" && block.command) parts.push(block.command);
  if (typeof block.detail === "string" && block.detail) parts.push(block.detail);
  return parts.join("  ");
}

function formatProjectContext(project) {
  if (!project || typeof project !== "object") return "";
  const projectId = typeof project.project_id === "string" ? project.project_id : "";
  const strategy = typeof project.strategy === "string" ? project.strategy : "";
  const phase = typeof project.phase === "string" ? project.phase : "";
  const bindingKind = typeof project.binding_kind === "string" ? project.binding_kind : "";
  const bindingValue = typeof project.binding_value === "string" ? project.binding_value : "";
  const parts = [];
  if (projectId) parts.push(projectId);
  if (strategy || phase) parts.push([strategy, phase].filter(Boolean).join("/"));
  if (bindingKind && bindingValue) parts.push(`${bindingKind}: ${bindingValue}`);
  return parts.join("  ");
}

function renderSuggestionGrant(suggestion) {
  const template = suggestion?.template || {};
  const profile = suggestion?.profile || {};
  const grants = [];
  if (Array.isArray(template.network) && template.network.length) {
    grants.push(["network", template.network.join(", ")]);
  }
  if (Array.isArray(template.env_allowlist) && template.env_allowlist.length) {
    grants.push(["env", template.env_allowlist.join(", ")]);
  }
  if (Array.isArray(template.shell_allowlist) && template.shell_allowlist.length) {
    grants.push(["shell", template.shell_allowlist.map(command => command.join(" ")).join("  |  ")]);
  }
  if (template.allow_git_mutation) grants.push(["git", "mutation allowed"]);
  if (!grants.length) grants.push(["grants", "no extra permissions"]);

  const sourceCount = Array.isArray(profile.source_entry_ids)
    ? profile.source_entry_ids.length
    : 0;
  return el("div", { class: "suggestion-grants" },
    grants.map(([label, value]) => el("p", {},
      el("span", { class: "suggestion-grant-key" }, label), ` ${value}`)),
    el("p", { class: "note" }, `${sourceCount} remembered approval(s) matched`));
}

async function previewTemplateSuggestion({ name, repo, instructions, caste }) {
  const query = new URLSearchParams({ name, repo, instructions, caste }).toString();
  return api("GET", `/api/suggestions?${query}`);
}

async function confirmTemplateSuggestion(name, body) {
  return api("POST", `/api/suggestions/${encodeURIComponent(name)}/confirm`, body);
}

function summarizeRunEvent(event) {
  const payload = event?.payload;
  if (!payload || typeof payload !== "object") return JSON.stringify(payload);

  if (event.type === "run.created" || event.type === "task.start") {
    const parts = [];
    const project = formatProjectContext(payload.project_context);
    if (project) parts.push(`project: ${project}`);
    const dispatch = formatDecision(payload.dispatch_decision);
    const landing = formatDecision(payload.landing_decision);
    if (dispatch) parts.push(`dispatch: ${dispatch}`);
    if (landing) parts.push(`landing: ${landing}`);
    return parts.join("  ") || JSON.stringify(payload);
  }

  if (event.type === "approval.requested") {
    const parts = [];
    if (typeof payload.action === "string" && payload.action) parts.push(payload.action);
    const project = formatProjectContext(payload.project_context);
    if (project) parts.push(`project: ${project}`);
    if (typeof payload.reason === "string" && payload.reason) parts.push(payload.reason);
    const decision = formatDecision(payload.decision);
    if (decision) parts.push(`policy: ${decision}`);
    return parts.join("  ") || JSON.stringify(payload);
  }

  if (event.type === "approval.resolved") {
    const parts = [];
    if (typeof payload.action === "string" && payload.action) parts.push(payload.action);
    if (typeof payload.status === "string" && payload.status) parts.push(payload.status);
    if (typeof payload.actor === "string" && payload.actor) parts.push(`by ${payload.actor}`);
    if (typeof payload.branch === "string" && payload.branch) parts.push(payload.branch);
    const project = formatProjectContext(payload.project_context);
    if (project) parts.push(`project: ${project}`);
    const decision = formatDecision(payload.decision);
    if (decision) parts.push(`policy: ${decision}`);
    if (typeof payload.note === "string" && payload.note) parts.push(payload.note);
    return parts.join("  ") || JSON.stringify(payload);
  }

  if (event.type === "reverify.result") {
    const parts = [];
    // v65-F2: not_applicable is benign (no patch by design / no changes) —
    // no "not confirmed" alarm for a run with nothing to land.
    const benign = payload.outcome === "not_applicable";
    if (typeof payload.outcome === "string" && payload.outcome) {
      parts.push(benign ? "nothing to re-verify" : payload.outcome);
    }
    if (typeof payload.confirmed === "boolean" && !benign) {
      parts.push(payload.confirmed ? "confirmed" : "not confirmed");
    }
    if (typeof payload.worker_outcome === "string" && payload.worker_outcome) {
      parts.push(`worker ${payload.worker_outcome}`);
    }
    if (typeof payload.detail === "string" && payload.detail) parts.push(payload.detail);
    if (Array.isArray(payload.commands) && payload.commands.length) {
      // Only claim a re-run when one actually happened (exit codes exist).
      const ran = Array.isArray(payload.exit_codes) && payload.exit_codes.length;
      parts.push(`${ran ? "re-ran" : "recorded verify:"} ${payload.commands.join(", ")}`);
    }
    if (Array.isArray(payload.exit_codes) && payload.exit_codes.length) {
      parts.push(`exit ${payload.exit_codes.join(", ")}`);
    }
    return parts.join("  ") || JSON.stringify(payload);
  }

  if (event.type === "command.start" || event.type === "command.result") {
    const parts = [];
    if (typeof payload.capability_id === "string" && payload.capability_id) {
      parts.push(payload.capability_id);
    }
    if (typeof payload.command === "string" && payload.command) parts.push(payload.command);
    if (typeof payload.exit_code === "number") parts.push(`exit ${payload.exit_code}`);
    const decision = formatDecision(payload.decision);
    if (decision) parts.push(`policy: ${decision}`);
    const detail = payload.error || payload.stderr_tail;
    if (typeof detail === "string" && detail) parts.push(detail);
    return parts.join("  ") || JSON.stringify(payload);
  }

  if (event.type === "file.changed") {
    const parts = [];
    if (typeof payload.capability_id === "string" && payload.capability_id) {
      parts.push(payload.capability_id);
    }
    if (
      typeof payload.change === "string" && payload.change &&
      typeof payload.path === "string" && payload.path
    ) {
      parts.push(`${payload.change} ${payload.path}`);
    } else {
      if (typeof payload.path === "string" && payload.path) parts.push(payload.path);
      if (typeof payload.change === "string" && payload.change) parts.push(payload.change);
    }
    return parts.join("  ") || JSON.stringify(payload);
  }

  return JSON.stringify(payload);
}

function chatTitle(chat) {
  return (chat?.title || "New Chat").trim() || "New Chat";
}

// H1-G9: display only. A title that is a bare URL shows its host and the tail
// of its path; a very long one is cut with an ellipsis. The stored title and
// every tooltip and aria-label keep the full text.
function shortTitle(text, max = 48) {
  let url = null;
  if (/^https?:\/\/\S+$/i.test(text)) {
    try { url = new URL(text); } catch { url = null; }
  }
  if (!url) return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
  const host = url.hostname.replace(/^www\./, "");
  const segments = url.pathname.split("/").filter(Boolean);
  let tail = `${segments.at(-1) || ""}${url.search}`;
  if (tail.length > 24) tail = `…${tail.slice(-23)}`;
  if (!tail) return host;
  return segments.length > 1 ? `${host}/…/${tail}` : `${host}/${tail}`;
}

// Phase 06 R1 (F6): the model's window and what skep replays of it are two
// numbers once they differ (a 262k model on a loopback ollama replays 16k) —
// one number labelled with the window's source read as the model's (I8).
function windowNote(replayCap, modelWindow, source) {
  const n = v => Number(v).toLocaleString();
  return modelWindow && modelWindow !== replayCap
    ? `replay cap ${n(replayCap)} of a ${n(modelWindow)}-token window (${source})`
    : `window ${n(replayCap)} (${source})`;
}

function contextLoadPercent(context) {
  // v56-F3: server truth — chat detail carries what the NEXT turn will send
  // vs the model window. Pre-load (no detail yet) shows a resting sliver.
  if (!context || typeof context.percent !== "number") return 8;
  return Math.min(100, Math.max(4, Math.round(context.percent)));
}

// graphite-nav: these pages open in the Settings surface — the sidebar shows
// the pages list in place of the threads, the header reads "Settings / …".
const SURFACE_PAGES = new Set(["runs", "approvals", "assign", "schedules", "templates", "notes",
  "memory", "policies", "settings", "projects", "setup"]);
const isSurface = hash => SURFACE_PAGES.has(hash.split(/[/?]/)[1]);
// Where the surface's close button goes: the last hash outside it (I11:
// in-memory only, a reload forgets it and close goes Home).
let surfaceReturn = null;

function setShellActiveRoute(hash) {
  const surface = isSurface(hash);
  if (!surface) surfaceReturn = hash;
  const shell = document.getElementById("shell");
  if (shell) {
    if (surface) shell.dataset.surface = "settings";
    else delete shell.dataset.surface;
  }
  for (const link of document.querySelectorAll(".sidebar a[data-ws]")) {
    const ws = link.dataset.ws;
    const nav = link.dataset.nav || "";
    let active = hash.startsWith(`#/${ws}`);
    if (ws === "chat") {
      active = nav === "new-chat" && hash === "#/chat";
    }
    if (ws === "inbox") active = /^#\/(inbox|review)\b/.test(hash); // a review is an Inbox item
    // Graphite round A: Projects and Setup are Settings tabs (settings.js).
    if (ws === "settings") active = /^#\/(settings|projects|setup)\b/.test(hash);
    // graphite-nav: a surface item for one tab is lit by its own hash; a
    // project's detail page lights Projects.
    if (link.dataset.tab) {
      active = link.getAttribute("href") === hash
        || (link.dataset.tab === "projects" && hash.startsWith("#/projects/"));
    }
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function installShellHandlers() {
  document.getElementById("surface-close")?.addEventListener("click", () => {
    location.hash = surfaceReturn || "#/home";
  });
  const newChat = document.getElementById("sidebar-new-chat");
  if (newChat) {
    newChat.addEventListener("click", (event) => {
      if (location.hash === "#/chat") {
        event.preventDefault();
        route();
      }
    });
  }
}

// ---------- shell chrome: command rail, top-bar search, chat dock ----------

// The rail is icon-only. index.html keeps text-labelled data-ws anchors (the
// structure tests pin them); at boot we decorate each with an inline SVG whose
// stroke is currentColor, so hover/active recolour the glyph for free.
const RAIL_ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>',
  chat: '<path d="M4 4h16v12H9l-5 4V4z"/>',
  inbox: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 2h6l1-2h5"/>',
  runs: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  approvals: '<circle cx="12" cy="12" r="9"/><path d="M8.4 12l2.3 2.3 4.9-4.9"/>',
  assign: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
  templates:
    '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>' +
    '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  schedules: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
  notes: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
  memory: '<path d="M7 4h10a1 1 0 0 1 1 1v16l-6-4-6 4V5a1 1 0 0 1 1-1z"/>',
  policies: '<path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/>',
  settings:
    '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6' +
    'M5.2 5.2l1.9 1.9M16.9 16.9l1.9 1.9M18.8 5.2l-1.9 1.9M7.1 16.9l-1.9 1.9"/>',
};
const SEARCH_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" aria-hidden="true" style="width:100%;height:100%">' +
  '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.2-3.2"/></svg>';

function decorateShell() {
  for (const link of document.querySelectorAll(".sidebar a[data-ws]")) {
    const ws = link.dataset.ws;
    if (!RAIL_ICONS[ws] || link.querySelector(".rail-icon")) continue;
    link.insertAdjacentHTML(
      "afterbegin",
      `<svg class="rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
        `stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
        `${RAIL_ICONS[ws]}</svg>`,
    );
  }
  const searchIcon = document.querySelector(".topbar-search-icon");
  if (searchIcon && !searchIcon.firstElementChild) searchIcon.innerHTML = SEARCH_ICON;
}

// Top-bar search filters the searchable rows/cards of the current view; ⌘K
// focuses it, and a bare hex id jumps straight to that run.
let searchTimer = null;
function applySearch(query) {
  const main = document.getElementById("main");
  if (!main) return;
  const q = query.trim().toLowerCase();
  for (const node of main.querySelectorAll(".searchable")) {
    node.classList.toggle("search-hidden", Boolean(q) && !node.textContent.toLowerCase().includes(q));
  }
}
function resetSearch() {
  const input = document.getElementById("app-search");
  if (input && input.value) { input.value = ""; }
}
function installSearch() {
  const input = document.getElementById("app-search");
  if (!input) return;
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applySearch(input.value), 120);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { input.value = ""; applySearch(""); input.blur(); }
    else if (event.key === "Enter") {
      const raw = input.value.trim();
      if (/^[0-9a-f]{6,}$/i.test(raw)) { location.hash = `#/runs/${raw}`; }
    }
  });
}

// ---------- v75-F8: the ⌘K command palette (C3) ----------
// Navigation and prefill ONLY (I5/I6): every entry resolves to a
// location.hash assignment. The palette holds no verbs — nothing here calls
// the API or resolves a card; mutations happen on their own pages behind the
// existing confirm flows. A structure test pins this region mutation-free.

const PALETTE_ACTIONS = [
  { label: "Home", hash: "#/home", keywords: "dashboard hive glance" },
  { label: "New chat", hash: "#/chat", keywords: "queen talk message" },
  { label: "Inbox", hash: "#/inbox", keywords: "waiting cards approvals pending" },
  { label: "Runs", hash: "#/runs", keywords: "tasks workers history" },
  { label: "Approvals", hash: "#/approvals", keywords: "queue gate pending verdict" },
  { label: "Assign", hash: "#/assign", keywords: "dispatch new task work" },
  { label: "Projects", hash: "#/projects", keywords: "packs bindings phase" },
  { label: "Schedules", hash: "#/schedules", keywords: "recurring ticker cron" },
  { label: "Templates & Skills", hash: "#/templates", keywords: "recipes learned" },
  { label: "Notes & Tasks", hash: "#/notes", keywords: "todo capture due" },
  { label: "Memory", hash: "#/memory", keywords: "durable proposals forget" },
  { label: "Policies", hash: "#/policies", keywords: "autonomy scopes gates budget" },
  { label: "Setup", hash: "#/setup", keywords: "onboarding first run wizard" },
  { label: "Settings", hash: "#/settings", keywords: "llm model channels webhooks repos" },
];

function installPalette() {
  const input = el("input", {
    class: "palette-input", type: "text", autocomplete: "off",
    placeholder: "Jump to a page, or paste a run id…",
    "aria-label": "command palette",
  });
  const list = el("div", { class: "palette-list" });
  const overlay = el("div", { class: "palette-overlay hidden" },
    el("div", { class: "palette" }, input, list));
  document.body.append(overlay);
  let items = [];
  let selected = 0;
  const close = () => { overlay.classList.add("hidden"); input.value = ""; };
  const go = (hash) => { close(); location.hash = hash; };
  const renderList = () => {
    const q = input.value.trim().toLowerCase();
    items = [];
    // A hex-looking query offers the run jump (same test as the topbar Enter).
    if (/^[0-9a-f]{6,}$/i.test(q)) {
      items.push({ label: `Open run ${q}`, hash: `#/runs/${q}` });
    }
    items.push(...PALETTE_ACTIONS.filter(action =>
      !q || action.label.toLowerCase().includes(q) || action.keywords.includes(q)));
    selected = Math.min(selected, Math.max(0, items.length - 1));
    if (!items.length) {
      list.replaceChildren(el("p", { class: "note" },
        "No matches — try a page name, or a run id to jump to it."));
      return;
    }
    list.replaceChildren(...items.map((item, i) => el("button", {
      type: "button",
      class: `palette-item${i === selected ? " selected" : ""}`,
      onclick: () => go(item.hash),
    },
      el("span", {}, item.label),
      el("span", { class: "palette-hint mono" }, item.hash))));
  };
  input.addEventListener("input", () => { selected = 0; renderList(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { close(); return; }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      selected = Math.min(selected + 1, items.length - 1);
      renderList();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      selected = Math.max(selected - 1, 0);
      renderList();
    } else if (event.key === "Enter" && items[selected]) {
      event.preventDefault();
      go(items[selected].hash);
    }
  });
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  const open = () => {
    overlay.classList.remove("hidden");
    selected = 0;
    renderList();
    input.focus();
  };
  // The topbar's ⌘K hint now tells the truth (C3/I9): it opens the palette.
  // The per-view search input keeps its own typed filtering untouched.
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      open();
    }
  });
}

// The dock is a launcher: it stashes a draft and hands off to the Chat view,
// which owns the real streaming composer — no duplicated stream plumbing.
let pendingChatDraft = "";
function installDock() {
  const form = document.getElementById("dock-form");
  const input = document.getElementById("dock-input");
  if (!form || !input) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    pendingChatDraft = text;
    input.value = "";
    if (location.hash === "#/chat") route();
    else location.hash = "#/chat";
  });
}
// v76-F2 (C10): the Queen tile's hover facts — composed once from the one
// config fetch; poll() appends the usage tally at most once per minute.
let queenTitleBase = "";
let queenUsageAt = 0;

async function refreshDockModel() {
  const label = document.getElementById("dock-model");
  if (!label) return;
  try {
    const llm = await api("GET", "/api/llm/config");
    label.textContent = llm.default_model || "";
    // The Queen tile claims only sourced fields (C10/I8): the model name
    // visible, window + tool delivery on hover. Per-chat context stays in
    // the chat meter where it lives.
    const model = document.getElementById("queen-model-label");
    const tile = document.getElementById("topbar-queen-status");
    if (tile && model && llm.default_model) {
      model.textContent = llm.default_model;
      queenTitleBase = `${windowNote(llm.num_ctx, llm.model_window, llm.num_ctx_source)} · tools ${llm.tool_delivery}`;
      tile.title = queenTitleBase;
    }
  } catch { /* leave the dock model label blank */ }
}
function updateShellChrome(hash) {
  // H1-G2: the header line is the view's own. + Assign shows where work is
  // started from (Home, Runs); it is also "New run" in the Settings
  // surface and in ⌘K. The Queen tile (default model + liveness) belongs to Home; a thread
  // shows its own model in its header.
  const page = hash.split(/[/?]/)[1] || "home";
  const assign = document.querySelector(".topbar-assign");
  if (assign) assign.hidden = !["home", "runs"].includes(page);
  const queenTile = document.getElementById("topbar-queen-status");
  if (queenTile) queenTile.hidden = page !== "home";
  const close = document.getElementById("surface-close");
  if (close) close.hidden = !isSurface(hash);
  viewControls()?.replaceChildren();
  const hideDock = hash.startsWith("#/chat");
  const dock = document.getElementById("dock");
  if (dock) dock.classList.toggle("hidden", hideDock);
  // Reserve space so the sticky dock never hides the tail of long content.
  document.body.classList.toggle("dock-open", !hideDock);
  resetSearch();
}

function flash(kind, message) {
  const box = el("div", { class: `flash ${kind}` }, message);
  document.getElementById("main").prepend(box);
  setTimeout(() => box.remove(), 6000);
}

// ---------- login ----------

async function tryConnect() {
  const response = await fetch("/api/status", { headers: { "X-Skep-Token": token() } });
  // A stored token the server now rejects is the re-auth state; no token yet is first contact.
  if (!response.ok) { showLogin(token() && response.status === 401 ? REAUTH : ""); return false; }
  document.getElementById("login").classList.add("hidden");
  document.getElementById("shell").classList.remove("hidden");
  setToken(token()); // refresh the SSE cookie
  return true;
}

document.getElementById("token-save").addEventListener("click", async () => {
  setToken(document.getElementById("token-input").value.trim());
  if (await tryConnect()) route();
  else document.getElementById("login-error").textContent = "that token was rejected";
});

// ---------- router ----------

let cleanup = null; // per-view teardown (closes EventSources, timers)
let routeGeneration = 0; // Phase 10 R1 D7: stamps #main per navigation (routeLive)

const routes = [
  [/^#\/home$/, viewHome],
  [/^#\/chat$/, viewChat],
  [/^#\/chat\/([^/]+)$/, viewChat],
  [/^#\/inbox$/, viewInbox], // H1: everything waiting on you (js/pages/inbox.js)
  // Phase 10 10.1: one pending approval, reviewed — diff, forensics, verdict.
  [/^#\/review\/([^/]+)$/, viewReview],
  [/^#\/notes$/, viewNotesTasks],
  [/^#\/memory$/, viewMemory],
  // v75-F7 (C9): tolerate a query suffix — template cards link to
  // #/assign?template=<name>; the param parsing itself ships in v76-F4.
  [/^#\/assign(?:\?.*)?$/, viewAssign],
  [/^#\/runs$/, viewRuns],
  [/^#\/runs\/([^/]+)$/, viewRunDetail],
  [/^#\/approvals$/, viewApprovals],
  [/^#\/templates$/, viewTemplates],
  // v76-F3: projects stop being write-only — a detail page composes reads.
  [/^#\/projects\/([^/]+)$/, viewProjectDetail],
  [/^#\/schedules$/, viewSchedules],
  // Graphite round A: #/policies/<tab> opens that tab (policies.js).
  [/^#\/policies(?:\/([\w-]+))?$/, viewPolicies],
  // graphite-nav: #/settings/<tab> opens that section (settings.js).
  [/^#\/settings(?:\/([\w-]+))?$/, viewSettings],
];

// graphite-nav: a page that became a section answers at its old hash too.
// Rewritten in place (no history entry), before anything reads the hash.
const HASH_ALIASES = {
  "#/settings": "#/settings/models",
  "#/projects": "#/settings/projects",
  "#/setup": "#/settings/setup",
  "#/policies": "#/policies/now",
};
// The sections behind #/settings/<tab> and #/policies/<tab>, first = fallback.
// An unknown tab is rewritten to the first, so an item is always lit.
const SECTIONS = {
  settings: ["models", "workers", "projects", "channels", "setup"],
  policies: ["now", "decisions", "grants", "groups", "defaults", "tree"],
};

async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  // replaceState fires no hashchange, so this route() call is the only one.
  if (HASH_ALIASES[location.hash]) history.replaceState(null, "", HASH_ALIASES[location.hash]);
  const tabbed = location.hash.match(/^#\/(settings|policies)\/([\w-]+)$/);
  if (tabbed && !SECTIONS[tabbed[1]].includes(tabbed[2])) {
    history.replaceState(null, "", `#/${tabbed[1]}/${SECTIONS[tabbed[1]][0]}`);
  }
  const hash = location.hash || "#/home";
  setShellActiveRoute(hash);
  updateShellChrome(hash);
  const main = document.getElementById("main");
  main.className = "";
  main.replaceChildren();
  main.dataset.route = String(++routeGeneration);
  let setup = null;
  try { setup = await setupStatus(); }
  catch { return; }
  if (!setup.complete && !setupRouteAllowed(hash)) {
    location.hash = "#/setup";
    return;
  }
  // First run: there is nowhere to close to — the gate would bounce back here.
  if (!setup.complete) {
    const close = document.getElementById("surface-close");
    if (close) close.hidden = true;
  }
  for (const [pattern, view] of routes) {
    const match = hash.match(pattern);
    if (match) {
      // Phase 10 10.1: a 404 is a page state ("not found"), not a toast.
      await view(main, ...match.slice(1)).catch(e => {
        if (e.status === 404) { header(main, "Not found"); main.replaceChildren(notFound(e.message)); }
        else flash("bad", e.message);
      });
      // H1-G2: the list filter shows only on a view that has a list to filter.
      const filter = document.getElementById("app-search")?.closest(".topbar-search");
      if (filter) filter.hidden = !main.querySelector(".searchable");
      return;
    }
  }
  location.hash = "#/home";
}

window.addEventListener("hashchange", route);

// ---------- shared fragments ----------

function header(main, title, subtitle) {
  // The page title lives in the view's one header line. H1-G2: the subtitle
  // sentence no longer takes a row of its own — it is the title's hover text,
  // so nothing it said is lost.
  const titleEl = document.getElementById("topbar-title");
  if (titleEl) {
    titleEl.textContent = isSurface(location.hash || "#/home") ? surfaceCrumb(title) : title;
    titleEl.title = subtitle || "";
  }
  document.title = `${title} · skep`;
}

// graphite-nav: "Settings / <page>"; a one-tab page adds its tab's label
// ("Settings / Policies / Now"); a repeat ("Settings / Settings") is dropped.
function surfaceCrumb(title) {
  const item = [...document.querySelectorAll(".sidebar-pages a[data-tab]")]
    .find(link => link.getAttribute("href") === location.hash);
  const parts = ["Settings", title, item?.textContent.trim()];
  return parts.filter((part, i) => part && parts.indexOf(part) === i).join(" / ");
}

// H1-G2: a view puts its own controls in its header line, right of the title.
function viewControls() {
  return document.getElementById("view-controls");
}

async function repoOptions() {
  const { repos } = await api("GET", "/api/repos");
  return repos.map(r => r.name);
}

async function setupStatus() {
  return api("GET", "/api/setup/status");
}

// graphite-nav: every Settings section, so Setup's links to Models and
// Projects work before setup is complete. #/setup arrives normalised.
function setupRouteAllowed(hash) {
  return /^#\/settings\/[\w-]+$/.test(hash);
}

function chatTimeLabel(ts) {
  const date = ts ? new Date(ts) : new Date();
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function copyText(text) {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

function renderMessageFooter(message, role, options = {}) {
  const raw = () => message.dataset.raw || "";
  const copy = iconButton("copy message", "⧉", {
    class: "chat-message-action icon-button",
    onclick: () => copyText(raw()),
  });
  const actions = [copy];
  if (role === "user" && options.onResend) {
    actions.push(iconButton("resend message", "↻", {
      class: "chat-message-action icon-button",
      onclick: () => options.onResend(raw()),
    }));
  }
  if (role === "assistant" && options.onRetry) {
    actions.push(iconButton("retry from here", "↺", {
      class: "chat-message-action icon-button",
      onclick: () => options.onRetry(raw()),
    }));
  }
  return el("div", { class: "chat-message-footer" }, actions);
}

function renderThinkingPanel(text = "") {
  const body = el("pre", { class: "chat-thinking-body mono" }, text);
  const panel = el("details", { class: "chat-thinking" },
    el("summary", {
      class: "chat-thinking-toggle",
      title: "toggle thinking",
      "aria-label": "toggle thinking",
    },
      el("span", { class: "chat-thinking-title" }, "Thinking"),
      el("span", { class: "chat-thinking-chevron button-icon", "aria-hidden": "true" }, "⌄")),
    body);
  return {
    node: panel,
    append(value) { body.append(document.createTextNode(value)); },
    set(value) { body.textContent = value || ""; },
  };
}

// ---------- Markdown (chat bubbles) ----------
//
// A deliberately small, XSS-safe renderer: the source text is NEVER parsed as
// HTML — every piece of user/model content becomes a text node, and only tags
// this code creates exist in the output. Covers what the Queen actually
// writes: headings, bold/italic/inline code, fenced code blocks, lists,
// tables, links, paragraphs.

function mdInline(text) {
  const nodes = [];
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push(el("code", {}, token.slice(1, -1)));
    } else if (token.startsWith("**")) {
      nodes.push(el("strong", {}, ...mdInline(token.slice(2, -2))));
    } else if (token.startsWith("*")) {
      nodes.push(el("em", {}, ...mdInline(token.slice(1, -1))));
    } else {
      const split = token.indexOf("](");
      const label = token.slice(1, split);
      const href = token.slice(split + 2, -1);
      nodes.push(el("a", { href, target: "_blank", rel: "noopener noreferrer" }, label));
    }
    last = match.index + token.length;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

function mdTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map(cell => cell.trim());
}

function renderMarkdown(text) {
  const root = document.createDocumentFragment();
  const lines = (text || "").split("\n");
  let paragraph = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const p = el("p", { class: "md-p" });
    paragraph.forEach((line, index) => {
      if (index > 0) p.append(el("br"));
      p.append(...mdInline(line));
    });
    root.append(p);
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushParagraph();
      const code = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { code.push(lines[i]); i++; }
      root.append(el("pre", { class: "md-code mono" }, code.join("\n")));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = Math.min(heading[1].length + 2, 6); // #-> h3 ... keeps bubble hierarchy sane
      root.append(el(`h${level}`, { class: "md-h" }, ...mdInline(heading[2])));
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
        && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flushParagraph();
      const head = mdTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(mdTableRow(lines[i])); i++; }
      i--;
      root.append(el("div", { class: "md-table-wrap" },
        el("table", { class: "md-table" },
          el("thead", {}, el("tr", {}, head.map(cell => el("th", {}, ...mdInline(cell))))),
          el("tbody", {}, rows.map(cells =>
            el("tr", {}, cells.map(cell => el("td", {}, ...mdInline(cell)))))))));
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      const items = [];
      while (i < lines.length) {
        const item = ordered
          ? lines[i].match(/^\s*\d+[.)]\s+(.*)$/)
          : lines[i].match(/^\s*[-*]\s+(.*)$/);
        if (!item) break;
        items.push(el("li", {}, ...mdInline(item[1])));
        i++;
      }
      i--;
      root.append(el(ordered ? "ol" : "ul", { class: "md-list" }, items));
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flushParagraph(); root.append(el("hr")); continue; }
    if (!line.trim()) { flushParagraph(); continue; }
    paragraph.push(line);
  }
  flushParagraph();
  return root;
}

function renderChatMessage(log, role, text, options = {}) {
  const label = options.label || (role === "user" ? "You" : "skep");
  const node = el("article", {
    class: `chat-message bubble ${role}`,
    "data-role": role,
  });
  node.dataset.raw = text || "";
  const roleLine = el("div", { class: "chat-message-role" },
    el("span", { class: "chat-message-dot", "aria-hidden": "true" }),
    el("span", { class: "chat-message-label" }, label),
    el("time", { class: "chat-message-time", datetime: options.createdAt || new Date().toISOString() },
      chatTimeLabel(options.createdAt)));
  const body = el("div", { class: "chat-message-body" });
  // Assistant replies are markdown; user messages stay verbatim text.
  if (role === "assistant") body.append(renderMarkdown(text || ""));
  else body.append(text || "");
  // v44-F9: image attachments render as thumbnails (cookie-authed GET).
  if (options.attachments && options.attachments.length && options.chatId) {
    for (const name of options.attachments) {
      body.append(el("img", {
        src: `/api/chats/${options.chatId}/attachments/${name}`,
        alt: "attached image",
        loading: "lazy",
        style: "display:block;max-width:220px;max-height:220px;border-radius:8px;margin-top:6px",
      }));
    }
  }
  node.append(roleLine);
  if (role === "assistant" && options.thinking) {
    node.append(renderThinkingPanel(options.thinking).node);
  }
  node.append(body);
  if (options.footer !== false) {
    node.append(renderMessageFooter(node, role, options));
  }
  log.append(node);
  if (options.scroll) options.scroll();
  return node;
}

function createStreamingReply(log, options = {}) {
  const node = renderChatMessage(log, "assistant", "", { ...options, footer: false });
  node.classList.add("streaming");
  const body = node.querySelector(".chat-message-body");
  const indicator = el("span", { class: "streaming-indicator" }, "Thinking");
  body.append(indicator);
  let content = "";
  let thinking = null;
  const render = () => {
    if (content) body.replaceChildren(renderMarkdown(content));
    else body.replaceChildren(indicator);
    node.dataset.raw = content;
    if (options.scroll) options.scroll();
  };
  return {
    node,
    append(text) {
      content += text;
      render();
    },
    appendThinking(text) {
      if (!thinking) {
        thinking = renderThinkingPanel();
        body.before(thinking.node);
      }
      thinking.append(text);
      if (options.scroll) options.scroll();
    },
    isEmpty() { return content.length === 0; },
    finalize() {
      render();
      node.classList.remove("streaming");
      if (!node.querySelector(".chat-message-footer")) {
        node.append(renderMessageFooter(node, "assistant", options));
      }
    },
    remove() { node.remove(); },
  };
}

function renderToolEvent(log, tool, result = null, options = {}) {
  let thread = log.lastElementChild;
  if (!thread || !thread.classList.contains("agent-thread")) {
    thread = el("div", { class: "agent-thread" });
    log.append(thread);
  }
  const summary = options.summary || `tool: ${tool}`;
  // v128-F12: an object result reads as labeled rows (renderArgs), not JSON.
  // Keys beside a wrapped result (a kept `grant`) stay; the envelope does not.
  const inner = unwrapToolResult(result);
  const beside = inner === result ? {} : Object.fromEntries(Object.entries(result)
    .filter(([key]) => !["ok", "result", "action_id", "tool"].includes(key)));
  const shown = inner && typeof inner === "object" && !Array.isArray(inner) ? { ...inner, ...beside } : inner;
  const rows = shown && typeof shown === "object" && !Array.isArray(shown) ? renderArgs(shown) : null;
  const content = rows || result === null || result === undefined
    ? ""
    : JSON.stringify(result, null, 2);
  const node = el("div", { class: "agent-thread-node" },
    el("div", { class: "agent-thread-dot", "aria-hidden": "true" }),
    el("button", {
      class: "agent-thread-header",
      type: "button",
      onclick: () => node.classList.toggle("open"),
    },
      el("span", { class: "agent-thread-tool" }, tool),
      el("span", { class: "agent-thread-summary" }, summary),
      el("span", { class: "agent-thread-chevron", "aria-hidden": "true" }, "›")),
    rows
      ? el("div", { class: "agent-thread-content" }, rows)
      : el("pre", { class: "agent-thread-content mono" }, content));
  if (!content && !rows) node.querySelector(".agent-thread-content").classList.add("hidden");
  thread.append(node);
  if (options.scroll) options.scroll();
  return node;
}

// Confirmed mutations store their result wrapped as {ok, result}; read tools
// store the raw view. Summaries look through the wrapper so live and replay
// read the same fields.
function unwrapToolResult(raw) {
  if (raw && typeof raw === "object" && "ok" in raw && raw.result && typeof raw.result === "object") {
    return raw.result;
  }
  return raw;
}

function shortTaskId(id) { return String(id || "").slice(0, 8); }

// v128-F11: a confirmed /workon set up a project — the chat moves onto it.
// The server binds only an EMPTY slot, and a new chat already sits on the
// workspace project, so the operator's own command rebinds it here.
function projectAfterCommand(tool, response) {
  if (tool !== "workon" || !response?.ok) return null;
  return unwrapToolResult(response)?.project?.project_id || null;
}

function toolLine(tool, raw = null) {
  // v40-F2 (v35): every supervisor tool gets a deterministic human summary
  // read off the tool RESULT json — never model text.
  const result = unwrapToolResult(raw);
  if (result && tool === "dispatch_run" && result.task_id) {
    const where = result.repo ? ` on ${result.repo}` : "";
    const delegated = delegationText(result.delegation);
    return `dispatched worker ${shortTaskId(result.task_id)}${where}${delegated ? ` — ${delegated}` : ""}`;
  }
  if (result && tool === "batch_dispatch" && result.team) {
    const chain = (result.delegation_chain || []).join(" → ");
    return `dispatched team ${shortTaskId(result.team)}: ${chain} → ${result.count} worker(s)`;
  }
  if (result && tool === "start_research" && result.task_id) {
    return `dispatched researcher ${shortTaskId(result.task_id)}`;
  }
  if (result && tool === "get_run" && (result.task_id || result.run)) {
    const run = result.run && typeof result.run === "object" ? result.run : result;
    const verified = run.verification_outcome ? `, ${run.verification_outcome}` : "";
    return `checked run ${shortTaskId(run.task_id)} — ${run.state || "?"}${verified}`;
  }
  if (result && tool === "list_runs" && Array.isArray(result.runs)) {
    return `listed ${result.runs.length} run${result.runs.length === 1 ? "" : "s"}`;
  }
  if (result && tool === "effective_policy") {
    return `read effective policy${result.repo ? ` for ${result.repo}` : ""}`;
  }
  if (result && tool === "repo_state" && result.repo) {
    return `checked repo state: ${result.repo}`;
  }
  if (result && tool === "land_run" && result.action) {
    return result.action === "applied" ? `landed patch on ${result.branch}` : `land: ${result.action}`;
  }
  if (result && tool === "approve_review" && result.action) {
    if (result.action === "resumed") return `approved — resumed as ${shortTaskId(result.resumed_as)}`;
    if (result.action === "applied") return `approved — applied on ${result.branch}`;
    return `approved (${result.action})`;
  }
  if (result && tool === "deny_review" && result.action) {
    return "denied the review";
  }
  if (result && tool === "add_task" && result.task) {
    return `task added: ${result.task.title}`;
  }
  if (result && tool === "complete_task" && result.task) {
    return `task completed: ${result.task.title}`;
  }
  if (result && tool === "list_tasks" && Array.isArray(result.tasks)) {
    const open = result.tasks.filter(task => task.status === "todo");
    if (!open.length) return "todo: none";
    const titles = open.slice(0, 3).map(task => task.title).join("; ");
    return `todo: ${titles}${open.length > 3 ? ` (+${open.length - 3} more)` : ""}`;
  }
  if (result && tool === "add_note" && result.note) {
    return "note added";
  }
  if (result && tool === "list_notes" && Array.isArray(result.notes)) {
    return `${result.notes.length} note${result.notes.length === 1 ? "" : "s"}`;
  }
  return `tool: ${tool}`;
}

// v54-F3: labeled key-value rows instead of raw JSON — a human can scan
// "repo: skep" where {"repo":"skep",...} was syntax noise. Nested values
// (batch_dispatch.tasks) stay JSON, but under a labeled key.
function renderArgs(args) {
  if (!args || typeof args !== "object" || !Object.keys(args).length) return null;
  return el("div", { class: "card-args" },
    Object.entries(args).map(([key, value]) => el("div", { class: "arg-row" },
      el("span", { class: "arg-key mono" }, `${key}:`),
      el("span", { class: "arg-value" },
        typeof value === "string" ? value : JSON.stringify(value, null, 2)))));
}

// v128-F12: a command result's row says what happened (toolLine) when there
// is a deterministic line for the verb; otherwise the title stays.
function commandSummary(title, tool, result) {
  const line = tool ? toolLine(tool, result) : null;
  return line && line !== `tool: ${tool}` ? line : title;
}

// v40-F1 (v35): consecutive Queen tool calls fold into ONE collapsed row with
// a deterministic aggregate summary (computed from tool names + toolLine
// output — the model writes none of it). Expanding shows the existing
// per-call detail; renderToolEvent stays the member renderer, so the
// agent-thread-node markup survives unchanged. Live and replay share this.
function activityGroupSummary(members) {
  const summaries = members.map(member => member.summary).filter(Boolean);
  const head = summaries.slice(0, 3).join(", ");
  const more = summaries.length > 3 ? ` (+${summaries.length - 3} more)` : "";
  const noun = members.length === 1 ? "1 tool" : `${members.length} tools`;
  return `Used ${noun}: ${head}${more}`;
}

function renderActivityGroup(log, options = {}) {
  const summarySpan = el("span", { class: "activity-group-summary" }, "");
  const body = el("div", { class: "activity-group-body" });
  const group = el("div", { class: "activity-group" },
    el("button", {
      class: "activity-group-header",
      type: "button",
      onclick: () => group.classList.toggle("open"),
    },
      summarySpan,
      el("span", { class: "agent-thread-chevron", "aria-hidden": "true" }, "›")),
    body);
  log.append(group);
  const members = [];
  return {
    node: group,
    add(tool, result, memberOptions = {}) {
      members.push({ tool, summary: memberOptions.summary || `tool: ${tool}` });
      renderToolEvent(body, tool, result, memberOptions);
      summarySpan.textContent = activityGroupSummary(members);
      if (options.scroll) options.scroll();
    },
    isEmpty() { return members.length === 0; },
  };
}

// v40-F3 (v35): the worker activity block — live run telemetry inline in the
// chat, rendered BY REFERENCE from the existing run endpoints (never copied
// into the transcript; the events table stays the single source of truth).
// READ-ONLY by design: approvals resolve on the run page or via /approve —
// this block never grows buttons (no shadow approval surface).
const ACTIVE_RUN_STATES = new Set(["created", "dispatched", "running"]);
const DISPATCHED_RESULT_STATES = new Set(["dispatched", "research_dispatched"]);
const MAX_LIVE_WORKER_BLOCKS = 3;
const liveWorkerSources = new Set();
// v92-F1: terminal announcements dedupe here — the status stream replays
// terminals for 30s after a reconnect (v56-F7), and the post-redraw
// resubscribe must not re-flash (or re-redraw) the same ending.
const notifiedTerminalRuns = new Set();

function closeLiveWorkerSources() {
  for (const source of liveWorkerSources) source.close();
  liveWorkerSources.clear();
}

// Pure diffstat over a unified diff: count +/- body lines, skip the
// +++/--- file headers.
function diffstat(text) {
  let added = 0;
  let removed = 0;
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

function workerRow(body, summary, detailNode = null) {
  const row = el("div", { class: "worker-activity-row" });
  const label = el("span", { class: "worker-activity-row-summary" }, summary);
  if (detailNode) {
    detailNode.classList.add("worker-activity-row-detail");
    row.append(
      el("button", {
        class: "worker-activity-row-header",
        type: "button",
        onclick: () => row.classList.toggle("open"),
      },
        label,
        el("span", { class: "agent-thread-chevron", "aria-hidden": "true" }, "\u203a")),
      detailNode);
  } else {
    row.append(el("div", { class: "worker-activity-row-header static" }, label));
  }
  body.append(row);
  return { row, setSummary(next) { label.textContent = next; } };
}

function renderWorkerActivity(log, taskId, options = {}) {
  const short = shortTaskId(taskId);
  const chip = el("span", { class: "state running" }, "running");
  const title = el("span", { class: "worker-activity-title" }, `Worker ${short}`);
  const diffPill = el("span", { class: "worker-activity-diff hidden" });
  const body = el("div", { class: "worker-activity-body" });
  const block = el("section", { class: "worker-activity open" },
    el("div", { class: "worker-activity-header" },
      el("button", {
        class: "worker-activity-toggle",
        type: "button",
        onclick: () => block.classList.toggle("open"),
      },
        title, chip, diffPill,
        el("span", { class: "agent-thread-chevron", "aria-hidden": "true" }, "\u203a")),
      el("a", { class: "worker-activity-link", href: `#/runs/${taskId}` }, "open run \u2192")),
    body);
  log.append(block);
  if (options.scroll) options.scroll();

  const setState = (state) => {
    chip.textContent = state || "?";
    chip.className = `state ${state || ""}`;
  };

  // Grouped rows, F1-style: a status line (latest heartbeat wins), a current
  // commands group, a current files group; a different event type closes the
  // open group so consecutive events fold and interleaved ones split.
  let statusRow = null;
  let commandRow = null;
  let commands = [];
  let fileRow = null;
  let files = [];
  const closeGroups = () => { commandRow = null; commands = []; fileRow = null; files = []; };

  const feed = (view) => {
    const type = String(view.type || "");
    const payload = view.payload && typeof view.payload === "object" ? view.payload : {};
    if (type === "heartbeat") {
      const line = `worker running \u2014 phase: ${payload.phase || "working"}`;
      if (!statusRow) statusRow = workerRow(body, line);
      else statusRow.setSummary(line);
    } else if (type === "command.start") {
      // its command.result carries everything, including the tails
    } else if (type === "command.result") {
      fileRow = null; files = [];
      if (!commandRow) { commandRow = workerRow(body, "", el("div", {})); commands = []; }
      commands.push(payload);
      const failures = commands.filter(c => Number(c.exit_code) !== 0).length;
      commandRow.setSummary(`ran ${commands.length} command${commands.length === 1 ? "" : "s"}`
        + (failures ? ` (${failures} failed)` : ""));
      const failed = Number(payload.exit_code) !== 0;
      const item = el("div", { class: `worker-command${failed ? " failed" : ""}` },
        el("div", { class: "mono worker-command-line" },
          `exit ${payload.exit_code}`
          + (payload.duration_ms != null ? ` \u00b7 ${payload.duration_ms}ms` : "")
          + ` \u00b7 ${payload.command || ""}`));
      if (payload.stdout_tail) {
        item.append(el("pre", { class: "mono worker-command-output" }, payload.stdout_tail));
      }
      if (payload.stderr_tail) {
        item.append(el("pre", { class: "mono worker-command-output" }, payload.stderr_tail));
      }
      commandRow.row.querySelector(".worker-activity-row-detail").append(item);
    } else if (type === "file.changed") {
      commandRow = null; commands = [];
      if (!fileRow) { fileRow = workerRow(body, "", el("div", {})); files = []; }
      files.push(payload);
      fileRow.setSummary(`changed ${files.length} file${files.length === 1 ? "" : "s"}`);
      fileRow.row.querySelector(".worker-activity-row-detail").append(
        el("div", { class: "mono worker-command-line" },
          `${payload.kind || "edit"}  ${payload.path || ""}`));
    } else if (type === "plan.created") {
      closeGroups();
      const steps = Array.isArray(payload.steps) ? payload.steps : [];
      workerRow(body, `planned ${steps.length} step${steps.length === 1 ? "" : "s"}`,
        el("ul", { class: "worker-plan" }, steps.map(step => el("li", {}, String(step)))));
    } else if (type === "approval.requested") {
      closeGroups();
      body.append(el("div", { class: "worker-activity-gate" },
        el("strong", {}, "waiting for your approval"),
        ` \u2014 ${payload.reason || payload.action || ""}. `
        + "Open the run page or use /approvals."));
    } else if (type === "verify.result") {
      closeGroups();
      workerRow(body, `verification: ${payload.outcome || "?"}`,
        payload.details
          ? el("pre", { class: "mono worker-command-output" }, String(payload.details))
          : null);
    } else if (type === "task.terminal") {
      closeGroups();
      if (payload.summary) workerRow(body, String(payload.summary));
    } else if (type !== "task.start" && type !== "run.created") {
      const line = summarizeRunEvent(view);
      if (line) workerRow(body, `${type}: ${line}`);
    }
    if (options.scroll) options.scroll();
  };

  const finalize = async (state) => {
    setState(state);
    if (state === "completed") {
      try {
        // v106-F9: only ask for a diff that exists — no-patch completions
        // were a steady 404 drumbeat in serve.log.
        const detail = await api("GET", `/api/runs/${taskId}`);
        if (!(detail.artifacts || []).some((a) => a.kind === "patch")) return;
        const diff = await api("GET", `/api/runs/${taskId}/diff`);
        const { added, removed } = diffstat(diff);
        diffPill.classList.remove("hidden");
        diffPill.replaceChildren(
          el("span", { class: "diff-add" }, `+${added}`), " ",
          el("span", { class: "diff-del" }, `\u2212${removed}`));
      } catch { /* no patch artifact */ }
    }
    if (options.scroll) options.scroll();
  };

  (async () => {
    let run;
    try { run = (await api("GET", `/api/runs/${taskId}`)).run; }
    catch { title.textContent = `Worker ${short} (run not found)`; setState("failed"); return; }
    if (run.repo) title.textContent = `Worker ${short} \u00b7 ${run.repo}`;
    setState(run.state);
    if (!ACTIVE_RUN_STATES.has(run.state)) {
      // Terminal (or gated): one replay from the audit events — a refresh
      // after terminal rebuilds the identical block.
      try {
        const { events } = await api("GET", `/api/runs/${taskId}/events`);
        for (const view of events) feed(view);
      } catch { /* header still stands */ }
      await finalize(run.state);
      return;
    }
    if (liveWorkerSources.size >= MAX_LIVE_WORKER_BLOCKS) {
      // Browsers cap per-origin connections; older blocks stay static.
      body.append(el("p", { class: "note" }, "live view capped \u2014 open run \u2192 for the stream"));
      return;
    }
    const source = new EventSource(`/api/runs/${taskId}/events?stream=1`);
    liveWorkerSources.add(source);
    // D7: the browser's own reconnect replays the run from its first event.
    const feedOnce = onceByEventId(feed);
    source.onmessage = (message) => feedOnce(JSON.parse(message.data));
    source.addEventListener("done", (message) => {
      source.close();
      liveWorkerSources.delete(source);
      let state = null;
      try { state = JSON.parse(message.data).state; } catch { state = null; }
      finalize(state || run.state);
    });
  })();
  return block;
}

// ---------- Home (the hive at a glance) ----------

function shortTime(ts) {
  const date = ts ? new Date(ts) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// The design's approval cards carry an invented priority + a snake_case kind.
// Derive both from the real approval action so nothing is fabricated.
function approvalKind(approval) {
  if (approval.action === "shell.run") return "shell_run";
  if (approval.action === "apply_patch") return "patch_apply";
  return (approval.action || "review").replace(/[.\s]+/g, "_");
}
function approvalPriority(approval) {
  if (approval.action === "apply_patch") return "high";
  if (approval.action === "shell.run") return "medium";
  return "low";
}
function approvalTitle(approval) {
  const reason = (approval.reason || "").trim();
  if (reason) return reason;
  const run = approval.run || {};
  return ((run.instructions || run.summary || approval.action || "Review needed").trim()).slice(0, 90);
}

function statTile(value, label, tone) {
  return el("div", { class: "stat-tile" },
    el("div", { class: `stat-value${tone ? ` ${tone}` : ""}` }, value),
    el("div", { class: "stat-label" }, label));
}

async function viewHome(main) {
  header(main, "Home",
    "The hive at a glance — everything skep is running, waiting on, and has learned.");
  const [{ runs }, { approvals }, { schedules }] = await Promise.all([
    api("GET", "/api/runs?limit=500"),
    api("GET", "/api/approvals"),
    api("GET", "/api/schedules"),
  ]);

  // Non-terminal runs are "running now"; superseded runs (v19-F8) never count.
  const active = new Set(["running", "dispatched", "created"]);
  const runningNow = runs.filter(run => active.has(run.state)).length;
  const pending = approvals.length;
  // Verify pass-rate stat (IMPLEMENTATION_NOTES #7): built from stored outcomes.
  const scored = runs.filter(run => ["passed", "failed", "error"].includes(run.verification_outcome));
  const passed = scored.filter(run => run.verification_outcome === "passed").length;
  const passRate = scored.length ? Math.round((passed / scored.length) * 100) : null;

  // v76-F1: the welcome-back banner counts ONLY what actually finished since
  // the operator's last visit (I8) — never the whole fetched list. The stamp
  // is a UI preference (localStorage), not server state.
  const HOME_TERMINAL = new Set(
    ["completed", "failed", "rejected", "worker_crashed", "worker_timeout"]);
  const lastVisit = localStorage.getItem("skep-last-visit");
  localStorage.setItem("skep-last-visit", new Date().toISOString());
  const AWAY_MS = 4 * 60 * 60 * 1000; // the review's 4h threshold
  if (lastVisit && Date.now() - new Date(lastVisit).getTime() > AWAY_MS) {
    const finished = runs.filter(run => HOME_TERMINAL.has(run.state)
      && new Date(run.updated_at || 0).getTime() > new Date(lastVisit).getTime()).length;
    if (finished || pending) {
      const banner = el("div", { class: "welcome-back" },
        el("span", {},
          `${finished} run${finished === 1 ? "" : "s"} finished while you were away`
          + ` · ${pending} approval${pending === 1 ? "" : "s"} waiting`),
        iconButton("dismiss", "×", {
          class: "ghost icon-button", onclick: () => banner.remove(),
        }));
      main.append(banner);
    }
  }

  // v76-F1: the total-runs tile (the least actionable number) leaves
  // deliberately — totals live in the Runs page's filter counts (C4).
  const activeSchedules = schedules.filter(s => s.enabled).length;
  const verifyTile = statTile(passRate === null ? "—" : `${passRate}%`, "Verify pass rate",
    passRate === null ? null : (passRate >= 80 ? "ok" : "warn"));
  if (scored.length) {
    verifyTile.append(buildSparkline(
      scored.slice(0, 20).reverse().map(run => run.verification_outcome === "passed")));
  }
  main.append(el("div", { class: "stat-grid" },
    statTile(String(runningNow), "Running now"),
    statTile(String(pending), "Pending approval", pending ? "warn" : null),
    verifyTile,
    statTile(String(activeSchedules), "Active schedules")));

  // The strip: what fires next. Hidden entirely when nothing is due.
  const upcoming = schedules
    .filter(s => s.enabled && s.next_run_at)
    .sort((a, b) => a.next_run_at.localeCompare(b.next_run_at))
    .slice(0, 3);
  if (upcoming.length) {
    main.append(el("div", { class: "schedule-strip" },
      el("span", { class: "schedule-strip-label" }, "Next up"),
      upcoming.map(s => el("a", {
        class: "schedule-strip-item",
        href: "#/schedules",
        title: fmtTs(s.next_run_at),
      },
        el("span", { class: "mono" }, s.name),
        el("span", { class: "schedule-strip-time" }, relativeTime(s.next_run_at))))));
  }

  // v76-F1: the activity feed — honestly a runs + approvals merge (the only
  // timestamped list views this page has), approvals stamped requested_at
  // (C5). Links go to real run pages only — never a dead activity route.
  const feedItems = [
    ...runs.slice(0, 20).map(run => ({
      type: "run",
      state: run.state,
      summary: (run.summary || run.instructions || "—").slice(0, 100),
      ts: run.updated_at || "",
      href: `#/runs/${run.task_id}`,
    })),
    ...approvals.slice(0, 10).map(approval => ({
      type: "approval",
      state: "pending",
      summary: approvalTitle(approval),
      ts: approval.requested_at || "",
      href: `#/runs/${approval.task_id}`,
    })),
  ].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 15);
  const feedList = feedItems.length
    ? el("div", { class: "activity-list" }, feedItems.map(item => el("a", {
      class: "activity-item searchable",
      href: item.href,
    },
      el("span", { class: `activity-dot activity-dot-${item.state}`, "aria-hidden": "true" }),
      el("span", { class: "activity-text" },
        el("span", { class: "activity-type" }, item.type),
        el("span", { class: "activity-summary" }, item.summary)),
      el("span", { class: "activity-time mono" }, shortTime(item.ts)))))
    : el("p", { class: "empty-state" }, "Nothing yet — assign a run or ask the Queen.");
  const feedCol = el("section", { class: "home-col" },
    el("div", { class: "home-col-head" },
      el("h3", {}, "Activity"),
      el("a", { class: "home-link", href: "#/runs" }, "All runs →")),
    feedList);

  const waiting = approvals.slice(0, 6);
  const waitingList = waiting.length
    ? el("div", { class: "waiting-list" }, waiting.map(approval => {
      const run = approval.run || {};
      const sub = [
        approval.task_id ? approval.task_id.slice(0, 12) : null,
        approval.project_context?.project_id || run.project_context?.project_id || null,
      ].filter(Boolean).join(" · ");
      const priority = approvalPriority(approval);
      return el("a", { class: "waiting-card searchable", href: `#/runs/${approval.task_id}` },
        el("div", { class: "waiting-head" },
          el("span", { class: `prio prio-${priority}` }, priority),
          el("span", { class: "waiting-kind mono" }, approvalKind(approval))),
        el("div", { class: "waiting-title" }, approvalTitle(approval)),
        sub ? el("div", { class: "waiting-sub mono" }, sub) : null);
    }))
    : el("p", { class: "empty-state" }, "Nothing waiting — the queue is clear.");
  const waitCol = el("section", { class: "home-col" },
    el("div", { class: "home-col-head" }, el("h3", {}, "Waiting on you")),
    waitingList);

  main.append(el("div", { class: "home-grid" }, feedCol, waitCol));
}

// ---------- Setup ----------

// Graphite round A: Setup is the last tab of Settings — the first-run
// checklist and the older setup flag live in settings.js.

// ---------- Deep research report rendering (v17) ----------

// A research report's report.html is rendered in a locked-down iframe: an empty
// sandbox attribute grants NOTHING (no scripts, no forms, and crucially no
// same-origin access), so a fetched page can never touch skep's origin, cookies,
// or token. The markdown stays readable as plain text alongside it.
function renderResearchReport(report) {
  const frame = el("iframe", {
    class: "research-report",
    sandbox: "",
    srcdoc: (report && report.html) || "",
  });
  const markdown = el("pre", { class: "research-markdown" }, (report && report.markdown) || "");
  return el("section", { class: "research" },
    el("h3", {}, "Research report"),
    frame,
    markdown);
}

// ---------- The command deck (v25-F1) ----------
// Deterministic /commands: parsed HERE, executed against the same HTTP API the
// buttons use. The Queen's model never sees or executes these.

const LAST_REPO_KEY = "skep-last-repo";
// v96-F2: the active chat's bound project repo (server truth from
// GET /api/chats/{id}.project) — deck commands default to it before the
// localStorage last-repo guess.
let chatBoundRepo = null;

// Phase 10 10.3: the deck is a view of the one registry (serve/commands.py).
// GET /api/commands serves the web surface's rows (name, usage, help) and the
// refusal each other surface's command gets here; /help, completion and the
// unknown-command path read it, runSlashCommand keeps the executors (a test
// pins the two to one set). A name the web does not carry is refused HERE,
// before anything runs — and the confirm endpoint refuses its card again.
let commandDeck = null;
function loadCommandDeck() {
  commandDeck ||= api("GET", "/api/commands").catch((e) => { commandDeck = null; throw e; });
  return commandDeck;
}

function deckRefusal(deck, name) {
  return deck.refusals[name] || `unknown command: /${name}`;
}

function parseSlashCommand(text) {
  const tokens = text.trim().split(/\s+/);
  const name = tokens[0].slice(1).toLowerCase();
  const args = [];
  const flags = {};
  for (let i = 1; i < tokens.length; i += 1) {
    if (tokens[i].startsWith("--") && i + 1 < tokens.length) {
      flags[tokens[i].slice(2)] = tokens[i + 1];
      i += 1;
    } else args.push(tokens[i]);
  }
  return { name, args, flags };
}

// A registered slug binds by slug; anything path-like binds by host path.
function repoBinding(repo) {
  return /[/~]/.test(repo) ? { repo_path: repo } : { repo_slug: repo };
}

function projectIdForRepo(repo) {
  const basename = repo.replace(/\/+$/, "").split("/").pop() || repo;
  return basename.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
}

// ---------- Chat (v6: the Queen's own model) ----------

// v110-F2 + ES-F1: the verdict a drawn card shows once its action left
// 'proposed' — [label, verb]. A failed or interrupted card never reads as
// approved, and an executing one (claimed by another tab or a messenger)
// never reads as denied.
function cardVerdict(action) {
  const note = (action.result && (action.result.error || action.result.note)) || "";
  switch (action.status) {
    case "confirmed": return ["✓ Approved", "confirm"];
    case "executing": return ["… Running — confirmed elsewhere", "confirm"];
    case "failed": return [`✗ Failed — ${note || "the action did not complete"}`, "deny"];
    case "interrupted": return [`⚠ Interrupted — ${note || "serve stopped mid-run"}`, "deny"];
    case "superseded": return [`✗ Superseded — ${note || "replaced by a newer card"}`, "deny"];
    default: return ["✗ Denied", "deny"];
  }
}

// v25-F1 + ES-F5 (B98): operator commands AND gate mirrors resolve on the
// commands endpoints — a gate's Deny denies the review there; on /actions it
// was dismissed and the run sat in pending_approval with no card.
function resolvesOnCommands(card) { return card.source !== "assistant"; }

// Phase 10 R1 D1: the cards that hold this composer — the server's own rule
// (chat.py blocking_cards, with the web's surface hint): pending, not a gate
// mirror, and one the web can confirm. A card skep chat must answer
// (web_refusal) is read-only here and never locks the conversation.
function blocksComposer(action) {
  return action.status === "proposed" && action.source !== "gate" && !action.web_refusal;
}

async function viewChat(main, chatId) {
  let activeChatId = chatId || null;
  // R1 (ADR 0055 D3): the role picker's choices; a broken custom role file
  // must not take the chat view down with it — no picker, server default.
  const [llm, chatList, roleList] = await Promise.all([
    api("GET", "/api/llm/config"),
    api("GET", "/api/chats"),
    api("GET", "/api/roles").catch(() => ({ roles: [], default: null })),
  ]);
  let chats = chatList.chats;
  renderThreads(chats); // H1: the thread list lives in the sidebar now

  header(main, "Chat", "Talk to the hive — the Queen's own model, with the supervisor at hand.");
  if (!llm.configured) {
    pendingChatDraft = "";  // nothing to send it to yet
    main.append(el("p", { class: "note" },
      "No assistant configured yet — set the base URL, API key, and default model in ",
      el("a", { href: "#/settings" }, "Settings"), "."));
    return;
  }

  let detail = activeChatId ? await api("GET", `/api/chats/${activeChatId}`) : null;
  chatBoundRepo = detail?.project?.repo || null;  // v96-F2
  let activeChat = detail?.chat || chats.find(chat => chat.chat_id === activeChatId) || null;
  // H1-G2: the thread's own header line — its title, not the word "Chat".
  const chatSubtitle = "Talk to the hive — the Queen's own model, with the supervisor at hand.";
  // The header shows the short title; hovering it shows the full one.
  const titleHeader = chat => header(main, shortTitle(chatTitle(chat)), chatTitle(chat));
  if (activeChat) titleHeader(activeChat);
  else header(main, "New thread", chatSubtitle);
  let messages = detail?.messages || [];
  let actions = detail?.actions || [];
  const assistantReady = Boolean(llm.configured && (activeChat?.model || llm.default_model));
  let availableModels = [];
  try {
    const modelList = await api("GET", "/api/llm/models");
    availableModels = modelList.models || [];
  } catch {
    availableModels = [];
  }
  const modelChoices = [...new Set([
    activeChat?.model,
    llm.default_model,
    ...availableModels,
  ].filter(Boolean))];

  const log = el("div", { class: "chat-log", role: "log", "aria-live": "polite" });
  const input = el("textarea", { placeholder: "Ask Skep what to do next" });
  const send = iconButton("send message", "↑", { class: "composer-send icon-button" });
  // v44-F9: image attachments — a picker button plus paste-from-clipboard.
  let pendingImages = [];
  const attach = iconButton("attach image", "📎", {
    class: "composer-attach icon-button ghost",
  });
  const attachInput = el("input", {
    type: "file",
    accept: "image/png,image/jpeg,image/webp,image/gif",
    multiple: true,
    style: "display:none",
  });
  const updateAttachBadge = () => {
    attach.title = pendingImages.length
      ? `${pendingImages.length} image(s) ride the next message`
      : "attach image";
    attach.style.opacity = pendingImages.length ? "1" : "";
  };
  attach.addEventListener("click", () => attachInput.click());
  attachInput.addEventListener("change", () => {
    pendingImages.push(...attachInput.files);
    attachInput.value = "";
    updateAttachBadge();
    flash("ok", `${pendingImages.length} image(s) will ride the next message`);
  });
  const uploadPendingImages = async () => {
    const names = [];
    for (const file of pendingImages) {
      const response = await fetch(`/api/chats/${activeChatId}/attachments`, {
        method: "POST", headers: { "X-Skep-Token": token() }, body: file,
      });
      if (response.ok) names.push((await response.json()).name);
      else flash("bad", `image upload failed: ${(await response.json()).detail || response.status}`);
    }
    pendingImages = [];
    updateAttachBadge();
    return names;
  };
  // v53-F6 (ADR 0031): web voice — the browser's speech APIs. Spoken
  // replies use the OS voice stack; RECOGNITION in Chrome is CLOUD-BACKED
  // (audio goes to Google's speech service) — the mic tooltip says so,
  // clicking it is choosing that.
  let voiceOn = localStorage.getItem("skep-voice") === "on";
  const voiceToggle = iconButton("spoken replies on/off", "\u{1F50A}", {
    class: "composer-voice icon-button ghost",
  });
  const updateVoiceBadge = () => {
    voiceToggle.style.opacity = voiceOn ? "1" : "";
    voiceToggle.title = voiceOn ? "spoken replies: on" : "spoken replies: off";
  };
  updateVoiceBadge();
  voiceToggle.addEventListener("click", () => {
    voiceOn = !voiceOn;
    localStorage.setItem("skep-voice", voiceOn ? "on" : "off");
    if (!voiceOn && window.speechSynthesis) speechSynthesis.cancel();
    updateVoiceBadge();
  });
  const speakReply = (text) => {
    if (!voiceOn || !text.trim() || !window.speechSynthesis) return;
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(text.slice(0, 1500)));
  };
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = iconButton(
    "dictate (Chrome sends audio to Google's speech service)", "\u{1F399}", {
      class: "composer-mic icon-button ghost",
    });
  if (!Recognition) mic.disabled = true;
  mic.addEventListener("click", () => {
    if (!Recognition) return;
    const recognizer = new Recognition();
    recognizer.lang = navigator.language || "en-US";
    mic.style.opacity = "1";
    recognizer.onresult = (event) => {
      const transcript = Array.from(event.results).map(r => r[0].transcript).join(" ");
      input.value = input.value ? `${input.value} ${transcript}` : transcript;
      input.focus();
    };
    recognizer.onend = () => { mic.style.opacity = ""; };
    recognizer.onerror = () => { mic.style.opacity = ""; };
    recognizer.start();
  });
  const addContext = iconButton("add context", "+", {
    class: "composer-add-context icon-button ghost",
  });
  addContext.addEventListener("click", () => {
    input.focus();
    flash("ok", "this chat is bound to a project — dispatches and deck commands default to it");
  });
  const modelSelect = el("select", {
    class: "composer-model-select",
    "aria-label": "chat model",
    title: activeChatId ? "start a new chat to change model" : "model for the next new chat",
  }, modelChoices.length
    ? modelChoices.map(model => el("option", { value: model }, model))
    : [el("option", { value: "" }, "No model")]);
  modelSelect.value = activeChat?.model || llm.default_model || "";
  modelSelect.disabled = Boolean(activeChatId);
  // R1: a new chat opens with a role picker defaulting to the onboard choice
  // (or `skep role use`, else assistant: ADR 0059 decision #4); an open chat
  // switches with /role (a confirm card).
  const legacyRoleLess = Boolean(activeChatId) && !activeChat?.role;
  const roleSelect = el("select", {
    class: "composer-model-select",
    "aria-label": "chat role",
    title: activeChatId ? "switch with /role <name>" : "role for the next new chat",
  }, roleChoices(roleList.roles, legacyRoleLess).map(c => el("option", { value: c.value }, c.label)));
  roleSelect.value = activeChatId ? (activeChat?.role || "") : (roleList.default || "assistant");
  roleSelect.disabled = Boolean(activeChatId);
  roleSelect.hidden = roleList.roles.length === 0;
  const contextPopover = el("div", { class: "context-popover" });
  let llmUsage = null; // v74-F6: the local token tally, fetched on hover
  const contextMeter = el("div", {
    class: "composer-context-meter",
    title: "context meter",
    tabindex: "0",
    style: `--context-load: ${contextLoadPercent(detail?.context)}%; --context-floor: 0%`,
  },
    el("span", { class: "composer-context-ring", "aria-hidden": "true" }),
    contextPopover);
  const updateContextMeter = () => {
    const context = detail?.context;
    const percent = contextLoadPercent(context);
    contextMeter.style.setProperty("--context-load", `${percent}%`);
    if (!context) {
      contextMeter.title = "context meter";
      contextPopover.replaceChildren(
        el("div", { class: "context-popover-line" }, "send a message to size the context"));
      return;
    }
    // v74-F4: the floor split from the conversation — "96% at message one"
    // meant "the floor is fixed", not "the chat is full"; now it says so.
    const windowChars = context.window_tokens * 4;
    const floorPercent = Math.min(percent,
      Math.round((context.floor_chars * 100) / windowChars));
    contextMeter.style.setProperty("--context-floor", `${floorPercent}%`);
    const tok = chars => Math.round(chars / 4).toLocaleString();
    const usedTokens = Math.round((context.floor_chars + context.history_chars) / 4);
    const freeTokens = Math.max(0, context.window_tokens - usedTokens);
    contextMeter.title = `context: ${percent}% of ${context.window_tokens} tokens`;
    contextPopover.replaceChildren(
      el("div", { class: "context-popover-head" },
        `${percent}% used of ${context.window_tokens.toLocaleString()} tokens`
        + ` (${windowNote(context.window_tokens, context.model_window, context.num_ctx_source)})`),
      el("div", { class: "context-bar" },
        el("span", { class: "context-bar-floor", style: `width:${floorPercent}%` }),
        el("span", {
          class: "context-bar-history",
          style: `width:${Math.max(0, percent - floorPercent)}%`,
        })),
      el("div", { class: "context-popover-line" },
        `fixed floor ~${tok(context.floor_chars)} tok — tools ${tok(context.tool_surface_chars)}`
        + ` + prompt ${tok(context.system_prompt_chars)}`
        + (context.digest_chars ? ` + digest ${tok(context.digest_chars)}` : "")),
      el("div", { class: "context-popover-line" },
        `conversation ~${tok(context.history_chars)} tok · free ~${freeTokens.toLocaleString()} tok`
        + (context.compacted ? " · older turns compacted" : "")),
      ...(llmUsage ? [el("div", { class: "context-popover-line context-popover-usage" },
        `skep usage — 5h: ${llmUsage.last_5h.total_tokens.toLocaleString()} tok`
        + ` · 7d: ${llmUsage.last_7d.total_tokens.toLocaleString()} tok`
        + (llmUsage.chat ? ` · this chat 5h: ${llmUsage.chat.last_5h.total_tokens.toLocaleString()}`
          + ` tok · 7d: ${llmUsage.chat.last_7d.total_tokens.toLocaleString()} tok` : "")
        + " (local count; account meter: ollama.com/settings)")] : []));
  };
  // v74-F6: refresh the local usage tally when the popover opens — ollama.com
  // exposes no account usage API, so skep counts its own requests.
  contextMeter.addEventListener("mouseenter", async () => {
    // W42b: and this chat's share of it
    const usagePath = activeChatId
      ? `/api/llm/usage?chat_id=${encodeURIComponent(activeChatId)}` : "/api/llm/usage";
    try { llmUsage = await api("GET", usagePath); updateContextMeter(); }
    catch { /* the popover simply omits the usage line */ }
  });
  // v96-F3: the composer strip reads server truth — the chat's bound project
  // (v96-F2), the repo's state, and the effective-policy view. The old
  // transcript scrape is deleted: a strip that guesses a branch from
  // tool-result strings renders stale lies (I8).
  let boundProject = detail?.project || null;
  let pendingProjectId = null;  // selected before the first message creates the chat
  let stripProjects = [];
  let stripFetchedAt = 0;
  const stripPill = (cls) => {
    const popover = el("div", { class: "context-popover" });
    const text = el("span", { class: "strip-pill-text" });
    const pill = el("span", { class: `strip-pill ${cls}`, tabindex: "0", hidden: true },
      text, popover);
    return { pill, text, popover };
  };
  const branchPill = stripPill("strip-branch");
  const policyPill = stripPill("strip-policy");
  const enginePill = stripPill("strip-engine");
  // v96-F4: Push / Open PR — one card each (proposeCommand → confirm), never
  // a direct call. push_branch itself refuses the default branch (I1), so the
  // buttons only show on a non-default checked-out branch.
  let stripBranch = null;
  const pushBtn = el("button", {
    type: "button", class: "strip-btn", hidden: true,
    title: "push this branch to origin (a confirmation card decides)",
  }, "Push");
  const prBtn = el("button", {
    type: "button", class: "strip-btn", hidden: true,
    title: "push and open a PR for this branch (a confirmation card decides)",
  }, "Open PR");
  pushBtn.addEventListener("click", () => {
    if (stripBranch && boundProject?.repo)
      proposeCommand("push_branch", { repo: boundProject.repo, name: stripBranch });
  });
  prBtn.addEventListener("click", () => {
    if (stripBranch && boundProject?.repo)
      proposeCommand("open_pr", { repo: boundProject.repo, branch: stripBranch });
  });
  const projectSelect = el("select", {
    class: "strip-project-select",
    "aria-label": "chat project",
    title: "the project this chat works on — dispatches and deck commands default to it",
  });
  const pop = (head, ...lines) => [
    el("div", { class: "context-popover-head" }, head),
    ...lines.filter(Boolean).map(line => el("div", { class: "context-popover-line" }, line)),
  ];
  const localProjectView = (project) => {
    const binding = (kind) => project.bindings.find(b => b.kind === kind)?.value;
    return {
      project_id: project.project_id,
      name: project.name,
      phase: project.phase,
      coding_engine: project.policy?.coding_engine || "builtin",
      repo: binding("repo_path") || binding("repo_slug") || null,
    };
  };
  const refreshStrip = async (force = true) => {
    if (!force && Date.now() - stripFetchedAt < 5000) return;
    const repo = boundProject?.repo;
    for (const { pill } of [branchPill, policyPill, enginePill]) pill.hidden = !repo;
    if (!repo) {
      stripBranch = null;
      pushBtn.hidden = prBtn.hidden = true;
      return;
    }
    stripFetchedAt = Date.now();
    const encoded = encodeURIComponent(repo);
    const [policy, state] = await Promise.all([
      api("GET", `/api/repos/${encoded}/effective-policy`).catch(exc => ({ error: String(exc) })),
      api("GET", `/api/repos/${encoded}/state`).catch(() => null),
    ]);
    if (state) {
      const behind = state.behind_origin > 0 ? ` ↓${state.behind_origin}` : "";
      branchPill.text.textContent = `⎇ ${state.checked_out_branch || "(detached)"}${behind}`;
      branchPill.popover.replaceChildren(...pop(
        `branch ${state.checked_out_branch || "(detached)"}`,
        `default branch: ${state.default_branch}`,
        state.behind_origin > 0
          ? `behind origin by ${state.behind_origin}`
          : "not behind origin (as of the last fetch)",
        state.last_fetched ? `last fetched ${state.last_fetched}` : "never fetched — local only",
      ));
    } else {
      branchPill.text.textContent = "⎇ ?";
      branchPill.popover.replaceChildren(...pop("repo state unavailable"));
    }
    stripBranch = state?.checked_out_branch || null;
    const onWorkingBranch = Boolean(stripBranch && stripBranch !== state?.default_branch);
    pushBtn.hidden = prBtn.hidden = !onWorkingBranch;
    if (policy.error) {
      // I8: an unresolved policy renders AS unresolved, never as defaults.
      policyPill.text.textContent = "policy unresolved";
      policyPill.popover.replaceChildren(...pop("policy does not resolve", policy.error));
      enginePill.hidden = true;
      return;
    }
    policyPill.text.textContent = policy.execution_mode;
    policyPill.popover.replaceChildren(...pop(
      `${policy.execution_mode} · ${policy.landing}`,
      policy.policy_groups?.length
        ? `groups: ${policy.policy_groups.map(g => g.name).join(", ")}` : null,
      `network: ${policy.network?.length ? policy.network.join(", ") : "(none)"}`,
      policy.provider_hosts?.length ? `provider: ${policy.provider_hosts.join(", ")}` : null,
      `shell allowlist: ${policy.shell_allowlist?.length || 0} commands`,
      policy.trust_root
        ? `trust root: ${policy.trust_root}`
        : "no trust root — the shell allowlist is not applied",
      `verify: ${policy.verify_command}`,
    ));
    enginePill.text.textContent = policy.coding_engine
      + (policy.worker_protocol !== "plan" ? ` · ${policy.worker_protocol}` : "");
    enginePill.popover.replaceChildren(...pop(
      `engine ${policy.coding_engine}`,
      `worker protocol: ${policy.worker_protocol}`,
      `verify: ${policy.verify_command}`,
    ));
  };
  branchPill.pill.addEventListener("mouseenter", () => refreshStrip(false));
  const loadStripProjects = async () => {
    try { stripProjects = (await api("GET", "/api/projects")).projects || []; }
    catch { stripProjects = []; }
    if (!activeChatId && !boundProject) {
      const fallback = newChatProject(stripProjects);
      if (fallback) {
        pendingProjectId = fallback.project_id;  // binds when the first message creates the chat
        boundProject = localProjectView(fallback);
        chatBoundRepo = boundProject.repo || null;
      }
    }
    projectSelect.replaceChildren(
      el("option", { value: "" }, "no project"),
      ...stripProjects.map(p =>
        el("option", { value: p.project_id }, p.name || p.project_id)));
    projectSelect.value = boundProject?.project_id || "";
  };
  projectSelect.addEventListener("change", async () => {
    const projectId = projectSelect.value || null;
    try {
      if (activeChatId) {
        const res = await api("PUT", `/api/chats/${activeChatId}/project`,
          { project_id: projectId });
        boundProject = res.project;
      } else {
        // The chat does not exist yet — bind right after ensureChat creates it.
        pendingProjectId = projectId;
        const picked = stripProjects.find(p => p.project_id === projectId);
        boundProject = picked ? localProjectView(picked) : null;
      }
    } catch (exc) {
      flash("error", `project binding failed: ${exc.message || exc}`);
      projectSelect.value = boundProject?.project_id || "";
      return;
    }
    chatBoundRepo = boundProject?.repo || null;
    refreshStrip();
  });
  loadStripProjects().then(() => refreshStrip());
  // v88-F2: collapsing a panel is a UI preference, so it lives in
  // localStorage like the v76-F8 pins — operator-owned local state, never
  // server state (I11). H1: the chat list moved to the sidebar; the toggle
  // folds the context panel instead.
  let contextHidden = localStorage.getItem("skep-context-panel") === "hidden";
  const toggleContext = iconButton("hide thread context", "ⓘ", {
    class: "chat-toolbar-action icon-button ghost chat-context-toggle",
  });
  const newChat = iconButton("new chat", "+", { class: "chat-toolbar-action icon-button ghost" });
  const deleteChat = iconButton("delete current chat", "×", {
    class: "chat-toolbar-action icon-button danger",
  });
  deleteChat.disabled = !activeChatId;
  newChat.addEventListener("click", () => {
    if (location.hash === "#/chat") route();
    else location.hash = "#/chat";
  });
  deleteChat.addEventListener("click", async () => {
    if (!activeChatId) return;
    await api("DELETE", `/api/chats/${activeChatId}`);
    chats = (await api("GET", "/api/chats")).chats;
    renderThreads(chats);
    location.hash = "#/chat";
  });
  // v75-F2: the two always-disabled placeholder pills (codeChrome) are gone —
  // a disabled placeholder is a broken promise (I9). The status row below is
  // the v96-F3 strip: project selector + branch/policy/engine pills.
  // v25-F3: typing "/" offers the deck — click to fill, no framework.
  const suggest = el("div", { class: "command-suggest", hidden: true });
  // v103-F1: the queued-steer receipt. A message taken mid-turn is SHOWN until
  // it is sent — "queued" that the operator cannot see is indistinguishable
  // from the drop this replaced (I8).
  const queuedText = el("span", { class: "queued-steer-text" });
  const queuedNote = el("div", { class: "queued-steer", hidden: true },
    el("span", { class: "chip tone-info" }, "queued"), queuedText);
  // Declared HERE, above setComposerLocked, on purpose: that function calls
  // flushQueued and runs during the first render, before the composer handlers
  // further down have initialized. Declared beside them it would be a temporal
  // dead zone and the chat would fail to render at all. The empty-queue guard
  // is the first line for the same reason — the startup calls return before
  // they can reach `deliver`.
  let queuedMessage = "";
  const renderQueued = () => {
    queuedNote.hidden = !queuedMessage;
    queuedText.textContent = queuedMessage;
  };
  const flushQueued = () => {
    if (!queuedMessage || send.disabled || !assistantReady) return;
    const content = queuedMessage;
    queuedMessage = "";
    renderQueued();
    input.value = content;
    deliver();
  };
  // Phase 10 10.3: completion, /help and refusals read the registry's web slice.
  let deck = { commands: [], refusals: {} };
  const updateSuggest = async () => {
    const value = input.value;
    if (!value.startsWith("/") || /[\s]/.test(value.slice(1))) {
      suggest.hidden = true;
      return;
    }
    try { deck = await loadCommandDeck(); } catch { suggest.hidden = true; return; }
    if (input.value !== value) return; // a newer keystroke draws its own list
    const needle = value.slice(1).toLowerCase();
    const matches = deck.commands.filter(command => command.name.startsWith(needle));
    suggest.replaceChildren(...matches.map(command =>
      el("button", {
        type: "button",
        class: "command-suggest-item",
        onclick: () => { input.value = `/${command.name} `; suggest.hidden = true; input.focus(); },
      },
        el("span", { class: "mono" }, command.usage),
        el("span", { class: "command-suggest-desc" }, command.help))));
    suggest.hidden = matches.length === 0;
  };

  // H1-G7: one rounded box — the message on top, then "+", role, model and
  // send. The "+" menu holds the same attach / dictate / spoken-replies /
  // project-context buttons as before, each now labelled in words.
  for (const [button, label] of [[attach, "Attach image"], [mic, "Dictate"],
    [voiceToggle, "Spoken replies"], [addContext, "Project context"]]) {
    button.append(el("span", { class: "composer-menu-label" }, label));
  }
  const plusMenu = el("details", { class: "composer-plus" },
    el("summary", {
      class: "composer-plus-toggle",
      title: "attach an image, dictate, spoken replies",
      "aria-label": "more: attach an image, dictate, spoken replies",
    }, el("span", { "aria-hidden": "true" }, "+")),
    el("div", { class: "composer-menu" },
      attach, mic, voiceToggle, boundProject ? addContext : null));
  plusMenu.querySelector(".composer-menu").addEventListener("click", () => { plusMenu.open = false; });
  const composer = el("div", { class: "composer-shell" },
    suggest,
    queuedNote,
    el("div", { class: "composer" },
      input,
      el("div", { class: "composer-meta" },
        plusMenu,
        attachInput,
        roleSelect,
        modelSelect,
        send)));
  // H1-G2: the workspace row moved out of the composer into the thread's
  // header line — project, branch, policy and engine chips, Push / Open PR —
  // then role and model at the right, the context meter, and the thread
  // actions. Same nodes, same handlers; only where they sit changed.
  const statusRow = el("div", { class: "composer-status-row" },
    projectSelect,
    branchPill.pill,
    policyPill.pill,
    enginePill.pill,
    pushBtn,
    prBtn);
  const roleModel = el("span", { class: "view-role-model" });
  // Redrawn wherever the context panel is: a card can change role or model.
  const drawContext = (chat, acts) => {
    roleModel.textContent = chat
      ? `${chat.role || "assistant"} · ${chat.model || llm.default_model || "no model"}` : "";
    if (chat) titleHeader(chat); // the first turn names it
    return renderContextPanel(contextPanel, { chat, defaultModel: llm.default_model, actions: acts });
  };
  // v92-F1 (was v43-F4's single shared line): the worker loader — one pulsing
  // row per live run this chat owns, phase from the status SSE, seconds
  // counted here so the timer runs from dispatch instead of jumping on
  // heartbeat boundaries. Ephemeral on purpose — nothing lands in the
  // transcript. Field test 2026-07-26: a run that gated and finished between
  // boundaries showed nothing at all, and the chat read as stuck.
  const statusLine = el("div", { class: "worker-loader", "aria-live": "polite" });
  const liveRunRows = new Map(); // task_id -> {node, textEl, phase, startedAt}
  const elapsedLabel = (ms) => {
    const secs = Math.max(0, Math.round(ms / 1000));
    return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;
  };
  const drawRunRows = () => {
    for (const [taskId, row] of liveRunRows) {
      row.textEl.textContent =
        `worker ${shortTaskId(taskId)} · ${row.phase} · ${elapsedLabel(Date.now() - row.startedAt)}`;
    }
  };
  const trackRun = (taskId, { phase = null, startedAt = null } = {}) => {
    let row = liveRunRows.get(taskId);
    if (!row) {
      const textEl = el("span", { class: "streaming-indicator" });
      row = {
        node: el("a", { class: "worker-loader-row", href: `#/runs/${taskId}` }, textEl),
        textEl, phase: "dispatched", startedAt: Date.now(),
      };
      liveRunRows.set(taskId, row);
      statusLine.append(row.node);
    }
    if (phase) row.phase = phase;
    if (startedAt !== null) row.startedAt = startedAt;
    drawRunRows();
  };
  const untrackRun = (taskId) => {
    const row = liveRunRows.get(taskId);
    if (row) { row.node.remove(); liveRunRows.delete(taskId); }
  };
  const runRowsTick = setInterval(drawRunRows, 1000);
  const watchStatus = () => {
    if (!activeChatId) return;
    if (window.__skepStatusES) window.__skepStatusES.close();
    let source;
    try { source = new EventSource(`/api/chats/${activeChatId}/status`); }
    catch { return; }
    window.__skepStatusES = source;
    source.addEventListener("status", (event) => {
      const d = JSON.parse(event.data);
      // The server computed elapsed from the run's own events — resync the
      // local clock to it, so the tick counts from dispatch, not from render.
      trackRun(d.task_id, {
        phase: d.phase,
        startedAt: Date.now() - (d.elapsed_seconds || 0) * 1000,
      });
    });
    source.addEventListener("terminal", (event) => {
      const d = JSON.parse(event.data);
      untrackRun(d.task_id);
      if (notifiedTerminalRuns.has(d.task_id)) return;
      notifiedTerminalRuns.add(d.task_id);
      if (d.state === "completed") flash("ok", `run ${d.task_id.slice(0, 13)}… completed`);
      else flash("bad", `run ${d.task_id.slice(0, 13)}… ${d.state}${d.detail ? `: ${d.detail}` : ""}`);
      // The stored terminal line (v59-F2's call to action) is already in the
      // transcript — redraw so it shows without a reload, v81-F13-style.
      // Never mid-stream, never over a draft the operator is typing.
      if (!chatStreamActive && !input.value.trim()) route();
    });
    source.onerror = () => {
      source.close();
      if (window.__skepStatusES === source) window.__skepStatusES = null;
    };
  };
  const pane = el("div", { class: "chat-main" }, log, statusLine, composer);
  // H1 (hive-rooms C10): role and model, cards waiting, runs, spend.
  const contextPanel = el("aside", { class: "chat-context", "aria-label": "thread context" });
  const layout = el("div", { class: `chat-layout${contextHidden ? " context-hidden" : ""}` },
    el("div", { class: "chat-content" }, pane),
    contextPanel);
  main.className = "chat-view";
  viewControls()?.replaceChildren(statusRow, roleModel, contextMeter,
    el("div", { class: "chat-toolbar" }, toggleContext, newChat, deleteChat));
  drawContext(activeChat, actions);
  // A card resolved or raised outside a turn (gate, deck command, a verdict)
  // redraws the panel from the server, so "Waiting on you" never lags the
  // sidebar and the Inbox (I8). The chat row comes from the same read: the
  // card just resolved may be the one that changed its role or model.
  const refreshContext = () => activeChatId && api("GET", `/api/chats/${activeChatId}`)
    .then(fresh => drawContext(fresh.chat, fresh.actions))
    .catch(() => {});
  const applyContextState = () => {
    layout.classList.toggle("context-hidden", contextHidden);
    const label = contextHidden ? "show thread context" : "hide thread context";
    toggleContext.title = label;
    toggleContext.setAttribute("aria-label", label);
    toggleContext.setAttribute("aria-expanded", String(!contextHidden));
  };
  toggleContext.addEventListener("click", () => {
    contextHidden = !contextHidden;
    localStorage.setItem("skep-context-panel", contextHidden ? "hidden" : "shown");
    applyContextState();
  });
  applyContextState();
  main.append(layout);

  const scrollBottom = () => { log.scrollTop = log.scrollHeight; };
  // v40-F3: a route change must close any live worker event streams.
  // v92-F1: … and the status stream + loader ticker with them.
  cleanup = () => {
    closeLiveWorkerSources();
    clearInterval(runRowsTick);
    if (window.__skepStatusES) { window.__skepStatusES.close(); window.__skepStatusES = null; }
  };
  const maybeMountWorkerActivity = (raw) => {
    const result = unwrapToolResult(raw);
    if (result && typeof result === "object" && result.task_id
        && DISPATCHED_RESULT_STATES.has(result.state)) {
      renderWorkerActivity(log, String(result.task_id), { scroll: scrollBottom });
      // v92-F1: the loader starts at dispatch, not at the first heartbeat
      // boundary. Live results only — a replayed dispatch result may be long
      // terminal; the status stream resyncs any still-active run.
      if (chatStreamActive) trackRun(String(result.task_id));
    }
  };
  const setComposerLocked = (locked) => {
    input.disabled = locked || !assistantReady;
    send.disabled = locked || !assistantReady;
    // v56-F6: let the global poll watch this chat's pending cards so a
    // resolution on another surface unlocks the composer here.
    pendingCardChatId = locked ? activeChatId : null;
    // v103-F1: a card resolved elsewhere unlocks this composer — a steer that
    // was waiting on that verdict goes now.
    if (!locked) flushQueued();
  };

  const fillInput = (text) => {
    input.value = text || "";
    input.focus();
  };

  const noteLine = (text) => {
    const node = el("p", { class: "note" }, text);
    log.append(node);
    scrollBottom();
    return node;
  };
  // v90-F2: the three lines a human reads — what runs, what it is for, and the
  // one thing that needs attention. The model-facing description and the raw
  // args move behind a disclosure: they are reference, not the decision.
  // `risk` is absent for benign verbs; an invented risk is as bad as a buried
  // one, so nothing is rendered in its place.
  const cardBody = (d) => {
    const card = d.card || {};
    const details = [];
    if (d.description) details.push(el("p", { class: "tool-description" }, d.description));
    const args = renderArgs(d.args);
    if (args) details.push(args);
    return [
      tierNote(card),
      el("p", { class: "card-headline mono" }, card.headline || d.tool),
      foldItems(card),
      scheduleNode(card),
      forgeNode(card),
      skillNode(card),
      manifestNode(card),
      card.purpose ? el("p", { class: "card-purpose" }, card.purpose) : null,
      card.risk
        ? el("p", { class: "card-risk" },
            el("span", { class: "card-risk-label" }, "Needs your attention: "), card.risk)
        : null,
      taintNote(card),
      details.length
        ? el("details", { class: "card-details" },
            el("summary", {}, "details"), details)
        : null,
    ];
  };

  // first-run O4: the turn's closing tally (whyLine is top-level, T3-1).
  const tallyNote = (d) => {
    const turnDown = d.offer ? el("button", {}, "Turn it down") : null;
    if (turnDown) {
      turnDown.addEventListener("click", async () => {
        turnDown.disabled = true;
        try {
          await api("POST", "/api/onboard/transparency", { verbose: false });
          flash("ok", "why-lines off — skep onboard --verbose brings them back");
        } catch (e) { flash("bad", e.message); turnDown.disabled = false; }
      });
    }
    log.append(el("div", { class: "turn-tally" },
      el("p", { class: "note" }, d.line),
      d.offer ? el("p", { class: "note" }, d.offer, " ", turnDown) : null));
    scrollBottom();
  };

  // A proposed mutation: the model never holds the trigger — you do.
  const actionCard = (d) => {
    const approve = el("button", { class: "primary" }, "Approve");
    const refuse = el("button", { class: "danger" }, "Deny");
    // v112-F1: the keep choice rides the confirm — session stays the default.
    const keep = keepSelect(d.card);
    // v81-F13: the id on the DOM lets reconcilers see which cards are drawn.
    const card = el("div", { class: cardClass(d.card), "data-action-id": d.action_id },
      ...cardBody(d),
      whyLine(d),
      keepCovers(d.card),
      cardReplay(d.card),
      el("div", { class: "actions" }, approve, refuse, keep));
    const verdict = (verb) => async () => {
      approve.disabled = refuse.disabled = true; // double-click guard
      try {
        await runStream(`/api/chats/${activeChatId}/actions/${d.action_id}/${verb}`,
          verb === "confirm" && keep ? { keep: keep.value } : {});
      }
      catch (e) {
        // v54-F2: a failed call leaves the card PENDING — buttons come back.
        flash("bad", e.message);
        approve.disabled = refuse.disabled = false;
        return;
      }
      resolveCardUI(card, verb === "confirm" ? "✓ Approved" : "✗ Denied", verb);
      watchStatus();  // v92-F3: a confirmed card can dispatch or revive a run
    };
    approve.addEventListener("click", verdict("confirm"));
    refuse.addEventListener("click", verdict("deny"));
    log.append(card);
    scrollBottom();
    return card;
  };

  const receiptCard = (d) => { log.append(receiptNode(d)); scrollBottom(); };

  // v54-F2: a resolved card hides its buttons (CSS on .resolved) and shows
  // the verdict instead — done, not just dimmed.
  const resolveCardUI = (card, label, verb) => {
    card.classList.add("resolved");
    card.append(el("p", { class: `verdict ${verb === "confirm" ? "approved" : "denied"}` }, label));
    refreshContext();
  };

  // ---------- the command deck executor (v25-F1) ----------

  const anyCardPending = () => [...log.querySelectorAll(".confirm-card")].some(card =>
    !["resolved", "gate-card", "elsewhere-card"].some(cls => card.classList.contains(cls)));

  const commandHelp = (intro) => {
    log.append(el("div", { class: "command-help" },
      intro ? el("p", { class: "note" }, intro) : null,
      el("ul", { class: "command-help-list" },
        deck.commands.map(command => el("li", {},
          el("span", { class: "mono" }, command.usage), ` — ${command.help}`)))));
    scrollBottom();
  };

  const commandResult = (title, result, tool = null) =>
    renderToolEvent(log, title, result,
      { summary: commandSummary(title, tool, result), scroll: scrollBottom });

  // v128-F11: bind the chat to the project a confirmed /workon made, then
  // reload the picker and the sidebar so both show it.
  const followProject = async (tool, result) => {
    const projectId = projectAfterCommand(tool, result);
    if (!projectId || !activeChatId) return;
    try {
      const res = await api("PUT", `/api/chats/${activeChatId}/project`, { project_id: projectId });
      boundProject = res.project;
      chatBoundRepo = boundProject?.repo || null;
    } catch (exc) {
      flash("error", `project binding failed: ${exc.message || exc}`);
    }
    await loadStripProjects();
    refreshSidebar().catch(() => {});
  };

  // A deck mutation: the same confirm-card, resolved on the commands endpoints
  // (actor operator-command) — the model is never in this loop.
  const commandCard = (d, notes = []) => {
    const approve = el("button", { class: "primary" }, "Confirm");
    const refuse = el("button", { class: "danger" }, "Cancel");
    const card = el("div", { class: "confirm-card command-card", "data-action-id": d.action_id },
      ...cardBody(d),
      notes.map(note => el("p", { class: "note" }, note)),
      el("div", { class: "actions" }, approve, refuse));
    const verdict = (verb) => async () => {
      approve.disabled = refuse.disabled = true; // double-click guard
      try {
        const result = await api(
          "POST", `/api/chats/${activeChatId}/commands/${d.action_id}/${verb}`);
        commandResult(verb === "confirm" ? `${d.tool}: result` : `${d.tool}: canceled`, result, d.tool);
        await followProject(d.tool, result);
        resolveCardUI(card, verb === "confirm" ? "✓ Confirmed" : "✗ Canceled", verb);
        refreshStrip();  // v96-F3: a confirmed verb may have moved branch/policy
      } catch (e) {
        // v54-F2: a failed call leaves the card PENDING — buttons come back.
        flash("bad", e.message);
        approve.disabled = refuse.disabled = false;
      }
      setComposerLocked(anyCardPending());
    };
    approve.addEventListener("click", verdict("confirm"));
    refuse.addEventListener("click", verdict("deny"));
    log.append(card);
    scrollBottom();
    return card;
  };

  // Phase 10 R1 D1: a desk card the web cannot confirm (skep chat's /model).
  // `?surface=` is a client hint, not authorization — so this is usability,
  // not a wall: the card says where to answer it, and Cancel works here
  // (a deny only narrows). Never locks the composer.
  const elsewhereCard = (d) => {
    const cancel = el("button", { class: "danger" }, "Cancel");
    const card = el("div", { class: "confirm-card command-card elsewhere-card", "data-action-id": d.action_id },
      ...cardBody(d),
      el("p", { class: "note" }, d.web_refusal),
      el("div", { class: "actions" }, cancel));
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        const result = await api("POST", `/api/chats/${activeChatId}/commands/${d.action_id}/deny`);
        commandResult(`${d.tool}: canceled`, result, d.tool);
        resolveCardUI(card, "✗ Canceled", "deny");
      } catch (e) {
        flash("bad", e.message);
        cancel.disabled = false;
      }
      setComposerLocked(anyCardPending());
    });
    log.append(card);
    scrollBottom();
    return card;
  };

  // v87-F2: a worker approval gate, mirrored into the chat as an actionable
  // card — resolved on the commands endpoints (actor operator-command), and
  // superseded automatically when the Approvals view answers first. Never
  // locks the composer: a gate can wait; the conversation must not.
  const gateCard = (d) => {
    const approve = el("button", { class: "primary" }, "Approve");
    const refuse = el("button", { class: "danger" }, "Deny");
    const card = el("div", { class: "confirm-card gate-card", "data-action-id": d.action_id },
      el("p", { class: "card-kicker" }, "A run is waiting on your approval"),
      ...cardBody(d),
      whyLine(d),
      el("div", { class: "actions" }, approve, refuse));
    const verdict = (verb) => async () => {
      approve.disabled = refuse.disabled = true; // double-click guard
      try {
        const result = await api(
          "POST", `/api/chats/${activeChatId}/commands/${d.action_id}/${verb}`);
        commandResult(verb === "confirm" ? "approve_review: result" : "deny_review: result", result,
          verb === "confirm" ? "approve_review" : "deny_review");
        resolveCardUI(card, verb === "confirm" ? "✓ Approved" : "✗ Denied", verb);
        refreshStrip();  // v96-F3: an approval may have landed a branch
      } catch (e) {
        // v54-F2: a failed call leaves the card PENDING — buttons come back.
        flash("bad", e.message);
        approve.disabled = refuse.disabled = false;
      }
    };
    approve.addEventListener("click", verdict("confirm"));
    refuse.addEventListener("click", verdict("deny"));
    log.append(card);
    scrollBottom();
    return card;
  };

  // v81-F13: draw every proposed card not already in the DOM. History replay,
  // the stream's finally (an SSE drop loses the live `action` event), and the
  // status poll all reconcile through this one loop — a pending card becomes
  // visible without a reload no matter where it was born.
  const renderPendingCards = () => {
    for (const action of actions) {
      const drawn = log.querySelector(`.confirm-card[data-action-id="${action.action_id}"]`);
      if (action.status !== "proposed") {
        // v110-F2: a drawn card whose action died server-side — superseded by
        // a newer proposal, or answered from another surface — must drop its
        // buttons: a live Approve on a dead action can only 409, which reads
        // as "approval is broken" (the Aug 9 five-close_pr field test).
        if (drawn && !drawn.classList.contains("resolved")) {
          resolveCardUI(drawn, ...cardVerdict(action));
        }
        continue;
      }
      if (drawn) continue;
      // v25-F1: operator /commands resolve on the commands endpoints; only
      // model proposals resume the model after the verdict. v87-F2: gate
      // mirrors ride the commands endpoints too, without locking anything.
      if (action.source === "operator") (action.web_refusal ? elsewhereCard : commandCard)(action);
      else if (action.source === "gate") gateCard(action);
      else actionCard(action);
    }
    setComposerLocked(actions.some(blocksComposer));
  };

  const proposeCommand = async (tool, args, notes = []) => {
    await ensureChat();
    const action = await api("POST", `/api/chats/${activeChatId}/commands`, { tool, args });
    commandCard(action, notes);
    setComposerLocked(true);
    refreshContext();
  };

  // v51-F0: /approve and /deny accept the id the pending-card hint prints.
  // The command IS the decision — resolve directly, no second card.
  const resolvePendingCardById = async (id, verb) => {
    if (!activeChatId) return false;
    const detail = await api("GET", `/api/chats/${activeChatId}`);
    const card = (detail.actions || []).find(
      a => a.action_id === id && a.status === "proposed");
    if (!card) return false;
    if (resolvesOnCommands(card)) {
      const result = await api("POST", `/api/chats/${activeChatId}/commands/${id}/${verb}`);
      commandResult(verb === "confirm" ? `${card.tool}: result` : `${card.tool}: canceled`, result, card.tool);
      await followProject(card.tool, result);
      refreshContext(); // no turn follows, so nothing else redraws the panel
    } else {
      await runStream(`/api/chats/${activeChatId}/actions/${id}/${verb}`);
    }
    setComposerLocked(anyCardPending());
    return true;
  };

  const runSlashCommand = async (content) => {
    renderChatMessage(log, "user", content, { label: "You", scroll: scrollBottom });
    const { name, args, flags } = parseSlashCommand(content);
    try {
      deck = await loadCommandDeck();
      const spec = deck.commands.find(command => command.name === name);
      if (!spec) { commandHelp(deckRefusal(deck, name)); return; }
      if (name === "help") { commandHelp(); return; }
      if (name === "repos") { commandResult("/repos", await api("GET", "/api/repos")); return; }
      if (name === "status") {
        // Phase 10 R1 (Test 10.5): the messengers' own status text, read-only.
        const { summary } = await api("GET", "/api/status");
        log.append(el("div", { class: "command-help command-status" },
          summary.split("\n").map(line => el("p", { class: "note" }, line))));
        scrollBottom();
        return;
      }
      if (name === "skills") { commandResult("/skills", await api("GET", "/api/skills")); return; }
      if (name === "approvals") {
        commandResult("/approvals", await api("GET", "/api/approvals"));
        return;
      }
      if (name === "runs") {
        const limit = Math.max(1, parseInt(args[0], 10) || 10);
        commandResult(`/runs ${limit}`, await api("GET", `/api/runs?limit=${limit}`));
        return;
      }
      if (name === "policy" || name === "state") {
        const repo = args[0] || chatBoundRepo
          || (name === "policy" ? localStorage.getItem(LAST_REPO_KEY) : null);
        if (!repo) { commandHelp(`usage: ${spec.usage}`); return; }
        localStorage.setItem(LAST_REPO_KEY, repo);
        const tail = name === "policy" ? "effective-policy" : "state";
        commandResult(`/${name} ${repo}`,
          await api("GET", `/api/repos/${encodeURIComponent(repo)}/${tail}`));
        return;
      }
      if (name === "browser") {
        await proposeCommand("setup_browser", {});
        return;
      }
      if (name === "trust") {
        // W20: typed by the operator — the decision itself, no card. The model
        // has no tool for this and no channel reaches it.
        if (args.some((bit) => !["private", "untrusted"].includes(bit))) {
          commandHelp(`usage: ${spec.usage}`);
          return;
        }
        commandResult("/trust",
          await api("POST", `/api/chats/${activeChatId}/trust`, { bits: args }));
        return;
      }
      if (name === "phase") {
        const [projectId, phase] = args;
        if (!projectId || !phase) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("set_project_phase", { project_id: projectId, phase });
        return;
      }
      if (name === "unregister") {
        const [projectId] = args;
        if (!projectId) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("remove_project", { project_id: projectId });
        return;
      }
      if (name === "land") {
        const [taskId, branch] = args;
        if (!taskId) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("land_run",
          branch ? { task_id: taskId, branch } : { task_id: taskId });
        return;
      }
      if (name === "steer") {
        const [steerTask, ...noteWords] = args;
        const note = noteWords.join(" ");
        if (!steerTask || !note) { commandHelp(`usage: ${spec.usage}`); return; }
        // v69-F4: typed text is the input — direct POST, no card.
        const steered = await api("POST",
          `/api/runs/${encodeURIComponent(steerTask)}/steer`, { text: note });
        commandResult(`/steer ${steerTask}`, steered);
        return;
      }
      if (name === "resume") {
        const [resumeTask] = args;
        if (!resumeTask) { commandHelp(`usage: ${spec.usage}`); return; }
        // v73-F2: the model-free crash-recovery path — provider trouble is
        // exactly when runs crash, so this never depends on the Queen.
        await proposeCommand("resume_run", { task_id: resumeTask });
        return;
      }
      if (name === "btw") {
        const question = args.join(" ");
        if (!question) { commandHelp(`usage: ${spec.usage}`); return; }
        // v67-F3 (R12b): read-only turn — no cards, no mutations, and the
        // server lets it run beside a pending confirmation.
        await runStream(`/api/chats/${activeChatId}/messages`,
          { content: question, read_only: true });
        return;
      }
      if (name === "approve") {
        const [reviewId, branch] = args;
        if (!reviewId) { commandHelp(`usage: ${spec.usage}`); return; }
        if (await resolvePendingCardById(reviewId, "confirm")) return;
        await proposeCommand("approve_review",
          branch ? { review_id: reviewId, branch } : { review_id: reviewId });
        return;
      }
      if (name === "deny") {
        const [reviewId] = args;
        if (!reviewId) { commandHelp(`usage: ${spec.usage}`); return; }
        if (await resolvePendingCardById(reviewId, "deny")) return;
        await proposeCommand("deny_review", { review_id: reviewId });
        return;
      }
      if (name === "schedule") {
        const [schedName, repo, every, ...rest] = args;
        const instructions = rest.join(" ");
        if (!schedName || !repo || !every || !instructions) {
          commandHelp(`usage: ${spec.usage}`);
          return;
        }
        await proposeCommand("propose_schedule",
          { name: schedName, repo, every, instructions });
        return;
      }
      if (name === "role") {
        if (args.length !== 1) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("set_role", { name: args[0] });
        return;
      }
      if (name === "personality") {
        const value = args.join(" ").trim();
        if (!value) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("set_personality", { value });
        return;
      }
      if (name === "persona") {
        const text = args.join(" ").trim();
        if (!text) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("set_persona", { text });
        return;
      }
      if (name === "workon") {
        const path = args[0];
        if (!path) { commandHelp(`usage: ${spec.usage}`); return; }
        const body = { path, pack: flags.pack || "trusted_local_dev", phase: flags.phase || "build" };
        // Preview first: the card must say exactly what confirming will do.
        const preview = await api("POST", "/api/workon/preview", body);
        const notes = [];
        if (preview.would_git_init) {
          notes.push("not a git repo yet: confirming runs git init here — skep needs a git "
            + "baseline to make changes reviewable and revertible");
        }
        if (preview.would_commit_baseline) {
          notes.push("the current tree will be committed as the baseline");
        }
        (preview.warnings || []).forEach(warning => notes.push(warning));
        const grants = (preview.project && preview.project.dangerous_grant_warnings) || [];
        if (grants.length) notes.push(`grants: ${grants.join(", ")}`);
        await proposeCommand("workon", body, notes);
        return;
      }
      if (name === "setup") {
        const repo = args[0];
        if (!repo) { commandHelp(`usage: ${spec.usage}`); return; }
        localStorage.setItem(LAST_REPO_KEY, repo);
        const projectId = projectIdForRepo(repo);
        const body = {
          project_id: projectId,
          name: projectId,
          pack: flags.pack || "trusted_local_dev",
          phase: flags.phase || "build",
          ...repoBinding(repo),
        };
        // Preview first, so the card states exactly what saving will grant.
        const preview = await api("POST", "/api/projects/preview", body);
        const notes = [];
        if ((preview.dangerous_grant_warnings || []).length) {
          notes.push(`grants: ${preview.dangerous_grant_warnings.join(", ")}`);
        }
        if ((preview.seeded_shell_commands || []).length) {
          notes.push(`seeds ${preview.seeded_shell_commands.length} toolchain command(s)`);
        }
        // v91-F1 (I8): which command re-verification will re-run — the project's
        // pin, or the weaker "whatever the worker nominates for itself".
        const verifyCommand = ((preview.effective_policy || {}).verify_command || "").trim();
        notes.push(
          verifyCommand
            ? `verify_command: ${verifyCommand}`
            : "no verify_command — G10 re-runs the worker's own verify step",
        );
        const { project_id, name: projectName, pack, phase, repo_path, repo_slug } = body;
        const commandArgs = { project_id, name: projectName, pack, phase };
        if (repo_path) commandArgs.repo_path = repo_path;
        if (repo_slug) commandArgs.repo_slug = repo_slug;
        await proposeCommand("setup_project", commandArgs, notes);
        return;
      }
      commandHelp(`unknown command: /${name}`);
    } catch (e) { flash("bad", e.message); }
  };

  // v51-F7: a clarification's choices render as buttons; clicking one sends
  // the choice through the REAL composer path — an answer is just a message.
  const clarificationChoices = (d) => {
    const choices = d.choices || [];
    if (!choices.length) return;
    const row = el("div", { class: "clarify-choices" },
      choices.map(choice => {
        const button = el("button", { class: "ghost" }, choice);
        button.addEventListener("click", () => {
          row.remove();
          input.value = choice;
          deliver();
        });
        return button;
      }));
    log.append(row);
    scrollBottom();
  };

  const runStream = async (path, body = {}) => {
    send.disabled = true;
    chatStreamActive = true;  // v60-F1: the poll must not route() mid-stream
    const replyOptions = {
      label: activeChat?.model || llm.default_model || "skep",
      scroll: scrollBottom,
    };
    let reply = null;
    const currentReply = () => {
      if (!reply) reply = createStreamingReply(log, replyOptions);
      return reply;
    };
    const finishReply = () => {
      if (!reply) return;
      if (reply.isEmpty()) reply.remove();
      else reply.finalize();
      reply = null;
    };
    let cardOpen = false;
    let spokenText = "";  // v53-F6: what the voice toggle reads aloud
    // v40-F1: consecutive tool events accumulate into one activity group; the
    // first content/thinking delta or action card closes it (the same
    // close-before-content ordering the bubble-split invariant pins).
    let group = null;
    const currentGroup = () => {
      if (!group) group = renderActivityGroup(log, { scroll: scrollBottom });
      return group;
    };
    log.setAttribute("aria-busy", "true");
    reply = currentReply();
    // v55-F7: the silent-gap line. The server emits nothing between a tool
    // result and the model's next token, and finishReply() removes the empty
    // placeholder bubble on every tool event — so without this the chat goes
    // blank exactly while it is busiest. Shown after tool events, hidden the
    // moment real content streams, always the last element in the log.
    const working = el("div", { class: "chat-working", hidden: "" },
      el("span", { class: "streaming-indicator" }, "Working"), stopButton(activeChatId));
    log.append(working);
    // v87-F7: the working line carries a local elapsed counter — a 3-minute
    // provider prefill or an await_runs block reads as "Thinking… · 142s",
    // never as a hung page. The server marks phase changes (turn_status);
    // the browser counts the seconds. v92-F2: each phase also stamps its own
    // start — "Running dispatch_run… · 11:17 · 5s" — and ticks from the
    // first second, the start clock anchoring phases too short to count.
    let workingSince = null;
    let workingBase = "";
    const showWorking = (text) => {
      workingBase = text;
      workingSince = Date.now();
      working.firstElementChild.textContent = `${text} · ${shortTime(workingSince)}`;
      working.hidden = false;
      log.append(working);
      scrollBottom();
    };
    const hideWorking = () => { working.hidden = true; workingSince = null; };
    const workingTick = setInterval(() => {
      if (working.hidden || workingSince === null) return;
      const secs = Math.round((Date.now() - workingSince) / 1000);
      working.firstElementChild.textContent =
        `${workingBase} · ${shortTime(workingSince)} · ${secs}s`;
    }, 1000);
    try {
      await streamSse(path, body, {
        message: (d) => {
          if (d.content) { hideWorking(); group = null; currentReply().append(d.content); spokenText += d.content; }
        },
        thinking: (d) => {
          if (d.thinking) { hideWorking(); group = null; currentReply().appendThinking(d.thinking); }
        },
        tool: (d) => {
          finishReply();
          // v90-F3: `decision` rides only an auto-allowed mutation — one that
          // ran because a grant already covered it. Those get a receipt card
          // instead of a bare transcript line; everything else is unchanged.
          if (d.decision) { group = null; receiptCard(d); }
          else {
            currentGroup().add(d.tool, d.result, {
              summary: toolLine(d.tool, d.result),
              scroll: scrollBottom,
            });
          }
          maybeMountWorkerActivity(d.result);
          showWorking(`Ran ${d.tool} — thinking…`);
        },
        turn_status: (d) => {
          showWorking(d.tool ? `Running ${d.tool}…` : "Thinking…");
        },
        action: (d) => { hideWorking(); group = null; finishReply(); cardOpen = true; actionCard(d); },
        tally: (d) => { hideWorking(); group = null; finishReply(); tallyNote(d); },
        // v51-F7: the question text already arrived as a normal message
        // delta; this event only adds the clickable choice buttons.
        clarification: (d) => { hideWorking(); finishReply(); clarificationChoices(d); },
        error: (d) => { hideWorking(); flash("bad", d.detail); },
      });
    } finally {
      chatStreamActive = false;
      log.setAttribute("aria-busy", "false");
      clearInterval(workingTick);
      working.remove();
      finishReply();
      speakReply(spokenText);
      setComposerLocked(cardOpen);  // locked while a verdict is pending
      const latest = await api("GET", "/api/chats");
      chats = latest.chats;
      activeChat = chats.find(chat => chat.chat_id === activeChatId) || activeChat;
      if (activeChatId) {
        detail = await api("GET", `/api/chats/${activeChatId}`);
        messages = detail.messages;
        actions = detail.actions;
        activeChat = detail.chat;
        // v81-F13: an SSE drop loses the live `action` event — the refreshed
        // state re-draws any pending card the stream never delivered.
        renderPendingCards();
      }
      updateContextMeter();
      renderThreads(chats); // titles may have changed
      drawContext(activeChat, actions);
      // v103-F1: the turn is over — send whatever the operator typed during it.
      // After the refresh above, so a card raised by this turn has already
      // locked the composer and the steer waits for the verdict instead of
      // racing it.
      flushQueued();
    }
  };

  // H1-G2: an open thread's title and model are in its header line now.
  if (!activeChat) {
    log.append(el("p", { class: "note" },
      `Start a new chat. Model: ${llm.default_model || "(none — set a default in Settings)"}`));
  }
  // v40-F1: replay batches consecutive tool records into one activity group,
  // so a refresh reconstructs exactly what the live stream drew.
  let replayGroup = null;
  for (const message of messages) {
    if (message.role === "tool") {
      let result = null;
      try { result = JSON.parse(message.content || "null"); }
      catch { result = null; }
      if (!replayGroup) replayGroup = renderActivityGroup(log, { scroll: scrollBottom });
      replayGroup.add(message.tool_name, result, {
        summary: toolLine(message.tool_name, result),
        scroll: scrollBottom,
      });
      maybeMountWorkerActivity(result);
    }
    // Skip empty turns (the model sometimes emits a blank message before a
    // tool call) — an empty bubble reads as a rendering bug.
    else if ((message.content && message.content.trim()) || message.thinking) {
      replayGroup = null;
      if (isInternalNudge(message)) { noteLine(`skep: ${message.content}`); continue; }
      const own = message.role === "user" && authorLabel(message, null) === null;
      renderChatMessage(log, message.role, message.content, {
        label: authorLabel(message,
          message.role === "user" ? "You" : (activeChat?.model || llm.default_model || "skep")),
        createdAt: message.created_at,
        thinking: message.thinking,
        onResend: own ? fillInput : null,
        scroll: scrollBottom,
        attachments: message.attachments || [],
        chatId: activeChatId,
      });
    }
  }
  renderPendingCards();
  scrollBottom();
  watchStatus();  // v43-F4: reopening a chat with a live run resumes the status line

  const ensureChat = async () => {
    if (activeChatId) return;
    activeChat = await api("POST", "/api/chats",
      { model: modelSelect.value || null, role: roleSelect.hidden ? null : roleSelect.value });
    activeChatId = activeChat.chat_id;
    chats = [activeChat, ...chats.filter(chat => chat.chat_id !== activeChatId)];
    modelSelect.value = activeChat.model || llm.default_model || "";
    modelSelect.disabled = true;
    modelSelect.title = "start a new chat to change model";
    roleSelect.value = activeChat.role || "";
    roleSelect.disabled = true;
    roleSelect.title = "switch with /role <name>";
    history.replaceState(null, "", `#/chat/${activeChatId}`);
    setShellActiveRoute(location.hash);
    renderThreads(chats);
    deleteChat.disabled = false;
    // v96-F3: a project picked before the first message binds now.
    if (pendingProjectId) {
      try {
        const res = await api("PUT", `/api/chats/${activeChatId}/project`,
          { project_id: pendingProjectId });
        boundProject = res.project;
        chatBoundRepo = boundProject?.repo || null;
      } catch { /* the selector still shows the pick; next change retries */ }
      pendingProjectId = null;
    }
  };

  // v103-F1: typing while the Queen is working used to be DISCARDED. runStream
  // sets `send.disabled = true` for the length of the turn, the textarea was
  // never disabled, and deliver() opened with `if (send.disabled) return` — so
  // an operator typed a whole steer, pressed Enter, and the message went
  // nowhere with no error, no queue and no cue. Reported as "I can't steer it".
  //
  // Queued, not blocked: mid-turn is exactly when an operator most wants to
  // say something, and the honest thing is to take the message and send it at
  // the first moment the composer is free. Nothing is ever silently dropped.
  const deliver = async () => {
    const content = input.value.trim();
    if (!content || !assistantReady) return;
    if (send.disabled) {
      // A second steer while one is already queued appends rather than
      // replaces — losing the first would be the same bug wearing a queue.
      queuedMessage = queuedMessage ? `${queuedMessage}\n${content}` : content;
      input.value = "";
      renderQueued();
      flash("ok", "queued — sends when this turn finishes");
      return;
    }
    input.value = "";
    suggest.hidden = true;
    // v25-F1: a /command is intercepted BEFORE the LLM chat loop — parsed
    // here, executed against the HTTP API, never sent to the model.
    if (content.startsWith("/")) {
      await runSlashCommand(content);
      // v92-F3: /approve, /resume, /dispatch start runs server-side — the
      // field-test lane where an approval re-dispatched with nobody watching.
      watchStatus();
      input.focus();
      return;
    }
    await ensureChat();
    const attachments = pendingImages.length ? await uploadPendingImages() : [];
    renderChatMessage(log, "user", content, {
      label: "You",
      onResend: fillInput,
      scroll: scrollBottom,
      attachments,
      chatId: activeChatId,
    });
    const payload = attachments.length ? { content, attachments } : { content };
    // D1: the surface hint, so the server's 409 follows the web's lock rule.
    try { await runStream(`/api/chats/${activeChatId}/messages?surface=web`, payload); }
    catch (e) { flash("bad", e.message); send.disabled = false; }
    watchStatus();  // v43-F4: a dispatched run keeps reporting after the turn
    input.focus();
  };
  send.addEventListener("click", deliver);
  // v56-F3: the meter is server truth — typing does not move it; it refreshes
  // when the chat detail does (after each turn).
  input.addEventListener("input", updateSuggest);
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.items || [])]
      .filter(item => item.type && item.type.startsWith("image/"))
      .map(item => item.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    pendingImages.push(...files);
    updateAttachBadge();
    flash("ok", "image attached from clipboard");
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { suggest.hidden = true; return; }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); deliver(); }
  });
  input.focus();

  // A draft handed over from the home/dock launcher: reuse the real composer.
  if (pendingChatDraft && assistantReady) {
    input.value = pendingChatDraft;
    pendingChatDraft = "";
    updateContextMeter();
    deliver();
  }
}

// ---------- Assign (C1: the structured composer) ----------

async function viewAssign(main) {
  header(main, "Assign", "Hand the hive a task — plain instructions, or a saved template.");
  const repos = await repoOptions();
  const { templates } = await api("GET", "/api/templates");
  const policy = await api("GET", "/api/policy");
  const roster = await api("GET", "/api/workers");

  const repoInput = el("input", { list: "repo-list", placeholder: "repo slug or host path" });
  const instructions = el("textarea", { placeholder: "What should the worker do?" });
  const caste = casteSelect(roster.castes);
  // v101-F10: the per-dispatch engine choice chat has had since v95-F3 and the
  // CLI/REST since v100-F9 had no control here at all. Coding only — an engine
  // is a coding-agent choice, and offering it beside `document` would be a lie
  // about what it does. The UI adds a control, never a bypass: an explicit
  // choice still cards (v95-F3/F4), an external engine without a pinned
  // verify_command is still refused by resolve_run_policy, and it is still
  // forced into the sandbox. No client-side pre-validation that could disagree
  // with the resolver (I5).
  const engine = el("select", {},
    el("option", { value: "" }, "(project default)"),
    roster.engines.map(e => el("option", { value: e.name, title: e.summary },
      e.present ? e.name : `${e.name} — ${e.detail}`)));
  const engineField = el("div", { class: "field" }, el("label", {}, "coding engine"),
    el("p", { class: "field-help" },
      "Overrides the project's engine for this run only. An external engine is "
      + "confined by the sandbox, not the capability layer, and needs a pinned "
      + "verify_command."),
    engine);
  const executionMode = el("select", {},
    el("option", { value: "" }, "(choose)"),
    el("option", { value: "workspace" }, "workspace"),
    el("option", { value: "sandbox" }, "sandbox"));
  if (policy.default_execution_mode !== "ask") executionMode.value = policy.default_execution_mode;
  const templateSel = el("select", {}, el("option", { value: "" }, "(no template)"),
    templates.map(t => el("option", { value: t.name }, t.name)));
  const paramsBox = el("div", { class: "row" });
  const network = el("input", { placeholder: "pypi.org, files.pythonhosted.org" });
  const wall = el("input", { type: "number", value: String(policy.default_wall_clock_seconds ?? 900), min: "1" });
  const iterations = el("input", { type: "number", value: String(policy.default_max_iterations ?? 16), min: "1" });
  const actions = el("input", { type: "number", value: String(policy.default_max_actions ?? 100), min: "1" });
  const providerCalls = el("input", { type: "number", value: String(policy.default_max_provider_calls ?? 64), min: "0" });

  templateSel.addEventListener("change", () => {
    paramsBox.replaceChildren();
    const chosen = templates.find(t => t.name === templateSel.value);
    instructions.disabled = Boolean(chosen);
    for (const p of chosen ? chosen.params : []) {
      paramsBox.append(el("div", { class: "field" },
        el("label", {}, `param ${p.name}${p.default !== null && p.default !== undefined ? ` (default ${p.default})` : ""}`),
        el("input", { "data-param": p.name, placeholder: p.name })));
    }
  });

  const submit = el("button", { class: "primary" }, "Dispatch");
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    try {
      let body;
      if (!executionMode.value) throw new Error("choose workspace or sandbox execution");
      if (templateSel.value) {
        const chosen = templates.find(t => t.name === templateSel.value);
        let text = chosen.instructions;
        const params = {};
        for (const input of paramsBox.querySelectorAll("input")) {
          params[input.dataset.param] = input.value;
        }
        for (const p of chosen.params) {
          const value = params[p.name] || p.default;
          if (value === null || value === undefined || value === "") throw new Error(`param ${p.name} is required`);
          text = text.replaceAll(`{{${p.name}}}`, value);
        }
        body = {
          repo: repoInput.value.trim(), instructions: text, caste: chosen.worker_kind,
          execution_mode: executionMode.value,
          network: [...chosen.network], env_allowlist: [...chosen.env_allowlist],
          wall_clock_seconds: chosen.budget?.wall_clock_seconds ?? Number(wall.value),
          max_iterations: chosen.budget?.max_iterations ?? Number(iterations.value),
          max_actions: chosen.budget?.max_actions ?? Number(actions.value),
          max_provider_calls: chosen.budget?.max_provider_calls ?? Number(providerCalls.value),
        };
      } else {
        body = {
          repo: repoInput.value.trim(), instructions: instructions.value,
          caste: caste.value, execution_mode: executionMode.value,
          // Omitted when unset: an absent engine means "the project's", which
          // is not the same request as naming the builtin one (it cards).
          ...(caste.value === "coding" && engine.value ? { engine: engine.value } : {}),
          wall_clock_seconds: Number(wall.value),
          max_iterations: Number(iterations.value),
          max_actions: Number(actions.value),
          max_provider_calls: Number(providerCalls.value),
          network: network.value.split(",").map(s => s.trim()).filter(Boolean),
        };
      }
      const { task_id } = await api("POST", "/api/runs", body);
      location.hash = `#/runs/${task_id}`;
    } catch (e) { flash("bad", e.message); } finally { submit.disabled = false; }
  });

  // v76-F4: the preview echoes the FORM — what you are asking for, never
  // what will be allowed (the dispatch decision stays the server's, I8).
  const preview = el("p", { class: "assign-preview" });
  const casteHelp = el("p", { class: "field-help" });
  const updatePreview = () => {
    const domains = network.value.split(",").map(s => s.trim()).filter(Boolean).length;
    // The operator reads what a caste does at the moment they choose it, from
    // the registry's own summary rather than a second wording (I9).
    casteHelp.textContent = casteSummary(roster.castes, caste.value);
    engineField.hidden = caste.value !== "coding";
    preview.textContent =
      `${caste.value} worker on ${repoInput.value.trim() || "(pick a repo)"}`
      + (caste.value === "coding" && engine.value ? ` via ${engine.value}` : "")
      + ` · ${executionMode.value || "choose execution"} · ${wall.value}s budget`
      + ` · ${domains ? `${domains} domain(s)` : "no network"}`;
  };
  for (const source of [repoInput, caste, engine, executionMode, network, wall]) {
    source.addEventListener("input", updatePreview);
    source.addEventListener("change", updatePreview);
  }

  const help = (text) => el("p", { class: "field-help" }, text);
  const templateStep = el("details", { class: "assign-step" },
    el("summary", {}, "2. Pick a template (optional)"),
    el("p", { class: "note" },
      "Templates pre-fill the instructions and set the caste and budgets."),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "template"), templateSel)),
    paramsBox);

  main.append(el("div", { class: "composer stacky" },
    el("div", { class: "assign-step" },
      el("h3", {}, "1. What should the worker do?"),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "repo"), repoInput,
          el("datalist", { id: "repo-list" }, repos.map(r => el("option", { value: r }))))),
      el("div", { class: "field" }, el("label", {}, "instructions"), instructions)),
    templateStep,
    el("details", { class: "assign-step" },
      el("summary", {}, "3. Advanced settings"),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "caste"), casteHelp, caste),
        engineField,
        el("div", { class: "field grow" },
          el("label", {}, "network allowlist (D1, empty = deny all)"),
          help("Domains the worker may reach; empty denies all network."), network)),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "wall clock (s)"),
          help("Maximum seconds before the run is stopped."), wall),
        el("div", { class: "field" }, el("label", {}, "max iterations"),
          help("Plan/act rounds before the worker must finish."), iterations),
        el("div", { class: "field" }, el("label", {}, "max actions"),
          help("Total capability calls this run may spend."), actions),
        el("div", { class: "field" }, el("label", {}, "max provider calls"),
          help("LLM requests this run may spend — the cost ceiling."), providerCalls))),
    preview,
    // The execution-mode choice stays visible beside Dispatch — workspace vs
    // sandbox is the critical call and never hides in a collapse (the
    // review's own KEEP).
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "execution"), executionMode),
      submit)));
  updatePreview();

  // v76-F4 (C9): the ?template= prefill closes the v75-F7 link contract.
  const query = new URLSearchParams((location.hash.split("?")[1]) || "");
  const wanted = query.get("template");
  if (wanted) {
    if (templates.some(t => t.name === wanted)) {
      templateSel.value = wanted;
      templateSel.dispatchEvent(new Event("change"));
      templateStep.open = true;
    } else {
      flash("bad", `no template named ${wanted} — see #/templates for what exists`);
    }
  }
}

// ---------- Runs (v75-F4: filter tabs + run cards + grouped sections) ----------

const RUN_FILTERS = [
  { key: "all", label: "All", test: () => true },
  {
    key: "running", label: "Running",
    test: r => ["running", "dispatched", "created"].includes(r.state),
  },
  { key: "pending", label: "Pending", test: r => r.state === "pending_approval" },
  { key: "completed", label: "Completed", test: r => r.state === "completed" },
  {
    key: "failed", label: "Failed",
    test: r => ["failed", "rejected", "worker_crashed", "worker_timeout"].includes(r.state),
  },
];

// Grouped "All" view: each run lands in its first matching group; whatever the
// named groups do not claim (superseded, research states, …) shows under
// Other — grouped, never dropped (I8).
const RUN_GROUPS = [
  { label: "Running now", filter: "running" },
  { label: "Waiting on you", filter: "pending" },
  { label: "Completed", filter: "completed" },
  { label: "Failed", filter: "failed" },
  { label: "Other", filter: null },
];

// v101-F11: with nine castes and four engines a Runs list was a list of
// anonymous work — two runs on the same repo, one by the builtin worker and one
// by Claude Code, were visually identical. Rides F4's columns; nothing is
// computed here.
//
// A chip that says "builtin" on every run is noise, and noise is how a card
// stops being read — so the engine appears only when it is NOT builtin. A
// pre-v101 run has NULL columns and renders nothing: no "unknown", no guess.
// An absent field is absent (I8).
function workerChips(run) {
  return [
    run.worker_kind
      ? el("span", { class: "chip tone-info", title: "caste" }, run.worker_kind)
      : null,
    run.coding_engine && run.coding_engine !== "builtin"
      ? el("span", { class: "chip tone-warn", title: "coding engine" }, run.coding_engine)
      : null,
  ].filter(Boolean);
}

function buildRunCard(run) {
  const project = run.project_context
    ? `${run.project_context.project_id} (${run.project_context.phase})`
    : null;
  const autonomy = formatRunAutonomy(run);
  return el("a", {
    // v19-F8: superseded runs were resumed as a successor — the dimming
    // survives the card redesign. searchable keeps topbar filtering working.
    class: `run-card searchable${run.state === "superseded" ? " superseded" : ""}`,
    href: `#/runs/${run.task_id}`,
  },
    el("div", { class: "run-card-header" },
      el("span", { class: "mono run-card-id" }, run.task_id.slice(0, 12)),
      stateChip(run.state),
      run.verification_outcome
        ? el("span", { class: `run-card-verify ${run.verification_outcome}` },
          run.verification_outcome)
        : null,
      el("span", { class: "run-card-time mono", title: fmtTs(run.updated_at) },
        relativeTime(run.updated_at))),
    el("div", { class: "run-card-summary" },
      (run.summary || run.instructions || "—").slice(0, 120)),
    el("div", { class: "run-card-meta" },
      ...workerChips(run),
      run.dispatch_decision?.delegation
        ? el("span", { class: "chip", title: "delegated by" },
          delegationText(run.dispatch_decision.delegation))
        : null,
      taintChip(run.dispatch_decision),
      project ? el("span", {}, project) : null,
      autonomy !== "-" ? el("span", { class: "run-card-autonomy mono" }, autonomy) : null));
}

// first-run L1: coding agents on this machine, governed or not — the same
// presence_view as `skep agents` and the Queen's list_agents. Blocked and
// ungoverned is the gap: an agent waiting on a prompt skep does not gate.
async function buildAgentsSection() {
  let view;
  try { view = await api("GET", "/api/agents"); } catch { return null; }
  if (!view.agents.length) return null;
  return el("section", { class: "run-group" },
    el("div", { class: "run-group-header" },
      el("h3", {}, "Agents on this machine"),
      el("span", { class: "run-group-count" }, String(view.agents.length))),
    el("p", { class: "run-card-summary" }, view.summary),
    el("ul", {}, view.agents.map(a => el("li", { class: "mono" },
      `${a.agent} · pid ${a.pid} · ${a.state} · `,
      a.governed ? `governed run ${a.task_id || "?"}${a.role ? ` (${a.role})` : ""}` : "ungoverned",
      a.gap ? el("strong", {}, " — blocked and ungoverned") : null))));
}

async function viewRuns(main) {
  header(main, "Runs", "Everything the hive has done, newest first.");
  const agents = await buildAgentsSection();
  if (agents) main.append(agents);
  const { runs } = await api("GET", "/api/runs?limit=100");
  if (!runs.length) { main.append(el("p", { class: "empty-state" }, "No runs yet — assign one.")); return; }
  const cards = el("div", { class: "run-cards" });
  const renderCards = (key) => {
    cards.replaceChildren();
    if (key !== "all") {
      cards.append(...runs.filter(RUN_FILTERS.find(f => f.key === key).test).map(buildRunCard));
      return;
    }
    const claimed = new Set();
    for (const group of RUN_GROUPS) {
      const test = group.filter ? RUN_FILTERS.find(f => f.key === group.filter).test : () => true;
      const items = runs.filter(r => !claimed.has(r.task_id) && test(r));
      for (const run of items) claimed.add(run.task_id);
      if (!items.length) continue;
      cards.append(el("section", { class: "run-group" },
        el("div", { class: "run-group-header" },
          el("h3", {}, group.label),
          el("span", { class: "run-group-count" }, String(items.length))),
        items.map(buildRunCard)));
    }
  };
  const counts = Object.fromEntries(
    RUN_FILTERS.map(f => [f.key, runs.filter(f.test).length]));
  main.append(buildFilterBar(RUN_FILTERS, counts, renderCards), cards);
  renderCards("all");
}

// ---------- Run detail (the centerpiece, B3) ----------

const TERMINAL = new Set(
  ["completed", "failed", "pending_approval", "rejected", "worker_timeout", "worker_crashed"]);

// v75-F6: the visual timeline SUMMARIZES the transitions; the Transitions tab
// stays the truth (C6, I8). Canonical happy path + one failure tail; any
// state the model does not know (superseded, resume states, …) is APPENDED
// in arrival order — never dropped.
const TIMELINE_STATES = ["created", "dispatched", "running", "pending_approval", "completed"];
const TIMELINE_FAILURES = new Set(["failed", "rejected", "worker_crashed", "worker_timeout"]);

function buildRunTimeline(transitions) {
  const seen = new Map(); // state -> first transition ts
  for (const t of transitions) if (!seen.has(t.state)) seen.set(t.state, t.ts);
  const nodes = TIMELINE_STATES.map(state => ({
    state, ts: seen.get(state), reached: seen.has(state),
  }));
  const failure = transitions.find(t => TIMELINE_FAILURES.has(t.state));
  if (failure) {
    if (!seen.has("completed")) nodes.pop(); // the failure replaces the unreached tail
    nodes.push({ state: failure.state, ts: failure.ts, reached: true });
  }
  const modeled = new Set([...TIMELINE_STATES, ...TIMELINE_FAILURES]);
  for (const t of transitions) {
    if (modeled.has(t.state)) continue;
    modeled.add(t.state); // append each unmodeled state once, honestly (C6)
    nodes.push({ state: t.state, ts: t.ts, reached: true });
  }
  return el("div", { class: "run-timeline" },
    nodes.map((node, i) => el("div", {
      class: `timeline-node${node.reached ? " reached" : ""} ${node.state}`,
    },
      el("div", { class: "timeline-dot", "aria-hidden": "true" }),
      el("div", { class: "timeline-label" }, node.state.replace(/_/g, " ")),
      node.ts ? el("div", { class: "timeline-time mono" }, shortTime(node.ts)) : null,
      i < nodes.length - 1 ? el("div", { class: "timeline-connector", "aria-hidden": "true" }) : null)));
}

async function viewRunDetail(main, taskId) {
  const live = routeLive(main); // D7: a late read draws nothing once the operator moved on
  const detail = await api("GET", `/api/runs/${taskId}`);
  if (!live()) return;
  const run = detail.run;
  header(main, `run ${taskId.slice(0, 12)}`, run.instructions.slice(0, 120));

  // v75-F6: the full id + a copy button — operators paste ids into commits,
  // PRs, and chat.
  main.append(el("div", { class: "run-id-row" },
    el("span", { class: "mono note" }, taskId),
    iconButton("copy task ID", "⧉", {
      class: "ghost icon-button",
      onclick: () => { copyText(taskId); flash("ok", "task ID copied"); },
    })));

  if (detail.transitions.length) main.append(buildRunTimeline(detail.transitions));

  const meta = el("dl", { class: "kv" });
  const kv = (k, v) => meta.append(el("dt", {}, k), el("dd", {}, v));
  kv("state", stateChip(run.state));
  kv("repo", el("span", { class: "mono" }, run.repo));
  // v101-F11: who ran. Absent on pre-v101 runs, and absent is absent (I8).
  const who = workerChips(run);
  if (who.length) kv("worker", el("span", {}, who));
  kv("execution", run.execution_mode || "sandbox");
  if (run.workspace) kv("workspace", el("span", { class: "mono" }, run.workspace));
  if (detail.project_context) {
    kv("project", `${detail.project_context.project_id} (${detail.project_context.strategy}/${detail.project_context.phase})`);
    kv("binding", `${detail.project_context.binding_kind}: ${detail.project_context.binding_value}`);
  }
  kv("dispatch", formatDecision(detail.dispatch_decision) || "-");
  const delegated = delegationText(detail.dispatch_decision?.delegation);
  if (delegated) kv("delegated", el("span", { class: "mono" }, delegated));
  kv("landing", formatDecision(detail.landing_decision) || "-");
  kv("verification", run.verification_outcome || "-");
  if (detail.reverification) {
    const rv = detail.reverification;
    // v65-F2: no DO-NOT-TRUST scream for a run with nothing to land.
    kv("re-verify (G10)", rv.outcome === "not_applicable"
      ? "nothing to re-verify — run made no file changes"
      : `${rv.outcome} — ${rv.confirmed ? "confirmed" : "NOT CONFIRMED, DO NOT TRUST"}`);
    // v101-F11: run detail is where "was this actually verified, and against
    // WHAT?" gets answered, and the answer lived only in the preflight card.
    // These are the commands G10 actually re-ran (the store's own record), not
    // the pin it was configured from — what happened, not what was intended.
    // Phase 10 R1 D3: "ran" only when one executed (exit codes recorded).
    if ((rv.exit_codes || []).length) {
      kv("re-verify ran", el("span", { class: "mono" },
        `${rv.commands.join(" && ")} → exit ${rv.exit_codes.join(", ")}`));
    } else if ((rv.commands || []).length) {
      kv("re-verify recorded (not run)", el("span", { class: "mono" }, rv.commands.join(" && ")));
    }
  } else if (detail.reverification_missing) {
    // ES-F291 follow-up (I8): a patch with no G10 record is "not re-verified",
    // never a blank row — landing refuses it until the record exists.
    kv("re-verify (G10)", `not re-verified — ${detail.reverification_missing}`);
  }
  if (detail.usage) {
    const u = detail.usage;
    kv("usage (G8)", `${u.provider_calls ?? 0} calls, ${(u.input_tokens ?? 0) + (u.output_tokens ?? 0)} tokens` +
      (u.cost_usd ? `, $${u.cost_usd.toFixed(4)}` : ""));
  }
  kv("summary", run.summary || "-");
  // v19-F12: a one-line "what to do next" for known failure classes.
  if (run.remediation) kv("what to do", `💡 ${run.remediation}`);
  main.append(el("div", { class: "card" }, meta));

  // Approval actions, right where the operator is looking.
  const actions = el("div", { class: "actions" });
  const pending = detail.approvals.filter(a => a.status === "pending");
  const act = (label, cls, fn) => {
    const b = el("button", { class: cls }, label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      const before = location.hash;
      try {
        await fn();
        // If the action navigated, hashchange already re-rendered — a second
        // route() here would interleave with it and duplicate the view.
        if (location.hash === before) route();
      } catch (e) { flash("bad", e.message); b.disabled = false; }
    });
    return b;
  };
  const ensureReview = async () =>
    pending.length ? pending[0].review_id
      : (await api("POST", `/api/runs/${taskId}/approvals`)).review_id;
  // v19-F1: one approval can carry a batch of shell commands.
  const batchCommands = Array.isArray(pending[0]?.commands) ? pending[0].commands : null;
  const isBatch = batchCommands && batchCommands.length > 1;
  // Phase 10 10.1: a pending gate opens in the review flow (diff → forensics → verdict).
  if (pending.length) {
    actions.append(el("a", { class: "home-link", href: `#/review/${pending[0].review_id}` }, "Review →"));
  }
  if (run.state === "pending_approval" || run.state === "completed") {
    actions.append(
      act(isBatch ? `Approve all ${batchCommands.length} commands` : "Approve", "primary", async () => {
        const result = await api("POST", `/api/approvals/${await ensureReview()}/approve`, {});
        if (result.resumed_as) location.hash = `#/runs/${result.resumed_as}`;
        // Phase 10 R1 D8: the landing's warning (G10 could not vouch) is said.
        else if (result.warning) flash("bad", `patch applied on ${result.branch} — ⚠ ${result.warning}`);
        else flash("ok", `patch applied on ${result.branch}`);
      }),
      act("Deny", "danger", async () => {
        await api("POST", `/api/approvals/${await ensureReview()}/deny`, { note: "denied from UI" });
        flash("ok", "denied");
      }));
    if (pending[0]?.action === "shell.run" && offersRemember(pending[0])) {
      actions.append(act(isBatch ? "Allow all & remember" : "Allow command", "ghost", async () => {
        const result = await api("POST", `/api/approvals/${await ensureReview()}/allow-command`, {});
        if (result.resumed_as) location.hash = `#/runs/${result.resumed_as}`;
      }), act("Skip", "ghost", async () => {
        await api("POST", `/api/approvals/${await ensureReview()}/deny`, { note: "skipped from UI" });
        flash("ok", "skipped");
      }));
    }
    if (run.state === "completed") {
      actions.append(act("Open PR", "ghost", async () => {
        const result = await api("POST", `/api/approvals/${await ensureReview()}/pr`, {});
        flash(result.opened ? "ok" : "bad",
          result.opened ? `PR: ${result.url}` : `PR not opened: ${result.detail}`);
      }));
    }
  }
  if (actions.children.length) main.append(actions);
  for (const approval of detail.approvals) {
    main.append(el("p", { class: "note" },
      `approval ${approval.review_id.slice(0, 8)}: `, stateChip(approval.status),
      ` ${approval.action} — ${approval.reason}`,
      approval.resolved_by ? ` (by ${approval.resolved_by} at ${fmtTs(approval.resolved_at)})` : ""));
    if (approval.decision) {
      main.append(el("p", { class: "note" }, `policy: ${formatDecision(approval.decision)}`));
    }
    // v19-F1: list every command a batch approval would grant.
    if (Array.isArray(approval.commands) && approval.commands.length > 1) {
      const list = el("ul", { class: "note mono" });
      for (const cmd of approval.commands) {
        list.append(el("li", {}, el("code", {}, Array.isArray(cmd) ? cmd.join(" ") : String(cmd))));
      }
      main.append(el("p", { class: "note" }, `${approval.commands.length} commands in this plan:`), list);
    }
    if (approval.policy_block) {
      main.append(el("p", { class: "note mono" }, formatPolicyBlock(approval.policy_block)));
    }
  }

  // v75-F6: the four record sections ride tabs — panels are built ONCE and
  // toggled (buildTabBar), so the live events stream keeps appending while
  // another tab is open. Transitions stays reachable: the raw log is the
  // truth the timeline summarizes (C6, I8).
  const tabs = buildTabBar([
    { key: "events", label: "Events" },
    { key: "commands", label: "Commands" },
    { key: "policy", label: "Policy" },
    { key: "transitions", label: "Transitions" },
  ]);

  // Events: live SSE while running, audit replay once terminal.
  const log = el("pre", { class: "log" });
  tabs.panels.get("events").append(log);
  const append = (event) => {
    const raw = JSON.stringify(event.payload);
    const summary = summarizeRunEvent(event);
    const line = el("div", { class: "event", title: raw },
      `${fmtTs(event.ts)}  `, el("span", { class: "etype" }, event.type),
      `  ${summary || raw}`);
    log.append(line);
    log.scrollTop = log.scrollHeight;
  };
  if (TERMINAL.has(run.state)) {
    const { events } = await api("GET", `/api/runs/${taskId}/events`);
    if (!live()) return;
    events.forEach(append);
  } else {
    // D7: a reopened stream replays from the first event — draw each once.
    const appendOnce = onceByEventId(append);
    const stream = liveSource(`/api/runs/${taskId}/events?stream=1`, {
      message: (message) => appendOnce(JSON.parse(message.data)),
      done: () => { stream.close(); route(); },
    });
    cleanup = () => stream.close();
  }

  if (detail.commands.length) {
    tabs.panels.get("commands").append(el("table", {},
      el("thead", {}, el("tr", {},
        ["exit", "command", "purpose", "stdout", "stderr"].map(h => el("th", {}, h)))),
      el("tbody", {}, detail.commands.map(c => el("tr", {},
        el("td", {}, String(c.exit_code)),
        el("td", { class: "mono" }, c.command),
        el("td", {}, c.purpose),
        el("td", { class: "mono command-output" }, c.stdout ?? c.stdout_tail ?? ""),
        el("td", { class: "mono command-output" }, c.stderr ?? c.stderr_tail ?? ""))))));
  } else {
    tabs.panels.get("commands").append(el("p", { class: "note" }, "No commands recorded."));
  }

  if (detail.policy_blocks?.length) {
    const pre = el("pre", { class: "log" });
    for (const block of detail.policy_blocks) {
      pre.append(el("div", { class: "event" },
        el("span", { class: "etype" }, block.capability_id),
        `  ${formatPolicyBlock(block)}`,
      ));
    }
    tabs.panels.get("policy").append(pre);
  } else {
    tabs.panels.get("policy").append(el("p", { class: "note" }, "No policy blocks recorded."));
  }

  if (detail.transitions.length) {
    const pre = el("pre", { class: "log" });
    for (const transition of detail.transitions) {
      const suffix = transition.detail == null
        ? ""
        : `  ${JSON.stringify(transition.detail)}`;
      pre.append(el("div", { class: "event" },
        `${fmtTs(transition.ts)}  `,
        el("span", { class: "etype" }, transition.state),
        suffix,
      ));
    }
    tabs.panels.get("transitions").append(pre);
  } else {
    tabs.panels.get("transitions").append(el("p", { class: "note" }, "No transitions yet."));
  }

  main.append(tabs.bar, tabs.content);

  // The final diff, syntax-lit by line. v106-F9: fetched only when the run
  // recorded a patch artifact — a no-patch run has nothing to 404 about.
  // Phase 10 10.1: drawn file by file (js/diff.js).
  if ((detail.artifacts || []).some((a) => a.kind === "patch")) try {
    const diff = await api("GET", `/api/runs/${taskId}/diff`);
    if (!live()) return;
    main.append(el("h2", {}, "diff"), renderDiff(diff).node);
  } catch { /* no patch artifact — nothing to show */ }
  // Phase 10 10.1: the forensics report (G10, V1, ledger, blame), on demand.
  main.append(forensicsSection(taskId));
}

// ---------- Approvals ----------

// v112-F3: the v109-F8 ledger nudge finally has a face — the keys the
// operator keeps approving with no standing grant, with the remember /
// attach-bundle actions inline (the attach is F2's covering-group offer).
function renderRememberSuggestions(main, suggestions) {
  if (!suggestions.length) return;
  const section = el("div", { class: "card remember-suggestions" },
    el("p", {}, el("strong", {}, "Keeps asking"),
      " — approved repeatedly with no standing grant:"));
  for (const s of suggestions) {
    const row = el("p", { class: "note" },
      el("code", {}, s.resource), ` (${s.action}, ${s.count}× on ${s.repo}) `);
    const act = (label, cls, body, doneNote) => {
      const button = el("button", cls ? { class: cls } : {}, label);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api("POST", "/api/ledger/remember", body);
          flash("ok", doneNote);
          row.remove();
        } catch (e) {
          flash("bad", e.message);
          button.disabled = false;
        }
      });
      return button;
    };
    row.append(act("Remember for project", "",
      { action: s.action, resource: s.resource, repo: s.repo },
      `remembered ${s.resource}`));
    if (s.covering_group) {
      row.append(" ", act(`Attach ${s.covering_group}`, "primary",
        { action: s.action, resource: s.resource, repo: s.repo,
          attach_group: s.covering_group },
        `attached ${s.covering_group} — its whole bundle is granted now`));
    }
    section.append(row);
  }
  main.append(section);
}

async function viewApprovals(main) {
  header(main, "Approvals",
    "The gate queue: risky work waits here for a human verdict — nothing lands on its own.");
  const [{ approvals }, ledger] = await Promise.all([
    api("GET", "/api/approvals"),
    api("GET", "/api/ledger/suggestions").catch(() => ({ suggestions: [] })),
  ]);
  renderRememberSuggestions(main, ledger.suggestions || []);
  if (!approvals.length) {
    // v76-F5: the empty state teaches the next step (I9).
    main.append(el("div", { class: "empty-state" },
      el("p", {}, "Queue is clear — the hive is running smoothly."),
      el("a", { class: "home-link", href: "#/runs" }, "Review completed runs →")));
    return;
  }

  // v76-F5: high-risk first — approvalPriority has ranked approvals since
  // v54 and the list finally sorts by it; oldest waits first within a tier.
  const PRIORITY_ORDER = { high: 0, medium: 1, low: 2 };
  approvals.sort((a, b) =>
    (PRIORITY_ORDER[approvalPriority(a)] ?? 3) - (PRIORITY_ORDER[approvalPriority(b)] ?? 3)
    || (a.requested_at || "").localeCompare(b.requested_at || ""));

  const APPROVAL_FILTERS = [
    { key: "all", label: "All", test: () => true },
    { key: "shell", label: "Shell commands", test: a => approvalKind(a) === "shell_run" },
    { key: "patch", label: "Patches", test: a => approvalKind(a) === "patch_apply" },
    {
      key: "other", label: "Other",
      test: a => !["shell_run", "patch_apply"].includes(approvalKind(a)),
    },
  ];

  const buildApprovalCard = (approval) => {
    const run = approval.run || {};
    const autonomy = formatRunAutonomy(approval.run);
    const card = el("div", { class: "card" },
      el("p", {}, el("a", { href: `#/runs/${approval.task_id}` },
        el("span", { class: "mono" }, approval.task_id.slice(0, 12))),
        " ", stateChip(run.state || "?"), ` — ${approval.action}: ${approval.reason} `,
        // Phase 10 10.1: the same gate, with its diff and forensics in view.
        el("a", { class: "home-link", href: `#/review/${approval.review_id}` }, "Review →")),
      el("p", { class: "note" }, (run.instructions || "").slice(0, 140)));
    // v76-F5 (C5): the age reads requested_at — the field the record carries.
    // The clock is visibility, not authority: timeouts still DENY (I6).
    if (approval.requested_at) {
      const waitedMs = Date.now() - new Date(approval.requested_at).getTime();
      card.append(el("p", {
        class: `approval-age${waitedMs > 3600000 ? " urgent" : ""}`,
        title: fmtTs(approval.requested_at),
      }, `waiting ${relativeTime(approval.requested_at).replace(" ago", "")}`));
    }
    // v106-F3: an unconfirmed G10 verdict belongs ON the approval being
    // granted, not in the response after the human already said yes.
    if (approval.reverification_warning) {
      card.append(el("p", { class: "note approval-reverify-warning" },
        `⚠ ${approval.reverification_warning}`));
    }
    if (approval.project_context) {
      card.append(el("p", { class: "note" },
        `project: ${formatProjectContext(approval.project_context)}`));
    }
    if (autonomy && autonomy !== "-") {
      card.append(el("p", { class: "note" }, `autonomy: ${autonomy}`));
    }
    if (approval.decision) {
      card.append(el("p", { class: "note" },
        `policy: ${formatDecision(approval.decision)}`));
    }
    if (approval.policy_block) {
      card.append(el("p", { class: "note mono" }, formatPolicyBlock(approval.policy_block)));
    }
    // v19-F1: one approval can carry a batch of shell commands — list each so
    // the operator sees exactly what "Approve all" would grant.
    const batchCommands = Array.isArray(approval.commands) ? approval.commands : [];
    const isBatch = batchCommands.length > 1;
    if (isBatch) {
      const list = el("ul", { class: "command-list mono" });
      for (const cmd of batchCommands) {
        list.append(el("li", {}, el("code", {}, Array.isArray(cmd) ? cmd.join(" ") : String(cmd))));
      }
      card.append(el("p", { class: "note" }, `${batchCommands.length} commands in this plan:`), list);
    }
    const actions = el("div", { class: "actions" });
    const verdict = (label, cls, path, body) => {
      const button = el("button", { class: cls }, label);
      button.addEventListener("click", async () => {
        button.disabled = true;
        const before = location.hash;
        try {
          const result = await api("POST", `/api/approvals/${approval.review_id}/${path}`, body);
          if (result.suggestion) flash("ok", result.suggestion);
          if (result.resumed_as) { location.hash = `#/runs/${result.resumed_as}`; return; }
          if (result.url) flash("ok", `PR: ${result.url}`);
          if (location.hash === before) route();
        } catch (e) { flash("bad", e.message); button.disabled = false; }
      });
      return button;
    };
    actions.append(
      verdict(isBatch ? `Approve all ${batchCommands.length}` : "Approve", "primary", "approve", {}),
      verdict("Deny", "danger", "deny", { note: "denied from UI" }));
    if (approval.action === "shell.run" && offersRemember(approval)) {
      actions.append(
        verdict(isBatch ? "Allow all & remember" : "Allow command", "ghost", "allow-command", {}),
        verdict("Skip", "ghost", "deny", { note: "skipped from UI" }),
      );
    }
    if ((approval.action === "network.fetch" || approval.action === "network.read")
        && offersRemember(approval)) {
      // v109-F7: the network twin of "Allow command" — remember the blocked
      // host for this repo's project and resume.
      actions.append(
        verdict("Allow host & remember", "ghost", "allow-host", {}),
        verdict("Skip", "ghost", "deny", { note: "skipped from UI" }),
      );
    }
    if (run.state === "completed") actions.append(verdict("Open PR", "ghost", "pr", {}));
    card.append(actions);
    return card;
  };

  const list = el("div", { class: "stack" });
  const renderList = (key) => {
    list.replaceChildren(
      ...approvals.filter(APPROVAL_FILTERS.find(f => f.key === key).test)
        .map(buildApprovalCard));
  };
  const counts = Object.fromEntries(
    APPROVAL_FILTERS.map(f => [f.key, approvals.filter(f.test).length]));
  main.append(buildFilterBar(APPROVAL_FILTERS, counts, renderList), list);
  renderList("all");
}

// ---------- Projects (Settings → Projects; the Schedules page is js/pages/schedules.js) ----------

// Graphite round A: the project cards and setup form, drawn into Settings →
// Projects (settings.js) — no page header of their own any more.
async function renderProjectList(main) {
  const [{ projects }, { packs }, { repos }, { templates }, { schedules }] = await Promise.all([
    api("GET", "/api/projects"),
    api("GET", "/api/projects/packs"),
    api("GET", "/api/repos"),
    api("GET", "/api/templates"),
    api("GET", "/api/schedules"),
  ]);

  const scheduleCounts = new Map();
  for (const schedule of schedules) {
    const projectId = schedule.project_context?.project_id;
    if (!projectId) continue;
    scheduleCounts.set(projectId, (scheduleCounts.get(projectId) || 0) + 1);
  }

  // v76-F3: cards instead of a table — the phase (the trust dial) is a
  // badge, and clicking a card opens the detail page. The effective-policy
  // JSON moved to the detail page that owns it (still reachable, I8).
  if (projects.length) {
    main.append(el("div", { class: "project-cards" }, projects.map(project => {
      const remove = el("button", {
        class: "danger ghost",
        onclick: async (event) => {
          event.preventDefault();
          event.stopPropagation();
          try { await api("DELETE", `/api/projects/${project.project_id}`); route(); }
          catch (e) { flash("bad", e.message); }
        },
      }, "remove");
      return el("a", {
        class: "project-card searchable",
        href: `#/projects/${project.project_id}`,
      },
        el("div", { class: "project-card-header" },
          el("span", { class: "mono" }, project.project_id),
          phaseChip(project.phase),
          remove),
        el("div", { class: "project-card-name" }, project.name || "—"),
        el("div", { class: "project-card-meta" },
          el("span", {}, project.pack_name
            ? `${project.pack_name}@${project.pack_version || "?"}` : "custom"),
          el("span", {}, project.strategy),
          el("span", {}, `${scheduleCounts.get(project.project_id) || 0} schedule(s)`)),
        el("div", { class: "project-card-bindings note" },
          (project.bindings || []).map(b => `${b.kind}: ${b.value}`).join(", ")
          || "no bindings"));
    })));
  } else main.append(el("p", { class: "empty-state" }, "No trusted projects yet."));

  const projectId = el("input", { placeholder: "acme-api" });
  const name = el("input", { placeholder: "Acme API" });
  const pack = el("select", {},
    packs.map(pack => el("option", { value: pack.name, disabled: pack.status === "draft" },
      `${pack.name}${pack.status === "draft" ? " (draft)" : ""}`)));
  const phase = el("select", {},
    el("option", { value: "build" }, "build"),
    el("option", { value: "bootstrap" }, "bootstrap"),
    el("option", { value: "maintain" }, "maintain"),
    el("option", { value: "publish_candidate" }, "publish_candidate"));
  const repoSlug = el("select", {},
    el("option", { value: "" }, "(no registered repo slug)"),
    repos.map(repo => el("option", { value: repo.name }, repo.name)));
  const repoPath = el("input", { placeholder: "/path/to/repo (optional)" });
  const templateNames = el("input", {
    placeholder: "template-a, template-b",
    value: templates.map(template => template.name).slice(0, 1).join(""),
  });
  const overrides = el("textarea", {
    class: "mono",
    placeholder: "{\"allowed_shell_commands\": [[\"pytest\"]]}",
  });
  const seedSchedules = el("input", { type: "checkbox" });
  seedSchedules.checked = true;
  const previewBox = el("div", { class: "card hidden" });
  const buildBody = () => {
    const body = {
      project_id: projectId.value.trim(),
      name: name.value.trim(),
      pack: pack.value,
      phase: phase.value,
      seed_default_schedules: seedSchedules.checked,
    };
    if (repoSlug.value) body.repo_slug = repoSlug.value;
    if (repoPath.value.trim()) body.repo_path = repoPath.value.trim();
    if (templateNames.value.trim()) {
      body.template_names = templateNames.value.split(",").map(s => s.trim()).filter(Boolean);
    }
    if (overrides.value.trim()) body.policy_overrides = JSON.parse(overrides.value);
    return body;
  };
  const renderProjectPreview = (result) => {
    previewBox.classList.remove("hidden");
    previewBox.replaceChildren(
      el("h3", {}, "Preview"),
      el("p", { class: "note" },
        `${result.project.project_id} · ${result.project.strategy}/${result.project.phase} · `,
        result.pack ? `${result.pack.name}@${result.pack.version}` : "custom strategy"),
      el("p", { class: "note" },
        `warnings: ${(result.dangerous_grant_warnings || []).join(", ") || "-"}`),
      el("p", { class: "note" },
        `dispatch: ${formatDecision(result.sample_dispatch_decision) || "-"}`),
      el("p", { class: "note" },
        `landing: ${formatDecision(result.sample_landing_decision) || "-"}`),
      el("div", { class: "row" },
        el("div", { class: "field grow" },
          el("label", {}, "seeded templates"),
          el("pre", { class: "mono" },
            (result.seeded_templates || []).map(template => template.name).join("\n") || "-")),
        el("div", { class: "field grow" },
          el("label", {}, "seeded schedules"),
          el("pre", { class: "mono" },
            (result.seeded_schedules || []).map(schedule => schedule.name).join("\n") || "-"))),
      el("div", { class: "field" },
        el("label", {}, "effective policy"),
        el("pre", { class: "mono" }, JSON.stringify(result.effective_policy || {}, null, 2))));
  };
  const preview = el("button", { class: "ghost" }, "Preview setup");
  preview.addEventListener("click", async () => {
    preview.disabled = true;
    try {
      const body = buildBody();
      const result = await api("POST", "/api/projects/preview", body);
      renderProjectPreview(result);
    } catch (e) { flash("bad", e.message); } finally { preview.disabled = false; }
  });
  const create = el("button", { class: "primary" }, "Save project");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const body = buildBody();
      const result = await api("POST", "/api/projects/setup", body);
      flash(
        "ok",
        `${result.project_id} saved${result.seeded_schedules?.length ? `; ${result.seeded_schedules.length} schedule(s) seeded` : ""}`,
      );
      route();
    } catch (e) { flash("bad", e.message); create.disabled = false; }
  });

  main.append(el("div", { class: "composer stacky" },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "project id"), projectId),
      el("div", { class: "field grow" }, el("label", {}, "name"), name),
      el("div", { class: "field" }, el("label", {}, "pack"), pack),
      el("div", { class: "field" }, el("label", {}, "phase"), phase)),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "repo slug"), repoSlug),
      el("div", { class: "field grow" }, el("label", {}, "repo path"), repoPath),
      el("div", { class: "field grow" }, el("label", {}, "template names"), templateNames)),
    el("div", { class: "field" }, el("label", {}, "policy overrides"), overrides),
    el("div", { class: "row" },
      el("label", { class: "field" }, seedSchedules, " seed default schedules"),
      preview,
      create)));
  main.append(previewBox);
}

// v76-F3: the project detail page — composed entirely from endpoints that
// already exist (projects + schedules + runs, filtered client-side).
async function viewProjectDetail(main, projectId) {
  const [{ projects }, { schedules }, { runs }] = await Promise.all([
    api("GET", "/api/projects"),
    api("GET", "/api/schedules"),
    api("GET", "/api/runs?limit=100"),
  ]);
  const project = projects.find(p => p.project_id === projectId);
  header(main, `project ${projectId}`, project ? (project.name || "") : "");
  main.append(el("p", {}, el("a", { class: "home-link", href: "#/projects" }, "← All projects")));
  if (!project) {
    main.append(el("p", { class: "empty-state" },
      "Project not found — it may have been removed. Pick one from the list above."));
    return;
  }
  const meta = el("dl", { class: "kv" });
  const kv = (k, v) => meta.append(el("dt", {}, k), el("dd", {}, v));
  kv("phase", phaseChip(project.phase));
  kv("strategy", project.strategy);
  kv("pack", project.pack_name
    ? `${project.pack_name}@${project.pack_version || "?"}` : "custom");
  kv("bindings", (project.bindings || [])
    .map(b => `${b.kind}: ${b.value}`).join(", ") || "-");
  // v97-F5: attached groups, editable in project context — the policies page
  // opens the editor with the protective fork default pre-checked when the
  // group serves more than one project. Reads + a route only; no mutation here.
  const attachedGroups = (project.policy || {}).policy_groups || [];
  if (attachedGroups.length) {
    kv("policy groups", el("span", {}, ...attachedGroups.map(name =>
      el("button", {
        class: "ghost project-group-edit",
        onclick: () => {
          sessionStorage.setItem("skep-group-edit",
            JSON.stringify({ group: name, project: projectId }));
          // graphite-nav: name the section, so the lit item is the one shown.
          location.hash = "#/policies/groups";
        },
      }, `${name} ✎`))));
  }
  // v109-F9 (RSoP): the resolved policy as key → value → decided-by. It
  // renders HERE, not on the Policies page, because "why is this the
  // effective policy" is a per-repo question — the answer depends on this
  // project's binding, phase, and attached groups, and the Policies page has
  // no repo in hand. The raw overlay stays one disclosure deeper.
  const rsopBinding = (project.bindings || [])
    .find(b => b.kind === "repo_path" || b.kind === "repo_slug");
  const effective = rsopBinding
    ? await api("GET",
      `/api/repos/${encodeURIComponent(rsopBinding.value)}/effective-policy`)
      .catch(exc => ({ error: String(exc) }))
    : null;
  const provenance = effective?.policy_provenance;
  const rsop = provenance
    ? el("table", { class: "rsop-table" },
      el("thead", {}, el("tr", {},
        ["key", "value", "decided by"].map(h => el("th", {}, h)))),
      el("tbody", {}, Object.entries(provenance).map(([key, entry]) => el("tr", {},
        el("td", { class: "mono" }, key),
        el("td", { class: "mono" }, JSON.stringify(entry.value)),
        el("td", {}, el("span", { class: "chip tone-info" }, entry.decided_by))))))
    : el("p", { class: "note" }, effective?.error
      ? `policy did not resolve: ${effective.error}`
      : "no repo binding — the resolved per-key view needs one");
  main.append(el("div", { class: "card" }, meta,
    el("details", {},
      el("summary", {}, "effective policy — who decided each key"),
      rsop,
      el("details", {},
        el("summary", {}, "project overlay (raw)"),
        el("pre", { class: "mono" }, JSON.stringify(project.policy || {}, null, 2))))));

  const bound = schedules.filter(s => s.project_context?.project_id === projectId);
  main.append(el("h3", {}, `schedules (${bound.length})`));
  if (bound.length) {
    main.append(el("div", { class: "stack" }, bound.map(s => el("div", { class: "item-card" },
      el("div", { class: "row" },
        el("span", { class: "mono" }, s.name),
        el("span", { class: "note" }, `every ${s.interval_seconds}s`),
        el("span", { class: "note", title: fmtTs(s.next_run_at) },
          s.enabled ? `next ${relativeTime(s.next_run_at)}` : "disabled"))))));
  } else main.append(el("p", { class: "empty-state" }, "No schedules bound to this project."));

  const projectRuns = runs.filter(r => r.project_context?.project_id === projectId);
  main.append(el("h3", {}, `recent runs (${projectRuns.length})`));
  if (projectRuns.length) {
    main.append(el("div", { class: "run-cards" },
      projectRuns.slice(0, 20).map(buildRunCard)));
  } else main.append(el("p", { class: "empty-state" }, "No runs for this project yet."));
}

// ---------- Policies (graphite round A: the page lives in policies.js) ----------

// ---------- Settings (v75-F3: five tabs — one section's DOM at a time) ----------

// Graphite round A: the page and its five tabs (Models, Workers, Projects,
// Channels, Setup) are composed in settings.js; the forms below are its parts.

// v108-F9: the preset picker — one catalog row (GET /api/provider-presets)
// fills protocol + base URL; registry spelling maps to the wire spelling by
// swapping underscores. The key still goes in by hand: values never ride
// the catalog, and azure (no fixed URL) stays type-it-yourself.
function buildPresetPicker(protocol, baseUrl) {
  const presets = {};
  const picker = el("select", {}, el("option", { value: "" }, "preset\u2026"));
  api("GET", "/api/provider-presets").then((cat) => {
    for (const p of cat.presets) {
      if (!p.base_url) continue;
      presets[p.preset_id] = p;
      picker.append(el("option", { value: p.preset_id }, p.label));
    }
  }).catch(() => {});
  picker.addEventListener("change", () => {
    const p = presets[picker.value];
    if (!p) return;
    protocol.value = p.protocol.replace(/_/g, "-");
    baseUrl.value = p.base_url;
  });
  return picker;
}

// v118-F1: on chatgpt.com the Queen signs in with the Codex login, not a key.
function keyFieldLabel(llm) {
  const login = llm.chatgpt_login;
  if (!login) return `API key (stored as a 0600 file${llm.api_key_set ? "; saved" : ""})`;
  if (login.problem) return `ChatGPT plan login (Codex): not usable — ${login.problem}`;
  const facts = [login.plan && `plan ${login.plan}`, login.expires_at && `expires ${login.expires_at}`];
  const shown = facts.filter(Boolean).join(", ");
  const saved = llm.api_key_set ? " (saved)" : "";
  return `ChatGPT plan login (Codex)${shown ? ` (${shown})` : ""} — the Queen needs no key; a key here is for workers${saved}`;
}

// -- assistant (v6): URL + key -> test -> pick a default model --------------
async function renderAssistantTab(panel) {
  const llm = await api("GET", "/api/llm/config");
  const protocol = el("select", {},
    el("option", { value: "ollama" }, "Ollama"),
    el("option", { value: "openai-compat" }, "OpenAI-compatible"),
    el("option", { value: "anthropic" }, "Anthropic"),
    el("option", { value: "openai-responses" }, "OpenAI Responses"),
    el("option", { value: "bedrock" }, "AWS Bedrock"));
  protocol.value = llm.protocol || "ollama";
  const baseUrl = el("input", {
    value: llm.base_url || "",
    placeholder: "https://ollama.com or http://localhost:11434",
  });
  const apiKey = el("input", {
    type: "password", autocomplete: "off",
    placeholder: llm.api_key_set ? "key saved — paste to replace" : "API key (cloud only)",
  });
  // Phase 10 R1 (E4): staged → probed (nothing saved) → a separate Save.
  const connection = stagedConnection({ protocol, baseUrl, apiKey });
  const llmCard = el("div", { class: "card" },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "preset"), buildPresetPicker(protocol, baseUrl)),
      el("div", { class: "field" }, el("label", {}, "assistant protocol"), protocol),
      el("div", { class: "field grow" }, el("label", {}, "assistant base URL"), baseUrl),
      el("div", { class: "field grow" },
        el("label", {}, keyFieldLabel(llm)), apiKey),
      connection.buttons),
    connection.result);
  if (llm.api_key_set) {
    const clearKey = el("button", { class: "ghost" }, "Clear saved key");
    clearKey.addEventListener("click", async () => {
      try { await api("PUT", "/api/llm/config", { api_key: "" }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    llmCard.querySelector(".row").append(clearKey);
  }
  // v74-F1: the num_ctx dial — the API has carried it since v56-F1; a dial
  // that exists only for curl users does not exist.
  const numCtx = el("input", {
    type: "number", min: "1024", step: "1024",
    // v74-F2: only an explicit override fills the field; empty = auto, and
    // the placeholder shows what auto currently resolves to.
    value: llm.num_ctx_source === "override" ? String(llm.num_ctx) : "",
    placeholder: `auto (${windowNote(llm.num_ctx, llm.model_window, llm.num_ctx_source)})`,
  });
  const saveCtx = el("button", { class: "primary" }, "Save context window");
  saveCtx.addEventListener("click", async () => {
    saveCtx.disabled = true;
    try {
      const raw = numCtx.value.trim();
      await api("PUT", "/api/llm/config", { num_ctx: raw ? parseInt(raw, 10) : 0 });
      flash("ok", raw ? `context window: ${raw} tokens` : "context window: auto");
      route();
    } catch (e) { flash("bad", e.message); saveCtx.disabled = false; }
  });
  llmCard.append(
    el("div", { class: "row" },
      el("div", { class: "field grow" },
        el("label", {}, "context window (tokens) — empty = auto"), numCtx),
      saveCtx),
    el("p", { class: "note" },
      "Budgets how much history each turn replays (chars ≈ tokens x 4) on every protocol; "
      + "only the ollama protocol also sends num_ctx on the wire."));
  // v74-F6: the local usage tally — ollama.com has no account usage API, so
  // skep counts its own requests; the account meter stays authoritative.
  const usageLine = el("p", { class: "note" }, "");
  llmCard.append(usageLine);
  api("GET", "/api/llm/usage").then(usage => {
    const fmt = w => `${w.total_tokens.toLocaleString()} tok / ${w.requests} req`;
    usageLine.replaceChildren(
      `Usage counted by skep — last 5h: ${fmt(usage.last_5h)} · last 7d: ${fmt(usage.last_7d)}. `
      + "The account's own 5h/weekly meter (all clients) lives at ",
      el("a", { href: "https://ollama.com/settings", target: "_blank", rel: "noreferrer" },
        "ollama.com/settings"),
      ".");
  }).catch(() => {});
  panel.append(llmCard);
  panel.append(providerShelf());
  if (llm.configured) {
    const modelRow = el("div", { class: "row" },
      el("p", { class: "note" }, "Loading available models..."));
    llmCard.append(modelRow);
    const loadModels = async () => {
      try {
        const { models } = await api("GET", "/api/llm/models");
        modelRow.replaceChildren();
      const known = llm.default_model && !models.includes(llm.default_model)
        ? [llm.default_model, ...models] : models;
      const select = el("select", {}, known.map(m => el("option", { value: m }, m)));
      if (llm.default_model) select.value = llm.default_model;
      const saveModel = el("button", { class: "primary" }, "Save default model");
      saveModel.addEventListener("click", async () => {
        try {
          await api("PUT", "/api/llm/config", { default_model: select.value });
          flash("ok", `default model: ${select.value}`);
        } catch (e) { flash("bad", e.message); }
      });
      modelRow.append(
        el("div", { class: "field grow" },
          el("label", {}, `default model (${models.length} available)`), select),
        saveModel);
      } catch (e) {
        modelRow.replaceChildren(el("p", { class: "note" }, `cannot list models: ${e.message}`));
      }
    };
    loadModels();
  } else {
    llmCard.append(el("p", { class: "note" },
      "Test the connection first — the model list and default model picker appear once it works."));
  }

}

// -- worker provider --------------------------------------------------------
// v101-F9: the roster. Settings → Worker used to be four provider inputs and
// nothing else, so the only way to learn skep can run a researcher, a curator,
// a script or Claude Code was to read the source. Every field here comes from
// the API, which reads the registries — presence included, probed with the same
// call `skep doctor` makes, so the UI and the CLI cannot disagree (I8).
const presenceChip = (present, detail) =>
  el("span", { class: `chip ${present ? "tone-ok" : "tone-bad"}`, title: detail },
    present ? "present" : "absent");

const rosterTable = (headers, rows) => el("table", {},
  el("thead", {}, el("tr", {}, headers.map(h => el("th", {}, h)))),
  el("tbody", {}, rows));

function casteRow(c) {
  return el("tr", {},
    el("td", { class: "mono" }, c.name),
    el("td", {}, c.summary),
    el("td", {}, c.lands
      ? el("span", { class: "chip tone-accent" }, "lands")
      : el("span", { class: "chip tone-muted" }, "no patch")),
    el("td", {},
      c.needs_provider ? el("span", { class: "chip tone-info" }, "provider") : null,
      c.needs_network ? el("span", { class: "chip tone-warn" }, "network") : null,
      !c.needs_provider && !c.needs_network
        ? el("span", { class: "chip tone-muted" }, "offline")
        : null),
    el("td", {}, presenceChip(c.present, c.detail)));
}

function engineRow(e) {
  return el("tr", {},
    el("td", { class: "mono" }, e.name),
    el("td", {}, e.summary),
    el("td", {}, e.external
      ? el("span", { class: "chip tone-warn" }, "sandbox only")
      : el("span", { class: "chip tone-ok" }, "capability layer")),
    el("td", { class: "mono note" }, e.binary || "—"),
    el("td", {}, presenceChip(e.present, e.detail)));
}

function renderWorkerRoster(panel, roster) {
  panel.append(el("div", { class: "card" },
    el("h3", {}, "Castes"),
    el("p", { class: "field-help" },
      "Which worker runs a task. Only castes marked “lands” can "
      + "produce a patch; the rest produce artifacts and reports."),
    rosterTable(["caste", "what it does", "lands", "needs", ""],
      roster.castes.map(casteRow))));

  panel.append(el("div", { class: "card" },
    el("h3", {}, "Coding engines"),
    el("p", { class: "field-help" },
      "Which coding agent runs a coding task — set per project, or per "
      + "dispatch. An absent engine names the binary that was probed."),
    rosterTable(["engine", "what it does", "confined by", "binary", ""],
      roster.engines.map(engineRow)),
    // I12: this is the one place an operator picks an engine, so this is where
    // the boundary gets stated — not in a doc they have to go find.
    el("p", { class: "field-help" },
      "An external engine's own commands do NOT pass skep's capability layer "
      + "— the sandbox confines it: workspace-only writes and the per-task "
      + "network pin, so it can commit inside its disposable worktree but "
      + "cannot reach a remote. Its built-in verification is "
      + "`git diff --check`, which says nothing about correctness, so an "
      + "external engine requires the project to pin a verify_command "
      + "(ADR 0047). Patches still land only through a human approval.")));
}

// Graphite round A: the roster is Settings → Workers; the override below is
// the "Worker model" block of Settings → Models.
async function renderWorkersTab(panel) {
  renderWorkerRoster(panel, await api("GET", "/api/workers"));
}

async function renderWorkerModel(panel) {
  const settings = await api("GET", "/api/settings");
  const provider = el("input", { value: settings.provider || "", placeholder: "anthropic" });
  const model = el("input", { value: settings.model || "", placeholder: "claude-fable-5" });
  const endpoint = el("input", { value: settings.endpoint || "", placeholder: "(provider default)" });
  const keyEnv = el("input", { value: settings.api_key_env || "", placeholder: "ANTHROPIC_API_KEY" });
  const save = el("button", { class: "primary" }, "Save provider");
  save.addEventListener("click", async () => {
    try {
      await api("PUT", "/api/settings", {
        provider: provider.value.trim(), model: model.value.trim(),
        endpoint: endpoint.value.trim() || null, api_key_env: keyEnv.value.trim() || null,
      });
      flash("ok", "provider saved — the key itself stays an env var, never stored");
    } catch (e) { flash("bad", e.message); }
  });
  panel.append(el("div", { class: "card" },
    settings.configured ? null
      : el("p", { class: "note" },
        "Optional override — the default coding worker uses the chat model's connection above."),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "provider"), provider),
      el("div", { class: "field grow" }, el("label", {}, "model"), model)),
    el("div", { class: "row" },
      el("div", { class: "field grow" }, el("label", {}, "endpoint (optional)"), endpoint),
      el("div", { class: "field grow" }, el("label", {}, "API key env var (name only, G2)"), keyEnv),
      save)));

}

// -- channels (v26): messenger entrances — same Queen, same gates ----------
async function renderChannelsTab(panel) {
  const { channels } = await api("GET", "/api/channels");
  // v75-F3: the status summary strip — reads only fields the API returns
  // (enabled / live / secret_configured), no invented state (I8).
  panel.append(el("div", { class: "channel-summary" },
    Object.entries(channels).map(([name, channel]) => el("div", { class: "channel-summary-item" },
      el("span", {
        class: `channel-status-dot ${channel.enabled && channel.live ? "live" : "off"}`,
        "aria-hidden": "true",
      }),
      el("strong", { class: "mono" }, name),
      el("span", { class: "note" },
        channel.enabled ? (channel.live ? "live" : "enabled, no live transport") : "disabled"),
      channel.secret_configured ? el("span", { class: "note" }, "· token configured") : null))));
  panel.append(el("p", { class: "note" },
    "Messenger entrances route into the same chat → confirmation → audit flow as this UI. "
    + "Only low-risk actions are confirmable from a channel; shell and policy approvals "
    + "stay web-UI-only — except for a channel admin you name below, who can confirm "
    + "every card from that messenger (for when the web UI is out of reach)."));
  for (const [name, channel] of Object.entries(channels)) {
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = channel.enabled;
    const canConfirm = el("input", { type: "checkbox" });
    canConfirm.checked = channel.channel_can_confirm;
    const identities = el("input", {
      value: (channel.allowed_identities || []).join(", "),
      placeholder: "allow-listed chat/channel ids, comma-separated (unknown fails closed)",
    });
    const secret = el("input", {
      type: "password", autocomplete: "off",
      placeholder: channel.secret_configured ? "token saved — paste to replace" : "bot token",
    });
    const signingSecret = name === "slack" ? el("input", {
      type: "password", autocomplete: "off",
      placeholder: channel.signing_secret_configured
        ? "signing secret saved — paste to replace" : "signing secret (webhook verify)",
    }) : null;
    // v78-F1: the delivery volume dial — filters pushes at the outbound choke
    // point; it can only silence, never allow. The record is never filtered.
    const notifyLevel = el("select", {},
      el("option", { value: "all" }, "all — every push"),
      el("option", { value: "approvals" }, "approvals — only action-needed pushes"),
      el("option", { value: "none" }, "none — no pushes"));
    notifyLevel.value = channel.notification_level || "all";
    // v44-F1: Discord routing parity — mention gating, auto-threads, user allowlist.
    const requireMention = name === "discord" ? el("input", { type: "checkbox" }) : null;
    if (requireMention) requireMention.checked = !!channel.require_mention;
    const autoThread = name === "discord" ? el("input", { type: "checkbox" }) : null;
    if (autoThread) autoThread.checked = !!channel.auto_thread;
    // ES (B114): every channel authorizes a PERSON — empty admits nobody.
    const users = el("input", {
      value: (channel.allowed_users || []).join(", "),
      placeholder: "user ids who may drive skep, comma-separated (empty = nobody)",
    });
    // v130-F5 (ADR 0067): the owner who may confirm EVERY card from this
    // messenger — named here, at the desk, never from the channel itself.
    const admins = el("input", {
      value: (channel.admin_users || []).join(", "),
      placeholder: "your user id: confirm every card from this channel (must be an allowed user)",
    });
    const saveChannel = el("button", { class: "primary" }, "Save channel");
    saveChannel.addEventListener("click", async () => {
      saveChannel.disabled = true;
      try {
        const body = {
          enabled: enabled.checked,
          channel_can_confirm: canConfirm.checked,
          allowed_identities: identities.value.split(",").map(s => s.trim()).filter(Boolean),
          notification_level: notifyLevel.value,
        };
        if (requireMention) body.require_mention = requireMention.checked;
        if (autoThread) body.auto_thread = autoThread.checked;
        body.allowed_users = users.value.split(",").map(s => s.trim()).filter(Boolean);
        body.admin_users = admins.value.split(",").map(s => s.trim()).filter(Boolean);
        // naming an admin turns confirming on (an admin who cannot confirm is none)
        if (body.admin_users.join() && body.admin_users.join() !== (channel.admin_users || []).join()) {
          body.channel_can_confirm = true;
        }
        if (secret.value.trim()) body.secret = secret.value.trim();
        if (signingSecret && signingSecret.value.trim()) {
          body.signing_secret = signingSecret.value.trim();
        }
        await api("PUT", `/api/channels/${name}`, body);
        flash("ok", `${name} saved — secrets stay 0600 files, never shown again`);
        route();
      } catch (e) { flash("bad", e.message); saveChannel.disabled = false; }
    });
    panel.append(el("div", { class: "card" },
      el("div", { class: "row" },
        el("strong", { class: "mono" }, name),
        el("span", { class: `state ${channel.live ? "completed" : "pending"}` },
          channel.live ? "live transport" : "web-UI-only (no live transport in this build)"),
        channel.secret_configured ? el("span", { class: "note" }, "token configured") : null),
      el("div", { class: "row" },
        el("label", {}, enabled, " enabled"),
        el("label", {
          title: "even when on, only low-risk action classes are channel-confirmable",
        }, canConfirm, " may confirm low-risk actions")),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "notification level"), notifyLevel),
        el("span", { class: "note" },
          "none: nothing is pushed to this messenger; the web UI and the chat "
          + "transcript still record everything")),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "allowed identities"), identities)),
      requireMention ? el("div", { class: "row" },
        el("label", { title: "guild messages need an @mention; threads skep opened and DMs are exempt" },
          requireMention, " require @mention in servers"),
        el("label", { title: "each routed mention opens a thread; the conversation continues there" },
          autoThread, " auto-thread per mention")) : null,
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "allowed users"), users)),
      el("div", { class: "row" },
        el("div", { class: "field grow" },
          el("label", { title: "confirms every card from this messenger, HIGH ones included" },
            "admin users (approve every card here)"), admins)),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "bot token (write-only)"), secret),
        signingSecret
          ? el("div", { class: "field grow" },
            el("label", {}, "signing secret (write-only)"), signingSecret)
          : null,
        saveChannel)));
  }

}

// -- webhooks (v44-F3): signed inbound events -> a chat you watch ----------
async function renderWebhooksTab(panel) {
  const { webhooks } = await api("GET", "/api/webhooks");
  panel.append(el("p", { class: "note" },
    "Inbound notifications (GitHub / generic CI). Point the sender at POST /hooks/<name>; "
    + "GitHub signs with the secret (X-Hub-Signature-256), generic senders present it as "
    + "X-Skep-Secret. The rendered line lands in the bound chat and its messenger — "
    + "never a model turn."));
  for (const hook of webhooks) {
    const removeHook = el("button", { class: "danger" }, "remove");
    removeHook.addEventListener("click", async () => {
      try { await api("DELETE", `/api/webhooks/${hook.name}`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    panel.append(el("div", { class: "card" },
      el("div", { class: "row" },
        el("strong", { class: "mono" }, hook.name),
        el("span", { class: "mono note" }, hook.url_path),
        el("span", { class: "note" }, hook.chat_id ? "→ chat" : "→ notes"),
        hook.secret_configured ? el("span", { class: "note" }, "secret configured") : null,
        removeHook),
      el("div", { class: "note mono" }, hook.template)));
  }
  {
    const hookName = el("input", { placeholder: "name (slug, e.g. github-ci)" });
    const hookTemplate = el("input", {
      placeholder: "template, e.g. 📦 {repository.full_name}: {workflow_run.conclusion}",
    });
    const hookChat = el("input", { placeholder: "chat id to deliver into (optional)" });
    const hookSecret = el("input", {
      type: "password", autocomplete: "off", placeholder: "secret (write-only)",
    });
    const addHook = el("button", { class: "primary" }, "Add webhook");
    addHook.addEventListener("click", async () => {
      addHook.disabled = true;
      try {
        const body = {
          name: hookName.value.trim(),
          template: hookTemplate.value,
          secret: hookSecret.value.trim(),
        };
        if (hookChat.value.trim()) body.chat_id = hookChat.value.trim();
        await api("POST", "/api/webhooks", body);
        flash("ok", "webhook saved — the secret stays a 0600 file, never shown again");
        route();
      } catch (e) { flash("bad", e.message); addHook.disabled = false; }
    });
    panel.append(el("div", { class: "card" },
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "name"), hookName),
        el("div", { class: "field grow" }, el("label", {}, "deliver into chat"), hookChat)),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "template"), hookTemplate)),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "secret"), hookSecret),
        addHook)));
  }
}

// -- repos: the registry the hive may work on -------------------------------
async function renderReposTab(panel) {
  const { repos } = await api("GET", "/api/repos");
  if (repos.length) {
    panel.append(el("table", {},
      el("thead", {}, el("tr", {}, ["name", "url", "path", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, repos.map(r => {
        const remove = el("button", { class: "danger" }, "remove");
        remove.addEventListener("click", async () => {
          try { await api("DELETE", `/api/repos/${r.name}`); route(); }
          catch (e) { flash("bad", e.message); }
        });
        return el("tr", {},
          el("td", { class: "mono" }, r.name),
          el("td", { class: "mono" }, r.url || "-"),
          el("td", { class: "mono note" }, r.path),
          el("td", {}, remove));
      }))));
  } else panel.append(el("p", { class: "empty-state" }, "No repos registered — add one by URL."));
  const url = el("input", { placeholder: "https://github.com/you/project.git" });
  const slug = el("input", { placeholder: "name (optional)" });
  const add = el("button", { class: "primary" }, "Clone repo");
  add.addEventListener("click", async () => {
    add.disabled = true;
    try {
      await api("POST", "/api/repos",
        { url: url.value.trim(), name: slug.value.trim() || null });
      route();
    } catch (e) { flash("bad", e.message); add.disabled = false; }
  });
  panel.append(el("div", { class: "composer stacky" }, el("div", { class: "row" },
    el("div", { class: "field grow" }, el("label", {}, "clone URL"), url),
    el("div", { class: "field" }, el("label", {}, "name"), slug),
    add)));
}

// ---------- health poll ----------

// v19-F10: visibility-aware, self-rescheduling poll with failure backoff — a
// backgrounded tab makes zero requests, and a flaky server is not hammered.
const POLL_MIN_MS = 5000;
const POLL_MAX_MS = 60000;
let pollDelay = POLL_MIN_MS;
let pollTimer = null;
// v56-F6 (ADR 0038): what the badge counts, the views must show. The poll
// tracks the pending count and the chat whose composer a card has locked.
let lastPendingApprovals = null;
let pendingCardChatId = null;
// v60-F1: true while a chat stream (message or card verdict) is being
// consumed. Confirming a card resolves it server-side BEFORE the model's
// continuation streams, so for those seconds the store shows zero proposed
// cards while pendingCardChatId is still set — without this guard the v56-F6
// poll called route() mid-stream, re-rendering the view and orphaning the
// live stream's DOM (field test 2026-07-18: screen flicker, the follow-up
// card invisible until it auto-denied, the chat apparently dead).
let chatStreamActive = false;

async function poll() {
  // Skip the fetch entirely while the tab is hidden.
  if (document.visibilityState !== "visible") return true;
  try {
    const status = await api("GET", "/api/status");
    document.getElementById("health").textContent =
      `${status.status} · ${status.pending_approvals} pending`;
    document.getElementById("sidebar-machine").textContent = status.machine; // H1
    refreshSidebar().catch(() => {}); // H1: dots and counts ride this poll too
    // v76-F2: the Queen tile's liveness rides THIS poll — no new timer. The
    // tile unhides once the model label is filled (an empty tile is a lie).
    const queenTile = document.getElementById("topbar-queen-status");
    const queenDot = queenTile?.querySelector(".queen-dot");
    if (queenDot) {
      queenDot.classList.remove("down");
      queenDot.classList.add("ok");
      if (document.getElementById("queen-model-label").textContent) {
        queenTile.classList.remove("hidden");
      }
      if (queenTitleBase && Date.now() - queenUsageAt > 60000) {
        queenUsageAt = Date.now();
        api("GET", "/api/llm/usage").then(usage => {
          queenTile.title = `${queenTitleBase} · ${usage.last_5h.requests} req/5h`;
        }).catch(() => {});
      }
    }
    const badge = document.getElementById("approvals-badge");
    badge.textContent = String(status.pending_approvals);
    badge.classList.toggle("hidden", status.pending_approvals === 0);
    const hash = location.hash || "#/";
    if (lastPendingApprovals !== null
        && status.pending_approvals !== lastPendingApprovals
        && (hash === "#/" || hash === "" || hash === "#/approvals")) {
      route(); // the approvals list / Home "waiting" panel re-render with the badge
    }
    lastPendingApprovals = status.pending_approvals;
    if (pendingCardChatId && !chatStreamActive && hash.startsWith("#/chat")) {
      // A card resolved elsewhere (second tab, deck, the auto-deny sweep)
      // unlocks this composer within one poll cycle instead of on reload.
      // v60-F1: never while a stream is in flight — the stream reconciles
      // the composer itself in its finally, and a route() here would tear
      // down the DOM the stream is appending to.
      const detail = await api("GET", `/api/chats/${pendingCardChatId}`);
      if (!detail.actions.some(blocksComposer)) {
        pendingCardChatId = null;
        route();
      }
    }
    const openChatMatch = !chatStreamActive && hash.match(/^#\/chat\/(.+)$/);
    if (openChatMatch) {
      // v81-F13: the inverse direction — a card born while this view sat idle
      // (background approval, another tab, the ticker) appears within one
      // poll instead of on reload. route() replays history, which draws it.
      const detail = await api("GET", `/api/chats/${openChatMatch[1]}`);
      const undrawn = detail.actions.some(action =>
        action.status === "proposed"
        && !document.querySelector(`.confirm-card[data-action-id="${action.action_id}"]`));
      if (undrawn) route();
    }
    const taskBadge = document.getElementById("tasks-badge");
    const { tasks } = await api("GET", "/api/tasks");
    const dueTasks = tasks.filter(t => t.due).length;
    taskBadge.textContent = String(dueTasks);
    taskBadge.classList.toggle("hidden", dueTasks === 0);
    return true;
  } catch {
    // v76-F2: a failed poll turns the Queen dot down — liveness, honestly.
    const queenDot = document.querySelector("#topbar-queen-status .queen-dot");
    if (queenDot) { queenDot.classList.remove("ok"); queenDot.classList.add("down"); }
    return false; /* login screen is already up */
  }
}

function schedulePoll(delay) {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(pollTick, delay);
}

async function pollTick() {
  const ok = await poll();
  pollDelay = ok ? POLL_MIN_MS : Math.min(pollDelay * 2, POLL_MAX_MS);
  schedulePoll(pollDelay);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    pollDelay = POLL_MIN_MS;
    schedulePoll(0); // poll immediately once on return to the tab
  }
});

// ---------- boot ----------

installShellHandlers();
decorateShell();
installSearch();
installPalette();
installDock();
installSidebar();
if (await tryConnect()) { await route(); refreshDockModel(); }
poll();
schedulePoll(POLL_MIN_MS);

// H1: the helpers sidebar.js and room.js import back (the cycle is safe: they
// only declare at top level). After the boot marker, so tests/web never sees it.
// Phase 10 10.1: api/el/iconButton now come from js/; re-exported so the
// modules that import them from here keep working unchanged.
export { api, buildFilterBar, buildTabBar, casteSelect, chatTitle, confirmTemplateSuggestion, copyText, el, flash, fmtTs, header, iconButton, offersRemember, previewTemplateSuggestion, relativeTime, renderAssistantTab, renderChannelsTab, renderMarkdown, renderProjectList, renderReposTab, renderSuggestionGrant, renderWebhooksTab, renderWorkerModel, renderWorkersTab, repoOptions, route, setupMissingLabels, setupStatus, shortTaskId, shortTitle, stateChip };
