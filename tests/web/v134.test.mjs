// v134 — the 2.1.0 live-test round (plan: skep-design plans/v134/README.md).
// F4 (S1): a landing card never shows a syntax-only G10 pass as clean.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
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

// ---------- F6 (W1, W2, N2): one line per worker; running ones in the panel ----------

const RUN_A = "01a211bf-5927-75b4-86ce-63b084010555";
const RUN_B = "01a211bf-9697-703e-b864-d0aefa52c9eb";
const dispatched = (id, taskId) => ({
  id, chat_id: "c1", role: "tool", tool_name: "dispatch_run", provenance: null,
  content: JSON.stringify({ task_id: taskId, state: "dispatched" }), created_at: "2026-11-25T10:00:00Z",
});

async function openWorkers({ runs, extra = {}, before = () => {} }) {
  const app = loadApp();
  before(app);
  const sources = [];
  let listed = 0;
  app.api = async (method, path) => {
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") {
      return { chat: { chat_id: "c1", title: "t" }, actions: [],
        messages: [msg(1, "user", "go"), ...Object.keys(runs).map((t, i) => dispatched(2 + i, t))] };
    }
    if (path.startsWith("/api/runs?chat_id=")) {
      listed += 1; // ``extra`` runs started after the first panel read
      return { runs: Object.entries(listed > 1 ? { ...runs, ...extra } : runs).map(([task_id, r]) => ({ task_id, ...r })) };
    }
    if (path.startsWith("/api/runs/")) return { run: { ...runs, ...extra }[path.slice("/api/runs/".length)] };
    if (path.startsWith("/api/llm/usage")) return { chat: { last_5h: { total_tokens: 1 }, last_7d: { total_tokens: 1 } } };
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.streamSse = async () => {};
  app.renderThreads = () => {};
  app.refreshSidebar = async () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.EventSource = class {
    constructor(url) { this.url = url; this.on = {}; sources.push(this); }
    addEventListener(type, fn) { this.on[type] = fn; }
    close() {}
  };
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  await settle();
  const emit = (type, data) => sources.at(-1).on[type]({ data: JSON.stringify(data) });
  return { app, main, log: main.querySelector(".chat-log"), emit, sources, listed: () => listed };
}

const RUNS = {
  [RUN_A]: { state: "completed", repo: "/home/x/nodestarters", instructions: "fix the farm route\nand more", updated_at: "2026-11-25T10:00:00Z" },
  [RUN_B]: { state: "running", repo: "/home/x/nodestarters", instructions: "bump express", updated_at: "2026-11-25T10:00:00Z" },
};

test("F6 (W1): each dispatch is one line — what, its state on the right, Open ↗ in a new tab; no stack under the chat", async () => {
  const { main, log } = await openWorkers({ runs: RUNS });
  const rows = log.querySelectorAll(".worker-row");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector(".worker-row-what").textContent,
    "Worker 01a211bf-5927 · nodestarters · fix the farm route");
  assert.equal(rows[0].querySelector(".live-run-status").textContent, "completed");
  const open = rows[0].querySelector(".open-run");
  assert.equal(open.getAttribute("href"), `#/runs/${RUN_A}`);
  assert.equal(open.getAttribute("target"), "_blank");
  assert.equal(main.querySelectorAll(".worker-loader").length, 0);
  assert.equal(main.querySelectorAll(".worker-activity").length, 0);
});

test("F6 (N2): a chat opened while another stream runs never shows its past dispatches as live", async () => {
  const { log } = await openWorkers({
    runs: { [RUN_A]: RUNS[RUN_A] },
    before: app => vm.runInContext("chatStreamActive = true", app),
  });
  const [status] = texts(log, "live-run-status");
  assert.equal(status, "completed");
});

test("F6 (W1, W2): a status event writes the transcript row and the panel row; a terminal ends both", async () => {
  const { main, log, emit } = await openWorkers({ runs: RUNS });
  const panelRun = main.querySelectorAll(".context-run").find(n => n.classList.contains("running"));
  assert.ok(panelRun, "the running run is in the panel's Running now");
  emit("status", { task_id: RUN_B, phase: "build", elapsed_seconds: 12 });
  const spans = main.querySelectorAll(`.live-run-status[data-task-id="${RUN_B}"]`);
  assert.equal(spans.length, 2);
  for (const span of spans) assert.equal(span.textContent, "build · 12s");
  emit("terminal", { task_id: RUN_B, state: "failed", detail: "boom" });
  const [row] = log.querySelectorAll(`.live-run-status[data-task-id="${RUN_B}"]`);
  assert.equal(row.textContent, "failed");
});

