// Phase 10 R1 (E2): the Inbox is ONE review queue (GET /api/review-queue) —
// gates, standalone cards, memory proposals and skills, grouped by the wall
// each crosses, riskiest first — and the sidebar badge counts that same queue.
// R4 K1: only a worker gate and a memory proposal — whose whole evidence the
// row shows — approve inline; every chat card and skill opens where its full
// evidence is. Deny is wired to each item's existing route; keys only move
// focus (I7).
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const AT = "2026-11-24T08:00:00Z";
const GATE = { kind: "gate", id: "r1", boundary: "network-egress", at: AT, task_id: "abcdef1234567890", action: "network.fetch", reason: "pypi.org", warning: null, tamper: null, stale_base: null, taint: null, mirrored_by: [], inline_approve: true };
// SD-2b: a landing's evidence is its diff, which no row shows — never inline (console.py)
const PATCH = { ...GATE, id: "r2", boundary: "patch-landing", action: "apply_patch", reason: "land it", inline_approve: false };
const CARD = { kind: "card", id: "a1", boundary: "network-egress", at: AT, chat_id: "c1", chat_title: "flaky monitor", tool: "read_url", source: "assistant", headline: "read_url — https://x.dev", risk: null, tamper: null, taint: null, high: null };
const MEMORY = { kind: "memory", id: "m1", boundary: "policy-change", at: AT, memory_class: "project_fact", content: "uses uv", taint: null, inline_approve: true };
const SKILL = { kind: "skill", id: "learned-one", boundary: "policy-change", at: AT, status: "tested", provenance: "learned", description: "learned procedure: x", source: "t1", approvable: true, taint: null };
const RUN = { kind: "run", id: "fedcba9876543210", boundary: "patch-landing", at: AT, task_id: "fedcba9876543210", g10: "NOT CONFIRMED", waiting: "completed — patch not landed", summary: "fixed it", warning: "re-verification RAN and FAILED (exit codes [1])", tamper: null, files: [{ path: "app.py", added: 2, removed: 1 }, { path: "b.py", added: 1, removed: 0 }], files_note: null };
const queue = (...items) => ({ items, count: items.length });

function inboxApp(items, routes = {}) {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.location.hash = "#/inbox";
  app.header = () => {};
  const calls = [];
  const table = {
    "GET /api/review-queue": queue(...items),
    "GET /api/chats": { chats: [] },
    ...routes,
  };
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    const key = Object.keys(table).filter(k => `${method} ${path}`.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`unstubbed api ${method} ${path}`);
    const out = table[key];
    return typeof out === "function" ? out(path, body) : out;
  };
  // The Queen's card lane answers as a stream: each call replays `sse[path]`
  // (or a default one-tool success) through the handlers it was given.
  const sse = routes.sse || {};
  app.streamSse = async (path, body, handlers) => {
    calls.push(["SSE", path, body]);
    const denied = path.endsWith("/deny");
    const events = sse[path] || [["tool", { tool: "t", result: denied ? { ok: false, denied: true } : { ok: true } }], ["done", {}]];
    for (const [event, data] of events) handlers[event]?.(data);
  };
  let routed = 0;
  app.route = () => { routed += 1; };
  return { app, calls, nodes, routed: () => routed };
}

async function render(items, routes) {
  const ctx = inboxApp(items, routes);
  const main = ctx.app.el("main");
  await ctx.app.viewInbox(main);
  return { ...ctx, main, rows: main.querySelectorAll(".inbox-item") };
}
const button = (row, label) => row.querySelectorAll("button").find(b => b.textContent === label);
const posts = calls => calls.filter(([m]) => m === "POST" || m === "SSE").map(([, p, b]) => [p, b === undefined ? undefined : JSON.parse(JSON.stringify(b))]);
const streamed = calls => calls.filter(([m]) => m === "SSE").map(([, p]) => p);

