// v134 — the 2.1.0 live-test round (plan: skep-design plans/v134/README.md).
// F4 (S1): a landing card never shows a syntax-only G10 pass as clean.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await new Promise(r => setTimeout(r, 0)); };

const msg = (id, role, content, provenance = role === "user" ? "operator" : "queen") =>
  ({ id, chat_id: "c1", role, content, provenance, created_at: "2026-11-25T10:00:00Z" });

// The chat view for chat c1 over a mutable server (as v133-chat.test.mjs).
async function openChat({ messages = [], actions = [] } = {}) {
  const app = loadApp();
  const server = { messages: [...messages], actions: [...actions] };
  let routed = 0;
  app.api = async (method, path) => {
    if (method === "POST") return { ok: true };
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
  app.streamSse = async () => {};
  app.renderThreads = () => {};
  app.refreshSidebar = async () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.EventSource = class { addEventListener() {} close() {} };
  app.route = () => { routed += 1; };
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  app.location.hash = "#/chat/c1";
  app.document.visibilityState = "visible";
  const input = main.querySelectorAll("textarea")[0];
  const log = main.querySelector(".chat-log");
  const textCount = (text) => log.querySelectorAll(".chat-message").filter(n => n.textContent.includes(text)).length;
  return { app, main, input, log, server, routed: () => routed, textCount };
}

// ---------- F4 (S1): a syntax-only G10 pass is marked, never clean ----------

const WEAK = "`node --check farm/index.js` is syntax-only: it checks that the code parses "
  + "(or that a tool starts), not that the patch works; it reads none of the files this patch changes";
const landing = (run) => ({
  action_id: "a1", chat_id: "c1", tool: "land_run", args: { task_id: run.task_id },
  status: "proposed", source: "assistant", verdict: "proposed",
  card: { headline: "land_run — land run 01a214c6", runs: [run] },
});
const passedRun = (extra = {}) => ({
  task_id: "01a214c6-7586-7a08-ba50-d29a154fabee", warning: null, tamper: null,
  g10: { command: ["node --check farm/index.js"], source: null, ran: true, exit_codes: [0], outcome: "passed" },
  worker: "worker's own verification: passed",
  supervisor: "G10 passed: `node --check farm/index.js`, exit codes [0]",
  landing: null, g10_weak: WEAK, ...extra,
});

test("F4: a syntax-only G10 pass reads as a warning, not as 'no G10 or V1 warning'", async () => {
  const chat = await openChat({ actions: [landing(passedRun())] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.ok(!card.textContent.includes("no G10 or V1 warning"), card.textContent);
  assert.ok(card.textContent.includes(`⚠ ${WEAK}`), card.textContent);
  assert.ok(card.querySelector(".card-run-supervisor").classList.contains("bad"));
});

test("F4: a real G10 pass still reads clean", async () => {
  const chat = await openChat({ actions: [landing(passedRun({ g10_weak: null }))] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.ok(card.textContent.includes("no G10 or V1 warning"));
  assert.ok(!card.querySelector(".card-run-supervisor").classList.contains("bad"));
});

// ---------- F5 (N3): short run ids never collide within a minute ----------

test("F5: runs started seconds apart get different short ids (UUIDv7: the first 8 hex are coarse time)", () => {
  const app = loadApp();
  const a = app.shortTaskId("01a211bf-5927-75b4-86ce-63b084010555");
  const b = app.shortTaskId("01a211bf-9697-703e-b864-d0aefa52c9eb");
  assert.notEqual(a, b);
  assert.equal(a, "01a211bf-5927");
});
