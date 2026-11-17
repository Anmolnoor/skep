/* H1 (hive-rooms C10): the sidebar — this machine, search, threads grouped
   by project with a status dot each, then the old workspaces folded below.

   app.js boots on load, so its helpers come in through the import cycle.
   Safe because this module only declares at top level: by the time any of
   its functions runs, app.js has finished evaluating. */
import { api, chatTitle, el, header, iconButton, relativeTime, route, shortTaskId } from "./app.js";

// v44-F3: channel groups the operator opened; survives re-renders.
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

// H1: unread is client state too, like the pins (I11) — the updated_at this
// browser last saw per chat. The very first load seeds every chat as seen, so
// a new shell does not open on a wall of unread dots.
const SEEN_KEY = "skep-seen-chats";

function loadSeen(chats) {
  const raw = localStorage.getItem(SEEN_KEY);
  if (raw !== null) return JSON.parse(raw);
  if (!chats.length) return {}; // nothing loaded yet: seed on the first real list
  const seeded = Object.fromEntries(chats.map(chat => [chat.chat_id, chat.updated_at]));
  localStorage.setItem(SEEN_KEY, JSON.stringify(seeded));
  return seeded;
}

const STATUS_TEXT = {
  wait: "a card is waiting on you",
  active: "a turn or a run is active",
  unread: "unread",
  idle: "idle",
};

// The dot, most urgent first: a card waits on you > something runs > unread.
export function threadStatus(chat, seen) {
  if (chat.pending_cards) return "wait";
  if (chat.turn_active || chat.active_runs) return "active";
  if (chat.updated_at > (seen[chat.chat_id] || "")) return "unread";
  return "idle";
}

