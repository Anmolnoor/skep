// Phase 10 R1: run detail and the review page tell the record's truth.
// D3: the G10 box says "ran" + exit codes only when a command executed, whose
// command it was, and the test counts; nothing-to-re-verify is neutral; the
// ledger line reports its real status and degraded coverage.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const counts = n => ({ total_executed: n, passed: n, failed: 0, errors: 0 });
const report = (g10, ledger = { status: "VALID", head_seq: 7, head_hash: "abcdef0123456789abcdef", unchained_rows: 0, checkpoints_missing: 0, break: null }) => ({
  task_id: "t1", identity: { taint: "unknown" }, outcome: { alarms: [], g10 }, ledger,
  resume_chain: ["t1"], policy_trail: [], worker: { log_tail: [] },
});
const g10 = extra => ({
  outcome: "passed", confirmed: true, commands: ["uv run pytest"], commands_source: "project",
  exit_codes: [0], tamper_flags: [], v1: "clean", detail: "",
  test_counts: { status: "ok", baseline: counts(4), patched: counts(5) }, ...extra,
});
// The G10 box as label → value pairs.
const box = (app, rec) => {
  const node = app.forensicsPanel(report(rec));
  const kv = node.querySelector(".kv");
  const dts = kv.querySelectorAll("dt").map(n => n.textContent);
  const dds = kv.querySelectorAll("dd").map(n => n.textContent);
  return { node, rows: Object.fromEntries(dts.map((k, i) => [k, dds[i]])) };
};

test("G10 ran: the command, its exit codes, its source and the counts", () => {
  const app = loadApp();
  const { rows } = box(app, g10());
  assert.equal(rows["re-verify (G10)"], "passed — confirmed");
  assert.equal(rows.ran, "uv run pytest");
  assert.equal(rows["exit codes"], "0");
  assert.match(rows["command from"], /^project pin/);
  assert.match(rows.tests, /baseline 4 executed .* → patched 5 executed/);
});

test("G10 recorded a command but ran nothing: no 'ran', no exit codes", () => {
  const app = loadApp();
  const { rows } = box(app, g10({ outcome: "unavailable", confirmed: false, exit_codes: [],
    commands_source: "worker", test_counts: null, detail: "the sandbox could not start" }));
  assert.equal(rows.ran, undefined);
  assert.equal(rows["exit codes"], undefined);
  assert.equal(rows["recorded verify (not run)"], "uv run pytest");
  assert.match(rows["command from"], /^worker fallback/);
  assert.match(rows["re-verify (G10)"], /NOT CONFIRMED/);
});

test("G10 with nothing to re-verify is neutral — never 'do not trust'", () => {
  const app = loadApp();
  const { node, rows } = box(app, g10({ outcome: "not_applicable", confirmed: false, commands: [],
    exit_codes: [], test_counts: null, v1: "not recorded", detail: "no patch was produced — nothing to re-verify" }));
  assert.equal(rows["re-verify (G10)"], "nothing to re-verify — no patch was produced — nothing to re-verify");
  assert.doesNotMatch(node.textContent, /do not trust/i);
  assert.equal(rows.ran, undefined);
  assert.equal(rows["command from"], undefined);
});

test("G10 command source unknown is said as unknown", () => {
  const app = loadApp();
  assert.match(box(app, g10({ commands_source: "unknown" })).rows["command from"], /^unknown/);
});

test("the ledger line reports the real status, and degraded coverage loudly", () => {
  const app = loadApp();
  const line = ledger => app.forensicsPanel(report(g10(), ledger));
  const valid = line({ status: "VALID", head_seq: 7, head_hash: "abcdef0123456789abcdef", unchained_rows: 0, checkpoints_missing: 0, break: null });
  assert.match(valid.textContent, /Ledger VALID: chain head seq 7, sha256 abcdef0123456789…/);
  assert.deepEqual(valid.querySelectorAll(".banner").map(b => b.textContent), []);
  const degraded = line({ status: "VALID", head_seq: 9, head_hash: "abcdef0123456789abcdef", unchained_rows: 3, checkpoints_missing: 2, break: null });
  const [banner] = degraded.querySelectorAll(".banner");
  assert.ok(banner.classList.contains("warn"), banner.className);
  assert.match(banner.textContent, /Ledger VALID, coverage degraded: 2 due checkpoints were never kept/);
  assert.match(banner.textContent, /3 older rows are not covered by the chain/);
  const empty = line({ status: "VALID", head_seq: null, head_hash: null, unchained_rows: 0, checkpoints_missing: 0, break: null });
  assert.match(empty.textContent, /Ledger VALID: no chained rows yet/);
  const unsaid = line({ head_seq: 1, head_hash: "ab", break: null });
  assert.match(unsaid.textContent, /Ledger status not reported/);
  assert.doesNotMatch(unsaid.textContent, /valid/i);
});

