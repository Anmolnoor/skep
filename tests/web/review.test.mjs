// Phase 10 10.1: the shared modules (js/api, js/sse, js/diff) and the
// review-first flow (js/pages/inbox.js → js/pages/run-detail.js), executed.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { loadApp, texts } from "./harness.mjs";

// getElementById hands back the same node per id, so a render can be read back.
function stableApp(hash = "#/home") {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.location.hash = hash;
  return { app, nodes };
}

// focus() records the node, so keyboard moves can be asserted.
function trackFocus(app) {
  const seen = [];
  Object.getPrototypeOf(app.el("div")).focus = function focus() { seen.push(this); };
  return seen;
}

// The vm realm's arrays are not this realm's: compare them as plain JSON.
const plain = value => JSON.parse(JSON.stringify(value));

const reply = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
});

// ---------- js/api.js: 401 is the re-auth state, 404 keeps its status ----------

test("api(): a 401 puts the re-auth state up and throws status 401", async () => {
  const { app, nodes } = stableApp();
  app.fetch = async () => reply(401, { detail: "missing or invalid access token" });
  nodes.shell = app.el("div");
  nodes.login = app.el("div", { class: "login hidden" });
  await assert.rejects(app.apiImpl("GET", "/api/status"), err => err.status === 401);
  assert.ok(nodes.shell.classList.contains("hidden"));
  assert.ok(!nodes.login.classList.contains("hidden"));
  assert.match(nodes["login-error"].textContent, /rejected \(401\)/);
  assert.match(nodes["login-error"].textContent, /~\/\.skep\/serve\.log/); // says where the token is
});

test("api(): a 404 throws an ApiError carrying the status and the server's detail", async () => {
  const { app } = stableApp();
  app.fetch = async () => reply(404, { detail: "no run bogus" });
  await assert.rejects(app.apiImpl("GET", "/api/runs/bogus"),
    err => err.status === 404 && err.message === "no run bogus");
});

test("api(): JSON parses, plain text (a diff) stays text, the token rides the header", async () => {
  const { app } = stableApp();
  app.localStorage.setItem("skep-token", "t0k");
  const sent = [];
  app.fetch = async (path, options) => { sent.push([path, options.headers["X-Skep-Token"]]); return reply(200, path.endsWith("diff") ? "+a\n" : { ok: 1 }); };
  assert.deepEqual({ ...(await app.apiImpl("GET", "/api/status")) }, { ok: 1 });
  assert.equal(await app.apiImpl("GET", "/api/runs/x/diff"), "+a\n");
  assert.deepEqual(sent, [["/api/status", "t0k"], ["/api/runs/x/diff", "t0k"]]);
});

test("tryConnect: a stored token the server rejects shows the re-auth line; first contact shows none", async () => {
  for (const [stored, expected] of [["old", /rejected \(401\)/], ["", /^$/]]) {
    const { app, nodes } = stableApp();
    app.localStorage.setItem("skep-token", stored);
    app.fetch = async () => reply(401, {});
    assert.equal(await app.tryConnect(), false);
    assert.match(nodes["login-error"].textContent, expected);
  }
});

test("route(): a view's 404 renders the not-found state, not a toast", async () => {
  const { app, nodes } = stableApp("#/runs/bogus");
  app.document.getElementById("app-search").closest = () => null;
  app.api = app.apiImpl;
  app.fetch = async path => (path === "/api/setup/status" ? reply(200, { complete: true }) : reply(404, { detail: "no run bogus" }));
  await app.routeImpl();
  const state = nodes.main.querySelector(".not-found");
  assert.ok(state, "a not-found state is drawn");
  assert.equal(state.getAttribute("role"), "status");
  assert.match(state.textContent, /Not found.*no run bogus/);
  assert.equal(state.querySelector("a").getAttribute("href"), "#/inbox");
  assert.deepEqual(app.flashes, []);
});