test("the Inbox renders the one queue in its order, grouped by wall, each row linking where it lives", async () => {
  const { main, rows } = await render([PATCH, GATE, CARD, MEMORY, SKILL]);
  assert.deepEqual(texts(main, "inbox-group"), ["patch-landing", "network-egress", "policy-change"]);
  assert.deepEqual(rows.map(r => r.querySelector(".inbox-open").getAttribute("href")),
    ["#/review/r2", "#/review/r1", "#/chat/c1", "#/memory", "#/templates"]);
  assert.deepEqual(texts(main, "inbox-title"), [
    "apply_patch · run abcdef12", "network.fetch · run abcdef12", "read_url — https://x.dev",
    "memory · project_fact", "skill · learned-one"]);
  assert.match(rows[2].querySelector("small").textContent, /^flaky monitor · proposed by the Queen · /);
  assert.match(rows[3].querySelector("small").textContent, /^uses uv · /);
  assert.match(main.querySelector(".inbox-heading").textContent, /Waiting on you · 5/);
});

test("R4 K1: only a gate and a memory proposal approve inline; every row denies on its existing route", async () => {
  const operatorCard = { ...CARD, id: "a2", source: "operator", tool: "setup_project", headline: "setup_project — app" };
  const { app, rows, calls, routed } = await render([GATE, CARD, operatorCard, MEMORY, SKILL], {
    "POST /": {},
  });
  assert.deepEqual(rows.map(row => Boolean(button(row, "Approve"))), [true, false, false, true, false]);
  assert.deepEqual(rows.map(row => row.querySelector(".inbox-elsewhere")?.getAttribute("href") ?? null),
    [null, "#/chat/c1", "#/chat/c1", null, "#/templates"]);
  for (const row of rows) assert.ok(button(row, "Deny"), row.textContent);
  for (const row of rows) await button(row, "Approve")?.click();
  assert.deepEqual(posts(calls), [
    ["/api/approvals/r1/approve", {}],
    ["/api/memory/proposals/m1/approve", undefined],
  ]);
  assert.equal(routed(), 2); // the list re-renders after each verdict
  assert.match(app.flashes.at(-1)[1], /approved memory · project_fact/);
  calls.length = 0;
  app.prompt = () => "not now";
  for (const row of rows) await button(row, "Deny").click();
  assert.deepEqual(posts(calls), [
    ["/api/approvals/r1/deny", {}],
    ["/api/chats/c1/actions/a1/deny", {}],
    ["/api/chats/c1/commands/a2/deny", undefined],
    ["/api/memory/proposals/m1/reject", { reason: "not now" }],
    ["/api/skills/learned-one/reject", {}],
  ]);
  // H6: the Queen's card lane is a stream, read as one — never buffered by api()
  assert.deepEqual(streamed(calls), ["/api/chats/c1/actions/a1/deny"]);
  assert.ok(app.flashes.every(([kind]) => kind === "ok"), JSON.stringify(app.flashes));
});

test("R4 K1: no chat card of any kind approves inline — headline, evidence, the full card link and Deny", async () => {
  const kinds = ["dispatch_run", "batch_dispatch", "land_run", "open_pr", "capture_skill",
    "propose_schedule", "merge_pr", "read_url", "search_web", "promote_tool", "set_policy"];
  const cards = kinds.map((tool, n) => ({ ...CARD, id: `k${n}`, tool, headline: `${tool} — x` }));
  cards[3] = { ...cards[3], warning: "re-verification RAN and FAILED", tamper: "TEST-TAMPER: tests changed" };
  cards[7] = { ...cards[7], items: ["read_url — https://a.dev", "read_url — https://b.dev"] };
  cards[8] = { ...cards[8], source: "operator" };
  const { rows } = await render(cards);
  rows.forEach((row, n) => {
    assert.equal(row.querySelectorAll("button").filter(b => b.textContent.startsWith("Approve")).length, 0, kinds[n]);
    const open = row.querySelector(".inbox-elsewhere");
    assert.equal(open.getAttribute("href"), "#/chat/c1");
    assert.equal(open.textContent, "Open the full card →");
    assert.ok(button(row, "Deny"), kinds[n]);
    assert.match(row.querySelector(".inbox-title").textContent, new RegExp(`^${kinds[n]} — x`));
  });
  assert.match(rows[3].querySelector(".banner").textContent, /V1 · TEST-TAMPER/);
  assert.match(rows[3].textContent, /⚠ re-verification RAN and FAILED/);
  assert.deepEqual(rows[7].querySelectorAll("li").map(li => li.textContent), ["read_url — https://a.dev", "read_url — https://b.dev"]);
});