test("run detail's G10 row says 'ran' only when a command executed", async () => {
  const app = loadApp();
  const rows = async reverification => {
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
    return Object.fromEntries(dts.map((k, i) => [k, dds[i]]).filter(([k]) => k.startsWith("re-verify")));
  };
  const ran = await rows({ outcome: "passed", confirmed: true, commands: ["uv run pytest"], exit_codes: [0] });
  assert.equal(ran["re-verify ran"], "uv run pytest → exit 0");
  const unrun = await rows({ outcome: "unavailable", confirmed: false, commands: ["uv run pytest"], exit_codes: [] });
  assert.equal(unrun["re-verify ran"], undefined);
  assert.equal(unrun["re-verify recorded (not run)"], "uv run pytest");
});

// ---------- D7: a render belongs to the route that started it ----------

const GATE = { review_id: "r1", task_id: "t1", action: "apply_patch", reason: "land it",
  requested_at: "2026-11-24T10:00:00Z", status: "pending" };

// The review page's reads; `during` runs inside the named request — the
// operator navigating away while it is in flight.
function reviewReads(app, main, { during = {}, post } = {}) {
  app.header = () => { main.headed = (main.headed || 0) + 1; };
  app.api = async (method, path) => {
    if (during[path]) during[path]();
    if (path === "/api/approvals") return { approvals: [GATE] };
    if (path === "/api/runs/t1") return { run: { state: "completed", instructions: "x" }, artifacts: [{ kind: "patch" }] };
    if (path === "/api/runs/t1/forensics") return report(g10());
    if (path === "/api/runs/t1/diff") return "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n";
    if (method === "POST") return post(path);
    throw new Error(`unstubbed ${method} ${path}`);
  };
}
const navigateAway = main => () => { main.dataset.route = "2"; };

test("a review page whose reads finish after navigation draws nothing and takes no focus", async () => {
  for (const path of ["/api/approvals", "/api/runs/t1/forensics", "/api/runs/t1/diff"]) {
    const app = loadApp();
    const focused = [];
    Object.getPrototypeOf(app.el("div")).focus = function focus() { focused.push(this); };
    const main = app.el("main");
    main.dataset.route = "1";
    reviewReads(app, main, { during: { [path]: navigateAway(main) } });
    await app.viewReview(main, "r1");
    assert.equal(main.children.length, 0, path);
    assert.deepEqual(focused, [], path);
  }
});

test("a verdict that resolves after navigation does not draw or steal focus", async () => {
  const app = loadApp();
  const focused = [];
  Object.getPrototypeOf(app.el("div")).focus = function focus() { focused.push(this); };
  const main = app.el("main");
  main.dataset.route = "1";
  reviewReads(app, main, { post: () => { main.dataset.route = "2"; return { branch: "skep/t1" }; } });
  await app.viewReview(main, "r1");
  focused.length = 0;
  await main.querySelector(".review-verdict").querySelectorAll("button")[0].click();
  assert.doesNotMatch(main.textContent, /Approved/);
  assert.deepEqual(focused, []);
});

const RUNNING = {
  run: { task_id: "t1", instructions: "go", state: "running", repo: "/r" },
  transitions: [], artifacts: [], commands: [], approvals: [], policy_blocks: [],
};

test("a run page whose read finishes after navigation opens no stream and draws nothing", async () => {
  const app = loadApp();
  const main = app.el("main");
  main.dataset.route = "1";
  const opened = [];
  app.liveSource = url => { opened.push(url); return { close() {} }; };
  app.api = async () => { main.dataset.route = "2"; return RUNNING; };
  await app.viewRunDetail(main, "t1");
  assert.equal(main.children.length, 0);
  assert.deepEqual(opened, []);
});

test("the run stream draws an event once, though a reconnect replays it", async () => {
  const app = loadApp();
  const main = app.el("main");
  main.dataset.route = "1";
  let handlers = null;
  app.liveSource = (url, h) => { handlers = h; return { close() {} }; };
  app.api = async () => RUNNING;
  await app.viewRunDetail(main, "t1");
  const event = id => ({ data: JSON.stringify({ event_id: id, ts: "2026-11-24T10:00:00Z", type: "heartbeat", payload: {} }) });
  handlers.message(event("e1"));
  handlers.message(event("e2"));
  handlers.message(event("e1")); // the reopened stream starts from the top
  handlers.message(event("e2"));
  handlers.message(event("e3"));
  assert.equal(main.querySelectorAll(".event").length, 3);
});

