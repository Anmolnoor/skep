// Phase 10 R1 (E2): the Inbox is ONE review queue (GET /api/review-queue) —
// gates, standalone cards, memory proposals and skills, grouped by the wall
// each crosses, riskiest first — and the sidebar badge counts that same queue.
// Each row carries Approve and Deny buttons wired to the item's existing
// route; keys only move focus (I7).
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const AT = "2026-11-24T08:00:00Z";
const GATE = { kind: "gate", id: "r1", boundary: "network-egress", at: AT, task_id: "abcdef1234567890", action: "network.fetch", reason: "pypi.org", warning: null, tamper: null, stale_base: null, taint: null, mirrored_by: [] };
const PATCH = { ...GATE, id: "r2", boundary: "patch-landing", action: "apply_patch", reason: "land it" };
const CARD = { kind: "card", id: "a1", boundary: "network-egress", at: AT, chat_id: "c1", chat_title: "flaky monitor", tool: "read_url", source: "assistant", headline: "read_url — https://x.dev", risk: null, tamper: null, taint: null, high: null };
const MEMORY = { kind: "memory", id: "m1", boundary: "policy-change", at: AT, memory_class: "project_fact", content: "uses uv", taint: null };
const SKILL = { kind: "skill", id: "captured-one", boundary: "policy-change", at: AT, status: "draft", provenance: "captured", description: "captured procedure: x", source: "chat:abc", approvable: true, taint: null };
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
const posts = calls => calls.filter(([m]) => m === "POST").map(([, p, b]) => [p, b === undefined ? undefined : JSON.parse(JSON.stringify(b))]);

test("the Inbox renders the one queue in its order, grouped by wall, each row linking where it lives", async () => {
  const { main, rows } = await render([PATCH, GATE, CARD, MEMORY, SKILL]);
  assert.deepEqual(texts(main, "inbox-group"), ["patch-landing", "network-egress", "policy-change"]);
  assert.deepEqual(rows.map(r => r.querySelector(".inbox-open").getAttribute("href")),
    ["#/review/r2", "#/review/r1", "#/chat/c1", "#/memory", "#/templates"]);
  assert.deepEqual(texts(main, "inbox-title"), [
    "apply_patch · run abcdef12", "network.fetch · run abcdef12", "read_url — https://x.dev",
    "memory · project_fact", "skill · captured-one"]);
  assert.match(rows[2].querySelector("small").textContent, /^flaky monitor · proposed by the Queen · /);
  assert.match(rows[3].querySelector("small").textContent, /^uses uv · /);
  assert.match(main.querySelector(".inbox-heading").textContent, /Waiting on you · 5/);
});

test("every row has Approve and Deny, each wired to the item's existing route", async () => {
  const operatorCard = { ...CARD, id: "a2", source: "operator", tool: "setup_project", headline: "setup_project — app" };
  const { app, rows, calls, routed } = await render([GATE, CARD, operatorCard, MEMORY, SKILL], {
    "POST /": {},
  });
  for (const row of rows) {
    assert.ok(button(row, "Approve"), row.textContent);
    assert.ok(button(row, "Deny"), row.textContent);
  }
  for (const row of rows) await button(row, "Approve").click();
  assert.deepEqual(posts(calls), [
    ["/api/approvals/r1/approve", {}],
    ["/api/chats/c1/actions/a1/confirm", { keep: "once" }], // an inline approve keeps nothing
    ["/api/chats/c1/commands/a2/confirm", undefined],
    ["/api/memory/proposals/m1/approve", undefined],
    ["/api/skills/captured-one/approve", {}],
  ]);
  assert.equal(routed(), 5); // the list re-renders after each verdict
  assert.match(app.flashes.at(-1)[1], /approved skill · captured-one/);
  calls.length = 0;
  app.prompt = () => "not now";
  for (const row of rows) await button(row, "Deny").click();
  assert.deepEqual(posts(calls), [
    ["/api/approvals/r1/deny", {}],
    ["/api/chats/c1/actions/a1/deny", undefined],
    ["/api/chats/c1/commands/a2/deny", undefined],
    ["/api/memory/proposals/m1/reject", { reason: "not now" }],
    ["/api/skills/captured-one/reject", {}],
  ]);
});

test("a memory deny needs a reason: cancelling the prompt sends nothing", async () => {
  const { app, rows, calls } = await render([MEMORY], { "POST /": {} });
  app.prompt = () => null;
  await button(rows[0], "Deny").click();
  assert.deepEqual(posts(calls), []);
});

test("a patch-landing row with a G10 or V1 warning goes to its review page — no inline approve", async () => {
  const warned = { ...PATCH, warning: "the supervisor could not re-verify this run" };
  const tampered = { ...PATCH, id: "r3", tamper: "test-tamper guard: tests deleted" };
  const { rows } = await render([PATCH, warned, tampered]);
  assert.ok(button(rows[0], "Approve"));
  for (const [row, id] of [[rows[1], "r2"], [rows[2], "r3"]]) {
    assert.equal(button(row, "Approve"), undefined);
    const review = row.querySelectorAll("a").find(a => a.textContent === "Review →");
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
  const items = [PATCH, GATE, CARD, MEMORY, SKILL];
  const light = items.map(({ kind, id, boundary, at }) => ({ kind, id, boundary, at }));
  const { app, nodes, calls } = inboxApp(items, {
    "GET /api/review-queue?detail=false": queue(...light),
  });
  await app.refreshSidebar();
  assert.ok(calls.some(([, p]) => p === "/api/review-queue?detail=false"));
  assert.equal(nodes["inbox-badge"].textContent, "5");
  const main = app.el("main");
  await app.viewInbox(main);
  assert.equal(main.querySelectorAll(".inbox-item").length, Number(nodes["inbox-badge"].textContent));
});