// ---------- F7 (N5): a waiting card says who proposed it, not a role-like word ----------

test("F7: Waiting on you names the proposer: skep for the Queen's cards, you for your commands", async () => {
  const app = loadApp();
  app.api = async (method, path) => (path.startsWith("/api/runs") ? { runs: [] }
    : { chat: { last_5h: { total_tokens: 0 }, last_7d: { total_tokens: 0 } } });
  const panel = app.el("aside", { class: "chat-context" });
  const at = "2026-11-25T10:00:00Z";
  await app.renderContextPanel(panel, {
    chat: { chat_id: "c1", role: "builder", model: "m" }, defaultModel: "m",
    actions: [
      { action_id: "a1", tool: "land_run", status: "proposed", source: "assistant", created_at: at },
      { action_id: "a2", tool: "workon", status: "proposed", source: "operator", created_at: at },
    ],
  });
  const [queen, mine] = panel.querySelectorAll(".context-card").map(n => n.querySelector("small").textContent);
  assert.match(queen, /^proposed by skep · /);
  assert.match(mine, /^your command · /);
});

// ---------- review R1 (S1, S2): live rows settle; runs from elsewhere show ----------

test("R1 S1: a live row whose terminal never arrives settles from the store when the stream closes", async () => {
  const runs = { [RUN_B]: { ...RUNS[RUN_B] } };
  const { main, emit, sources } = await openWorkers({ runs });
  emit("status", { task_id: RUN_B, phase: "build", elapsed_seconds: 3 });
  runs[RUN_B].state = "completed"; // it ended; the stream closed before saying so
  sources.at(-1).onerror();
  await settle();
  const spans = main.querySelectorAll(`.live-run-status[data-task-id="${RUN_B}"]`);
  assert.ok(spans.length >= 1);
  for (const span of spans) assert.equal(span.textContent, "completed");
});

test("R2 S1: a stream that closes on runs still live is reopened once, after a pause", async () => {
  const RUN_C = "01a21483-d097-7d7b-b075-a778042b82f1";
  const runs = { [RUN_B]: { ...RUNS[RUN_B] }, [RUN_C]: { ...RUNS[RUN_B] } };
  const { app, emit, sources } = await openWorkers({ runs });
  const timers = [];
  app.setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
  emit("status", { task_id: RUN_B, phase: "build", elapsed_seconds: 3 });
  emit("status", { task_id: RUN_C, phase: "build", elapsed_seconds: 3 });
  const opened = sources.length;
  sources.at(-1).onerror(); // the server's 30-minute cap; both runs still running
  await settle();
  assert.deepEqual(timers.map(([, ms]) => ms), [5000], "one pending reopen, 5 s out, for both runs");
  timers[0][0]();
  assert.equal(sources.length, opened + 1, "a new status stream was opened");
});

test("R2 N1: after leaving the chat, a late settle never reopens its stream", async () => {
  const { app, emit, sources } = await openWorkers({ runs: { [RUN_B]: { ...RUNS[RUN_B] } } });
  emit("status", { task_id: RUN_B, phase: "build", elapsed_seconds: 3 });
  const opened = sources.length;
  app.setTimeout = (fn) => { fn(); return 0; }; // the pending reopen fires inside the test
  vm.runInContext("cleanup()", app); // the route changed
  sources.at(-1).onerror();
  await settle();
  assert.equal(sources.length, opened);
});

test("R1 S2: a status event for a run this page never drew refreshes the panel, which then shows it live", async () => {
  const RUN_C = "01a21483-d097-7d7b-b075-a778042b82f1";
  const extra = { [RUN_C]: { state: "running", repo: "/r", instructions: "resumed", updated_at: "2026-11-25T10:00:00Z" } };
  const { main, emit, listed } = await openWorkers({ runs: { [RUN_A]: RUNS[RUN_A] }, extra });
  const before = listed();
  emit("status", { task_id: RUN_C, phase: "test", elapsed_seconds: 5 });
  await settle();
  assert.equal(listed(), before + 1, "the panel was re-read");
  const [span] = main.querySelectorAll(`.live-run-status[data-task-id="${RUN_C}"]`);
  assert.equal(span.textContent, "test · 5s");
});