const activeChatId = () => (location.hash.match(/^#\/chat\/([^/]+)$/) || [])[1] || null;

let lastChats = [];
let lastRender = "";

export function renderThreads(chats) {
  lastChats = chats;
  const list = document.getElementById("sidebar-threads");
  if (!list) return;
  const active = activeChatId();
  const seen = loadSeen(chats);
  const open = chats.find(chat => chat.chat_id === active);
  if (open && seen[active] !== open.updated_at) {
    seen[active] = open.updated_at; // reading a thread is seeing it
    localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
  }
  // The poll re-renders every few seconds; an unchanged list is left alone so
  // a hover, a focus or an open group is never torn down under the pointer.
  const key = JSON.stringify([chats, active, [...pinnedChats]]);
  if (key === lastRender) return;
  lastRender = key;
  list.replaceChildren();
  const item = (chat) => {
    const status = threadStatus(chat, seen);
    const isPinned = pinnedChats.has(chat.chat_id);
    const pin = iconButton(isPinned ? "unpin chat" : "pin chat", isPinned ? "★" : "☆", {
      class: `chat-pin icon-button ghost${isPinned ? " pinned" : ""}`,
      onclick: (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (isPinned) pinnedChats.delete(chat.chat_id);
        else pinnedChats.add(chat.chat_id);
        savePinnedChats();
        renderThreads(chats);
      },
    });
    return el("a", {
      class: `sidebar-chat-item${chat.chat_id === active ? " active" : ""}`,
      href: `#/chat/${chat.chat_id}`,
      title: `${chatTitle(chat)} · ${chat.model || "default model"} · ${STATUS_TEXT[status]}`,
      "aria-label": `open chat: ${chatTitle(chat)} (${STATUS_TEXT[status]})`,
    },
      el("span", { class: `thread-dot ${status}`, "aria-hidden": "true" }),
      el("span", { class: "sidebar-chat-label" }, chatTitle(chat)),
      pin);
  };
  // Pinned first — moved, not duplicated, whatever their group.
  const pinned = chats.filter(chat => pinnedChats.has(chat.chat_id));
  if (pinned.length) {
    list.append(el("div", { class: "sidebar-chat-pinned" },
      el("div", { class: "sidebar-chat-pinned-label" }, "pinned"),
      pinned.map(item)));
  }
  // Web threads group by project, channel faces by source. Insertion order is
  // recency (the list arrives updated_at DESC); "no project" sits last.
  const projects = new Map();
  const groups = new Map();
  for (const chat of chats) {
    if (pinnedChats.has(chat.chat_id)) continue;
    const source = chat.source || "web";
    const bucket = source === "web" ? projects : groups;
    const name = source === "web" ? (chat.project_id || "") : source;
    if (!bucket.has(name)) bucket.set(name, []);
    bucket.get(name).push(chat);
  }
  const unbound = projects.get("");
  projects.delete("");
  for (const [project, group] of projects) {
    list.append(el("div", { class: "sidebar-thread-group" },
      el("a", {
        class: "sidebar-chat-pinned-label sidebar-thread-project",
        href: `#/projects/${encodeURIComponent(project)}`,
        title: `project ${project}`,
      }, project),
      group.map(item)));
  }
  if (unbound) {
    list.append(el("div", { class: "sidebar-thread-group" },
      el("div", { class: "sidebar-chat-pinned-label" }, "no project"),
      unbound.map(item)));
  }
  for (const [source, group] of groups) {
    const isOpen = openChatGroups.has(source) || group.some((c) => c.chat_id === active);
    list.append(el("details", {
      class: "sidebar-chat-group",
      open: isOpen ? "" : null,
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

function applyChatSearch() {
  const list = document.getElementById("sidebar-threads");
  const q = chatSearchQuery.trim().toLowerCase();
  for (const node of list.querySelectorAll(".sidebar-chat-item")) {
    node.classList.toggle("search-hidden",
      Boolean(q) && !node.textContent.toLowerCase().includes(q));
  }
}

let lastInbox = null;

export async function refreshSidebar() {
  const [{ chats }, inbox] = await Promise.all([api("GET", "/api/chats"), api("GET", "/api/inbox")]);
  renderThreads(chats);
  const waiting = inbox.cards.length + inbox.approvals.length;
  const badge = document.getElementById("inbox-badge");
  badge.textContent = String(waiting);
  badge.classList.toggle("hidden", waiting === 0);
  // What the badge counts, the open Inbox shows (the v56-F6 rule).
  const key = JSON.stringify(inbox);
  if (lastInbox !== null && key !== lastInbox && location.hash === "#/inbox") route();
  lastInbox = key;
}

// ---------- the Inbox (H1, hive-rooms C9) ----------
// Read-only by construction: every item links to where it is resolved — a
// card in its thread, a gate on the Approvals page. No verb lives here (I6).

const SOURCE_TEXT = { assistant: "proposed by the Queen", operator: "your command", gate: "a run's gate" };

const inboxItem = (href, title, detail) => el("a", { class: "inbox-item searchable", href },
  el("span", { class: "thread-dot wait", "aria-hidden": "true" }),
  el("span", { class: "inbox-title mono" }, title),
  el("small", {}, detail));

export async function viewInbox(main) {
  header(main, "Inbox", "Everything waiting on you, across threads. Each one is resolved where it lives.");
  const { cards, approvals } = await api("GET", "/api/inbox");
  if (!cards.length && !approvals.length) {
    main.append(el("p", { class: "note" }, "Nothing is waiting on you."));
    return;
  }
  main.append(el("div", { class: "inbox-list" },
    cards.map(card => inboxItem(`#/chat/${card.chat_id}`, card.tool,
      `${chatTitle({ title: card.chat_title })} · ${SOURCE_TEXT[card.source] || card.source} · ${relativeTime(card.created_at)}`)),
    approvals.map(gate => inboxItem("#/approvals", `${gate.action} · run ${shortTaskId(gate.task_id)}`,
      `${gate.reason || "approval"} · ${relativeTime(gate.requested_at)}`))));
}

// A folded workspace group opens while its route is showing; its summary
// goes to the group's first page, so a closed group is one click away.
function openActiveGroup() {
  const ws = (location.hash || "#/home").split(/[/?]/)[1];
  for (const group of document.querySelectorAll(".sidebar details.rail-group")) {
    if ([...group.querySelectorAll("a")].some(a => a.getAttribute("data-ws") === ws)) group.open = true;
  }
}

export function installSidebar() {
  const search = document.getElementById("thread-search");
  search?.addEventListener("input", () => {
    chatSearchQuery = search.value;
    applyChatSearch();
  });
  // The search row is the existing ⌘K palette, opened the way the key does.
  document.getElementById("sidebar-search")?.addEventListener("click", () =>
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true })));
  for (const group of document.querySelectorAll(".sidebar details.rail-group")) {
    // The summary wears its first page's icon (decorateShell drew it already).
    const icon = group.querySelector(".rail-icon");
    if (icon) group.querySelector("summary").prepend(icon.cloneNode(true));
    group.querySelector("summary").addEventListener("click", () => {
      if (!group.open) location.hash = group.querySelector("a").getAttribute("href");
    });
  }
  addEventListener("hashchange", () => {
    openActiveGroup();
    renderThreads(lastChats); // the active row follows the route
  });
  openActiveGroup();
}
