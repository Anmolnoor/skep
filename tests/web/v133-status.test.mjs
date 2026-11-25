// v133 B7 — run detail and card rendering (findings F-I1, F-I3, F-I5, J1, J2,
// G8 of the Alfin live test). The web pages show what wave 1's store now
// says, in the store's words: a landing G10 did not confirm says so on the
// card the operator clicks, each run's worker and supervisor results apart,
// a failed install apart from a check that ran and failed, a rule's verdict
// as "allowed by policy", and the stream's message id when it carries one.
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

// ---------- F34: the landing card says what approving does ----------

const LABEL = "Land despite failed supervisor verification";
const failedRun = (extra = {}) => ({
  task_id: "01a211d0-f243-4000-8000-000000000000",
  warning: "re-verification RAN and FAILED (exit codes [1])",
  tamper: null,
  g10: { command: ["node scripts/smoke.js"], source: "the project's pinned verify_command", ran: true, exit_codes: [1], outcome: "failed" },
  worker: "worker's own verification: passed",
  supervisor: "G10 failed: `node scripts/smoke.js` (the project's pinned verify_command), exit codes [1]",
  landing: null,
  ...extra,
});
const landCard = (extra = {}, runs = [failedRun()]) => ({
  action_id: "a1", chat_id: "c1", tool: "land_run", args: { task_id: runs[0]?.task_id },
  status: "proposed", source: "assistant", verdict: "proposed",
  card: {
    headline: "land_run — land run 01a211d0", tier: "high",
    high: `${LABEL}: G10 failed: \`node scripts/smoke.js\`, exit codes [1]`,
    action_label: LABEL, runs, ...extra,
  },
});