test("R4 K1 (SD-2): Approve renders only from the server's inline_approve — never from the kind", async () => {
  const card = { ...CARD, inline_approve: true }; // no card route approves inline, whatever it claims
  const { rows } = await render([{ ...GATE, inline_approve: false }, { ...MEMORY, inline_approve: false }, card]);
  for (const row of rows) assert.equal(button(row, "Approve"), undefined, row.textContent);
  assert.deepEqual(rows.map(r => r.querySelector(".inbox-elsewhere").getAttribute("href")),
    ["#/review/r1", "#/memory", "#/chat/c1"]);
  for (const row of rows) assert.ok(button(row, "Deny"));
});

test("R4 K1: the Inbox header says what can be approved here", async () => {
  const ctx = inboxApp([GATE]);
  const said = [];
  ctx.app.header = (_main, title, line) => said.push([title, line]);
  await ctx.app.viewInbox(ctx.app.el("main"));
  assert.equal(said[0][0], "Inbox");
  assert.match(said[0][1], /worker gate or memory proposal/);
  assert.match(said[0][1], /chat cards and skills open where their full evidence is/);
  assert.doesNotMatch(said[0][1], /Approve or deny here/);
});

test("a memory deny needs a reason: cancelling the prompt sends nothing", async () => {
  const { app, rows, calls } = await render([MEMORY], { "POST /": {} });
  app.prompt = () => null;
  await button(rows[0], "Deny").click();
  assert.deepEqual(posts(calls), []);
});

test("SD-2b: every patch-landing gate — clean, warned or tampered — is reviewed on its page, never approved inline", async () => {
  const warned = { ...PATCH, id: "r3", warning: "the supervisor could not re-verify this run" };
  const tampered = { ...PATCH, id: "r4", tamper: "test-tamper guard: tests deleted" };
  const { rows } = await render([PATCH, warned, tampered, GATE]);
  assert.ok(button(rows[3], "Approve")); // a non-landing gate whose row is its whole evidence still is
  for (const [row, id] of [[rows[0], "r2"], [rows[1], "r3"], [rows[2], "r4"]]) {
    assert.equal(button(row, "Approve"), undefined);
    const review = row.querySelectorAll("a").find(a => a.textContent === "Review the diff →");
    assert.equal(review.getAttribute("href"), `#/review/${id}`);
    assert.ok(button(row, "Deny"));
  }
  assert.match(rows[1].querySelector(".review-warning").textContent, /^⚠ the supervisor could not re-verify/);
});

test("V1 tamper and taint are loud banners on the row", async () => {
  const { rows } = await render([
    { ...PATCH, tamper: "test-tamper guard: tests deleted" },
    { ...CARD, taint: "⚠ tainted — this chat read outside content" },
  ]);
  const tamper = rows[0].querySelector(".banner");
  assert.equal(tamper.className, "banner bad");
  assert.equal(tamper.getAttribute("role"), "alert");
  assert.match(tamper.textContent, /V1 · test-tamper guard: tests deleted/);
  assert.equal(rows[0].querySelector(".thread-dot").className, "thread-dot bad");
  const taint = rows[1].querySelector(".banner");
  assert.equal(taint.className, "banner warn");
  assert.match(taint.textContent, /⚠ tainted — this chat read outside content/);
});

