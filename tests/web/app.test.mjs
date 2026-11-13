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

// ---------- P3-4: the keep selector offers only what the server honours ----------

test("keepSelect: no choice, no selector; a grantable card offers the server's list", () => {
  const app = loadApp();
  assert.equal(app.keepSelect(undefined), null);
  assert.equal(app.keepSelect({ headline: "set_personality" }), null);
  assert.equal(app.keepSelect({ keep: ["once"] }), null); // a guarded line: approve-once
  const select = app.keepSelect({ keep: ["once", "session", "always"] });
  const options = select.querySelectorAll("option");
  assert.deepEqual(options.map(o => o.getAttribute("value")), ["once", "session", "always"]);
  assert.deepEqual(options.map(o => o.textContent), ["once", "this chat, this session", "always"]);
  assert.equal(options[1].getAttribute("selected"), "");
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

test("a denial receipt says denied, not 'ran without asking' (T3-2, web twin of v115-F3)", () => {
  // `decision` rides a denial too (v40-F10): the kicker follows the verdict.
  const app = loadApp();
  const node = app.receiptNode({
    tool: "run_shell",
    card: { headline: "git push origin main" },
    decision: { verdict: "deny", reason: "floor.git_push", detail: "workers never push" },
  });
  assert.equal(texts(node, "card-kicker")[0], "Denied — this did not run");
  assert.equal(texts(node, "card-purpose")[0], "denied by floor.git_push (workers never push)");
  assert.ok(!node.textContent.includes("without asking"));
});

test("allow_with_constraints ran too — the receipt follows allows_execution()", () => {
  const app = loadApp();
  const node = app.receiptNode({
    tool: "run_shell", decision: { verdict: "allow_with_constraints", reason: "ops.allow.dry_run" },
  });
  assert.equal(texts(node, "card-kicker")[0], "Ran without asking — you approved this already");
});

// ---------- ES-F1: a card's verdict follows its real status ----------

test("cardVerdict: failed and interrupted never read as approved, executing never as denied", () => {
  const app = loadApp();
  const verdict = action => [...app.cardVerdict(action)];
  assert.deepEqual(verdict({ status: "confirmed" }), ["✓ Approved", "confirm"]);
  assert.deepEqual(verdict({ status: "executing" }), ["… Running — confirmed elsewhere", "confirm"]);
  assert.deepEqual(verdict({ status: "failed", result: { ok: false, error: "no such run" } }),
    ["✗ Failed — no such run", "deny"]);
  assert.deepEqual(verdict({ status: "interrupted", result: { note: "serve stopped" } }),
    ["⚠ Interrupted — serve stopped", "deny"]);
  assert.deepEqual(verdict({ status: "superseded", result: null }),
    ["✗ Superseded — replaced by a newer card", "deny"]);
  assert.deepEqual(verdict({ status: "denied" }), ["✗ Denied", "deny"]);
});

// ---------- ES-F5 (B98): a gate mirror resolves on the commands endpoints ----------

test("resolvesOnCommands: operator commands and gate mirrors, never a model card", () => {
  const app = loadApp();
  assert.equal(app.resolvesOnCommands({ source: "operator" }), true);
  assert.equal(app.resolvesOnCommands({ source: "gate" }), true);
  assert.equal(app.resolvesOnCommands({ source: "assistant" }), false);
});

test("cardReplay: a policy-write card shows its replay, shadowed grants and keep replay", () => {
  const app = loadApp();
  assert.equal(app.cardReplay(undefined), null);
  assert.equal(app.cardReplay({ headline: "x" }), null);
  const node = app.cardReplay({ replay: "replay of the last 3 decisions: now allows 1 past asks",
    policy_warnings: ["grant g now lands inside a deny"] });
  assert.equal(node.children.length, 2);
  assert.equal(node.children[0].textContent, "replay of the last 3 decisions: now allows 1 past asks");
  // the keep replay shows only where the card offers a keep
  assert.equal(app.cardReplay({ keep: ["once"], keep_replay: "r" }), null);
  const keep = app.cardReplay({ keep: ["once", "session", "always"], keep_replay: "r" });
  assert.equal(keep.children[0].textContent, "r");
});

// ---------- P4-1: the trifecta guard on the card and the transcript ----------

test("taintNote renders the server's plain-words why; a clean chat's card has none", () => {
  const app = loadApp();
  assert.equal(app.taintNote({ headline: "x" }), null);
  assert.equal(app.taintNote(undefined), null);
  const line = "this chat has read private data (it ran a shell command), so sending anything out needs your OK";
  assert.equal(app.taintNote({ taint: line }).textContent, line);
});

test("cardClass: a HIGH card (both taint bits) looks different", () => {
  const app = loadApp();
  assert.equal(app.cardClass({ tier: "high" }), "confirm-card card-high");
  assert.equal(app.cardClass({}), "confirm-card");
  assert.equal(app.cardClass(undefined), "confirm-card");
});

test("a doubly tainted card offers no keep: the server sends keep ['once']", () => {
  const app = loadApp();
  assert.equal(app.keepSelect({ keep: ["once"], tier: "high" }), null);
  const select = app.keepSelect({ keep: ["once", "always"] }); // untrusted run_shell
  assert.deepEqual(select.querySelectorAll("option").map(o => o.getAttribute("value")), ["once", "always"]);
});

test("authorLabel: a row you did not write says who did (B26, B37)", () => {
  const app = loadApp();
  assert.equal(app.authorLabel({ role: "user", provenance: "operator" }, "You"), "You");
  assert.equal(app.authorLabel({ role: "user" }, "You"), "You"); // a row older than the column
  assert.equal(app.authorLabel({ role: "user", provenance: "webhook:ci" }, "You"), "from webhook:ci");
  assert.equal(app.authorLabel({ role: "user", provenance: "worker:t1" }, "You"), "from worker:t1");
  assert.equal(app.authorLabel({ role: "user", provenance: "queen" }, "You"), "skep");
  assert.equal(app.authorLabel({ role: "assistant", provenance: "queen" }, "qwen3"), "qwen3");
});

test("a run dispatched from a tainted chat carries the taint as a chip", () => {
  const app = loadApp();
  const run = { task_id: "task-abcdef123456", state: "running", updated_at: "2026-11-09T00:00:00Z" };
  const chip = (card) => card.querySelectorAll(".chip")
    .find(c => c.getAttribute("title") === "the dispatching chat's taint");
  assert.equal(chip(app.buildRunCard(run)), undefined);
  const tainted = app.buildRunCard({
    ...run, dispatch_decision: { constraints: { chat_taint: ["private", "untrusted"] } },
  });
  assert.equal(chip(tainted).textContent, "chat: private + untrusted");
});

test("keepCovers: the card says what a keep would allow, and nothing when it allows nothing", () => {
  const app = loadApp();
  assert.equal(app.keepCovers(undefined), null);
  assert.equal(app.keepCovers({ keep: ["once"] }), null);
  const line = app.keepCovers({ keep: ["once", "session", "always"], keep_covers: "a keep allows each of these on its own, exactly: `ls`; `make`" });
  assert.equal(line.textContent, "a keep allows each of these on its own, exactly: `ls`; `make`");
});
