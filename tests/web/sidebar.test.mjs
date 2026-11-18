// H1 (hive-rooms C10): the sidebar's thread list (sidebar.js), executed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

// getElementById hands back the same node per id, so a render can be read back.
function withSidebar(hash = "#/home") {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.location.hash = hash;
  return { app, list: app.document.getElementById("sidebar-threads") };
}

const chat = (id, extra = {}) => ({
  chat_id: id, title: id, model: null, source: "web", project_id: null,
  updated_at: "2026-11-17T10:00:00Z", pending_cards: 0, active_runs: 0, turn_active: false,
  ...extra,
});
const rowText = list => list.querySelectorAll(".sidebar-chat-item").map(n => n.querySelector(".sidebar-chat-label").textContent);
const dotOf = (list, id) => list.querySelectorAll(".sidebar-chat-item")
  .find(n => n.getAttribute("href") === `#/chat/${id}`).querySelector(".thread-dot").className;

test("threadStatus: a waiting card beats a live run beats unread beats idle", () => {
  const { app } = withSidebar();
  const seen = { a: "2026-11-17T10:00:00Z" };
  assert.equal(app.threadStatus(chat("a", { pending_cards: 1, active_runs: 1 }), seen), "wait");
  assert.equal(app.threadStatus(chat("a", { active_runs: 2 }), seen), "active");
  assert.equal(app.threadStatus(chat("a", { turn_active: true }), seen), "active");
  assert.equal(app.threadStatus(chat("a", { updated_at: "2026-11-17T11:00:00Z" }), seen), "unread");
  assert.equal(app.threadStatus(chat("a"), seen), "idle");
  assert.equal(app.threadStatus(chat("new"), seen), "unread"); // never seen in this browser
});

test("threads group by project, then no project, then channel faces; the open thread is active", () => {
  const { app, list } = withSidebar("#/chat/b");
  app.renderThreads([
    chat("a", { project_id: "skep" }),
    chat("b", { project_id: "skep-sample" }),
    chat("c"),
    chat("d", { project_id: "skep" }),
    chat("e", { source: "discord" }),
  ]);
  assert.deepEqual(texts(list, "sidebar-chat-pinned-label"), ["skep", "skep-sample", "no project"]);
  assert.deepEqual(rowText(list), ["a", "d", "b", "c", "e"]);
  const project = list.querySelector(".sidebar-thread-project");
  assert.equal(project.getAttribute("href"), "#/projects/skep");
  assert.equal(list.querySelectorAll("summary")[0].textContent, "discord1⌄");
  const active = list.querySelectorAll(".sidebar-chat-item").filter(n => n.classList.contains("active"));
  assert.deepEqual(active.map(n => n.getAttribute("href")), ["#/chat/b"]);
  // The channel group holding the open thread is open; others stay closed.
  assert.equal(list.querySelector("details").getAttribute("open"), null);
});

test("unread: the first load seeds everything seen; a later change is unread until opened", () => {
  const { app, list } = withSidebar();
  app.renderThreads([chat("a"), chat("b")]);
  assert.match(dotOf(list, "a"), /idle/);
  app.renderThreads([chat("a", { updated_at: "2026-11-17T12:00:00Z" }), chat("b"), chat("fresh")]);
  assert.match(dotOf(list, "a"), /unread/);
  assert.match(dotOf(list, "fresh"), /unread/);
  app.location.hash = "#/chat/a"; // opening it is reading it
  app.renderThreads([chat("a", { updated_at: "2026-11-17T12:00:00Z" }), chat("b"), chat("fresh")]);
  assert.match(dotOf(list, "a"), /idle/);
  assert.match(JSON.parse(app.localStorage.getItem("skep-seen-chats")).a, /12:00/);
});

test("unread: a render before the first list loads seeds nothing, so the real list still reads as seen", () => {
  const { app, list } = withSidebar();
  app.renderThreads([]); // a hashchange before the first poll
  assert.equal(app.localStorage.getItem("skep-seen-chats"), null);
  app.renderThreads([chat("a"), chat("b")]);
  assert.match(dotOf(list, "a"), /idle/);
  assert.match(dotOf(list, "b"), /idle/);
});

test("status dots: waiting and active show on the row, and the title says it in words", () => {
  const { app, list } = withSidebar();
  app.renderThreads([chat("w", { pending_cards: 2 }), chat("r", { active_runs: 1 })]);
  assert.match(dotOf(list, "w"), /wait/);
  assert.match(dotOf(list, "r"), /active/);
  const row = list.querySelectorAll(".sidebar-chat-item")[0];
  assert.match(row.getAttribute("title"), /a card is waiting on you/);
});

test("pinning moves a thread to the top without duplicating it, and persists", async () => {
  const { app, list } = withSidebar();
  app.renderThreads([chat("a", { project_id: "skep" }), chat("b")]);
  const pin = list.querySelectorAll(".sidebar-chat-item")[1].querySelector(".chat-pin");
  await pin.click();
  assert.deepEqual(rowText(list), ["b", "a"]);
  assert.equal(texts(list, "sidebar-chat-pinned-label")[0], "pinned");
  assert.deepEqual(JSON.parse(app.localStorage.getItem("skep-pinned-chats")), ["b"]);
});

test("an unchanged list is not torn down; a changed one is", () => {
  const { app, list } = withSidebar();
  const chats = [chat("a")];
  app.renderThreads(chats);
  const first = list.querySelector(".sidebar-chat-item");
  app.renderThreads([chat("a")]);
  assert.equal(list.querySelector(".sidebar-chat-item"), first);
  app.renderThreads([chat("a", { title: "renamed" })]);
  assert.notEqual(list.querySelector(".sidebar-chat-item"), first);
});