test("a HIGH card, a tainted skill and an untested skill are approved where their evidence is", async () => {
  const { rows } = await render([
    { ...CARD, high: "it reshapes standing policy" },
    { ...SKILL, taint: "⚠ tainted — captured from chat:abc, which carries: outside content" },
    { ...SKILL, id: "learned-x", status: "draft", provenance: "learned", approvable: false },
  ]);
  const open = row => row.querySelectorAll("a").filter(a => a.classList.contains("inbox-elsewhere"))[0];
  assert.equal(button(rows[0], "Approve"), undefined);
  assert.equal(open(rows[0]).getAttribute("href"), "#/chat/c1");
  assert.equal(open(rows[0]).textContent, "Open the full card →");
  assert.match(rows[0].textContent, /HIGH · it reshapes standing policy/);
  assert.equal(button(rows[1], "Approve"), undefined);
  assert.equal(open(rows[1]).getAttribute("href"), "#/templates");
  assert.match(open(rows[1]).textContent, /accept the taint/);
  assert.equal(button(rows[2], "Approve"), undefined);
  assert.match(open(rows[2]).textContent, /Test it in the Library/);
  for (const row of rows) assert.ok(button(row, "Deny"));
});

test("a gate a chat card mirrors is one row, linking its review and naming the chat", async () => {
  const mirrored = { ...PATCH, mirrored_by: [{ chat_id: "c9", action_id: "x", tool: "approve_review" }] };
  const { rows } = await render([mirrored]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].querySelector(".inbox-open").getAttribute("href"), "#/review/r2");
  const chatLink = rows[0].querySelectorAll("a").find(a => a.getAttribute("href") === "#/chat/c9");
  assert.ok(chatLink);
  assert.match(rows[0].textContent, /also asked in 1 chat card — resolved with it/);
});

test("keys only move focus: j/k walk the rows and remember where you were; no key gives a verdict", async () => {
  const seen = [];
  const { app, main, calls } = await (async () => {
    const ctx = inboxApp([GATE, CARD]);
    Object.getPrototypeOf(ctx.app.el("div")).focus = function focus() { seen.push(this); };
    const main = ctx.app.el("main");
    await ctx.app.viewInbox(main);
    return { ...ctx, main };
  })();
  const links = main.querySelectorAll(".inbox-open");
  assert.equal(seen.at(-1), links[0]);
  const list = main.querySelector(".inbox-list");
  for (const key of ["a", "d", "Enter", "y"]) list.listeners.keydown[0]({ key, target: links[0], preventDefault() {} });
  list.listeners.keydown[0]({ key: "j", target: links[0], preventDefault() {} });
  assert.equal(seen.at(-1), links[1]);
  assert.deepEqual(posts(calls), []);
  const again = app.el("main");
  await app.viewInbox(again);
  assert.equal(seen.at(-1).getAttribute("href"), "#/chat/c1");
});

test("a failed verdict says why and leaves the row standing", async () => {
  const { app, rows, routed } = await render([GATE], {
    "POST /api/approvals/r1/approve": () => { throw new Error("no pending approval r1"); },
  });
  await button(rows[0], "Approve").click();
  assert.deepEqual(app.flashes.at(-1), ["bad", "no pending approval r1"]);
  assert.equal(routed(), 0);
  assert.equal(button(rows[0], "Approve").disabled, false);
});

test("nothing waiting says so", async () => {
  const { main } = await render([]);
  assert.equal(main.textContent, "Nothing is waiting on you.");
});

test("the badge counts the same queue the Inbox lists, and an open Inbox redraws when it changes", async () => {
  const items = [PATCH, RUN, GATE, CARD, MEMORY, SKILL]; // H1: a waiting run counts too
  const light = items.map(({ kind, id, boundary, at }) => ({ kind, id, boundary, at }));
  const { app, nodes, calls } = inboxApp(items, {
    "GET /api/review-queue?detail=false": queue(...light),
  });
  await app.refreshSidebar();
  assert.ok(calls.some(([, p]) => p === "/api/review-queue?detail=false"));
  assert.equal(nodes["inbox-badge"].textContent, "6");
  const main = app.el("main");
  await app.viewInbox(main);
  assert.equal(main.querySelectorAll(".inbox-item").length, Number(nodes["inbox-badge"].textContent));
});

