// T3-1: app.js render code, executed — not grepped. Run: node --test tests/web/
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const byText = (root, tag, text) =>
  root.querySelectorAll(tag).find(n => n.textContent === text);
const byPlaceholder = (root, placeholder) =>
  root.querySelectorAll("input").find(n => n.getAttribute("placeholder") === placeholder);

// ---------- R3: delegationText ----------

test("delegationText names the chain, the worker role and the team", () => {
  const app = loadApp();
  assert.equal(
    app.delegationText({ chain: ["queen@operator", "manager@builder"], role: "builder", team: "t-9" }),
    "queen@operator → manager@builder → worker@builder · team t-9");
});

test("delegationText: no chain means the operator dispatched; no role means plain worker", () => {
  const app = loadApp();
  assert.equal(app.delegationText({}), "operator → worker");
  assert.equal(app.delegationText({ chain: "not-a-list", role: "operator" }), "operator → worker@operator");
});

test("delegationText renders nothing for a missing or malformed delegation", () => {
  const app = loadApp();
  for (const value of [null, undefined, "queen", 7]) assert.equal(app.delegationText(value), "");
});

test("a delegated run card carries the chain as a chip; an undelegated one does not", () => {
  const app = loadApp();
  const run = { task_id: "task-abcdef123456", state: "completed", updated_at: "2026-11-09T00:00:00Z" };
  const plain = app.buildRunCard(run);
  assert.equal(plain.querySelectorAll(".chip").filter(c => c.getAttribute("title") === "delegated by").length, 0);
  const delegated = app.buildRunCard({
    ...run, dispatch_decision: { delegation: { chain: ["queen@operator"], role: "builder" } },
  });
  const chip = delegated.querySelectorAll(".chip").find(c => c.getAttribute("title") === "delegated by");
  assert.equal(chip.textContent, "queen@operator → worker@builder");
});

// ---------- L1: the Runs page's agents section ----------

test("agents section: a failed or empty /api/agents renders nothing", async () => {
  const app = loadApp();
  app.api = async () => { throw new Error("down"); };
  assert.equal(await app.buildAgentsSection(), null);
  app.api = async () => ({ agents: [], summary: "" });
  assert.equal(await app.buildAgentsSection(), null);
});

test("agents section names governed runs and flags blocked-and-ungoverned", async () => {
  const app = loadApp();
  app.api = async (method, path) => {
    assert.equal(`${method} ${path}`, "GET /api/agents");
    return {
      summary: "2 agents, 1 blocked and ungoverned",
      agents: [
        { agent: "claude", pid: 11, state: "working", governed: true, task_id: "t1", role: "builder", gap: false },
        { agent: "codex", pid: 22, state: "blocked", governed: false, gap: true },
      ],
    };
  };
  const section = await app.buildAgentsSection();
  const rows = section.querySelectorAll("li").map(li => li.textContent);
  assert.deepEqual(rows, [
    "claude · pid 11 · working · governed run t1 (builder)",
    "codex · pid 22 · blocked · ungoverned — blocked and ungoverned",
  ]);
  assert.equal(texts(section, "run-group-count")[0], "2");
});

// ---------- O4: why-lines and receipts ----------

test("whyLine renders only when the server sent a why", () => {
  const app = loadApp();
  assert.equal(app.whyLine({}), null);
  assert.equal(app.whyLine({ why: "allowed: rule shell.allow" }).textContent, "why: allowed: rule shell.allow");
});

test("a grant-covered receipt says it ran, names the grant, and has no buttons", () => {
  const app = loadApp();
  const node = app.receiptNode({
    tool: "run_shell",
    card: { headline: "uv run pytest", risk: "runs the test suite" },
    decision: { verdict: "allow", reason: "grant", detail: "session grant: uv run pytest" },
    why: "allowed: a session grant",
    grant: { tier: "session", granted_at: "2026-11-09T10:00:00Z" },
  });
  assert.equal(texts(node, "card-kicker")[0], "Ran without asking — you approved this already");
  assert.equal(texts(node, "card-headline")[0], "uv run pytest");
  assert.equal(texts(node, "card-purpose")[0], "covered by session grant: uv run pytest");
  assert.equal(texts(node, "card-why")[0], "why: allowed: a session grant");
  assert.equal(node.querySelectorAll("button").length, 0);
});

// ---------- O3: the web onboard card ----------

function onboardApp(onboard, calls = []) {
  const app = loadApp();
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    if (method === "GET" && path === "/api/onboard") return onboard;
    if (method === "GET" && path.startsWith("/api/onboard/template?")) {
      return { locked: false, table: "| rule | verdict |", sha256: "abc123", tightened: [], diff: [] };
    }
    if (method === "POST") return {};
    throw new Error(`unexpected ${method} ${path}`);
  };
  return app;
}

test("onboard card: done steps show their record, open steps say so", async () => {
  const app = onboardApp({
    relock: null,
    steps: {
      model: { provider: "ollama", model: "qwen3:14b", egress: "local only" },
      template: { template: "builder", sha256: "0123456789abcdef" },
    },
  });
  const states = texts(await app.onboardCard(), "note").filter(t => /^(done|not done|re-lock)/.test(t));
  assert.deepEqual(states, [
    "done: ollama qwen3:14b — local only",
    "not done yet",
    "not done yet",
    "done: builder locked, sha256 0123456789ab",
    "not done yet",
  ]);
});

test("onboard card: a sandbox switch after the lock says why step 4 re-opened (O3-F1)", async () => {
  const app = onboardApp({ relock: "builder", steps: {} });
  const notes = texts(await app.onboardCard(), "note");
  assert.ok(notes.includes(
    "re-lock needed: the locked baseline is not what builder locks under this sandbox"));
});

test("onboard card: lock needs a preview and a typed yes, then posts the previewed sha", async () => {
  const calls = [];
  const app = onboardApp({ relock: null, steps: {} }, calls);
  const card = await app.onboardCard();
  const lock = byText(card, "button", "Lock baseline");
  assert.equal(lock.disabled, true);

  await byText(card, "button", "Preview").click();
  assert.equal(lock.disabled, false);
  await lock.click();
  assert.deepEqual(app.flashes.at(-1), ["bad", "preview, then type yes"]);
  assert.equal(calls.filter(([m]) => m === "POST").length, 0);

  byPlaceholder(card, "type yes to lock this as the policy baseline").value = "yes";
  await lock.click();
  // JSON round-trip: the bodies were built inside the vm realm.
  const posts = JSON.parse(JSON.stringify(calls.filter(([m]) => m === "POST")));
  assert.deepEqual(posts, [["POST", "/api/onboard/template",
    { template: "assistant", expect_sha256: "abc123", approved_by: "web" }]]);
});

test("onboard card: changing the template throws away the preview", async () => {
  const app = onboardApp({ relock: null, steps: {} });
  const card = await app.onboardCard();
  const lock = byText(card, "button", "Lock baseline");
  await byText(card, "button", "Preview").click();
  const template = card.querySelectorAll("select").find(s => s.value === "assistant");
  template.value = "operator";
  for (const fn of template.listeners.change) fn();
  assert.equal(lock.disabled, true);
});