test("route(): #/review/<id> opens the review page, and the Inbox link stays lit", async () => {
  const { app } = stableApp("#/review/r1");
  app.document.getElementById("app-search").closest = () => null;
  const inbox = app.el("a", { href: "#/inbox" });
  inbox.dataset.ws = "inbox";
  app.document.querySelectorAll = selector => (selector === ".sidebar a[data-ws]" ? [inbox] : []);
  app.api = async () => ({ complete: true });
  const calls = [];
  for (const entry of vm.runInContext("routes", app)) {
    const name = entry[1].name;
    entry[1] = async (_main, ...args) => { calls.push([name, ...args]); };
  }
  await app.routeImpl();
  assert.deepEqual(calls, [["viewReview", "r1"]]);
  assert.equal(inbox.getAttribute("aria-current"), "page");
});

// ---------- js/diff.js ----------

const DIFF = [
  "diff --git a/src/app.py b/src/app.py",
  "index 1..2 100644",
  "--- a/src/app.py",
  "+++ b/src/app.py",
  "@@ -10,3 +10,3 @@ def main():",
  " keep",
  "--- a removed line that looks like a header",
  "+++ an added line that looks like a header",
  " tail",
  "diff --git a/NEW.md b/NEW.md",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/NEW.md",
  "@@ -0,0 +1 @@",
  "+hello",
  "\\ No newline at end of file",
  "diff --git a/old.txt b/new.txt",
  "similarity index 100%",
  "rename from old.txt",
  "rename to new.txt",
  "diff --git a/logo.png b/logo.png",
  "Binary files a/logo.png and b/logo.png differ",
  "",
].join("\n");

test("parseDiff: files, statuses, counts, and line numbers — hunk counts decide what a line is", () => {
  const { app } = stableApp();
  const files = app.parseDiff(DIFF);
  assert.deepEqual(plain(files.map(f => [f.path, f.status, f.added, f.removed])),
    [["src/app.py", "modified", 1, 1], ["NEW.md", "added", 1, 0], ["new.txt", "renamed", 0, 0], ["logo.png", "binary", 0, 0]]);
  const lines = files[0].lines;
  assert.deepEqual(plain(lines.map(l => l.kind)), ["hunk", "ctx", "del", "add", "ctx"]);
  assert.deepEqual(plain(lines.map(l => [l.oldNo, l.newNo])), [[null, null], [10, 10], [11, null], [null, 11], [12, 12]]);
  assert.deepEqual(plain(files[1].lines.map(l => l.kind)), ["hunk", "add", "meta"]);
  assert.equal(files[2].oldPath, "old.txt");
});

test("parseDiff: a plain (non-git) unified diff starts its file at ---", () => {
  const { app } = stableApp();
  const files = app.parseDiff("--- a.txt\t2026-01-01\n+++ a.txt\t2026-01-02\n@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+y\n\\ No newline at end of file\n");
  assert.deepEqual(plain(files.map(f => [f.path, f.added, f.removed])), [["a.txt", 1, 1]]);
  // A mid-hunk "no newline" marker counts as neither side.
  assert.deepEqual(plain(files[0].lines.map(l => l.kind)), ["hunk", "del", "meta", "add", "meta"]);
});

test("renderDiff: a file list, then one focusable section per file, lines lit and numbered", () => {
  const { app } = stableApp();
  const { node, sections } = app.renderDiff(DIFF);
  assert.equal(sections.length, 4);
  assert.deepEqual(node.querySelectorAll(".diff-file-link").map(b => b.querySelector(".mono").textContent),
    ["src/app.py", "NEW.md", "new.txt", "logo.png"]);
  assert.equal(sections[0].getAttribute("tabindex"), "-1");
  assert.equal(sections[0].getAttribute("aria-label"), "src/app.py, +1 −1");
  assert.equal(texts(sections[0], "add")[1], "11+++ an added line that looks like a header");
  assert.match(sections[3].textContent, /Binary file — no text diff/);
  assert.match(node.textContent, /^4 files changed, \+2 −1/);
});

test("renderDiff: text that is not a diff is shown as it is, never dropped", () => {
  const { app } = stableApp();
  const { node, sections } = app.renderDiff("just words");
  assert.equal(sections.length, 0);
  assert.equal(node.textContent, "just words");
});

// ---------- js/sse.js: the reconnecting EventSource ----------