test("F34: a landing G10 did not confirm shows its action label on the card and on the button that approves it", async () => {
  const chat = await openChat({ actions: [landCard()] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.deepEqual(texts(card, "card-action-label"), [LABEL]);
  const label = card.querySelector(".card-action-label");
  assert.ok(label.classList.contains("banner") && label.classList.contains("bad"), label.className);
  assert.equal(label.getAttribute("role"), "alert");
  assert.equal(card.querySelectorAll("button")[0].textContent, LABEL, "the click names the override");
  assert.ok(card.classList.contains("card-high"));
});

test("F34: each run on a landing card shows the worker's and the supervisor's results apart, in the store's words", async () => {
  const chat = await openChat({ actions: [landCard()] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.deepEqual(texts(card, "card-run-worker"), ["worker's own verification: passed"]);
  assert.deepEqual(texts(card, "card-run-supervisor"),
    ["G10 failed: `node scripts/smoke.js` (the project's pinned verify_command), exit codes [1]"]);
  assert.ok(card.querySelector(".card-run-supervisor").classList.contains("bad"), "a G10 that did not pass reads as bad");
  assert.deepEqual(texts(card, "card-run-landing"), [], "no landing note when the store sends none");
});

test("F34: a confirmed run's supervisor line is not drawn as bad; a pre-v133 landing says its commit was not recorded", async () => {
  const passed = failedRun({
    warning: null, g10: { command: ["npm test"], source: null, ran: true, exit_codes: [0], outcome: "passed" },
    supervisor: "G10 passed: `npm test`, exit codes [0]",
    landing: "landing commit not recorded (landed before v133)",
  });
  const chat = await openChat({ actions: [landCard({ action_label: undefined, tier: undefined, high: undefined, headline: "open_pr" }, [passed])] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.ok(!card.querySelector(".card-run-supervisor").classList.contains("bad"));
  assert.deepEqual(texts(card, "card-run-landing"), ["landing commit not recorded (landed before v133)"]);
  assert.deepEqual(texts(card, "card-action-label"), [], "an ordinary landing has no override label");
  assert.equal(card.querySelectorAll("button")[0].textContent, "Approve");
});

test("F34: a landing card that arrives on a poll carries its label — the reconcile path draws the same card", async () => {
  const chat = await openChat();
  chat.server.actions.push(landCard());
  await chat.app.poll();
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.deepEqual(texts(card, "card-action-label"), [LABEL]);
  assert.equal(card.querySelectorAll("button")[0].textContent, LABEL);
  assert.equal(chat.routed(), 0);
});

test("F34: a gate mirror and an operator command card name the override on their confirm button too", async () => {
  const gate = { ...landCard(), action_id: "g1", tool: "approve_review", source: "gate" };
  const command = { ...landCard(), action_id: "o1", source: "operator" };
  const chat = await openChat({ actions: [gate, command] });
  const [gateNode, commandNode] = chat.log.querySelectorAll(".confirm-card");
  assert.equal(gateNode.querySelectorAll("button")[0].textContent, LABEL);
  assert.deepEqual(texts(gateNode, "card-action-label"), [LABEL]);
  assert.equal(commandNode.querySelectorAll("button")[0].textContent, LABEL);
});

test("F34: command output on a card is text, never markup", async () => {
  const evil = "G10 failed: `<img src=x onerror=alert(1)>`, exit codes [1]";
  const chat = await openChat({ actions: [landCard({}, [failedRun({ supervisor: evil, worker: "<b>passed</b>" })])] });
  const [card] = chat.log.querySelectorAll(".confirm-card");
  assert.deepEqual(texts(card, "card-run-supervisor"), [evil]);
  assert.equal(card.querySelectorAll("img").length, 0);
  assert.equal(card.querySelectorAll("b").length, 0);
});

// ---------- F34 (J2): a rule's verdict reads "allowed by policy" ----------

test("F34: a row the store says a rule decided reads 'allowed by policy', never a human's approval", () => {
  const app = loadApp();
  assert.deepEqual([...app.cardVerdict({ status: "confirmed", verdict: "allowed by policy" })],
    ["✓ Allowed by policy", "confirm"]);
  assert.deepEqual([...app.cardVerdict({ status: "confirmed", verdict: "confirmed" })], ["✓ Approved", "confirm"]);
  assert.deepEqual([...app.cardVerdict({ status: "confirmed" })], ["✓ Approved", "confirm"], "a row without the field is unchanged");
});

test("F34: the receipt of an action that ran without asking says a rule allowed it, not that the operator approved it", () => {
  const app = loadApp();
  const node = app.receiptNode({
    tool: "read_file", card: { headline: "read_file README.md" },
    decision: { verdict: "allow", reason: "global/operator-root" },
  });
  assert.equal(texts(node, "card-kicker")[0], "Allowed by policy — ran without asking");
  assert.doesNotMatch(node.textContent, /you approved/);
});

// ---------- F34 (F-I1, F-I5): run detail tells a failed install from a failed check ----------

const INSTALL_DETAIL = "could not install dependencies — could not prime the baseline Node dependencies "
  + "for offline re-verification (npm ci exited 1); the verification command was not run\n"
  + "--- install output (tail) ---\nnpm ERR! Cannot find module 'slugify'";
const OFFLINE = "the replay runs offline (no network, by design) and this output shows a failed "
  + "network lookup: a check that installs or downloads for itself fails here — pin "
  + "a command that uses the dependencies primed from the lockfile";
const OFFLINE_DETAIL = `re-run exit codes [1] (expected all 0); ${OFFLINE}; full output: /a/reverify.output.txt\n`
  + "--- re-run output (tail) ---\ngetaddrinfo EAI_AGAIN registry.npmjs.org [command from the project's pinned verify_command]";

async function runPage(reverification) {
  const app = loadApp();
  const body = {
    run: { task_id: "t1", instructions: "land me", state: "completed", repo: "/r" },
    transitions: [], artifacts: [], commands: [], approvals: [], policy_blocks: [], reverification,
  };
  app.api = async (method, path) => (path === "/api/runs/t1" ? body : { events: [] });
  const main = app.document.createElement("main");
  main.dataset.route = "1";
  await app.viewRunDetail(main, "t1");
  const dts = main.querySelectorAll("dt").map(n => n.textContent);
  const dds = main.querySelectorAll("dd").map(n => n.textContent);
  return { main, rows: Object.fromEntries(dts.map((k, i) => [k, dds[i]])) };
}

test("F34: run detail says G10 could not install dependencies — the lead line, no exit code, nothing 'ran'", async () => {
  const { rows } = await runPage({ outcome: "unavailable", confirmed: false, commands: ["node scripts/smoke.js"],
    exit_codes: [], commands_source: "unknown", detail: INSTALL_DETAIL });
  assert.equal(rows["re-verify did not run"],
    "could not install dependencies — could not prime the baseline Node dependencies for offline "
    + "re-verification (npm ci exited 1); the verification command was not run");
  assert.equal(rows["re-verify ran"], undefined);
  assert.equal(rows["re-verify recorded (not run)"], "node scripts/smoke.js");
  assert.equal(rows["offline replay"], undefined);
});

test("F34: run detail shows a check that ran and failed on a network lookup with its exit code and the offline note apart", async () => {
  const { rows } = await runPage({ outcome: "failed", confirmed: false, commands: ["node scripts/smoke.js"],
    exit_codes: [1], commands_source: "project", detail: OFFLINE_DETAIL });
  assert.equal(rows["re-verify ran"], "node scripts/smoke.js → exit 1");
  assert.match(rows["command from"], /^project pin/);
  assert.equal(rows["offline replay"], OFFLINE);
  assert.equal(rows["re-verify did not run"], undefined);
});

test("F34: a plain failed check gets neither note", async () => {
  const { rows } = await runPage({ outcome: "failed", confirmed: false, commands: ["uv run pytest"],
    exit_codes: [1], commands_source: "project", detail: "re-run exit codes [1] (expected all 0)\n--- re-run output (tail) ---\nFAILED" });
  assert.equal(rows["offline replay"], undefined);
  assert.equal(rows["re-verify did not run"], undefined);
});

test("F34: the forensics G10 box tells the same two cases apart", () => {
  const app = loadApp();
  const box = g10 => {
    const node = app.forensicsPanel({ task_id: "t1", identity: { taint: "unknown" }, outcome: { alarms: [], g10 },
      ledger: null, resume_chain: ["t1"], policy_trail: [], worker: { log_tail: [] } });
    const kv = node.querySelector(".kv");
    const dts = kv.querySelectorAll("dt").map(n => n.textContent);
    const dds = kv.querySelectorAll("dd").map(n => n.textContent);
    return Object.fromEntries(dts.map((k, i) => [k, dds[i]]));
  };
  const base = { confirmed: false, commands: ["node scripts/smoke.js"], tamper_flags: [], v1: "clean", test_counts: null };
  const install = box({ ...base, outcome: "unavailable", exit_codes: [], commands_source: "unknown", detail: INSTALL_DETAIL });
  assert.match(install["re-verify did not run"], /^could not install dependencies — .*the verification command was not run$/);
  assert.equal(install.ran, undefined);
  const offline = box({ ...base, outcome: "failed", exit_codes: [1], commands_source: "project", detail: OFFLINE_DETAIL });
  assert.equal(offline["offline replay"], OFFLINE);
  assert.equal(offline["exit codes"], "1");
  assert.equal(offline["re-verify did not run"], undefined);
});

// ---------- F39 (G8): the stream's message id, when it carries one ----------

// A turn whose stream draws "the answer" live while a background turn (no
// "kind:" author) stores its own reply in the same window.
async function turnWithBackgroundReply(messageEvent) {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.app.streamSse = async (path, body, handlers) => {
    chat.server.messages.push(msg(5, "user", "go"));
    chat.server.messages.push(msg(6, "assistant", "a background turn's reply"));
    handlers.message(messageEvent({ content: "the answer" }));
    chat.server.messages.push(msg(7, "assistant", "the answer"));
    handlers.message(messageEvent({}, 7));
  };
  chat.input.value = "go";
  chat.input.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: false, preventDefault() {} });
  await settle();
  return chat;
}

test("F39: with message_id on the stream's message events, a background reply stored mid-stream is drawn and the streamed one is not redrawn", async () => {
  const chat = await turnWithBackgroundReply((d, id) => (id ? { ...d, message_id: id } : d));
  assert.equal(chat.textCount("the answer"), 1);
  assert.equal(chat.textCount("a background turn's reply"), 1, "a row the stream never announced is drawn");
  assert.equal(chat.textCount("go"), 1, "the operator's own row is not drawn twice");
  await chat.app.poll();
  assert.equal(chat.textCount("a background turn's reply"), 1);
  assert.equal(chat.routed(), 0);
});

test("F39 (R1 S6): without the field, a reply the stream never showed is drawn; the ones it showed are not", async () => {
  const chat = await turnWithBackgroundReply(d => d);
  assert.equal(chat.textCount("the answer"), 1);
  assert.equal(chat.textCount("a background turn's reply"), 1, "an unmarked row the stream did not show is drawn");
  assert.equal(chat.textCount("go"), 1);
});

// ---------- R1: a syntax-only check is said beside its command ----------

const SYNTAX = "the pinned check only proves the code parses (node --check): it runs no test";

test("R1: run detail marks a syntax-only check on its own row beside the command; absent, no row", async () => {
  const rv = { outcome: "passed", confirmed: true, commands: ["node --check index.js"], exit_codes: [0],
    commands_source: "project", detail: "" };
  const marked = await runPage({ ...rv, syntax_only: SYNTAX });
  assert.equal(marked.rows["re-verify ran"], "node --check index.js → exit 0");
  assert.equal(marked.rows["syntax-only check"], SYNTAX);
  const keys = Object.keys(marked.rows);
  assert.equal(keys.indexOf("syntax-only check"), keys.indexOf("command from") + 1, "beside the command rows");
  for (const value of [null, undefined]) {
    const { rows } = await runPage({ ...rv, syntax_only: value });
    assert.equal(rows["syntax-only check"], undefined);
  }
});

test("R1: the forensics G10 box marks a syntax-only check too; absent, no row", () => {
  const app = loadApp();
  const box = g10 => {
    const node = app.forensicsPanel({ task_id: "t1", identity: { taint: "unknown" }, outcome: { alarms: [], g10 },
      ledger: null, resume_chain: ["t1"], policy_trail: [], worker: { log_tail: [] } });
    const kv = node.querySelector(".kv");
    const dts = kv.querySelectorAll("dt").map(n => n.textContent);
    const dds = kv.querySelectorAll("dd").map(n => n.textContent);
    return Object.fromEntries(dts.map((k, i) => [k, dds[i]]));
  };
  const base = { outcome: "passed", confirmed: true, commands: ["node --check index.js"], exit_codes: [0],
    commands_source: "project", tamper_flags: [], v1: "clean", test_counts: null };
  assert.equal(box({ ...base, syntax_only: SYNTAX })["syntax-only check"], SYNTAX);
  assert.equal(box(base)["syntax-only check"], undefined);
});