// ---------- Phase 10 R2 ----------

test("H1: a run holding an unlanded patch or a G10 disagreement is a row that opens the run — no inline land", async () => {
  const { rows } = await render([RUN]);
  const row = rows[0];
  assert.equal(row.querySelector(".inbox-open").getAttribute("href"), "#/runs/fedcba9876543210");
  assert.equal(row.querySelector(".inbox-title").textContent, "NOT CONFIRMED · run fedcba98");
  assert.match(row.textContent, /completed — patch not landed/);
  assert.match(row.textContent, /2 files changed: app\.py \+2 −1, b\.py \+1 −0/);
  assert.match(row.textContent, /⚠ re-verification RAN and FAILED/);
  assert.equal(row.querySelectorAll("button").length, 0); // no Approve, no Deny: no gate exists yet
  const open = row.querySelectorAll("a").find(a => a.textContent === "Open to review →");
  assert.equal(open.getAttribute("href"), "#/runs/fedcba9876543210");
});

test("H2: a land_run card is approved in its thread, branch and all, its G10 and V1 on the row", async () => {
  const landing = { ...CARD, id: "L1", boundary: "patch-landing", tool: "land_run", headline: "land_run — run x", task_id: "fedcba9876543210", branch: "skep/maintain", warning: null };
  const { rows } = await render([
    landing,
    { ...landing, id: "L2", warning: "re-verification RAN and FAILED" },
    { ...landing, id: "L3", tamper: "TEST-TAMPER: the patch changes the tests", warning: "TEST-TAMPER: the patch changes the tests" },
  ]);
  for (const row of rows) {
    assert.equal(button(row, "Approve"), undefined);
    const open = row.querySelectorAll("a").find(a => a.classList.contains("inbox-elsewhere"));
    assert.equal(open.getAttribute("href"), "#/chat/c1");
    assert.ok(button(row, "Deny"));
    assert.match(row.textContent, /lands on skep\/maintain/);
  }
  assert.match(rows[1].textContent, /⚠ re-verification RAN and FAILED/);
  assert.match(rows[2].querySelector(".banner").textContent, /V1 · TEST-TAMPER/);
});

test("H3: a batch gate lists every command its approve grants and says 'Approve all N'; too long → its review", async () => {
  const batch = { ...GATE, action: "shell.run", reason: "command: npm install", commands: ["npm install", "npm run build"] };
  const long = { ...batch, id: "r9", commands: Array.from({ length: 11 }, (_, i) => `cmd ${i}`), inline_approve: false };
  const { rows, calls } = await render([batch, long], { "POST /": {} });
  assert.deepEqual(rows[0].querySelectorAll(".inbox-command").map(n => n.textContent), ["npm install", "npm run build"]);
  await button(rows[0], "Approve all 2").click();
  assert.deepEqual(posts(calls), [["/api/approvals/r1/approve", {}]]);
  assert.equal(rows[1].querySelectorAll("button").filter(b => b.textContent.startsWith("Approve")).length, 0);
  const review = rows[1].querySelectorAll("a").find(a => a.classList.contains("inbox-elsewhere"));
  assert.equal(review.getAttribute("href"), "#/review/r9");
  assert.match(review.textContent, /all 11 commands/);
  assert.ok(button(rows[1], "Deny"));
});

test("H4: a captured draft is approved from its SKILL.md in the Library, not from its description", async () => {
  const captured = { ...SKILL, id: "captured-one", status: "draft", provenance: "captured", approvable: true };
  const { rows } = await render([captured]);
  assert.equal(button(rows[0], "Approve"), undefined);
  const open = rows[0].querySelectorAll("a").find(a => a.classList.contains("inbox-elsewhere"));
  assert.equal(open.getAttribute("href"), "#/templates");
  assert.match(open.textContent, /SKILL\.md/);
  assert.ok(button(rows[0], "Deny"));
});

