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

test("refreshSidebar reads /api/chats and draws it", async () => {
  const { app, list } = withSidebar();
  app.api = async (method, path) => {
    assert.equal(path, "/api/chats");
    return { chats: [chat("only")] };
  };
  await app.refreshSidebar();
  assert.deepEqual(rowText(list), ["only"]);
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
