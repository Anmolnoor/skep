/* skep — the face (v5 Stage F). No build step: ES modules + fetch + EventSource. */

const TOKEN_KEY = "skep-token";

// ---------- tiny DOM + API helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function iconButton(label, icon, attrs = {}) {
  return el("button", {
    type: "button",
    title: label,
    "aria-label": label,
    ...attrs,
  }, el("span", { class: "button-icon", "aria-hidden": "true" }, icon));
}

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

function setToken(value) {
  localStorage.setItem(TOKEN_KEY, value);
  // The cookie is what authenticates EventSource — it cannot set headers.
  document.cookie = `skep_token=${value}; path=/; SameSite=Strict`;
}

async function api(method, path, body) {
  const options = { method, headers: { "X-Skep-Token": token() } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401) { showLogin(); throw new Error("not authenticated"); }
  const text = await response.text();
  let data = text;
  try { data = JSON.parse(text); } catch { /* plain text (diff) stays text */ }
  if (!response.ok) throw new Error((data && data.detail) || `${response.status}`);
  return data;
}

// POST + read the response as an SSE stream (fetch sets headers, so no cookie
// dance here — EventSource stays only where the server pushes unprompted).
async function streamSse(path, body, handlers) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "X-Skep-Token": token(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) { showLogin(); throw new Error("not authenticated"); }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* keep the status */ }
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message"; let data = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data = JSON.parse(line.slice(6));
      }
      if (data !== null && handlers[event]) handlers[event](data);
    }
  }
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

