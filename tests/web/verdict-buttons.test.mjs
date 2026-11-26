// v134-F21: one verdict per card. The live test saw one card take three
// clicks, answered 200, 409, 409: the approval cards (Approvals view, run
// detail) and the Inbox rows disabled only the button pressed, so its
// siblings stayed live while the request was in flight. Every verdict row now
// holds all of its buttons on the first click (holdButtons, js/dom.js); a
// failed request lets them go so the operator can retry, a success keeps them
// held until the card redraws.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadApp } from "./harness.mjs";

const DECK = JSON.parse(readFileSync(new URL("./fixtures/commands-web.json", import.meta.url), "utf8"));
const AT = "2026-11-24T10:00:00Z";

// A request the test answers by hand, so a second click lands mid-flight.
function inFlight() {
  let settle;
  const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  return { promise, ...settle };
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const button = (root, label) => root.querySelectorAll("button").find(b => b.textContent === label);

// Press `first`, then `second` while the first is in flight; then settle the
// request with `outcome` and return what was posted and the buttons' state.
async function doubleClick(root, first, second, request, outcome) {
  const pressed = button(root, first).click();
  await tick();
  const again = button(root, second).click();
  await tick();
  const during = root.querySelectorAll("button").map(b => b.disabled);
  if (outcome === "ok") request.resolve({});
  else request.reject(Object.assign(new Error("server unreachable"), { status: 502 }));
  await Promise.all([pressed, again]);
  return { during, after: root.querySelectorAll("button").map(b => b.disabled) };
}

test("holdButtons disables every button in the row and its release turns them back on", () => {
  const app = loadApp();
  const row = app.el("div", {}, app.el("button", {}, "Approve"), app.el("button", {}, "Deny"));
  const release = app.holdButtons(row);
  assert.deepEqual(row.querySelectorAll("button").map(b => b.disabled), [true, true]);
  release();
  assert.deepEqual(row.querySelectorAll("button").map(b => b.disabled), [false, false]);
});

// ---------- the Approvals view ----------

const APPROVAL = {
  review_id: "r1", task_id: "abcdef1234567890", action: "network.fetch", reason: "pypi.org",
  status: "pending", requested_at: AT, run: { state: "pending_approval", instructions: "go" },
};

async function approvalsView(request, posts) {
  const app = loadApp();
  app.header = () => {};
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/approvals") return { approvals: [APPROVAL] };
    if (path === "/api/ledger/suggestions") return { suggestions: [] };
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  const main = app.el("main");
  await app.viewApprovals(main);
  return { app, main };
}
const approvalsCard = async (request, posts) =>
  (await approvalsView(request, posts)).main.querySelector(".actions");

test("Approvals: Deny pressed while Approve is in flight sends nothing", async () => {
  const request = inFlight();
  const posts = [];
  const row = await approvalsCard(request, posts);
  const { during, after } = await doubleClick(row, "Approve", "Deny", request, "ok");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(during.every(Boolean), "every verdict button is held while the request runs");
  assert.ok(after.every(Boolean), "a success keeps them held until the card redraws");
});

test("Approvals: a failed verdict gives every button back for a retry", async () => {
  const request = inFlight();
  const posts = [];
  const row = await approvalsCard(request, posts);
  const { after } = await doubleClick(row, "Approve", "Approve", request, "fail");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(after.every(b => b === false), after);
});

// v134-F21 r1 (review): a redraw mid-request — a filter switch, a poll's
// route() — drew fresh, enabled buttons, and a second verdict went out. The
// in-flight verdict is kept by its card's key, so a card drawn meanwhile is
// drawn held; a failure gives the card drawn last its buttons back.
test("Approvals: a filter switch while Approve is in flight draws the card held", async () => {
  const request = inFlight();
  const posts = [];
  const { main } = await approvalsView(request, posts);
  const pressed = button(main.querySelector(".actions"), "Approve").click();
  await tick();
  await main.querySelectorAll(".filter-tab").find(tab => tab.textContent.startsWith("Other")).click();
  const fresh = main.querySelector(".actions");
  assert.ok(fresh.querySelectorAll("button").every(b => b.disabled), "the redrawn card is held");
  await button(fresh, "Deny").click();
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  request.reject(Object.assign(new Error("server unreachable"), { status: 502 }));
  await pressed;
  assert.ok(fresh.querySelectorAll("button").every(b => !b.disabled), "a failure frees the card drawn last");
  await button(fresh, "Deny").click();
  assert.deepEqual(posts, ["/api/approvals/r1/approve", "/api/approvals/r1/deny"]);
});

test("Approvals: a route() redraw while a verdict is in flight draws the card held", async () => {
  const request = inFlight();
  const posts = [];
  const { app, main } = await approvalsView(request, posts);
  const pressed = button(main.querySelector(".actions"), "Approve").click();
  await tick();
  main.replaceChildren();
  await app.viewApprovals(main); // the poll's route(): the pending count moved
  assert.ok(main.querySelector(".actions").querySelectorAll("button").every(b => b.disabled));
  request.resolve({});
  await pressed;
  main.replaceChildren();
  await app.viewApprovals(main); // landed: the next draw is not held by it
  assert.ok(main.querySelector(".actions").querySelectorAll("button").every(b => !b.disabled));
});

// ---------- the run page ----------

async function runView(request, posts) {
  const app = loadApp();
  app.liveSource = () => ({ close() {} });
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/runs/t1") {
      return {
        run: { task_id: "t1", instructions: "go", state: "pending_approval", repo: "/r" },
        transitions: [], artifacts: [], commands: [], policy_blocks: [],
        approvals: [{ ...APPROVAL, task_id: "t1" }],
      };
    }
    return { events: [] };
  };
  const main = app.el("main");
  await app.viewRunDetail(main, "t1");
  return { app, main };
}
const runRow = main => main.querySelectorAll(".actions").find(row => button(row, "Approve"));
const runActions = async (request, posts) => runRow((await runView(request, posts)).main);