test("H5: a card the web cannot confirm is read-only with where to answer it; its cancel still works", async () => {
  const refusal = "/model is not available on web; it runs on cli — answer it in skep chat";
  const model = { ...CARD, id: "a7", source: "operator", tool: "set_assistant_model", headline: "set_assistant_model — m2", web_refusal: refusal };
  const { rows, calls } = await render([model], { "POST /": { ok: false, denied: true } });
  assert.equal(button(rows[0], "Approve"), undefined);
  assert.match(rows[0].textContent, /answer it in skep chat/);
  await button(rows[0], "Deny").click();
  assert.deepEqual(posts(calls), [["/api/chats/c1/commands/a7/deny", undefined]]);
});

test("H6: a verdict reports what really happened — ok:false, a warning, a failed or silent stream", async () => {
  const operator = { ...CARD, id: "a2", source: "operator", tool: "setup_project", headline: "setup_project — app" };
  const silent = { ...CARD, id: "a4" };
  const { app, rows, routed } = await render([GATE, operator, CARD, silent], {
    "POST /api/approvals/r1/approve": { action: "resumed", warning: "the base moved" },
    "POST /api/chats/c1/commands/a2/deny": { ok: false, error: "no such card" },
    sse: {
      "/api/chats/c1/actions/a1/deny": [["error", { detail: "card already resolved" }]],
      "/api/chats/c1/actions/a4/deny": [["done", {}]],
    },
  });
  await button(rows[0], "Approve").click();
  const flashes = [app.flashes.at(-1)];
  for (const row of rows.slice(1)) { await button(row, "Deny").click(); flashes.push(app.flashes.at(-1)); }
  assert.equal(flashes[0][0], "warn");
  assert.match(flashes[0][1], /approved network\.fetch · run abcdef12 — ⚠ the base moved/);
  assert.deepEqual(flashes[1], ["bad", "setup_project — app: no such card"]);
  assert.deepEqual(flashes[2], ["bad", "read_url — https://x.dev: card already resolved"]);
  assert.deepEqual(flashes[3], ["bad", "read_url — https://x.dev: no outcome came back — open its thread to see what happened"]);
  assert.equal(routed(), 4); // each was answered — the list redraws to say so
});

test("R4 K7: the operation's own outcome decides the flash — opened/merged/ok false is never green", async () => {
  const { app } = await render([]);
  assert.equal(app.outcomeTrouble({ ok: true, result: { opened: true, url: "u" } }), null);
  assert.equal(app.outcomeTrouble({ ok: false, denied: true }), null);
  assert.equal(app.outcomeTrouble({ ok: true, result: { opened: false, detail: "no remote" } }),
    "partial — the PR was not opened: no remote");
  assert.equal(app.outcomeTrouble({ ok: true, result: { merged: false, detail: "checks failing" } }),
    "partial — not merged: checks failing");
  assert.equal(app.outcomeTrouble({ ok: true, result: { ok: false, error: "push refused" } }), "push refused");
  assert.equal(app.outcomeTrouble({ ok: false, error: "boom" }), "boom");
  const { app: inbox, rows } = await render([GATE], {
    "POST /api/approvals/r1/approve": { ok: true, result: { opened: false, detail: "no remote" } },
  });
  await button(rows[0], "Approve").click();
  assert.deepEqual(inbox.flashes.at(-1), ["bad", "network.fetch · run abcdef12: partial — the PR was not opened: no remote"]);
});

test("R4 T1: a stream that answers fewer items than the card holds says it is incomplete", async () => {
  const fold = { ...CARD, id: "F1", items: ["read_url — https://x.dev", "read_url — https://y.dev"] };
  const { app, rows } = await render([fold], {
    sse: { "/api/chats/c1/actions/F1/deny": [["tool", { result: { ok: false, denied: true } }], ["done", {}]] },
  });
  await button(rows[0], "Deny").click();
  assert.deepEqual(app.flashes.at(-1), ["bad", "read_url — https://x.dev: incomplete: 1 of 2 answered"]);
});