test("onceByEventId: one call per event_id; an event without one always passes", () => {
  const app = loadApp();
  const seen = [];
  const once = app.onceByEventId(e => seen.push(e.event_id ?? "none"));
  [{ event_id: "a" }, { event_id: "a" }, {}, {}, { event_id: "b" }].forEach(once);
  assert.deepEqual(seen, ["a", "none", "none", "b"]);
});

test("the chat's worker block counts a replayed command once", async () => {
  const app = loadApp();
  const made = [];
  app.EventSource = class { constructor(url) { this.url = url; made.push(this); } addEventListener() {} close() {} };
  app.api = async () => ({ run: { state: "running", repo: "/r" } });
  const log = app.el("div");
  app.renderWorkerActivity(log, "t1");
  await new Promise(resolve => setTimeout(resolve, 0));
  const command = { data: JSON.stringify({ event_id: "c1", type: "command.result", payload: { exit_code: 0, command: "ls" } }) };
  made[0].onmessage(command);
  made[0].onmessage(command); // the browser reconnected; the server replayed
  assert.deepEqual(texts(log, "worker-activity-row-summary"), ["ran 1 command"]);
});

// ---------- D8: the receipt itself, copied honestly; approve's warning shown ----------

const RECEIPT = "<details>\n<summary>skep verifiable local receipt · t1</summary>\n</details>";

function receiptPanel(app, clipboard) {
  const asked = [];
  app.api = async (method, path) => { asked.push(path); return RECEIPT; };
  app.navigator = clipboard === undefined ? undefined : { clipboard };
  const node = app.forensicsPanel(report(g10()));
  const button = node.querySelectorAll("button").find(b => b.textContent === "Copy receipt");
  return { node, button, asked };
}

test("Copy receipt copies the receipt text and says so only once the clipboard took it", async () => {
  const app = loadApp();
  const written = [];
  const { node, button, asked } = receiptPanel(app, { writeText: async text => { written.push(text); } });
  assert.ok(button, "the button copies the receipt, not a command");
  assert.match(node.textContent, /skep proof export t1/); // the CLI twin stays named
  await button.click();
  assert.deepEqual(asked, ["/api/runs/t1/receipt?format=md"]);
  assert.deepEqual(written, [RECEIPT]);
  assert.deepEqual(app.flashes, [["ok", "receipt copied"]]);
});

test("a refused or missing clipboard shows the receipt to select — never a false 'copied'", async () => {
  for (const clipboard of [{ writeText: async () => { throw new Error("denied"); } }, undefined]) {
    const app = loadApp();
    const { node, button } = receiptPanel(app, clipboard);
    await button.click();
    assert.ok(!app.flashes.some(([kind]) => kind === "ok"), app.flashes);
    const pre = node.querySelector(".receipt-text");
    assert.ok(pre, "the receipt is shown");
    assert.equal(pre.tagName, "PRE");
    assert.equal(pre.children.length, 1);
    assert.equal(pre.children[0].nodeType, 3);
    assert.equal(pre.textContent, RECEIPT);
  }
});

test("an approve that lands with a warning shows the warning", async () => {
  const app = loadApp();
  const main = app.el("main");
  reviewReads(app, main, { post: () => ({ branch: "skep/t1", warning: "G10 could not re-verify this patch" }) });
  await app.viewReview(main, "r1");
  await main.querySelector(".review-verdict").querySelectorAll("button")[0].click();
  assert.match(main.textContent, /Approved — patch applied on skep\/t1\./);
  const alerts = main.querySelectorAll(".banner").filter(b => b.getAttribute("role") === "alert").map(b => b.textContent);
  assert.ok(alerts.includes("⚠ G10 could not re-verify this patch"), alerts);
});

// Phase 10 R2 I10: the run page names G10's command source and the counts,
// as the review page does.
test("run detail's G10 rows name the command source and the test counts", async () => {
  const app = loadApp();
  const body = {
    run: { task_id: "t1", instructions: "land me", state: "completed", repo: "/r" },
    transitions: [], artifacts: [], commands: [], approvals: [], policy_blocks: [],
    reverification: g10(), // commands_source "project", counts 4 → 5
  };
  app.api = async (method, path) => (path === "/api/runs/t1" ? body : { events: [] });
  const main = app.document.createElement("main");
  main.dataset.route = "1";
  await app.viewRunDetail(main, "t1");
  const dts = main.querySelectorAll("dt").map(n => n.textContent);
  const dds = main.querySelectorAll("dd").map(n => n.textContent);
  const rows = Object.fromEntries(dts.map((k, i) => [k, dds[i]]));
  assert.match(rows["command from"], /^project pin/);
  assert.match(rows.tests, /baseline 4 executed .* → patched 5 executed/);
  body.reverification = g10({ commands_source: "worker", test_counts: null });
  main.replaceChildren();
  await app.viewRunDetail(main, "t1");
  assert.match(main.textContent, /worker fallback/);
});
