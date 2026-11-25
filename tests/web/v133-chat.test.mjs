// v133 B4 — the web chat (findings G1, G2, G3, G6, G7, G8 of the Alfin live
// test). The chat page must not rebuild itself for a live event, the composer
// must always take typing, and nothing the operator typed is lost or sent
// without their action.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp } from "./harness.mjs";

const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await new Promise(r => setTimeout(r, 0)); };

const msg = (id, role, content, provenance = role === "user" ? "operator" : "queen") =>
  ({ id, chat_id: "c1", role, content, provenance, created_at: "2026-11-25T10:00:00Z" });
const card = (id, extra = {}) => ({
  action_id: id, chat_id: "c1", tool: "run_shell", args: { command: "ls" },
  status: "proposed", source: "assistant", card: { headline: `run_shell — ${id}` }, ...extra,
});

class FakeSource {
  constructor(url) { this.url = url; this.listeners = {}; FakeSource.last = this; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, data) { for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) }); }
  close() { this.closed = true; }
}

// The chat view for chat c1 over a mutable server: `server.messages` and
// `server.actions` are what the next GET returns. Every POST, every stream
// and every route() (a rebuild of #main) is counted.
async function openChat({ messages = [], actions = [] } = {}) {
  const app = loadApp();
  const server = { messages: [...messages], actions: [...actions] };
  const posts = [];
  const streams = [];
  let routed = 0;
  app.api = async (method, path, body) => {
    if (method === "POST") { posts.push([path, body]); return { detail: "stopping", ok: true }; }
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") {
      return { chat: { chat_id: "c1", title: "t" }, messages: [...server.messages], actions: server.actions.map(a => ({ ...a })) };
    }
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path === "/api/status") return { status: "ok", pending_approvals: 0, machine: "m" };
    if (path === "/api/tasks") return { tasks: [] };
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.streamSse = async (path, body, handlers) => { streams.push([path, body]); };
  app.renderThreads = () => {};
  app.refreshSidebar = async () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.EventSource = FakeSource;
  app.route = () => { routed += 1; };
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  app.location.hash = "#/chat/c1";
  app.document.visibilityState = "visible";
  const input = main.querySelectorAll("textarea")[0];
  const log = main.querySelector(".chat-log");
  const firstChild = main.children[0];
  const press = (key, extra = {}) => {
    const event = { type: "keydown", key, shiftKey: false, prevented: false, preventDefault() { this.prevented = true; }, ...extra };
    input.dispatchEvent(event);
    return event;
  };
  const textCount = (text) => log.querySelectorAll(".chat-message").filter(n => n.textContent.includes(text)).length;
  return { app, main, input, log, server, posts, streams, routed: () => routed, firstChild, press, textCount };
}

// ---------- F19 (G8): the poll reconciles by message id ----------

test("F19: a reply stored by a background turn appears after one poll, without rebuilding #main, drawn once", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello"), msg(2, "assistant", "hi there")] });
  chat.server.messages.push(msg(9, "assistant", "the run finished, here is what it did"));
  await chat.app.poll();
  assert.equal(chat.textCount("the run finished"), 1, "the background reply is drawn");
  assert.equal(chat.routed(), 0, "#main is not rebuilt");
  assert.equal(chat.main.children[0], chat.firstChild);
  await chat.app.poll();
  assert.equal(chat.textCount("the run finished"), 1, "a message already drawn is not drawn twice");
  assert.equal(chat.textCount("hi there"), 1);
});

test("F19: a message the live stream drew is not drawn again by the poll; a worker line stored mid-stream is", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.app.streamSse = async (path, body, handlers) => {
    chat.streams.push([path, body]);
    chat.server.messages.push(msg(5, "user", "do the thing"));
    chat.server.messages.push(msg(6, "user", "run abc completed", "worker:abc"));
    handlers.message({ content: "done, the thing is done" });
    chat.server.messages.push(msg(7, "assistant", "done, the thing is done"));
  };
  chat.input.value = "do the thing";
  chat.press("Enter");
  await settle();
  await chat.app.poll();
  await chat.app.poll();
  assert.equal(chat.streams.length, 1);
  assert.equal(chat.textCount("do the thing"), 1);
  assert.equal(chat.textCount("done, the thing is done"), 1);
  assert.equal(chat.textCount("run abc completed"), 1, "a line another author stored during the stream is drawn");
  assert.equal(chat.routed(), 0);
});

// ---------- F20 (G1): live events update the node they concern ----------

test("F20: a card arriving and a card resolved elsewhere do not rebuild #main; draft and scroll survive", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.input.value = "a draft in progress";
  Object.assign(chat.log, { scrollTop: 100, scrollHeight: 1000, clientHeight: 300 });
  chat.server.actions.push(card("a1"));
  await chat.app.poll();
  const drawn = chat.log.querySelectorAll(".confirm-card");
  assert.equal(drawn.length, 1, "the card arriving is drawn");
  chat.server.actions[0].status = "denied";
  await chat.app.poll();
  assert.ok(drawn[0].classList.contains("resolved"), "the verdict from elsewhere resolves the drawn card");
  assert.equal(chat.log.querySelectorAll(".confirm-card").length, 1, "a card is drawn once");
  assert.equal(chat.routed(), 0);
  assert.equal(chat.main.children[0], chat.firstChild);
  assert.equal(chat.input.value, "a draft in progress");
  assert.equal(chat.log.scrollTop, 100, "the operator's scroll position is kept");
});

test("F20: a run's terminal event draws its stored line in place, with no rebuild", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  const source = FakeSource.last;
  assert.ok(source, "the status stream is open");
  chat.server.messages.push(msg(4, "user", "run abcdef completed — land it?", "worker:abcdef"));
  source.emit("terminal", { task_id: "abcdef0123456789", state: "completed" });
  await settle();
  assert.equal(chat.routed(), 0);
  assert.equal(chat.main.children[0], chat.firstChild);
  assert.equal(chat.textCount("run abcdef completed"), 1);
});


test("F20: a quiet poll touches nothing on screen, not even the context panel", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")], actions: [card("a1")] });
  const panel = chat.main.querySelector(".chat-context");
  const before = [...panel.children];
  const logBefore = [...chat.log.children];
  await chat.app.poll();
  assert.deepEqual([...panel.children], before);
  assert.deepEqual([...chat.log.children], logBefore);
  chat.server.actions[0].status = "confirmed";
  await chat.app.poll();
  assert.notDeepEqual([...panel.children], before, "a card resolved elsewhere redraws the panel");
});
