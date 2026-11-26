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

async function approvalsCard(request, posts) {
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
  return main.querySelector(".actions");
}

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

// ---------- the run page ----------

async function runActions(request, posts) {
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
  return main.querySelectorAll(".actions").find(row => button(row, "Approve"));
}

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

// ---------- the Inbox ----------

const GATE = { kind: "gate", id: "r1", boundary: "network-egress", at: AT, task_id: "abcdef1234567890", action: "network.fetch", reason: "pypi.org", warning: null, tamper: null, stale_base: null, taint: null, mirrored_by: [], inline_approve: true };

async function inboxRow(request, posts) {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.location.hash = "#/inbox";
  app.header = () => {};
  app.api = async (method, path) => {
    if (method === "POST") { posts.push(path); return request.promise; }
    if (path === "/api/review-queue") return { items: [GATE], count: 1 };
    if (path === "/api/chats") return { chats: [] };
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  const main = app.el("main");
  await app.viewInbox(main);
  return main.querySelector(".inbox-actions");
}

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