test("thread search filters rows client-side and survives a re-render", () => {
  const { app, list } = withSidebar();
  app.installSidebar();
  app.renderThreads([chat("flaky monitor"), chat("pwa manifest")]);
  const search = app.document.getElementById("thread-search");
  search.value = "FLAKY";
  search.listeners.input[0]();
  const hidden = () => list.querySelectorAll(".sidebar-chat-item")
    .filter(n => n.classList.contains("search-hidden")).map(n => n.getAttribute("href"));
  assert.deepEqual(hidden(), ["#/chat/pwa manifest"]);
  app.renderThreads([chat("flaky monitor"), chat("pwa manifest"), chat("new one")]);
  assert.deepEqual(hidden(), ["#/chat/pwa manifest", "#/chat/new one"]);
});

// ---------- H1: the Inbox ----------

const emptyInbox = { cards: [], approvals: [] };
const fullInbox = {
  cards: [{ action_id: "a1", chat_id: "c1", chat_title: "flaky monitor", tool: "dispatch_run", source: "assistant", created_at: "2026-11-17T10:00:00Z" }],
  approvals: [{ review_id: "r1", task_id: "abcdef1234567890", action: "apply_patch", reason: "land it", requested_at: "2026-11-17T10:00:00Z" }],
};

test("refreshSidebar draws the threads and counts the Inbox on its badge", async () => {
  const { app, list } = withSidebar();
  const badge = app.document.getElementById("inbox-badge");
  app.api = async (method, path) => (path === "/api/chats" ? { chats: [chat("only")] } : fullInbox);
  await app.refreshSidebar();
  assert.deepEqual(rowText(list), ["only"]);
  assert.equal(badge.textContent, "2");
  assert.equal(badge.classList.contains("hidden"), false);
  app.api = async (method, path) => (path === "/api/chats" ? { chats: [] } : emptyInbox);
  await app.refreshSidebar();
  assert.equal(badge.classList.contains("hidden"), true);
});

test("an open Inbox redraws when what it counts changes, and only then", async () => {
  const { app } = withSidebar("#/inbox");
  let routed = 0;
  app.route = () => { routed += 1; };
  let inbox = emptyInbox;
  app.api = async (method, path) => (path === "/api/chats" ? { chats: [] } : inbox);
  await app.refreshSidebar(); // first sight: nothing to compare with
  await app.refreshSidebar();
  assert.equal(routed, 0);
  inbox = fullInbox;
  await app.refreshSidebar();
  assert.equal(routed, 1);
});

test("Inbox view: each item links to where it is resolved, and carries no buttons", async () => {
  const { app } = withSidebar("#/inbox");
  app.header = () => {};
  app.api = async () => fullInbox;
  const main = app.el("main");
  await app.viewInbox(main);
  const items = main.querySelectorAll(".inbox-item");
  assert.deepEqual(items.map(i => i.getAttribute("href")), ["#/chat/c1", "#/approvals"]);
  assert.deepEqual(texts(main, "inbox-title"), ["dispatch_run", "apply_patch · run abcdef12"]);
  assert.match(items[0].querySelector("small").textContent, /^flaky monitor · proposed by the Queen · /);
  assert.match(items[1].querySelector("small").textContent, /^land it · /);
  assert.equal(main.querySelectorAll("button").length, 0); // read-only (I6)
});

test("Inbox view: an untitled thread reads as the sidebar names it", async () => {
  const { app } = withSidebar("#/inbox");
  app.header = () => {};
  app.api = async () => ({ cards: [{ ...fullInbox.cards[0], chat_title: "" }], approvals: [] });
  const main = app.el("main");
  await app.viewInbox(main);
  assert.match(main.querySelector(".inbox-item").querySelector("small").textContent, /^New Chat · /);
});

test("Inbox view: nothing waiting says so", async () => {
  const { app } = withSidebar("#/inbox");
  app.header = () => {};
  app.api = async () => emptyInbox;
  const main = app.el("main");
  await app.viewInbox(main);
  assert.equal(main.textContent, "Nothing is waiting on you.");
});

test("H1-G9: a bare-URL or very long title is shortened for display only", () => {
  const { app, list } = withSidebar();
  assert.equal(app.shortTitle("https://www.youtube.com/watch?v=gEJ26rECifc"), "youtube.com/watch?v=gEJ26rECifc");
  assert.equal(app.shortTitle("https://github.com/Anmolnoor/skep/pull/96"), "github.com/…/96");
  assert.equal(app.shortTitle("https://example.com/"), "example.com");
  assert.equal(app.shortTitle("https://x.io/a/b?q=0123456789abcdefghijklmnop"), "x.io/…/…3456789abcdefghijklmnop");
  assert.equal(app.shortTitle("hello boss"), "hello boss");
  const long = "Reply with exactly one word: pong, and then explain why in detail please";
  assert.equal(app.shortTitle(long), `${long.slice(0, 47).trimEnd()}…`);
  assert.equal(app.shortTitle("not a url: https://a.b/c"), "not a url: https://a.b/c");
  // The row shows the short form; its tooltip and aria-label keep the stored title.
  const url = "https://www.youtube.com/watch?v=gEJ26rECifc";
  app.renderThreads([chat("u", { title: url })]);
  const row = list.querySelectorAll(".sidebar-chat-item")[0];
  assert.equal(row.querySelector(".sidebar-chat-label").textContent, "youtube.com/watch?v=gEJ26rECifc");
  assert.match(row.getAttribute("title"), new RegExp(`^${url.replace(/[?.]/g, "\\$&")} · `));
  assert.match(row.getAttribute("aria-label"), /youtube\.com\/watch\?v=gEJ26rECifc \(/);
});