test("run page: Deny pressed while Approve is in flight sends nothing", async () => {
  const request = inFlight();
  const posts = [];
  const row = await runActions(request, posts);
  const { during } = await doubleClick(row, "Approve", "Deny", request, "ok");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(during.every(Boolean), during);
});

test("run page: a failed verdict gives every button back for a retry", async () => {
  const request = inFlight();
  const posts = [];
  const row = await runActions(request, posts);
  const { after } = await doubleClick(row, "Approve", "Deny", request, "fail");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(after.every(b => b === false), after);
});

test("run page: a redraw while a verdict is in flight draws its buttons held", async () => {
  const request = inFlight();
  const posts = [];
  const { app, main } = await runView(request, posts);
  const pressed = button(runRow(main), "Approve").click();
  await tick();
  main.replaceChildren();
  await app.viewRunDetail(main, "t1"); // the event stream's done → route()
  const fresh = runRow(main);
  assert.ok(fresh.querySelectorAll("button").every(b => b.disabled), "the redrawn row is held");
  await button(fresh, "Deny").click();
  request.reject(Object.assign(new Error("server unreachable"), { status: 502 }));
  await pressed;
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(fresh.querySelectorAll("button").every(b => !b.disabled), "a failure frees the row drawn last");
});

test("run page: a gate the first click opened keeps the row held across a redraw", async () => {
  // v134-F21 r3 (Sol): with no gate open the row was held as run:<id>; the
  // click opens gate r9, and a redraw then keyed it gate:r9 — drawn live
  const request = inFlight();
  const posts = [];
  let gateOpen = false;
  const app = loadApp();
  app.liveSource = () => ({ close() {} });
  app.api = async (method, path) => {
    if (method === "POST" && path === "/api/runs/t1/approvals") {
      gateOpen = true;
      return { review_id: "r9" };
    }
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/runs/t1") {
      return {
        run: { task_id: "t1", instructions: "go", state: "completed", repo: "/r" },
        transitions: [], artifacts: [], commands: [], policy_blocks: [],
        approvals: gateOpen ? [{ ...APPROVAL, review_id: "r9", task_id: "t1" }] : [],
      };
    }
    return { events: [] };
  };
  const main = app.el("main");
  await app.viewRunDetail(main, "t1");
  const pressed = button(runRow(main), "Approve").click();
  await tick();
  await tick();
  main.replaceChildren();
  await app.viewRunDetail(main, "t1");
  const fresh = runRow(main);
  assert.ok(fresh.querySelectorAll("button").every(b => b.disabled), "the redrawn row is held");
  await button(fresh, "Approve").click();
  request.reject(Object.assign(new Error("server unreachable"), { status: 502 }));
  await pressed;
  assert.deepEqual(posts, ["/api/approvals/r9/approve"]);
  assert.ok(fresh.querySelectorAll("button").every(b => !b.disabled), "a failure frees the row drawn last");
});

// ---------- the Inbox ----------

const GATE = { kind: "gate", id: "r1", boundary: "network-egress", at: AT, task_id: "abcdef1234567890", action: "network.fetch", reason: "pypi.org", warning: null, tamper: null, stale_base: null, taint: null, mirrored_by: [], inline_approve: true };

async function inboxView(request, posts) {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.location.hash = "#/inbox";
  app.header = () => {};
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/review-queue") return { items: [GATE], count: 1 };
    if (path === "/api/chats") return { chats: [] };
    // the review page of the same gate
    if (path === "/api/approvals") return { approvals: [{ ...APPROVAL, task_id: GATE.task_id }] };
    if (path === `/api/runs/${GATE.task_id}`) return { run: { state: "pending_approval", instructions: "go" }, artifacts: [] };
    if (path === `/api/runs/${GATE.task_id}/forensics`) return { error: "not stubbed" };
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  const main = app.el("main");
  await app.viewInbox(main);
  return { app, main };
}
const inboxRow = async (request, posts) =>
  (await inboxView(request, posts)).main.querySelector(".inbox-actions");