const SETUP_MISSING_LABELS = {
  llm: "LLM connection",
  default_model: "default model",
  workspace_project: "workspace/project",
  policy: "execution policy",
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

function modelLabel(model) {
  return (model || "No model").trim() || "No model";
}

function contextLoadPercent(context) {
  // v56-F3: server truth — chat detail carries what the NEXT turn will send
  // vs the model window. Pre-load (no detail yet) shows a resting sliver.
  if (!context || typeof context.percent !== "number") return 8;
  return Math.min(100, Math.max(4, Math.round(context.percent)));
}

function setShellActiveRoute(hash) {
  for (const link of document.querySelectorAll(".sidebar a[data-ws]")) {
    const ws = link.dataset.ws;
    const nav = link.dataset.nav || "";
    let active = hash.startsWith(`#/${ws}`);
    if (ws === "chat") {
      active = nav === "new-chat" && hash === "#/chat";
    }
    link.classList.toggle("active", active);
  }
}

// v44-F3: sources whose group the operator opened; survives re-renders.
// ponytail: in-memory; localStorage if reload-reset annoys
const openChatGroups = new Set();

// v76-F8: pinned chats are a UI preference (localStorage, I11), not a
// server fact — the daily-driver conversation stays on top. The search
// query survives re-renders the same way the open groups do.
const pinnedChats = new Set(JSON.parse(localStorage.getItem("skep-pinned-chats") || "[]"));
let chatSearchQuery = "";

function savePinnedChats() {
  localStorage.setItem("skep-pinned-chats", JSON.stringify([...pinnedChats]));
}

function renderSidebarChats(list, chats, activeChatId, fallbackModel) {
  if (!list) return;
  list.replaceChildren();
  const applyChatSearch = () => {
    const q = chatSearchQuery.trim().toLowerCase();
    for (const node of list.querySelectorAll(".sidebar-chat-item")) {
      node.classList.toggle("search-hidden",
        Boolean(q) && !node.textContent.toLowerCase().includes(q));
    }
  };
  const searchInput = el("input", {
    class: "chat-sidebar-search", type: "search",
    placeholder: "Search chats…", value: chatSearchQuery,
    "aria-label": "search chats",
  });
  searchInput.addEventListener("input", () => {
    chatSearchQuery = searchInput.value;
    applyChatSearch();
  });
  list.append(searchInput);
  const item = (chat) => {
    const model = modelLabel(chat.model || fallbackModel);
    const isPinned = pinnedChats.has(chat.chat_id);
    const pin = iconButton(isPinned ? "unpin chat" : "pin chat", isPinned ? "●" : "○", {
      class: `chat-pin icon-button ghost${isPinned ? " pinned" : ""}`,
      onclick: (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (isPinned) pinnedChats.delete(chat.chat_id);
        else pinnedChats.add(chat.chat_id);
        savePinnedChats();
        renderSidebarChats(list, chats, activeChatId, fallbackModel);
      },
    });
    return el("a", {
      class: `sidebar-chat-item${chat.chat_id === activeChatId ? " active" : ""}`,
      href: `#/chat/${chat.chat_id}`,
      title: `${chatTitle(chat)} · ${model}`,
      "aria-label": `open chat: ${chatTitle(chat)}`,
    },
      el("span", { class: "sidebar-chat-label" }, `${chatTitle(chat)} · ${model}`),
      pin);
  };
  // Pinned first — moved, not duplicated, whatever their source group.
  const pinned = chats.filter(chat => pinnedChats.has(chat.chat_id));
  if (pinned.length) {
    list.append(el("div", { class: "sidebar-chat-pinned" },
      el("div", { class: "sidebar-chat-pinned-label" }, "pinned"),
      pinned.map(item)));
  }
  const groups = new Map(); // insertion order = recency (list arrives updated_at DESC)
  for (const chat of chats) {
    if (pinnedChats.has(chat.chat_id)) continue;
    const source = chat.source || "web";
    if (source === "web") { list.append(item(chat)); continue; }
    if (!groups.has(source)) groups.set(source, []);
    groups.get(source).push(chat);
  }
  for (const [source, group] of groups) {
    const open = openChatGroups.has(source) || group.some((c) => c.chat_id === activeChatId);
    list.append(el("details", {
      class: "sidebar-chat-group",
      open: open ? "" : null,
      ontoggle: (event) => {
        if (event.target.open) openChatGroups.add(source);
        else openChatGroups.delete(source);
      },
    },
      el("summary", {
        class: "sidebar-chat-group-toggle",
        title: `toggle ${source} chats`,
        "aria-label": `toggle ${source} chats`,
      },
        source,
        el("span", { class: "badge" }, String(group.length)),
        el("span", { class: "chat-thinking-chevron button-icon", "aria-hidden": "true" }, "⌄")),
      group.map(item)));
  }
  applyChatSearch(); // a re-render keeps the live filter applied
}

function installShellHandlers() {
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
  runs: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  approvals: '<circle cx="12" cy="12" r="9"/><path d="M8.4 12l2.3 2.3 4.9-4.9"/>',
  assign: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
  templates:
    '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>' +
    '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  projects: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  schedules: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
  notes: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
  memory: '<path d="M7 4h10a1 1 0 0 1 1 1v16l-6-4-6 4V5a1 1 0 0 1 1-1z"/>',
  policies: '<path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/>',
  setup: '<path d="M6 21V4M6 4h11l-2 4 2 4H6"/>',
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
      queenTitleBase = `window ${llm.num_ctx} (${llm.num_ctx_source}) · tools ${llm.tool_delivery}`;
      tile.title = queenTitleBase;
    }
  } catch { /* leave the dock model label blank */ }
}
function updateShellChrome(hash) {
  const hideDock = hash.startsWith("#/chat") || hash.startsWith("#/setup");
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

function showLogin() {
  document.getElementById("shell").classList.add("hidden");
  document.getElementById("login").classList.remove("hidden");
}

async function tryConnect() {
  const response = await fetch("/api/status", { headers: { "X-Skep-Token": token() } });
  if (!response.ok) { showLogin(); return false; }
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

const routes = [
  [/^#\/home$/, viewHome],
  [/^#\/setup$/, viewSetup],
  [/^#\/chat$/, viewChat],
  [/^#\/chat\/([^/]+)$/, viewChat],
  [/^#\/notes$/, viewNotesTasks],
  [/^#\/memory$/, viewMemory],
  // v75-F7 (C9): tolerate a query suffix — template cards link to
  // #/assign?template=<name>; the param parsing itself ships in v76-F4.
  [/^#\/assign(?:\?.*)?$/, viewAssign],
  [/^#\/runs$/, viewRuns],
  [/^#\/runs\/([^/]+)$/, viewRunDetail],
  [/^#\/approvals$/, viewApprovals],
  [/^#\/templates$/, viewTemplates],
  [/^#\/projects$/, viewProjects],
  // v76-F3: projects stop being write-only — a detail page composes reads.
  [/^#\/projects\/([^/]+)$/, viewProjectDetail],
  [/^#\/schedules$/, viewSchedules],
  [/^#\/policies$/, viewPolicies],
  [/^#\/settings$/, viewSettings],
];

async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || "#/home";
  setShellActiveRoute(hash);
  updateShellChrome(hash);
  const main = document.getElementById("main");
  main.className = "";
  main.replaceChildren();
  let setup = null;
  try { setup = await setupStatus(); }
  catch { return; }
  if (!setup.complete && !setupRouteAllowed(hash)) {
    location.hash = "#/setup";
    return;
  }
  for (const [pattern, view] of routes) {
    const match = hash.match(pattern);
    if (match) {
      const args = view === viewSetup ? [setup] : match.slice(1);
      await view(main, ...args).catch(e => flash("bad", e.message));
      return;
    }
  }
  location.hash = "#/home";
}

window.addEventListener("hashchange", route);

// ---------- shared fragments ----------

function header(main, title, subtitle) {
  // The page title lives in the persistent top bar now; the subtitle stays in
  // the content column (matching the design's "Home / hive at a glance" split).
  const titleEl = document.getElementById("topbar-title");
  if (titleEl) titleEl.textContent = title;
  document.title = `${title} · skep`;
  if (subtitle) main.append(el("p", { class: "sub" }, subtitle));
}

async function repoOptions() {
  const { repos } = await api("GET", "/api/repos");
  return repos.map(r => r.name);
}

async function setupStatus() {
  return api("GET", "/api/setup/status");
}

function setupRouteAllowed(hash) {
  return hash === "#/setup" || hash === "#/settings";
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
  const content = result === null || result === undefined
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
    el("pre", { class: "agent-thread-content mono" }, content));
  if (!content) node.querySelector(".agent-thread-content").classList.add("hidden");
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
    source.onmessage = (message) => feed(JSON.parse(message.data));
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

async function viewSetup(main, setup) {
  header(main, "Setup", "Connect a model, bind a project, and choose the first-run policy.");
  const missing = setupMissingLabels(setup);
  main.append(el("div", { class: "card" },
    el("h3", {}, setup.complete ? "Ready" : "Missing"),
    setup.missing.length
      ? el("p", { class: "note" }, missing.join(", "))
      : el("p", { class: "note" }, "Setup is complete."),
    setup.marked_complete && setup.completed_at
      ? el("p", { class: "note" }, `completed ${fmtTs(setup.completed_at)}`)
      : null));

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
    type: "password",
    autocomplete: "off",
    placeholder: llm.api_key_set ? "key saved — paste to replace" : "API key",
  });
  const modelSelect = el("select", {},
    llm.default_model
      ? el("option", { value: llm.default_model }, llm.default_model)
      : el("option", { value: "" }, "(test connection first)"));
  modelSelect.disabled = !llm.default_model;
  const modelStatus = el("p", { class: "note" },
    llm.default_model ? `default model: ${llm.default_model}` : "No default model saved.");
  const setModelOptions = (models) => {
    const known = [...new Set([llm.default_model, ...models].filter(Boolean))];
    modelSelect.replaceChildren(...(known.length
      ? known.map(model => el("option", { value: model }, model))
      : [el("option", { value: "" }, "(no models found)")]));
    modelSelect.disabled = known.length === 0;
    if (llm.default_model) modelSelect.value = llm.default_model;
    modelStatus.textContent = `${models.length} model(s) available`;
  };
  const loadModels = async () => {
    try {
      const { models } = await api("GET", "/api/llm/models");
      setModelOptions(models);
    } catch (e) {
      modelStatus.textContent = `cannot list models: ${e.message}`;
    }
  };
  if (llm.configured) loadModels();
  const testConnection = el("button", { class: "primary" }, "Test connection");
  testConnection.addEventListener("click", async () => {
    testConnection.disabled = true;
    try {
      const body = { base_url: baseUrl.value.trim(), protocol: protocol.value };
      if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
      const verdict = await api("POST", "/api/llm/test", body);
      if (!verdict.ok) throw new Error(verdict.detail || "connection failed");
      await api("PUT", "/api/llm/config", body);
      const { models } = await api("GET", "/api/llm/models");
      setModelOptions(models);
      flash("ok", `connected — ${verdict.models} model(s) available`);
    } catch (e) { flash("bad", e.message); }
    finally { testConnection.disabled = false; }
  });
  const saveModel = el("button", { class: "primary" }, "Save default model");
  saveModel.addEventListener("click", async () => {
    if (!modelSelect.value) return;
    try {
      await api("PUT", "/api/llm/config", { default_model: modelSelect.value });
      flash("ok", `default model: ${modelSelect.value}`);
      route();
    } catch (e) { flash("bad", e.message); }
  });
  main.append(el("div", { class: "card" },
    el("h3", {}, "LLM"),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "preset"), buildPresetPicker(protocol, baseUrl)),
      el("div", { class: "field" }, el("label", {}, "protocol"), protocol),
      el("div", { class: "field grow" }, el("label", {}, "base URL"), baseUrl),
      el("div", { class: "field grow" }, el("label", {}, "API key"), apiKey),
      testConnection),
    el("div", { class: "row" },
      el("div", { class: "field grow" }, el("label", {}, "default model"), modelSelect),
      saveModel),
    modelStatus));

  const applyWorkspace = el("input", { type: "checkbox" });
  applyWorkspace.checked = !setup.workspace_project.ready;
  const workspace = setup.default_workspace || {};
  const saveWorkspace = el("button", { class: "primary" }, "Apply workspace");
  saveWorkspace.addEventListener("click", async () => {
    saveWorkspace.disabled = true;
    try {
      const result = await api("POST", "/api/setup/default-workspace", { apply: applyWorkspace.checked });
      flash("ok", result.applied ? "workspace applied" : "workspace skipped");
      route();
    } catch (e) { flash("bad", e.message); saveWorkspace.disabled = false; }
  });
  main.append(el("div", { class: "card" },
    el("h3", {}, "Workspace"),
    el("p", { class: "note" },
      `Default path: ${workspace.path || "~/workspace"}`),
    el("p", { class: "note" },
      "Predefined policy: trusted_local_dev, build phase, workspace execution."),
    el("div", { class: "row" },
      el("label", { class: "field" }, applyWorkspace, " Use default workspace"),
      saveWorkspace)));

  const finish = el("button", { class: "primary" }, "Finish setup");
  finish.addEventListener("click", async () => {
    finish.disabled = true;
    try {
      if (applyWorkspace.checked && !setup.workspace_project.ready) {
        await api("POST", "/api/setup/default-workspace", { apply: true });
      }
      const done = await api("POST", "/api/setup/complete");
      if (done.complete) {
        flash("ok", "setup complete");
        location.hash = "#/runs";
      }
    } catch (e) { flash("bad", e.message); finish.disabled = false; }
  });
  main.append(el("div", { class: "actions" }, finish));
}

// ---------- Notes & Tasks (v76-F7: markdown notes + due-grouped tasks) ----------

// v76-F7: the EXPLICIT run:<task-id> token links to the run page.
// Deliberately not bare-hex autolinking — prose full of hex-looking words
// would false-positive, and a link to a non-run is a lie (I8).
function linkifyRunTokens(text) {
  const nodes = [];
  let last = 0;
  for (const match of String(text || "").matchAll(/run:([0-9a-f]{6,})/gi)) {
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    nodes.push(el("a", { class: "mono run-token", href: `#/runs/${match[1]}` }, match[0]));
    last = match.index + match[0].length;
  }
  if (last < String(text || "").length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

function linkifyRunTokensInPlace(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) {
    if (!/run:[0-9a-f]{6,}/i.test(node.textContent)) continue;
    node.replaceWith(el("span", {}, linkifyRunTokens(node.textContent)));
  }
}

function noteTags(content) {
  return [...new Set([...(content || "").matchAll(/\B#([\w-]+)/g)].map(m => m[1]))];
}

async function viewNotesTasks(main) {
  header(main, "Notes & Tasks", "Capture local context and keep lightweight todos.");
  const [{ notes }, { tasks }] = await Promise.all([
    api("GET", "/api/notes"),
    api("GET", "/api/tasks"),
  ]);

  const noteText = el("textarea", { placeholder: "New note" });
  const addNote = el("button", { class: "primary" }, "Add note");
  addNote.addEventListener("click", async () => {
    if (!noteText.value.trim()) return;
    addNote.disabled = true;
    try { await api("POST", "/api/notes", { content: noteText.value.trim() }); route(); }
    catch (e) { flash("bad", e.message); addNote.disabled = false; }
  });

  // v76-F7: notes read as markdown (renderMarkdown — rung-2 reuse); edit
  // swaps to the raw textarea with the same PATCH/DELETE verbs.
  const notesList = el("div", { class: "stack" });
  const noteFilter = el("input", {
    type: "search", placeholder: "Filter notes by text or #tag",
  });
  noteFilter.addEventListener("input", () => {
    const q = noteFilter.value.trim().toLowerCase();
    for (const item of notesList.children) {
      item.classList.toggle("search-hidden",
        Boolean(q) && !item.textContent.toLowerCase().includes(q));
    }
  });
  for (const note of notes) {
    const body = el("div", { class: "note-md" }, renderMarkdown(note.content));
    linkifyRunTokensInPlace(body);
    const tags = noteTags(note.content);
    const content = el("textarea", { class: "hidden" }, note.content);
    const save = el("button", { class: "ghost hidden" }, "Save");
    const remove = el("button", { class: "danger hidden" }, "Delete");
    const edit = el("button", { class: "ghost" }, "Edit");
    edit.addEventListener("click", () => {
      body.classList.add("hidden");
      edit.classList.add("hidden");
      for (const node of [content, save, remove]) node.classList.remove("hidden");
    });
    save.addEventListener("click", async () => {
      try { await api("PATCH", `/api/notes/${note.note_id}`, { content: content.value.trim() }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    remove.addEventListener("click", async () => {
      try { await api("DELETE", `/api/notes/${note.note_id}`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    notesList.append(el("div", { class: "item-card searchable" },
      body,
      content,
      tags.length
        ? el("div", { class: "note-tags" },
          tags.map(tag => el("span", { class: "note-tag" }, `#${tag}`)))
        : null,
      el("div", { class: "row" },
        el("span", { class: "note grow" }, fmtTs(note.updated_at)),
        edit, save, remove)));
  }
  if (!notes.length) notesList.append(el("p", { class: "empty-state" }, "No notes yet."));

  const taskTitle = el("input", { placeholder: "New task" });
  const taskDue = el("input", { type: "datetime-local" });
  const addTask = el("button", { class: "primary" }, "Add task");
  addTask.addEventListener("click", async () => {
    if (!taskTitle.value.trim()) return;
    addTask.disabled = true;
    const due = taskDue.value ? `${taskDue.value}:00Z` : null;
    try { await api("POST", "/api/tasks", { title: taskTitle.value.trim(), due_at: due }); route(); }
    catch (e) { flash("bad", e.message); addTask.disabled = false; }
  });

  // v76-F7: tasks group by urgency; the checkbox is the daily verb (the
  // same PATCH the old Complete button used), the full editor stays one
  // toggle away — the raw record remains editable (I8).
  const dueTone = (task) => {
    if (!task.due_at) return null;
    const ms = new Date(task.due_at).getTime() - Date.now();
    if (ms < 0) return "overdue";
    if (ms < 24 * 60 * 60 * 1000) return "today";
    return "upcoming";
  };
  // v101-F8: the tone lives beside the vocabulary that produces it, not spelled
  // out longhand per variant in the stylesheet.
  const DUE_TONE = { overdue: "tone-bad", today: "tone-warn", upcoming: "tone-muted" };
  const TASK_GROUPS = [
    { label: "Overdue", test: t => t.status !== "done" && dueTone(t) === "overdue" },
    { label: "Due today", test: t => t.status !== "done" && dueTone(t) === "today" },
    { label: "Upcoming", test: t => t.status !== "done" && dueTone(t) === "upcoming" },
    { label: "No due date", test: t => t.status !== "done" && !t.due_at },
    { label: "Done", test: t => t.status === "done", collapsed: true },
  ];
  const taskRow = (task) => {
    const checkbox = el("input", { type: "checkbox", "aria-label": "toggle done" });
    checkbox.checked = task.status === "done";
    checkbox.addEventListener("change", async () => {
      try {
        await api("PATCH", `/api/tasks/${task.task_id}`,
          { status: checkbox.checked ? "done" : "todo" });
        route();
      } catch (e) { flash("bad", e.message); }
    });
    const tone = dueTone(task);
    const duePill = task.due_at
      ? el("span", { class: `chip ${DUE_TONE[tone] || "tone-muted"}`, title: fmtTs(task.due_at) },
        tone === "overdue" ? "overdue" : relativeTime(task.due_at))
      : null;
    const title = el("input", { value: task.title });
    const due = el("input", { value: task.due_at || "", placeholder: "YYYY-MM-DDTHH:MM:SSZ" });
    const status = el("select", {},
      el("option", { value: "todo" }, "todo"),
      el("option", { value: "done" }, "done"));
    status.value = task.status;
    const save = el("button", { class: "ghost" }, "Save");
    const remove = el("button", { class: "danger" }, "Delete");
    save.addEventListener("click", async () => {
      try {
        await api("PATCH", `/api/tasks/${task.task_id}`, {
          title: title.value.trim(),
          status: status.value,
          due_at: due.value.trim() || null,
        });
        route();
      } catch (e) { flash("bad", e.message); }
    });
    remove.addEventListener("click", async () => {
      try { await api("DELETE", `/api/tasks/${task.task_id}`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    const editor = el("div", { class: "task-editor hidden" },
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "task"), title),
        el("div", { class: "field" }, el("label", {}, "status"), status)),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "due"), due),
        save, remove));
    return el("div", { class: "item-card searchable" },
      el("div", { class: "task-row" },
        checkbox,
        el("span", { class: `task-title${task.status === "done" ? " done" : ""}` },
          linkifyRunTokens(task.title)),
        duePill,
        iconButton("edit task", "✎", {
          class: "ghost icon-button",
          onclick: () => editor.classList.toggle("hidden"),
        })),
      editor);
  };
  const tasksList = el("div", { class: "stack" });
  for (const group of TASK_GROUPS) {
    const items = tasks.filter(group.test);
    if (!items.length) continue;
    tasksList.append(group.collapsed
      ? el("details", { class: "task-group" },
        el("summary", {}, `${group.label} (${items.length})`),
        items.map(taskRow))
      : el("section", { class: "task-group" },
        el("h4", {}, `${group.label} (${items.length})`),
        items.map(taskRow)));
  }
  if (!tasks.length) tasksList.append(el("p", { class: "empty-state" }, "No tasks yet."));

  main.append(el("div", { class: "nt-grid" },
    el("section", {}, el("h3", {}, "Notes"),
      el("p", { class: "note" },
        "Markdown renders; #tags become pills; run:<task-id> links the run."),
      el("div", { class: "composer inline" }, noteText, addNote),
      noteFilter,
      notesList),
    el("section", {}, el("h3", {}, "Tasks"),
      el("div", { class: "composer stacky" },
        el("div", { class: "row" },
          el("div", { class: "field grow" }, el("label", {}, "task"), taskTitle),
          el("div", { class: "field" }, el("label", {}, "due"), taskDue),
          addTask)),
      tasksList)));
}

// ---------- Curated memory (v13; v76-F8 class chips + counts) ----------

// v76-F8: the 7 REAL memory classes (MEMORY_CLASSES, memory.py) — a class
// added server-side must be added here consciously (a lockstep test compares
// this map to the store's set); unknown falls back muted. The build spec's
// user/environment/convention list had no data source and is not used (I8).
const MEMORY_CLASS_COLORS = {
  durable_preference: "var(--accent)",
  project_fact: "var(--info)",
  todo: "var(--warn)",
  not_to_do: "var(--bad)",
  reminder: "var(--accent-2)",
  policy_hint: "var(--ok)",
  observation: "var(--muted)",
};

function memoryClassChip(cls) {
  const color = MEMORY_CLASS_COLORS[cls] || "var(--muted)";
  return el("span", {
    // v101-F8: the shared .chip primitive; the tone is this chip's own colour
    // rather than one of the named tones, so it still sets --chip-color itself.
    class: "chip upper",
    style: `--chip-color: ${color}`,
  }, cls);
}

async function viewMemory(main) {
  header(main, "Memory", "Curated durable memory — nothing becomes durable without approval.");
  const [{ proposals }, { items }] = await Promise.all([
    api("GET", "/api/memory/proposals?state=pending_review"),
    api("GET", "/api/memory"),
  ]);

  const propList = el("div", { class: "stack" });
  for (const p of proposals) {
    const approve = el("button", { class: "primary" }, "Approve");
    const reject = el("button", { class: "danger" }, "Reject");
    const clarify = el("button", { class: "ghost" }, "Needs info");
    approve.addEventListener("click", async () => {
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/approve`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    reject.addEventListener("click", async () => {
      const reason = window.prompt("Why reject this proposal?");
      if (!reason) return;
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/reject`, { reason }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    clarify.addEventListener("click", async () => {
      const reason = window.prompt("What needs clarification?");
      if (!reason) return;
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/clarify`, { reason }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    const sources = (p.sources || []).map(s => `${s.kind}:${s.source_id}`).join(", ");
    propList.append(el("div", { class: "item-card" },
      el("div", { class: "row" },
        memoryClassChip(p.memory_class),
        el("span", { class: "note grow" }, sources ? `from ${sources}` : "manual"),
        // v76-F8: an old proposal means the operator is the bottleneck.
        el("span", { class: "note", title: fmtTs(p.created_at) },
          `proposed ${relativeTime(p.created_at)}`)),
      el("p", {}, p.content),
      el("div", { class: "row" }, approve, clarify, reject)));
  }
  if (!proposals.length) propList.append(el("p", { class: "empty-state" }, "No proposals to review."));

  const memList = el("div", { class: "stack" });
  function renderItems(list) {
    memList.replaceChildren();
    for (const item of list) {
      const forget = el("button", { class: "danger" }, "Forget");
      forget.addEventListener("click", async () => {
        try { await api("DELETE", `/api/memory/${item.memory_id}`); route(); }
        catch (e) { flash("bad", e.message); }
      });
      memList.append(el("div", { class: "item-card" },
        el("div", { class: "row" },
          memoryClassChip(item.memory_class),
          el("span", { class: "note grow" }, item.project_id || "global"),
          forget),
        el("p", {}, item.content)));
    }
    if (!list.length) memList.append(el("p", { class: "empty-state" }, "No durable memory yet."));
  }
  renderItems(items);

  const search = el("input", { placeholder: "Search durable memory" });
  search.addEventListener("input", async () => {
    const q = search.value.trim();
    if (!q) { renderItems(items); return; }
    try {
      const res = await api("GET", `/api/memory/search?q=${encodeURIComponent(q)}`);
      renderItems(res.items);
    } catch (e) { flash("bad", e.message); }
  });

  // v76-F8: how much the Queen knows — count + summed content size.
  const memKb = (items.reduce((n, item) => n + (item.content || "").length, 0) / 1024)
    .toFixed(1);
  main.append(el("div", { class: "nt-grid" },
    el("section", {}, el("h3", {}, "Proposals"), propList),
    el("section", {},
      el("h3", {}, "Durable memory"),
      el("p", { class: "note" }, `${items.length} items · ${memKb} KB`),
      el("div", { class: "composer inline" }, search),
      memList)));
}

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
// buttons use. The Queen's model never sees or executes these. Keep COMMANDS
// beside the executor in runSlashCommand — /help renders from this table, so
// the list and the parser cannot drift.

const LAST_REPO_KEY = "skep-last-repo";
// v96-F2: the active chat's bound project repo (server truth from
// GET /api/chats/{id}.project) — deck commands default to it before the
// localStorage last-repo guess.
let chatBoundRepo = null;

const COMMANDS = {
  help: { usage: "/help", desc: "list the command deck" },
  policy: { usage: "/policy [repo]", desc: "effective policy for a repo (default: last used)" },
  repos: { usage: "/repos", desc: "registered repos" },
  skills: { usage: "/skills", desc: "learned-skill candidates and admitted skills" },
  runs: { usage: "/runs [n]", desc: "recent runs" },
  approvals: { usage: "/approvals", desc: "the pending approval queue" },
  state: { usage: "/state <repo>", desc: "a repo's git state: branches, HEAD, recent commits" },
  setup: {
    usage: "/setup <repo> [--pack X] [--phase Y]",
    desc: "bind a repo to a trusted project (confirm card)",
  },
  phase: {
    usage: "/phase <project-id> <bootstrap|build|maintain>",
    desc: "move a project's trust phase (confirm card)",
  },
  land: { usage: "/land <task-id> [branch]", desc: "land a completed run's patch (confirm card)" },
  approve: { usage: "/approve <review-id|card-id> [branch]", desc: "approve a pending review (confirm card), or a pending card by its id" },
  deny: { usage: "/deny <review-id|card-id>", desc: "deny a pending review (confirm card), or a pending card by its id" },
  workon: {
    usage: "/workon <path> [--pack X] [--phase Y]",
    desc: "make a local directory a first-class workspace — git baseline + trusted project (confirm card)",
  },
  schedule: {
    usage: "/schedule <name> <repo> <every> <instructions…>",
    desc: "create a recurring schedule the ticker dispatches — every takes 30m/6h/1d (confirm card)",
  },
  personality: {
    usage: "/personality <concise|technical|friendly|custom:text|default>",
    desc: "set this chat's reply style — style only, never policy (confirm card)",
  },
  persona: {
    usage: "/persona <text…|default>",
    desc: "set the profile-level identity every chat starts with — identity only, never policy (confirm card)",
  },
  btw: {
    usage: "/btw <question…>",
    desc: "ask a side question WITHOUT touching running work — read-only turn: no cards, no mutations, runs beside a pending card",
  },
  steer: {
    usage: "/steer <task-id> <text…>",
    desc: "send a steering note into a RUNNING react run — input, never authority: resolves no card, approval, or gate",
  },
  resume: {
    usage: "/resume <task-id>",
    desc: "continue a crashed/timed-out run from its checkpoint — model-free (confirm card)",
  },
  browser: {
    usage: "/browser",
    desc: "register the built-in Playwright browser under the browse scope (confirm card)",
  },
  sync: {
    usage: "/sync",
    desc: "run the operator-pinned fleet sync command — pin it with `skep sync --set` (confirm card)",
  },
};

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

async function viewChat(main, chatId) {
  let activeChatId = chatId || null;
  const [llm, chatList] = await Promise.all([
    api("GET", "/api/llm/config"),
    api("GET", "/api/chats"),
  ]);
  let chats = chatList.chats;
  const chatSidebar = el("nav", { class: "chat-sidebar", "aria-label": "Chats" });
  renderSidebarChats(chatSidebar, chats, activeChatId, llm.default_model);

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
        + ` (window: ${context.num_ctx_source})`),
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
        + " (local count; account meter: ollama.com/settings)")] : []));
  };
  // v74-F6: refresh the local usage tally when the popover opens — ollama.com
  // exposes no account usage API, so skep counts its own requests.
  contextMeter.addEventListener("mouseenter", async () => {
    try { llmUsage = await api("GET", "/api/llm/usage"); updateContextMeter(); }
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
  // v88-F2: collapsing the chat list is a UI preference, so it lives in
  // localStorage like the v76-F8 pins — operator-owned local state, never
  // server state (I11).
  let sidebarHidden = localStorage.getItem("skep-chat-sidebar") === "hidden";
  const toggleSidebar = iconButton("hide chat list", "☰", {
    class: "chat-toolbar-action icon-button ghost chat-sidebar-toggle",
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
    renderSidebarChats(chatSidebar, chats, null, llm.default_model);
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
  const updateSuggest = () => {
    const value = input.value;
    if (!value.startsWith("/") || /[\s]/.test(value.slice(1))) {
      suggest.hidden = true;
      return;
    }
    const needle = value.slice(1).toLowerCase();
    const matches = Object.entries(COMMANDS).filter(([name]) => name.startsWith(needle));
    suggest.replaceChildren(...matches.map(([name, command]) =>
      el("button", {
        type: "button",
        class: "command-suggest-item",
        onclick: () => { input.value = `/${name} `; suggest.hidden = true; input.focus(); },
      },
        el("span", { class: "mono" }, command.usage),
        el("span", { class: "command-suggest-desc" }, command.desc))));
    suggest.hidden = matches.length === 0;
  };

  const composer = el("div", { class: "composer-shell" },
    suggest,
    queuedNote,
    el("div", { class: "composer" },
      boundProject ? addContext : null,
      input,
      el("div", { class: "composer-meta" },
        attach,
        attachInput,
        mic,
        voiceToggle,
        modelSelect,
        send)),
    el("div", { class: "composer-status-row" },
      projectSelect,
      branchPill.pill,
      policyPill.pill,
      enginePill.pill,
      pushBtn,
      prBtn,
      contextMeter));
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
  const layout = el("div", { class: `chat-layout${sidebarHidden ? " sidebar-hidden" : ""}` },
    chatSidebar,
    el("div", { class: "chat-content" },
      el("div", { class: "chat-toolbar" }, toggleSidebar, newChat, deleteChat),
      pane));
  const applySidebarState = () => {
    layout.classList.toggle("sidebar-hidden", sidebarHidden);
    const label = sidebarHidden ? "show chat list" : "hide chat list";
    toggleSidebar.title = label;
    toggleSidebar.setAttribute("aria-label", label);
    toggleSidebar.setAttribute("aria-expanded", String(!sidebarHidden));
  };
  toggleSidebar.addEventListener("click", () => {
    sidebarHidden = !sidebarHidden;
    localStorage.setItem("skep-chat-sidebar", sidebarHidden ? "hidden" : "shown");
    applySidebarState();
  });
  applySidebarState();
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
  const toolLine = (tool, raw = null) => {
    // v40-F2 (v35): every supervisor tool gets a deterministic human summary
    // read off the tool RESULT json — never model text.
    const result = unwrapToolResult(raw);
    if (result && tool === "dispatch_run" && result.task_id) {
      const where = result.repo ? ` on ${result.repo}` : "";
      return `dispatched worker ${shortTaskId(result.task_id)}${where}`;
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
  };

  // v54-F3: labeled key-value rows instead of raw JSON — a human can scan
  // "repo: skep" where {"repo":"skep",...} was syntax noise. Nested values
  // (batch_dispatch.tasks) stay JSON, but under a labeled key.
  const renderArgs = (args) => {
    if (!args || typeof args !== "object" || !Object.keys(args).length) return null;
    return el("div", { class: "card-args" },
      Object.entries(args).map(([key, value]) => el("div", { class: "arg-row" },
        el("span", { class: "arg-key mono" }, `${key}:`),
        el("span", { class: "arg-value" },
          typeof value === "string" ? value : JSON.stringify(value, null, 2)))));
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
      el("p", { class: "card-headline mono" }, card.headline || d.tool),
      card.purpose ? el("p", { class: "card-purpose" }, card.purpose) : null,
      card.risk
        ? el("p", { class: "card-risk" },
            el("span", { class: "card-risk-label" }, "Needs your attention: "), card.risk)
        : null,
      details.length
        ? el("details", { class: "card-details" },
            el("summary", {}, "details"), details)
        : null,
    ];
  };

  // A proposed mutation: the model never holds the trigger — you do.
  const actionCard = (d) => {
    const approve = el("button", { class: "primary" }, "Approve");
    const refuse = el("button", { class: "danger" }, "Deny");
    // v81-F13: the id on the DOM lets reconcilers see which cards are drawn.
    const card = el("div", { class: "confirm-card", "data-action-id": d.action_id },
      ...cardBody(d),
      el("div", { class: "actions" }, approve, refuse));
    const verdict = (verb) => async () => {
      approve.disabled = refuse.disabled = true; // double-click guard
      try { await runStream(`/api/chats/${activeChatId}/actions/${d.action_id}/${verb}`); }
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

  // v90-F3: a receipt — an action that ran WITHOUT asking because a grant the
  // operator already gave covers it. Same headline and risk as the approval
  // card would have shown, and deliberately no buttons: it is a record, not a
  // decision. Silence here used to be indistinguishable from nothing happening.
  const receiptCard = (d) => {
    const card = d.card || {};
    const covered = d.decision && (d.decision.detail || d.decision.reason);
    log.append(el("div", { class: "confirm-card receipt-card" },
      el("p", { class: "card-kicker" }, "Ran without asking — you approved this already"),
      el("p", { class: "card-headline mono" }, card.headline || d.tool),
      card.risk ? el("p", { class: "card-risk" },
        el("span", { class: "card-risk-label" }, "Risk: "), card.risk) : null,
      covered ? el("p", { class: "card-purpose" }, `covered by ${covered}`) : null,
      // v106-F11 (v90-F3): which tier of grant, given when.
      d.grant ? el("p", { class: "note" },
        `${d.grant.tier} grant · given ${d.grant.granted_at}`) : null));
    scrollBottom();
  };

  // v54-F2: a resolved card hides its buttons (CSS on .resolved) and shows
  // the verdict instead — done, not just dimmed.
  const resolveCardUI = (card, label, verb) => {
    card.classList.add("resolved");
    card.append(el("p", { class: `verdict ${verb === "confirm" ? "approved" : "denied"}` }, label));
  };

  // ---------- the command deck executor (v25-F1) ----------

  const anyCardPending = () =>
    log.querySelector(".confirm-card:not(.resolved):not(.gate-card)") !== null;

  const commandHelp = (intro) => {
    log.append(el("div", { class: "command-help" },
      intro ? el("p", { class: "note" }, intro) : null,
      el("ul", { class: "command-help-list" },
        Object.values(COMMANDS).map(command => el("li", {},
          el("span", { class: "mono" }, command.usage), ` — ${command.desc}`)))));
    scrollBottom();
  };

  const commandResult = (title, result) =>
    renderToolEvent(log, title, result, { summary: title, scroll: scrollBottom });

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
        commandResult(verb === "confirm" ? `${d.tool}: result` : `${d.tool}: canceled`, result);
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
      el("div", { class: "actions" }, approve, refuse));
    const verdict = (verb) => async () => {
      approve.disabled = refuse.disabled = true; // double-click guard
      try {
        const result = await api(
          "POST", `/api/chats/${activeChatId}/commands/${d.action_id}/${verb}`);
        commandResult(verb === "confirm" ? "approve_review: result" : "deny_review: result", result);
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
      if (action.status !== "proposed") continue;
      if (log.querySelector(`.confirm-card[data-action-id="${action.action_id}"]`)) continue;
      // v25-F1: operator /commands resolve on the commands endpoints; only
      // model proposals resume the model after the verdict. v87-F2: gate
      // mirrors ride the commands endpoints too, without locking anything.
      if (action.source === "operator") commandCard(action);
      else if (action.source === "gate") gateCard(action);
      else actionCard(action);
    }
    setComposerLocked(actions.some(
      action => action.status === "proposed" && action.source !== "gate"));
  };

  const proposeCommand = async (tool, args, notes = []) => {
    await ensureChat();
    const action = await api("POST", `/api/chats/${activeChatId}/commands`, { tool, args });
    commandCard(action, notes);
    setComposerLocked(true);
  };

  // v51-F0: /approve and /deny accept the id the pending-card hint prints.
  // The command IS the decision — resolve directly, no second card.
  const resolvePendingCardById = async (id, verb) => {
    if (!activeChatId) return false;
    const detail = await api("GET", `/api/chats/${activeChatId}`);
    const card = (detail.actions || []).find(
      a => a.action_id === id && a.status === "proposed");
    if (!card) return false;
    if (card.source === "operator") {
      const result = await api("POST", `/api/chats/${activeChatId}/commands/${id}/${verb}`);
      commandResult(verb === "confirm" ? `${card.tool}: result` : `${card.tool}: canceled`, result);
    } else {
      await runStream(`/api/chats/${activeChatId}/actions/${id}/${verb}`);
    }
    setComposerLocked(anyCardPending());
    return true;
  };

  const runSlashCommand = async (content) => {
    renderChatMessage(log, "user", content, { label: "You", scroll: scrollBottom });
    const { name, args, flags } = parseSlashCommand(content);
    const spec = COMMANDS[name];
    try {
      if (!spec) { commandHelp(`unknown command: /${name}`); return; }
      if (name === "help") { commandHelp(); return; }
      if (name === "repos") { commandResult("/repos", await api("GET", "/api/repos")); return; }
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
      if (name === "phase") {
        const [projectId, phase] = args;
        if (!projectId || !phase) { commandHelp(`usage: ${spec.usage}`); return; }
        await proposeCommand("set_project_phase", { project_id: projectId, phase });
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
      if (name === "sync") {
        // v110-F2: the pin is terminal-set; the card only ever runs it
        // verbatim, and says exactly what that is (I8).
        const status = await api("GET", "/api/sync");
        if (!status.command) {
          commandHelp("no fleet sync command pinned — in the terminal: skep sync --set '<cmd>'");
          return;
        }
        const notes = [`runs: ${status.command}`];
        if (status.last) {
          notes.push(status.last.ok
            ? `last: ok at ${status.last.at}`
            : `last: failed (exit ${status.last.exit_code}) at ${status.last.at}`);
        }
        await proposeCommand("sync_fleet", {}, notes);
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
      el("span", { class: "streaming-indicator" }, "Working"));
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
      renderSidebarChats(chatSidebar, chats, activeChatId, llm.default_model); // titles may have changed
      // v103-F1: the turn is over — send whatever the operator typed during it.
      // After the refresh above, so a card raised by this turn has already
      // locked the composer and the steer waits for the verdict instead of
      // racing it.
      flushQueued();
    }
  };

  log.append(el("p", { class: "note" },
    activeChat
      ? `${chatTitle(activeChat)} · model: ${activeChat.model || llm.default_model || "(none)"}`
      : `Start a new chat. Model: ${llm.default_model || "(none — set a default in Settings)"}`));
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
      renderChatMessage(log, message.role, message.content, {
        label: message.role === "user" ? "You" : (activeChat?.model || llm.default_model || "skep"),
        createdAt: message.created_at,
        thinking: message.thinking,
        onResend: message.role === "user" ? fillInput : null,
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
    activeChat = await api("POST", "/api/chats", { model: modelSelect.value || null });
    activeChatId = activeChat.chat_id;
    chats = [activeChat, ...chats.filter(chat => chat.chat_id !== activeChatId)];
    modelSelect.value = activeChat.model || llm.default_model || "";
    modelSelect.disabled = true;
    modelSelect.title = "start a new chat to change model";
    history.replaceState(null, "", `#/chat/${activeChatId}`);
    setShellActiveRoute(location.hash);
    renderSidebarChats(chatSidebar, chats, activeChatId, llm.default_model);
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
    try { await runStream(`/api/chats/${activeChatId}/messages`, payload); }
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
      project ? el("span", {}, project) : null,
      autonomy !== "-" ? el("span", { class: "run-card-autonomy mono" }, autonomy) : null));
}

async function viewRuns(main) {
  header(main, "Runs", "Everything the hive has done, newest first.");
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
  const detail = await api("GET", `/api/runs/${taskId}`);
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
    if ((rv.commands || []).length) {
      kv("re-verify ran", el("span", { class: "mono" }, rv.commands.join(" && ")));
    }
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
  if (run.state === "pending_approval" || run.state === "completed") {
    actions.append(
      act(isBatch ? `Approve all ${batchCommands.length} commands` : "Approve", "primary", async () => {
        const result = await api("POST", `/api/approvals/${await ensureReview()}/approve`, {});
        if (result.resumed_as) location.hash = `#/runs/${result.resumed_as}`;
        else flash("ok", `patch applied on ${result.branch}`);
      }),
      act("Deny", "danger", async () => {
        await api("POST", `/api/approvals/${await ensureReview()}/deny`, { note: "denied from UI" });
        flash("ok", "denied");
      }));
    if (pending[0]?.action === "shell.run") {
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
    main.append(actions);
  }
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
    events.forEach(append);
  } else {
    const source = new EventSource(`/api/runs/${taskId}/events?stream=1`);
    source.onmessage = (message) => append(JSON.parse(message.data));
    source.addEventListener("done", () => { source.close(); route(); });
    cleanup = () => source.close();
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
  if ((detail.artifacts || []).some((a) => a.kind === "patch")) try {
    const diff = await api("GET", `/api/runs/${taskId}/diff`);
    const pre = el("pre", { class: "diff" });
    for (const line of String(diff).split("\n")) {
      const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del"
        : line.startsWith("@@") ? "hunk" : "";
      pre.append(el("div", { class: cls }, line || " "));
    }
    main.append(el("h2", {}, "diff"), pre);
  } catch { /* no patch artifact — nothing to show */ }
}

// ---------- Approvals ----------

async function viewApprovals(main) {
  header(main, "Approvals",
    "The gate queue: risky work waits here for a human verdict — nothing lands on its own.");
  const { approvals } = await api("GET", "/api/approvals");
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
        " ", stateChip(run.state || "?"), ` — ${approval.action}: ${approval.reason}`),
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
    if (approval.action === "shell.run") {
      actions.append(
        verdict(isBatch ? "Allow all & remember" : "Allow command", "ghost", "allow-command", {}),
        verdict("Skip", "ghost", "deny", { note: "skipped from UI" }),
      );
    }
    if (approval.action === "network.fetch" || approval.action === "network.read") {
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

// ---------- Templates & Skills (v75-F7: two tabs — authored vs learned) ----------

let activeTemplatesTab = "templates";

async function viewTemplates(main) {
  header(main, "Templates & Skills",
    "Saved recipes — hand-authored, or learned from confirmed runs and human-approved.");
  const tabs = buildTabBar([
    { key: "templates", label: "Templates", render: renderTemplatesTab },
    { key: "skills", label: "Skills", render: renderSkillsTab },
  ], { initial: activeTemplatesTab, onActivate: (key) => { activeTemplatesTab = key; } });
  main.append(tabs.bar, tabs.content);
}

async function renderTemplatesTab(panel) {
  const [{ templates }, repos, roster] = await Promise.all([
    api("GET", "/api/templates"),
    repoOptions(),
    api("GET", "/api/workers"),
  ]);

  if (templates.length) {
    panel.append(el("div", { class: "template-cards" }, templates.map(t => {
      const remove = el("button", { class: "danger ghost" }, "remove");
      remove.addEventListener("click", async () => {
        try { await api("DELETE", `/api/templates/${t.name}`); route(); }
        catch (e) { flash("bad", e.message); }
      });
      return el("div", { class: "template-card searchable" },
        el("div", { class: "template-card-header" },
          el("span", { class: "mono" }, t.name),
          stateChip(t.worker_kind),
          stateChip(t.provenance)),
        el("div", { class: "template-card-params note" },
          (t.params || []).length
            ? `params: ${(t.params || []).map(p => p.name).join(", ")}`
            : "no params"),
        el("div", { class: "actions" },
          // v75-F7 (C9): Use navigates to Assign; the ?template= prefill
          // parsing ships in v76-F4 — until then this is a plain navigation.
          el("a", {
            class: "template-card-use",
            href: `#/assign?template=${encodeURIComponent(t.name)}`,
            title: "opens Assign",
          }, "Use →"),
          remove));
    })));
  } else panel.append(el("p", { class: "empty-state" }, "No templates yet."));

  const name = el("input", { placeholder: "name" });
  const caste = casteSelect(roster.castes);
  const params = el("input", { placeholder: "target, level=high" });
  const instructions = el("textarea", { placeholder: "Audit {{target}} dependencies…" });
  const add = el("button", { class: "primary" }, "Save template");
  add.addEventListener("click", async () => {
    try {
      await api("POST", "/api/templates", {
        name: name.value.trim(), instructions: instructions.value, worker_kind: caste.value,
        params: params.value.split(",").map(s => s.trim()).filter(Boolean).map(spec => {
          const [n, d] = spec.split("=");
          return d === undefined ? { name: n } : { name: n, default: d };
        }),
      });
      route();
    } catch (e) { flash("bad", e.message); }
  });
  panel.append(el("h3", {}, "new template"),
    el("div", { class: "composer stacky" },
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "name"), name),
        el("div", { class: "field" }, el("label", {}, "caste"), caste),
        el("div", { class: "field grow" }, el("label", {}, "params (name or name=default)"), params)),
      el("div", { class: "field" }, el("label", {}, "instructions ({{param}} placeholders)"), instructions),
      add));

  const suggestionName = el("input", { placeholder: "web-feature" });
  const suggestionRepo = el("input", { list: "suggestion-repo-list", placeholder: "repo slug or host path" });
  const suggestionInstructions = el("textarea", { placeholder: "Task that should reuse remembered approvals" });
  const suggestionCaste = casteSelect(roster.castes);
  const suggestionPreview = el("div", { class: "suggestion-preview" });
  const preview = el("button", { class: "ghost" }, "Preview suggestion");
  const confirm = el("button", { class: "primary", disabled: true }, "Confirm suggestion");
  let currentSuggestion = null;
  const resetSuggestion = () => {
    currentSuggestion = null;
    confirm.disabled = true;
    suggestionPreview.replaceChildren();
  };
  for (const input of [suggestionName, suggestionRepo, suggestionInstructions, suggestionCaste]) {
    input.addEventListener("input", resetSuggestion);
    input.addEventListener("change", resetSuggestion);
  }
  const suggestionInput = () => {
    const payload = {
      name: suggestionName.value.trim(),
      repo: suggestionRepo.value.trim(),
      instructions: suggestionInstructions.value.trim(),
      caste: suggestionCaste.value,
    };
    if (!payload.name) throw new Error("suggestion name is required");
    if (!payload.repo) throw new Error("repo is required");
    if (!payload.instructions) throw new Error("instructions are required");
    return payload;
  };
  preview.addEventListener("click", async () => {
    preview.disabled = true;
    resetSuggestion();
    try {
      const payload = suggestionInput();
      const { suggestions } = await previewTemplateSuggestion(payload);
      if (!suggestions.length) {
        suggestionPreview.append(el("p", { class: "empty-state" }, "No matching remembered approvals."));
        return;
      }
      currentSuggestion = suggestions[0];
      confirm.disabled = false;
      suggestionPreview.append(el("div", { class: "suggestion-result" },
        el("p", {}, el("span", { class: "mono" }, currentSuggestion.template.name),
          ` ${currentSuggestion.template.worker_kind}`),
        el("p", { class: "note" }, currentSuggestion.template.instructions),
        renderSuggestionGrant(currentSuggestion)));
    } catch (e) { flash("bad", e.message); } finally { preview.disabled = false; }
  });
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      const payload = suggestionInput();
      await confirmTemplateSuggestion(payload.name, {
        repo: payload.repo,
        instructions: payload.instructions,
        caste: payload.caste,
      });
      flash("ok", `saved template ${payload.name}`);
      route();
    } catch (e) { flash("bad", e.message); confirm.disabled = currentSuggestion === null; }
  });
  panel.append(el("h3", {}, "suggest from approvals"),
    el("div", { class: "composer stacky" },
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "name"), suggestionName),
        el("div", { class: "field grow" }, el("label", {}, "repo"), suggestionRepo,
          el("datalist", { id: "suggestion-repo-list" }, repos.map(r => el("option", { value: r })))),
        el("div", { class: "field" }, el("label", {}, "caste"), suggestionCaste)),
      el("div", { class: "field" }, el("label", {}, "instructions"), suggestionInstructions),
      el("div", { class: "actions" }, preview, confirm),
      suggestionPreview));
}

// v75-F7: the lifecycle stepper renders the states that EXIST in skills.py
// (draft → tested → approved; rejected is the terminal failure branch) — the
// spec's six-step pipeline invented states with no data source (I8).
const SKILL_STEPS = ["draft", "tested", "approved"];

function buildSkillStepper(status) {
  const currentIdx = SKILL_STEPS.indexOf(status);
  return el("div", { class: "skill-stepper" },
    SKILL_STEPS.map((step, i) => el("div", {
      class: `skill-step${i <= currentIdx ? " reached" : ""}${i === currentIdx ? " current" : ""}`,
    },
      el("div", { class: "skill-step-dot", "aria-hidden": "true" }),
      el("div", { class: "skill-step-label" }, step))),
    status === "rejected" ? el("span", { class: "state rejected" }, "rejected") : null);
}

async function renderSkillsTab(panel) {
  const { skills } = await api("GET", "/api/skills");
  const propose = el("button", { class: "ghost" }, "Propose from confirmed runs");
  propose.addEventListener("click", async () => {
    try {
      const { proposed } = await api("POST", "/api/skills/propose");
      flash("ok", `${proposed.length} new draft(s)`); route();
    } catch (e) { flash("bad", e.message); }
  });
  panel.append(el("div", { class: "actions" }, propose));
  if (!skills.length) {
    panel.append(el("p", { class: "empty-state" },
      "No candidates — propose after a few confirmed runs."));
    return;
  }
  for (const skill of skills) {
    const actions = el("div", { class: "actions" });
    const decide = (label, cls, path, body) => {
      const button = el("button", { class: cls }, label);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await api("POST", `/api/skills/${skill.name}/${path}`, body); route(); }
        catch (e) { flash("bad", e.message); button.disabled = false; }
      });
      return button;
    };
    if (skill.status === "draft") {
      const repo = el("input", { placeholder: "test repo (slug or path)", style: "width:160px" });
      const go = el("button", { class: "ghost" }, "Test");
      go.addEventListener("click", async () => {
        go.disabled = true;
        try {
          const result = await api("POST", `/api/skills/${skill.name}/test`,
            { repo: repo.value.trim(), params: {} });
          flash(result.passed ? "ok" : "bad",
            result.passed ? "passed the G10 test gate" : "failed the test gate");
          route();
        } catch (e) { flash("bad", e.message); go.disabled = false; }
      });
      actions.append(repo, go);
    }
    if (skill.status === "tested") actions.append(decide("Approve", "primary", "approve", { actor: "operator" }));
    if (skill.status === "draft" || skill.status === "tested") {
      actions.append(decide("Reject", "danger", "reject", { actor: "operator" }));
    }
    panel.append(el("div", { class: "item-card searchable" },
      el("div", { class: "row" },
        el("span", { class: "mono" }, skill.name),
        skill.registry_name ? el("span", { class: "note" }, `→ ${skill.registry_name}`) : null),
      buildSkillStepper(skill.status),
      el("div", { class: "note" }, skill.template.instructions.slice(0, 90)),
      el("div", { class: "note" }, `${skill.occurrences} runs; test: ${skill.test_outcome || "-"}`),
      actions));
  }
}

// ---------- Schedules ----------

async function viewProjects(main) {
  header(main, "Projects", "Trusted packs, bindings, and the schedules they seed.");
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
          location.hash = "#/policies";
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

async function viewSchedules(main) {
  header(main, "Schedules", "Recurring work the in-process ticker dispatches — no cron, no terminal.");
  // v76-F6: one fetch — the health facts render ABOVE the table as banners,
  // so a failing schedule is visible (and actionable) without scrolling.
  const [
    { schedules }, { templates },
    { health: schedHealth }, { health: provHealth }, { nodes },
  ] = await Promise.all([
    api("GET", "/api/schedules"),
    api("GET", "/api/templates"),
    api("GET", "/api/schedules/health"),
    api("GET", "/api/providers/health"),
    api("GET", "/api/nodes"),
  ]);

  // Banners fire only for actual failures (I8/v65) — a clean page shows none.
  const failing = schedHealth.filter(h => h.consecutive_failures >= 3);
  if (failing.length) {
    main.append(el("div", { class: "health-banner warn" },
      el("span", {}, `${failing.length} schedule(s) failing:`),
      failing.map(h => {
        const entry = el("span", { class: "health-banner-item" },
          el("span", { class: "mono" }, h.name),
          ` ${h.consecutive_failures} consecutive failures`);
        // The disable goes through the EXISTING toggle (no new mutation
        // path, I5); the ticker itself auto-disables at 5 — the button is
        // early action, not the only guard (I8).
        if (h.consecutive_failures >= 5 && h.enabled) {
          entry.append(el("button", {
            class: "ghost",
            onclick: async () => {
              try { await api("PATCH", `/api/schedules/${h.name}`, { enabled: false }); route(); }
              catch (e) { flash("bad", e.message); }
            },
          }, "disable"));
        }
        return entry;
      }),
      el("span", { class: "note" },
        "the ticker auto-disables a schedule after 5 consecutive failures")));
  }
  const unreachable = provHealth.filter(h => !h.reachable);
  if (unreachable.length) {
    main.append(el("div", { class: "health-banner bad" },
      el("span", {}, `${unreachable.length} provider(s) unreachable:`),
      unreachable.map(h => el("span", { class: "health-banner-item mono" }, h.provider_id))));
  }

  if (schedules.length) {
    main.append(el("table", {},
      el("thead", {}, el("tr", {},
        ["name", "caste", "project", "every", "on", "next run", "last run", "last outcome", "source", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, schedules.map(s => {
        const toggle = el("button", { class: "ghost" }, s.enabled ? "disable" : "enable");
        toggle.addEventListener("click", async () => {
          await api("PATCH", `/api/schedules/${s.name}`, { enabled: !s.enabled }); route();
        });
        const remove = el("button", { class: "danger" }, "remove");
        remove.addEventListener("click", async () => {
          await api("DELETE", `/api/schedules/${s.name}`); route();
        });
        const project = s.project_context
          ? `${s.project_context.project_id} (${s.project_context.phase})`
          : "-";
        // v76-F6: next-run is a countdown ("in 4h 12m"; a past stamp reads
        // "overdue"), the absolute time one hover away (I8). Last-run stays
        // absolute — it is a record, not a countdown.
        const nextRun = s.next_run_at && new Date(s.next_run_at).getTime() < Date.now()
          ? "overdue" : relativeTime(s.next_run_at);
        return el("tr", {},
          el("td", { class: "mono" }, s.name),
          el("td", {}, s.worker_kind),
          el("td", {}, project),
          el("td", {}, `${s.interval_seconds}s`),
          el("td", {}, s.enabled ? "yes" : "no"),
          el("td", { title: fmtTs(s.next_run_at) }, nextRun),
          el("td", {}, fmtTs(s.last_run_at)),
          el("td", {}, s.last_state || "-"),
          el("td", {}, s.template_name ? `template ${s.template_name}` : "inline"),
          el("td", { class: "actions" }, toggle, remove));
      }))));
  } else main.append(el("p", { class: "empty-state" }, "No schedules yet."));

  // v14: schedule + provider health. v15: ops nodes. (Fetched up top since
  // v76-F6 — the banners summarize; this table stays the record.)
  if (schedHealth.length || provHealth.length || nodes.length) {
    const rows = [];
    for (const n of nodes) {
      rows.push(el("tr", {},
        el("td", {}, `node: ${n.node_id}`),
        el("td", {}, n.trust_tier),
        el("td", {}, `${n.allowed_capabilities.length} ops capabilities`)));
    }
    for (const h of schedHealth) {
      const rate = h.success_rate == null ? "-" : `${Math.round(h.success_rate * 100)}%`;
      rows.push(el("tr", {},
        el("td", {}, `schedule: ${h.name}`),
        el("td", {}, h.enabled ? "enabled" : "disabled"),
        el("td", {}, `success ${rate}, consecutive fails ${h.consecutive_failures}`)));
    }
    for (const h of provHealth) {
      rows.push(el("tr", {},
        el("td", {}, `provider: ${h.provider_id}`),
        el("td", {}, h.reachable ? "reachable" : "unreachable"),
        el("td", {}, h.model_found ? "model ok" : (h.error || "model missing"))));
    }
    main.append(el("section", {}, el("h3", {}, "Health"),
      el("table", {}, el("tbody", {}, rows))));
  }

  const name = el("input", { placeholder: "nightly-audit" });
  const repo = el("input", { placeholder: "repo slug or path" });
  const every = el("input", { placeholder: "30s / 5m / 2h / 1d", value: "1d" });
  const templateSel = el("select", {}, el("option", { value: "" }, "(inline instructions)"),
    templates.map(t => el("option", { value: t.name }, t.name)));
  const params = el("input", { placeholder: "key=value, key=value" });
  const instructions = el("input", { placeholder: "instructions (inline schedules)" });
  const add = el("button", { class: "primary" }, "Add schedule");
  add.addEventListener("click", async () => {
    try {
      const body = {
        name: name.value.trim(), repo: repo.value.trim(), every: every.value.trim(),
      };
      if (templateSel.value) {
        body.template = templateSel.value;
        body.params = Object.fromEntries(params.value.split(",")
          .map(s => s.trim()).filter(Boolean).map(pair => pair.split("=").map(x => x.trim())));
      } else body.instructions = instructions.value;
      await api("POST", "/api/schedules", body);
      route();
    } catch (e) { flash("bad", e.message); }
  });
  main.append(el("div", { class: "composer stacky" },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "name"), name),
      el("div", { class: "field grow" }, el("label", {}, "repo"), repo),
      el("div", { class: "field" }, el("label", {}, "every"), every)),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "template"), templateSel),
      el("div", { class: "field grow" }, el("label", {}, "params"), params),
      el("div", { class: "field grow" }, el("label", {}, "instructions"), instructions),
      add)));
}

// ---------- Policies (v75-F5: four sections + field-help + argv-safe editor) ----------

// v75-F5 (C8): each row holds ONE command as its exact JSON array, so argv
// semantics survive — ["bash", "-c", "a b"] never becomes four words. The
// raw-JSON toggle is the whole-list escape hatch; the server-side validation
// stays the authority either way.
function buildShellCommandEditor(commands) {
  const rows = el("div", { class: "shell-cmd-list" });
  const rawBox = el("textarea", { class: "mono hidden" });
  let raw = false;
  const renderRows = () => {
    rows.replaceChildren(...commands.map((command, i) => {
      const input = el("input", { class: "mono", value: JSON.stringify(command) });
      input.addEventListener("change", () => {
        try {
          const parsed = JSON.parse(input.value || "[]");
          if (!Array.isArray(parsed) || !parsed.every(part => typeof part === "string")) {
            throw new Error("not a string array");
          }
          commands[i] = parsed;
        } catch {
          flash("bad", 'each command is a JSON string array, e.g. ["ruff", "check"]');
          input.value = JSON.stringify(commands[i]);
        }
      });
      return el("div", { class: "shell-cmd-row" },
        input,
        iconButton("remove command", "×", {
          class: "danger ghost",
          onclick: () => { commands.splice(i, 1); renderRows(); },
        }));
    }));
  };
  renderRows();
  const addRow = el("button", { type: "button", class: "ghost" }, "+ Add command");
  addRow.addEventListener("click", () => { commands.push([]); renderRows(); });
  const structured = el("div", {}, rows, addRow);
  const toggle = el("button", { type: "button", class: "ghost" }, "raw JSON");
  toggle.addEventListener("click", () => {
    if (!raw) {
      rawBox.value = JSON.stringify(commands, null, 2);
      structured.classList.add("hidden");
      rawBox.classList.remove("hidden");
      toggle.textContent = "row editor";
      raw = true;
      return;
    }
    try {
      const parsed = JSON.parse(rawBox.value || "[]");
      commands.splice(0, commands.length, ...parsed);
    } catch {
      flash("bad", "raw JSON does not parse — fix it, or save straight from raw mode");
      return;
    }
    renderRows();
    rawBox.classList.add("hidden");
    structured.classList.remove("hidden");
    toggle.textContent = "raw JSON";
    raw = false;
  });
  return {
    node: el("div", { class: "shell-cmd-editor" }, structured, rawBox,
      el("div", { class: "actions" }, toggle)),
    value: () => {
      if (!raw) return commands;
      try { return JSON.parse(rawBox.value || "[]"); }
      catch { throw new Error("allowed shell commands: the raw JSON does not parse"); }
    },
  };
}

async function viewPolicies(main) {
  header(main, "Policies",
    "Policy and scopes: autonomy and defaults. Every change rebuilds the config for the next "
    + "run; templates (skep setup --template) set these in one move.");
  // v109-F9: every policy tier on one page — Global knobs, the Queen's
  // Operator rules, Groups, and the Learned grants that auto-run things —
  // behind the shared filter-bar pattern (Runs/Approvals).
  const [policy, { groups }, ruleData] = await Promise.all([
    api("GET", "/api/policy"),
    api("GET", "/api/policy-groups"),
    api("GET", "/api/policy/rules"),
  ]);
  const tiers = {
    global: el("div", { class: "policy-tier" }),
    operator: el("div", { class: "policy-tier" }),
    groups: el("div", { class: "policy-tier" }),
    learned: el("div", { class: "policy-tier" }),
  };
  const auto = el("input", { type: "checkbox" });
  auto.checked = policy.auto_approve;
  const workerCmd = el("input", { value: policy.worker_cmd, class: "mono" });
  const network = el("input", { value: policy.default_network.join(", ") });
  const env = el("input", { value: policy.default_env_allowlist.join(", ") });
  const mode = el("select", {},
    el("option", { value: "ask" }, "ask"),
    el("option", { value: "workspace" }, "workspace"),
    el("option", { value: "sandbox" }, "sandbox"));
  mode.value = policy.default_execution_mode;
  const trustedRoots = el("input", { value: policy.trusted_workspace_roots.join(", ") });
  const sandboxRequired = el("input", { value: policy.sandbox_required_for.join(", ") });
  const shellEditor = buildShellCommandEditor(
    (policy.allowed_shell_commands || []).map(command => [...command]));
  const tick = el("input", { type: "number", min: "1", value: String(policy.ticker_interval_seconds) });
  const defaultWall = el("input", { type: "number", min: "1", value: String(policy.default_wall_clock_seconds) });
  const defaultIterations = el("input", { type: "number", min: "1", value: String(policy.default_max_iterations) });
  const defaultActions = el("input", { type: "number", min: "1", value: String(policy.default_max_actions) });
  const defaultProviderCalls = el("input", { type: "number", min: "0", value: String(policy.default_max_provider_calls) });
  const save = el("button", { class: "primary" }, "Save policy");
  save.addEventListener("click", async () => {
    try {
      await api("PUT", "/api/policy", {
        auto_approve: auto.checked,
        worker_cmd: workerCmd.value.trim(),
        default_network: network.value.split(",").map(s => s.trim()).filter(Boolean),
        default_env_allowlist: env.value.split(",").map(s => s.trim()).filter(Boolean),
        default_execution_mode: mode.value,
        trusted_workspace_roots: trustedRoots.value.split(",").map(s => s.trim()).filter(Boolean),
        sandbox_required_for: sandboxRequired.value.split(",").map(s => s.trim()).filter(Boolean),
        ticker_interval_seconds: Number(tick.value),
        default_wall_clock_seconds: Number(defaultWall.value),
        default_max_iterations: Number(defaultIterations.value),
        default_max_actions: Number(defaultActions.value),
        default_max_provider_calls: Number(defaultProviderCalls.value),
        allowed_shell_commands: shellEditor.value(),
      });
      flash("ok", "policy saved — applies to the next run");
    } catch (e) { flash("bad", e.message); }
  });

  // v75-F5: every field teaches (I9) — a one-line "what this does" hint.
  const field = (label, input, help, grow = false) => el("div",
    { class: `field${grow ? " grow" : ""}` },
    el("label", {}, label),
    help ? el("p", { class: "field-help" }, help) : null,
    input);
  const section = (title, open, ...body) => el("details",
    { class: "policy-section", open: open ? "" : null },
    el("summary", { class: "policy-section-header" }, title),
    el("div", { class: "policy-section-body" }, body));

  tiers.global.append(
    section("Execution defaults", true,
      el("div", { class: "row" },
        field("default execution", mode,
          "Where workers run: workspace (a checkout under a trusted root) or "
          + "sandbox (bubblewrap/seatbelt walls); ask = choose per dispatch."),
        field("wall clock (s)", defaultWall,
          "Maximum seconds a worker may run before it is stopped."),
        field("max iterations", defaultIterations,
          "Plan/act rounds a worker gets before it must finish."),
        field("max actions", defaultActions,
          "Total capability calls (files, shell, …) per run."),
        field("max provider calls", defaultProviderCalls,
          "LLM requests a run may spend — the cost ceiling."))),
    section("Security", false,
      el("div", { class: "row" },
        field("default network allowlist (D1)", network,
          "Domains workers may reach, comma-separated. Empty = deny all network.", true),
        field("default env allowlist (G2)", env,
          "Environment variable NAMES passed through to workers — never values.", true)),
      el("div", { class: "row" },
        field("trusted workspace roots", trustedRoots,
          "Directories where workspace (non-sandbox) execution is allowed, comma-separated.", true),
        field("sandbox required for", sandboxRequired,
          "Repos or paths that must always run sandboxed, whatever the mode says.", true))),
    section("Advanced", false,
      el("div", { class: "row" },
        field("worker command", workerCmd,
          "The command skep uses to spawn the coding worker.", true),
        field("ticker interval (s)", tick,
          "How often the in-process scheduler checks for due schedules.")),
      field("allowed shell commands", shellEditor.node,
        "Commands workers may run without an approval card. One row per "
        + "command, as a JSON array — quoted arguments survive exactly.")),
    section("Auto-approval", false,
      el("div", { class: "field" },
        el("label", {}, "auto-approval (D3: verified + re-verified manifest-only fixes)"),
        el("p", { class: "field-help" },
          "Auto-applies only verified, re-verified, manifest-only fixes on the "
          + "constrained skep/ integration branch — never on main, never for "
          + "other change classes."),
        el("div", {}, auto, " auto-apply safe dependency fixes"))),
    el("div", { class: "actions" }, save));

  // v97-F5 (ADR 0048): policy groups — named convenience-grant bundles,
  // live-composed into every attached project's run policy. Edit once, all
  // follow; "Save as new group" is the copy-on-write escape hatch.
  let groupEditHandoff = null;
  try {
    const raw = sessionStorage.getItem("skep-group-edit");
    sessionStorage.removeItem("skep-group-edit");
    groupEditHandoff = raw ? JSON.parse(raw) : null;
  } catch { groupEditHandoff = null; }

  const groupRow = (group) => {
    const fromProject = groupEditHandoff?.group === group.name
      ? groupEditHandoff.project : null;
    const textarea = el("textarea", { class: "mono", rows: "7" });
    textarea.value = JSON.stringify(group.policy, null, 2);
    const forkBox = el("input", { type: "checkbox" });
    // Context-sensitive default: opened FROM a project's view while the
    // group serves more than one project → the protective choice (fork)
    // starts checked; on this global page editing the shared thing is the
    // point, so it starts unchecked. The click decides either way.
    forkBox.checked = Boolean(fromProject && group.attached_projects.length > 1);
    const forkName = el("input", { class: "mono", value: `${group.name}-2` });
    const repoint = el("select", {},
      el("option", { value: "" }, "repoint: none"),
      ...group.attached_projects.map(p =>
        el("option", { value: p }, `repoint ${p}`)));
    if (fromProject && group.attached_projects.includes(fromProject)) {
      repoint.value = fromProject;
    }
    const syncFork = () => { forkName.hidden = repoint.hidden = !forkBox.checked; };
    forkBox.addEventListener("change", syncFork);
    syncFork();
    const saveGroup = el("button", { class: "primary" }, "Save");
    const removeGroup = el("button", { class: "danger" }, "Delete");
    saveGroup.addEventListener("click", async () => {
      let policy;
      try { policy = JSON.parse(textarea.value); }
      catch { flash("bad", "policy must be valid JSON"); return; }
      try {
        if (forkBox.checked) {
          const newName = forkName.value.trim();
          await api("POST", `/api/policy-groups/${encodeURIComponent(group.name)}/fork`, {
            new_name: newName,
            policy,
            repoint_project: repoint.value || null,
          });
          flash("ok", `forked ${group.name} → ${newName}`
            + (repoint.value ? ` and repointed ${repoint.value}` : "")
            + " — the source group is untouched");
        } else {
          const affected = group.attached_projects.length
            ? `every attached project follows: ${group.attached_projects.join(", ")}`
            : "no projects attach it yet";
          if (!window.confirm(`Update ${group.name} IN PLACE — ${affected}. Continue?`)) return;
          await api("PUT", `/api/policy-groups/${encodeURIComponent(group.name)}`, policy);
          flash("ok", `updated ${group.name} — ${affected}`);
        }
        route();
      } catch (e) { flash("bad", e.message); }
    });
    removeGroup.addEventListener("click", async () => {
      try {
        await api("DELETE", `/api/policy-groups/${encodeURIComponent(group.name)}`);
        flash("ok", `deleted ${group.name}`);
        route();
      } catch (e) { flash("bad", e.message); }  // attached → the refusal names the projects
    });
    const badge = group.builtin ? (group.edited ? "builtin · edited" : "builtin") : "custom";
    const attachedNote = group.attached_projects.length
      ? `attached: ${group.attached_projects.join(", ")}` : "attached: none";
    return el("details", { class: "policy-group-row", open: fromProject ? "" : null },
      el("summary", {},
        el("span", { class: "mono" }, group.name),
        el("span", { class: "note" }, ` ${badge} · ${attachedNote}`)),
      el("p", { class: "field-help" },
        "Save updates IN PLACE (every attached project follows on its next "
        + "dispatch). “Save as new group” forks copy-on-write: the source and "
        + "its other projects stay untouched."),
      textarea,
      el("div", { class: "row" },
        el("label", {}, forkBox, " Save as new group"),
        forkName, repoint, saveGroup, removeGroup));
  };

  const newGroupName = el("input", { class: "mono", placeholder: "new-group-name" });
  const newGroupBtn = el("button", {}, "Create group");
  newGroupBtn.addEventListener("click", async () => {
    try {
      await api("PUT", `/api/policy-groups/${encodeURIComponent(newGroupName.value.trim())}`,
        { default_network: [] });
      flash("ok", `created ${newGroupName.value.trim()} — edit its grants below`);
      route();
    } catch (e) { flash("bad", e.message); }
  });

  // v109-F9: within Groups, filter by attached project — the same chip
  // pattern, one level down.
  const attachedProjects = [...new Set(groups.flatMap(g => g.attached_projects))].sort();
  const groupList = el("div", {});
  const renderGroupRows = (projectKey) => {
    groupList.replaceChildren(...groups
      .filter(g => projectKey === "all" || g.attached_projects.includes(projectKey))
      .map(groupRow));
  };
  const projectBar = attachedProjects.length
    ? buildFilterBar(
      [{ key: "all", label: "All projects" },
        ...attachedProjects.map(p => ({ key: p, label: p }))],
      Object.fromEntries([
        ["all", groups.length],
        ...attachedProjects.map(p =>
          [p, groups.filter(g => g.attached_projects.includes(p)).length]),
      ]),
      renderGroupRows)
    : null;
  renderGroupRows("all");
  tiers.groups.append(
    el("details", { class: "policy-section", open: groupEditHandoff ? "" : null },
      el("summary", { class: "policy-section-header" }, "Policy groups"),
      el("div", { class: "policy-section-body" },
        el("p", { class: "field-help" },
          "Reusable grant bundles (network hosts, shell prefixes, env vars, "
          + "budgets, engine) attached to projects and composed live — the "
          + "project's own policy always wins scalars; trust-ramp keys can "
          + "never ride a group."),
        projectBar,
        groupList,
        el("div", { class: "row" }, newGroupName, newGroupBtn))));

  // v109-F9: the Queen's standing operator document — visible, read-only
  // (its edits stay the carded set_operator_policy chat verb).
  const operatorRules = ruleData.operator_rules || [];
  tiers.operator.append(
    section("Operator rules (the Queen's standing policy)", true,
      el("p", { class: "field-help" },
        "The Queen's own standing document (the set_operator_policy chat "
        + "verb) — it governs Queen-side reads and fetches only, never "
        + "workers. Read-only here."),
      operatorRules.length
        ? el("table", {},
          el("thead", {}, el("tr", {},
            ["scope", "action", "pattern", "verdict"].map(h => el("th", {}, h)))),
          el("tbody", {}, operatorRules.map(r => el("tr", {},
            el("td", {}, r.scope),
            el("td", {}, r.action),
            el("td", { class: "mono" }, r.pattern),
            el("td", {}, r.verdict)))))
        : el("p", { class: "empty-state" }, "No operator rules stored.")));

  // v109-F9: learned rules — the standing grants that auto-run things. Until
  // this section the operator could grant them but never see or revoke them.
  const learnedRules = ruleData.rules || [];
  const ruleRow = (rule) => {
    const revoke = el("button", { class: "danger" }, "Revoke");
    revoke.addEventListener("click", async () => {
      if (!window.confirm(
        `Revoke ${rule.rule_id}? The next matching action cards again.`)) return;
      try {
        await api("DELETE", `/api/policy/rules/${encodeURIComponent(rule.rule_id)}`);
        flash("ok", `revoked ${rule.rule_id} — the next matching action cards again`);
        route();
      } catch (e) { flash("bad", e.message); }
    });
    return el("div", { class: "item-card learned-rule-row" },
      el("div", { class: "row" },
        el("span", { class: "mono" }, rule.rule_id),
        el("span", { class: "note" },
          `${rule.scope}/${rule.action} · `
          + (rule.tier === "session" ? "session (ends on restart)" : "always")
          + ` · ${rule.provenance}`
          + (rule.created_at ? ` · granted ${rule.created_at}` : "")),
        revoke));
  };
  tiers.learned.append(
    section("Learned rules (standing grants)", true,
      el("p", { class: "field-help" },
        "Written by allow-always confirmations and approved cards — each rule "
        + "lets its exact subject auto-run without a card. Session rules end "
        + "when serve restarts; Revoke ends any rule now."),
      learnedRules.length
        ? el("div", { class: "stack" }, learnedRules.map(ruleRow))
        : el("p", { class: "empty-state" },
          "No learned rules — nothing auto-runs beyond stored policy.")));

  // The tier filter shows one tier (or all); counts follow the tier contents.
  const TIER_FILTERS = [
    { key: "all", label: "All" },
    { key: "global", label: "Global" },
    { key: "operator", label: "Operator" },
    { key: "groups", label: "Groups" },
    { key: "learned", label: "Learned" },
  ];
  const globalSections = 4; // Execution defaults / Security / Advanced / Auto-approval
  const tierCounts = {
    all: globalSections + operatorRules.length + groups.length + learnedRules.length,
    global: globalSections,
    operator: operatorRules.length,
    groups: groups.length,
    learned: learnedRules.length,
  };
  main.append(
    buildFilterBar(TIER_FILTERS, tierCounts, (key) => {
      for (const [tier, wrap] of Object.entries(tiers)) {
        wrap.classList.toggle("hidden", key !== "all" && key !== tier);
      }
    }),
    tiers.global, tiers.operator, tiers.groups, tiers.learned);
}

// ---------- Settings (v75-F3: five tabs — one section's DOM at a time) ----------

// Per-session tab memory (the openChatGroups pattern): route() re-renders
// after a save land back on the tab the operator was using.
let activeSettingsTab = "assistant";

async function viewSettings(main) {
  header(main, "Settings",
    "The assistant the Queen chats with, the worker's provider, and the repos the hive may work on.");
  const tabs = buildTabBar([
    { key: "assistant", label: "Assistant", render: renderAssistantTab },
    { key: "worker", label: "Worker", render: renderWorkerTab },
    { key: "channels", label: "Channels", render: renderChannelsTab },
    { key: "webhooks", label: "Webhooks", render: renderWebhooksTab },
    { key: "repos", label: "Repos", render: renderReposTab },
  ], { initial: activeSettingsTab, onActivate: (key) => { activeSettingsTab = key; } });
  main.append(tabs.bar, tabs.content);
}

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
  const probe = el("button", { class: "primary" }, "Test & save connection");
  probe.addEventListener("click", async () => {
    probe.disabled = true;
    try {
      const body = { base_url: baseUrl.value.trim(), protocol: protocol.value };
      if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
      await api("PUT", "/api/llm/config", body);
      const verdict = await api("POST", "/api/llm/test", {});
      if (verdict.ok) { flash("ok", `connected — ${verdict.models} model(s) available`); route(); }
      else { flash("bad", `connection failed: ${verdict.detail}`); probe.disabled = false; }
    } catch (e) { flash("bad", e.message); probe.disabled = false; }
  });
  const llmCard = el("div", { class: "card" },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "preset"), buildPresetPicker(protocol, baseUrl)),
      el("div", { class: "field" }, el("label", {}, "assistant protocol"), protocol),
      el("div", { class: "field grow" }, el("label", {}, "assistant base URL"), baseUrl),
      el("div", { class: "field grow" },
        el("label", {}, `API key (stored as a 0600 file${llm.api_key_set ? "; saved" : ""})`), apiKey),
      probe));
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
    placeholder: `auto (${llm.num_ctx}${llm.num_ctx_source === "detected" ? ", detected" : ""})`,
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

async function renderWorkerTab(panel) {
  const [settings, roster] = await Promise.all([
    api("GET", "/api/settings"),
    api("GET", "/api/workers"),
  ]);
  renderWorkerRoster(panel, roster);
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
    el("h3", {}, "Worker LLM override"),
    settings.configured ? null
      : el("p", { class: "note" },
        "Optional override — the default coding worker uses the assistant connection above."),
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
    + "Only low-risk actions are ever confirmable from a channel; shell and policy approvals "
    + "stay web-UI-only."));
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
    const users = name === "discord" ? el("input", {
      value: (channel.allowed_users || []).join(", "),
      placeholder: "allow-listed user ids, comma-separated (empty = any user in an allowed channel)",
    }) : null;
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
        if (users) body.allowed_users = users.value.split(",").map(s => s.trim()).filter(Boolean);
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
      users ? el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "allowed users"), users)) : null,
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
      if (!detail.actions.some(action => action.status === "proposed")) {
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
if (await tryConnect()) { await route(); refreshDockModel(); }
poll();
schedulePoll(POLL_MIN_MS);