test("liveSource: reopens after the browser gives up, backing off; close() ends it", () => {
  const { app } = stableApp();
  const made = [];
  class FakeSource {
    static CLOSED = 2;
    constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; made.push(this); }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    close() { this.readyState = 2; this.closed = true; }
  }
  const timers = [];
  app.EventSource = FakeSource;
  app.setTimeout = (fn, ms) => { timers.push(ms); fn(); return timers.length; };
  app.clearTimeout = () => {};
  const got = [];
  const live = app.liveSource("/api/runs/x/events?stream=1", { message: e => got.push(e.data) });
  made[0].listeners.message({ data: "1" });
  made[0].readyState = 1; made[0].onerror(); // a transient drop: the browser retries itself
  assert.equal(made.length, 1);
  made[0].readyState = 2; made[0].onerror(); // given up: we reopen
  made[1].readyState = 2; made[1].onerror();
  assert.equal(made.length, 3);
  assert.deepEqual(timers, [1000, 2000]);
  made[2].onopen(); // a good open resets the backoff
  made[2].readyState = 2; made[2].onerror();
  assert.deepEqual(timers, [1000, 2000, 1000]);
  live.close();
  made[3].onerror();
  assert.equal(made.length, 4);
  assert.ok(made[3].closed);
  assert.deepEqual(got, ["1"]);
});

// ---------- the review flow ----------

const GATE = {
  review_id: "r1", task_id: "task-0123456789", action: "apply_patch", reason: "land the fix",
  boundary: "patch-landing", requested_at: "2026-11-24T10:00:00Z", status: "pending",
  reverification_warning: "test-tamper guard flagged this patch: test_total_dropped (baseline 12 → patched 9)",
};
const REPORT = {
  task_id: "task-0123456789",
  identity: { task_id: "task-0123456789", engine: "builtin", taint: "untrusted" },
  outcome: {
    state: "completed", alarms: [],
    g10: {
      outcome: "tampered", confirmed: false, commands: ["uv run pytest"], commands_source: "project",
      exit_codes: [0], tamper_flags: ["test_total_dropped"], v1: "tampered: test_total_dropped",
      test_counts: { status: "ok", baseline: { total_executed: 12, passed: 12, failed: 0, errors: 0 }, patched: { total_executed: 9, passed: 9, failed: 0, errors: 0 } },
      detail: "",
    },
  },
  ledger: { status: "VALID", head_seq: 41, head_hash: "abcdef0123456789abcdef", unchained_rows: 0, checkpoints_missing: 0, break: null },
  resume_chain: ["task-0123456789"],
  policy_trail: [{ at: "2026-11-24T09:59:00Z", actor: "worker", verdict: "allow", scope: "shell", action: "run", value: "pytest", decided_by: "policy" }],
  worker: { log_tail: ["done"] },
};