test("Inbox: Deny pressed while Approve is in flight sends nothing", async () => {
  const request = inFlight();
  const posts = [];
  const row = await inboxRow(request, posts);
  const { during } = await doubleClick(row, "Approve", "Deny", request, "ok");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(during.every(Boolean), during);
});

test("Inbox: a failed verdict gives every button back for a retry", async () => {
  const request = inFlight();
  const posts = [];
  const row = await inboxRow(request, posts);
  const { after } = await doubleClick(row, "Approve", "Deny", request, "fail");
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(after.every(b => b === false), after);
});

test("Inbox: another row's verdict redrawing the list draws an in-flight row held", async () => {
  const request = inFlight();
  const posts = [];
  const { app, main } = await inboxView(request, posts);
  const pressed = button(main.querySelector(".inbox-actions"), "Approve").click();
  await tick();
  main.replaceChildren();
  await app.viewInbox(main); // route() after another row settled
  const fresh = main.querySelector(".inbox-actions");
  assert.ok(fresh.querySelectorAll("button").every(b => b.disabled), "the redrawn row is held");
  await button(fresh, "Deny").click();
  request.reject(Object.assign(new Error("server unreachable"), { status: 502 }));
  await pressed;
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
  assert.ok(fresh.querySelectorAll("button").every(b => !b.disabled), "a failure frees the row drawn last");
});

test("Inbox → review page: the same gate's verdict in flight holds its review buttons", async () => {
  const request = inFlight();
  const posts = [];
  const { app, main } = await inboxView(request, posts);
  const pressed = button(main.querySelector(".inbox-actions"), "Approve").click();
  await tick();
  const review = app.el("main");
  await app.viewReview(review, "r1");
  const bar = review.querySelector(".review-verdict");
  assert.ok(bar.querySelectorAll("button").every(b => b.disabled), "the review page draws the gate held");
  await button(bar, "Deny").click();
  request.resolve({});
  await pressed;
  assert.deepEqual(posts, ["/api/approvals/r1/approve"]);
});

// ---------- a chat card ----------

const ROLE_CARD = { action_id: "a-role", chat_id: "c1", tool: "set_role", args: { name: "x" },
  status: "proposed", source: "operator", card: { headline: "switch role" } };

async function chatCard(request, posts) {
  const app = loadApp();
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/commands") return DECK;
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") return { chat: { chat_id: "c1", title: "t" }, messages: [], actions: [ROLE_CARD] };
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path.startsWith("/api/status")) return {};
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.renderThreads = () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.watchStatus = () => {};
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  return main.querySelector(".command-card").querySelector(".actions");
}

function chatApp(request, posts, card = ROLE_CARD) {
  const app = loadApp();
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/commands") return DECK;
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") return { chat: { chat_id: "c1", title: "t" }, messages: [], actions: [card] };
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path.startsWith("/api/status")) return {};
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.renderThreads = () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.watchStatus = () => {};
  return app;
}

async function chatRow(app) {
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  return main.querySelector(".command-card").querySelector(".actions");
}

for (const [name, card, label] of [
  ["chat card", ROLE_CARD, "Confirm"],
  ["answer-elsewhere card", { ...ROLE_CARD, web_refusal: "answer it in skep chat" }, "Cancel"],
]) {
  test(`${name}: a chat redrawn while its verdict is in flight draws the card held`, async () => {
    // v134-F21 r2/r3 (Sol, Terra): reopening the chat mid-request drew live buttons
    const request = inFlight();
    const posts = [];
    const app = chatApp(request, posts, card);
    const pressed = button(await chatRow(app), label).click();
    await tick();
    const row = await chatRow(app);
    assert.ok(row.querySelectorAll("button").every(b => b.disabled), "the redrawn card is held");
    await button(row, "Cancel").click();
    request.reject(new Error("refused"));
    await pressed;
    assert.equal(posts.length, 1, posts);
    assert.ok(row.querySelectorAll("button").every(b => !b.disabled), "a failure gives the buttons back");
  });
}

test("chat card: a verdict in flight from the Inbox draws the chat's card held", async () => {
  // v134-F21 r3 (Opus): the Inbox keys a chat card card:<action_id>; so does the chat
  const app = chatApp(inFlight(), []);
  app.holdVerdict("card:a-role", app.el("div"));
  const row = await chatRow(app);
  assert.ok(row.querySelectorAll("button").every(b => b.disabled));
});

test("chat card: a double click on Confirm sends one request", async () => {
  const request = inFlight();
  const posts = [];
  const row = await chatCard(request, posts);
  const { during } = await doubleClick(row, "Confirm", "Confirm", request, "ok");
  assert.deepEqual(posts, ["/api/chats/c1/commands/a-role/confirm"]);
  assert.ok(during.every(Boolean), during);
});

test("chat card: a failed confirm gives Confirm and Cancel back", async () => {
  const request = inFlight();
  const posts = [];
  const row = await chatCard(request, posts);
  const { after } = await doubleClick(row, "Confirm", "Cancel", request, "fail");
  assert.deepEqual(posts, ["/api/chats/c1/commands/a-role/confirm"]);
  assert.ok(after.every(b => b === false), after);
});
