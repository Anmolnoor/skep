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