function reviewApp({ approvals = [GATE, { ...GATE, review_id: "r2", task_id: "task-second", reverification_warning: undefined }], report = REPORT, post } = {}) {
  const { app, nodes } = stableApp("#/review/r1");
  app.header = () => {};
  const calls = [];
  app.api = async (method, path, body) => {
    calls.push([method, path]);
    if (path === "/api/approvals") return { approvals };
    if (path === "/api/runs/task-0123456789") return { run: { state: "completed", instructions: "fix it" }, artifacts: [{ kind: "patch" }] };
    if (path === "/api/runs/task-0123456789/forensics") return report;
    if (path === "/api/runs/task-0123456789/diff") return DIFF;
    if (method === "POST") return post(path, body);
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  return { app, nodes, calls };
}

test("review page: the ask, then the diff, then forensics, then the verdict — in that order", async () => {
  const { app, calls } = reviewApp();
  const main = app.el("main");
  await app.viewReview(main, "r1");
  const headings = main.querySelectorAll("h2").map(h => h.textContent);
  assert.deepEqual(headings, ["apply_patch · patch-landing", "1 · Diff", "2 · Forensics", "3 · Verdict"]);
  assert.equal(main.querySelectorAll(".diff-file").length, 4);
  assert.match(main.textContent, /1 of 2 in the queue/);
  // Every request stays on this daemon's API: zero egress from the page.
  assert.ok(calls.every(([, path]) => path.startsWith("/api/")), calls);
});

test("review page: V1 tamper, the G10 warning and taint are loud; the command's source is named (I2)", async () => {
  const { app } = reviewApp();
  const main = app.el("main");
  await app.viewReview(main, "r1");
  const alerts = main.querySelectorAll(".banner").filter(b => b.getAttribute("role") === "alert").map(b => b.textContent);
  assert.ok(alerts.some(a => a.startsWith("⚠ test-tamper guard flagged")), alerts);
  assert.ok(alerts.some(a => a.startsWith("V1 TEST TAMPER — tampered: test_total_dropped")), alerts);
  assert.ok(alerts.some(a => a.startsWith("TAINTED")), alerts);
  assert.match(main.textContent, /the project's pinned verify_command/);
  assert.match(main.textContent, /baseline 12 executed .* → patched 9 executed/);
  assert.match(main.textContent, /Ledger VALID: chain head seq 41, sha256 abcdef0123456789…/);
  assert.match(main.textContent, /skep proof export task-0123456789/);
});

test("review page: a clean run raises no alarm, and a broken ledger does", async () => {
  const clean = { ...REPORT, identity: { taint: "unknown" },
    outcome: { ...REPORT.outcome, g10: { ...REPORT.outcome.g10, outcome: "passed", confirmed: true, tamper_flags: [], v1: "clean" } },
    ledger: { break: { file: "decisions.jsonl", line: 7, reason: "hash mismatch" } } };
  const { app } = reviewApp({ approvals: [{ ...GATE, reverification_warning: undefined }], report: clean });
  const main = app.el("main");
  await app.viewReview(main, "r1");
  const alerts = main.querySelectorAll(".banner").filter(b => b.getAttribute("role") === "alert").map(b => b.textContent);
  assert.deepEqual(alerts, ["Ledger BROKEN at decisions.jsonl:7 — hash mismatch"]);
  assert.match(main.textContent, /passed — confirmed/);
});

test("review page: keys move focus — files, forensics, approve, deny — and never press anything", async () => {
  const { app } = reviewApp({ post: () => assert.fail("a key must never give the verdict") });
  const focused = trackFocus(app);
  const main = app.el("main");
  await app.viewReview(main, "r1");
  const root = main.querySelector(".review");
  assert.equal(focused.at(-1), root, "the page takes focus, so the keys work at once");
  const key = k => root.listeners.keydown[0]({ key: k, target: root, preventDefault() {} });
  const files = main.querySelectorAll(".diff-file");
  key("j"); assert.equal(focused.at(-1), files[0]);
  key("j"); assert.equal(focused.at(-1), files[1]);
  key("k"); assert.equal(focused.at(-1), files[0]);
  key("f"); assert.equal(focused.at(-1).querySelector("h2").textContent, "2 · Forensics");
  key("a"); assert.equal(focused.at(-1).textContent, "Approve");
  key("d"); assert.equal(focused.at(-1).textContent, "Deny");
  // Typing in a field is not a shortcut.
  const input = app.el("input");
  root.listeners.keydown[0]({ key: "a", target: input, preventDefault() {} });
  assert.equal(focused.at(-1).textContent, "Deny");
  key("i");
  assert.equal(app.location.hash, "#/inbox");
});

test("review page: Approve lands through the existing verb and offers the next gate", async () => {
  const posted = [];
  const { app } = reviewApp({ post: (path, body) => { posted.push([path, { ...body }]); return { branch: "skep/task-0123" }; } });
  const focused = trackFocus(app);
  const main = app.el("main");
  await app.viewReview(main, "r1");
  await main.querySelector(".review-verdict").querySelectorAll("button")[0].click();
  assert.deepEqual(posted, [["/api/approvals/r1/approve", {}]]);
  assert.match(main.textContent, /Approved — patch applied on skep\/task-0123\./);
  const next = focused.at(-1);
  assert.equal(next.getAttribute("href"), "#/review/r2");
  assert.ok(main.querySelector(".review-verdict").querySelectorAll("button").every(b => b.disabled));
});

test("review page: Deny records its note; a failure re-enables the buttons and says why", async () => {
  let fail = true;
  const posted = [];
  const { app } = reviewApp({ approvals: [GATE], post: (path, body) => {
    posted.push([path, { ...body }]);
    if (fail) throw new Error("store busy");
    return {};
  } });
  const main = app.el("main");
  await app.viewReview(main, "r1");
  const deny = main.querySelector(".review-verdict").querySelectorAll("button")[1];
  await deny.click();
  assert.deepEqual(app.flashes, [["bad", "store busy"]]);
  assert.equal(deny.disabled, false);
  fail = false;
  await deny.click();
  assert.deepEqual(posted.at(-1), ["/api/approvals/r1/deny", { note: "denied from UI review" }]);
  assert.match(main.textContent, /Denied\.Back to the Inbox →/);
});

test("review page: a gate that is no longer pending is a not-found state", async () => {
  const { app } = reviewApp({ approvals: [] });
  const main = app.el("main");
  await app.viewReview(main, "r1");
  assert.match(main.querySelector(".not-found").textContent, /No pending approval r1/);
});

test("review page: forensics that fails is said in place; the diff and the verdict still render", async () => {
  const { app } = reviewApp();
  const api = app.api;
  app.api = async (method, path, body) => {
    if (path.endsWith("/forensics")) throw new Error("audit dir unreadable");
    return api(method, path, body);
  };
  const main = app.el("main");
  await app.viewReview(main, "r1");
  assert.match(main.textContent, /forensics unavailable: audit dir unreadable/);
  assert.equal(main.querySelectorAll(".diff-file").length, 4);
  assert.equal(main.querySelector(".review-verdict").querySelectorAll("button").length, 2);
});

test("run detail's forensics section loads once, on first open", async () => {
  const { app } = stableApp();
  let fetched = 0;
  app.api = async (method, path) => { fetched += 1; assert.equal(path, "/api/runs/t1/forensics"); return REPORT; };
  const section = app.forensicsSection("t1");
  assert.equal(fetched, 0, "nothing is paid until the panel opens");
  const toggle = section.listeners.toggle[0];
  await toggle({ target: { open: true } });
  await toggle({ target: { open: true } });
  assert.equal(fetched, 1);
  assert.match(section.textContent, /G10 & V1/);
});

test("Inbox: gates render in the queue's order, grouped by wall; the warning shows on the row", async () => {
  const { app } = stableApp("#/inbox");
  app.header = () => {};
  // Phase 10 R1 (E2): the server orders the one queue (riskiest wall, then oldest).
  const gate = (id, boundary, warning) => ({ kind: "gate", id, boundary, at: "2026-11-24T08:00:00Z", task_id: GATE.task_id, action: GATE.action, reason: GATE.reason, warning });
  const items = [
    gate("c", "tainted-dispatch"), gate("b", "patch-landing", GATE.reverification_warning),
    gate("a", "host-shell"), gate("d", undefined),
  ];
  app.api = async (method, path) => (path === "/api/review-queue" ? { items, count: items.length } : {});
  const focused = trackFocus(app);
  const main = app.el("main");
  await app.viewInbox(main);
  const rows = main.querySelectorAll(".review-item");
  const links = main.querySelectorAll(".inbox-open");
  assert.deepEqual(links.map(r => r.getAttribute("href")), ["#/review/c", "#/review/b", "#/review/a", "#/review/d"]);
  assert.deepEqual(texts(main, "inbox-group"), ["tainted-dispatch", "patch-landing", "host-shell", "other"]);
  assert.match(rows[1].querySelector(".review-warning").textContent, /^⚠ test-tamper guard/);
  assert.equal(rows[1].querySelector(".thread-dot").className, "thread-dot bad");
  // The first row has focus; j / k walk the rows and remember where the operator was.
  assert.equal(focused.at(-1), links[0]);
  const list = main.querySelector(".inbox-list");
  list.listeners.keydown[0]({ key: "j", target: links[0], preventDefault() {} });
  assert.equal(focused.at(-1), links[1]);
  const again = app.el("main");
  await app.viewInbox(again);
  assert.equal(focused.at(-1).getAttribute("href"), "#/review/b");
});