test("H7: a queue that arrives after the operator moved on draws nothing and takes no focus", async () => {
  const seen = [];
  const ctx = inboxApp([GATE]);
  Object.getPrototypeOf(ctx.app.el("div")).focus = function focus() { seen.push(this); };
  const main = ctx.app.el("main");
  main.dataset.route = "1";
  ctx.app.api = async () => { main.dataset.route = "2"; return queue(GATE); };
  await ctx.app.viewInbox(main);
  assert.equal(main.querySelectorAll(".inbox-item").length, 0);
  assert.deepEqual(seen, []);
});

test("H7: several mirroring cards are separated by commas", async () => {
  const mirrored = { ...PATCH, mirrored_by: [
    { chat_id: "c8", action_id: "x", tool: "approve_review" }, { chat_id: "c9", action_id: "y", tool: "land_run" }] };
  const { rows } = await render([mirrored]);
  assert.match(rows[0].textContent, /resolved with it: approve_review, land_run/);
});

test("R3 J1: an open_pr card is approved in its thread, whatever its wall or evidence", async () => {
  const pr = { ...CARD, id: "P1", boundary: "outbound-content", tool: "open_pr", headline: "open_pr — run x", task_ids: ["fedcba9876543210"], warning: null };
  const { rows } = await render([
    pr,
    { ...pr, id: "P2", warning: "re-verification RAN and FAILED" },
    { ...pr, id: "P3", tamper: "TEST-TAMPER: the patch changes the tests" },
  ]);
  for (const row of rows) {
    assert.equal(button(row, "Approve"), undefined);
    assert.equal(row.querySelector(".inbox-elsewhere").textContent, "Open the full card →");
    assert.ok(button(row, "Deny"));
  }
});

test("R3 J2: a folded card lists every item (past 10, how many) and is approved in its thread", async () => {
  const urls = n => Array.from({ length: n }, (_, i) => `read_url — https://x.dev/${i}`);
  const fold = { ...CARD, id: "F1", headline: "read_url — https://x.dev/0 (+2 more)", items: urls(3) };
  const big = { ...CARD, id: "F2", headline: "read_url — https://x.dev/0 (+10 more)", items: urls(11) };
  const { rows, calls } = await render([fold, big]);
  assert.deepEqual(rows[0].querySelectorAll("li").map(li => li.textContent), urls(3));
  for (const row of rows) assert.equal(row.querySelectorAll("button").filter(b => b.textContent.startsWith("Approve")).length, 0);
  assert.match(rows[1].textContent, /11 items — every one runs when the card is approved/);
  await button(rows[0], "Deny").click();
  await flush();
  assert.deepEqual(posts(calls), [["/api/chats/c1/actions/F1/deny", {}]]);
});

test("R3 J5: only the card's verdict events count; the Queen's follow-up never turns a verdict red", async () => {
  const land = { ...CARD, id: "L1", tool: "land_run", headline: "land_run — run x", boundary: "patch-landing" };
  const llm = { ...land, id: "L2" };
  const denied = { ok: false, denied: true, note: "the user denied this action" };
  const { app, rows } = await render([land, llm], {
    sse: {
      // the verdict, then the continuation: the Queen calls get_run, which "fails"
      "/api/chats/c1/actions/L1/deny": [
        ["tool", { tool: "land_run", result: denied }],
        ["tool", { tool: "get_run", args: { task_id: "x" }, result: { ok: false, error: "no run" } }],
        ["done", {}]],
      "/api/chats/c1/actions/L2/deny": [
        ["tool", { tool: "land_run", result: denied }],
        ["error", { detail: "model offline" }]],
    },
  });
  const flashes = [];
  for (const row of rows) { await button(row, "Deny").click(); flashes.push(app.flashes.at(-1)); }
  assert.deepEqual(flashes[0], ["ok", "denied land_run — run x"]);
  assert.deepEqual(flashes[1], ["warn", "denied land_run — run x — ⚠ the Queen's follow-up failed: model offline"]);
});
