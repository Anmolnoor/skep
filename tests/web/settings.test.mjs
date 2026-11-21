// Graphite round A: Settings as five pages, Setup as one checklist
// (settings.js), executed. graphite-nav: #/settings/<tab> renders the one page
// it names; the sidebar's Settings surface is the list. Run: node --test tests/web/
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const byText = (root, tag, text) =>
  root.querySelectorAll(tag).find(n => n.textContent === text);
const byPlaceholder = (root, placeholder) =>
  root.querySelectorAll("input").find(n => n.getAttribute("placeholder") === placeholder);
// A page's forms load their own reads after the view resolves.
const settle = async () => { for (let i = 0; i < 5; i += 1) await new Promise(r => setTimeout(r, 0)); };

// ---------- the Setup checklist (moved from app.test.mjs's O3 onboard card) ----------

const OFFER = { path: "/w", project_id: "w", on: false, asked: false, offer: true, reason: null };

function onboardApp(onboard, calls = [], unlocked = OFFER) {
  const app = loadApp();
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    if (method === "GET" && path === "/api/onboard") return onboard;
    if (method === "GET" && path === "/api/onboard/unlocked") return unlocked;
    if (method === "GET" && path === "/api/onboard/check") return { ready: false, report: "step ② missing" };
    if (method === "GET" && path.startsWith("/api/onboard/template?")) {
      return { locked: false, table: "| rule | verdict |", sha256: "abc123", tightened: [], diff: [] };
    }
    if (method === "POST") return {};
    throw new Error(`unexpected ${method} ${path}`);
  };
  return app;
}

test("checklist: five rows in order, done steps show their record, open steps say so", async () => {
  const app = onboardApp({
    relock: null,
    steps: {
      model: { provider: "ollama", model: "qwen3:14b", egress: "local only" },
      template: { template: "builder", sha256: "0123456789abcdef" },
    },
  });
  const list = await app.onboardChecklist();
  assert.deepEqual(texts(list, "setup-label"),
    ["Model", "Work folder", "Sandbox", "Policy baseline", "Role", "Work without asking"]);
  assert.deepEqual(texts(list, "setup-state"), [
    "done: ollama qwen3:14b — local only",
    "not done yet",
    "not done yet",
    "done: builder locked, sha256 0123456789ab",
    "not done yet",
    "off: say yes and, inside the sandbox, skep starts runs, answers routine command questions "
      + "and lands patches its own re-run of your tests confirmed, on skep/* branches. Merging to "
      + "main, pushes, PRs, messages, policy changes and new network hosts still ask you.",
  ]);
  // Status is never colour alone: the row's word says it, the mark is decoration.
  const marks = list.querySelectorAll(".setup-mark");
  assert.ok(marks.every(m => m.getAttribute("aria-hidden") === "true"));
});

test("checklist: a sandbox switch after the lock says why the baseline re-opened (O3-F1)", async () => {
  const app = onboardApp({ relock: "builder", steps: {} });
  const list = await app.onboardChecklist();
  assert.ok(texts(list, "setup-state").includes(
    "re-lock needed: the locked baseline is not what builder locks under this sandbox"));
  assert.ok(byText(list, "button", "Re-lock"));
});

test("checklist: each row's one button opens that step's form in place, and closes it", async () => {
  const app = onboardApp({ relock: null, steps: { role: { role: "builder" } } });
  const list = await app.onboardChecklist();
  const steps = list.querySelectorAll(".setup-step").slice(0, 5);
  for (const step of steps) {
    assert.equal(step.querySelectorAll("button").filter(b => b.classList.contains("setup-toggle")).length, 1);
    assert.ok(step.querySelector(".setup-form").classList.contains("hidden"));
  }
  const role = steps[4];
  const toggle = role.querySelector(".setup-toggle");
  assert.equal(toggle.textContent, "Change"); // done: change it, not set it up
  assert.equal(toggle.getAttribute("aria-label"), "Change Role");
  await toggle.click();
  assert.ok(!role.querySelector(".setup-form").classList.contains("hidden"));
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  await toggle.click();
  assert.ok(role.querySelector(".setup-form").classList.contains("hidden"));
  assert.equal(steps[0].querySelector(".setup-toggle").textContent, "Set up");
});

test("checklist: the model step can record the model Settings → Models already has", async () => {
  const calls = [];
  const app = onboardApp({ relock: null, steps: {} }, calls);
  const list = await app.onboardChecklist();
  await byText(list, "button", "Record the current model").click();
  // An empty body is the server's keep path (L2): test what is set, change nothing.
  assert.deepEqual(JSON.parse(JSON.stringify(calls.filter(([m]) => m === "POST"))),
    [["POST", "/api/onboard/model", {}]]);
  // The typed path stays, with its safety text.
  assert.ok(byPlaceholder(list, "env var NAME, never the key"));
});

test("checklist: lock needs a preview and a typed yes, then posts the previewed sha", async () => {
  const calls = [];
  const app = onboardApp({ relock: null, steps: {} }, calls);
  const card = await app.onboardChecklist();
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
    { template: "builder", expect_sha256: "abc123", approved_by: "web" }]]);
});

test("checklist: changing the template throws away the preview", async () => {
  const app = onboardApp({ relock: null, steps: {} });
  const card = await app.onboardChecklist();
  const lock = byText(card, "button", "Lock baseline");
  await byText(card, "button", "Preview").click();
  const template = card.querySelectorAll("select").find(s => s.value === "builder");
  template.value = "operator";
  for (const fn of template.listeners.change) fn();
  assert.equal(lock.disabled, true);
});

test("checklist: work without asking is one click when offered, and says why when not (D47)", async () => {
  const calls = [];
  const app = onboardApp({ relock: null, steps: {} }, calls);
  const list = await app.onboardChecklist();
  await byText(list, "button", "Turn on").click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.filter(([m]) => m === "POST"))),
    [["POST", "/api/onboard/unlocked", { on: true, path: "/w" }]]);

  const refused = { ...OFFER, offer: false, reason: "register a work directory first (step ②)" };
  const blocked = await onboardApp({ relock: null, steps: {} }, [], refused).onboardChecklist();
  assert.equal(byText(blocked, "button", "Turn on"), undefined);
  assert.equal(texts(blocked, "setup-state").at(-1),
    "not offered: register a work directory first (step ②)");
  const on = await onboardApp({ relock: null, steps: {} }, [], { ...OFFER, on: true, offer: false })
    .onboardChecklist();
  assert.equal(texts(on, "setup-state").at(-1), "on for /w");
});

test("checklist: Check everything runs the onboard check and shows its report", async () => {
  const app = onboardApp({ relock: null, steps: {} });
  const list = await app.onboardChecklist();
  await byText(list, "button", "Check everything").click();
  assert.ok(texts(list, "onboard-table").includes("step ② missing"));
  assert.deepEqual(app.flashes.at(-1), ["bad", "not ready yet"]);
});

// ---------- the older setup flag: one line, never a card that contradicts ----------

function setupApp(setup, calls = []) {
  const app = onboardApp({ relock: null, steps: {} }, calls);
  const onboard = app.api;
  app.api = async (method, path, body) => {
    if (path === "/api/setup/status") return setup;
    if (method === "POST" && path === "/api/setup/complete") {
      calls.push([method, path]);
      return { ...setup, complete: true, marked_complete: true };
    }
    return onboard(method, path, body);
  };
  return app;
}

const SETUP = {
  complete: true, marked_complete: true, completed_at: "2026-07-08T09:00:00Z", missing: [],
  default_workspace: { path: "/home/op/workspace" }, workspace_project: { ready: true },
};

test("older flag: complete is one plain line with its date, then the checklist", async () => {
  const app = setupApp(SETUP);
  const panel = app.el("div");
  await app.renderSetupTab(panel);
  assert.deepEqual(texts(panel, "setup-flag"), ["The older setup flag: complete since 2026-07-08 09:00:00."]);
  assert.equal(panel.querySelectorAll(".card").length, 0); // no "Ready" card above "not done yet"
  assert.equal(panel.querySelectorAll(".setup-step").length, 6); // five steps + work without asking (D47)
  assert.equal(byText(panel, "button", "Finish setup"), undefined);
});

test("older flag: each missing item names where it is set and opens that page's route", async () => {
  const opened = [];
  const app = setupApp({ ...SETUP, complete: false, marked_complete: false, completed_at: null,
    missing: ["llm", "default_model", "workspace_project", "policy"] });
  const panel = app.el("div");
  await app.renderSetupTab(panel);
  const flag = panel.querySelector(".setup-flag");
  assert.match(flag.textContent, /^The older setup flag: not complete/);
  const links = flag.querySelectorAll("button");
  assert.deepEqual(links.map(b => b.textContent),
    ["Open Models", "Open Models", "Open Projects", "Open Projects"]);
  assert.ok(flag.textContent.includes("LLM connection"));
  assert.ok(flag.textContent.includes("execution policy"));
  for (const link of links) { await link.click(); opened.push(app.location.hash); }
  assert.deepEqual(opened, ["#/settings/models", "#/settings/models", "#/settings/projects", "#/settings/projects"]);
});

test("older flag: everything set but not recorded offers Finish, which records it", async () => {
  const calls = [];
  const app = setupApp({ ...SETUP, marked_complete: false, completed_at: null }, calls);
  const panel = app.el("div");
  await app.renderSetupTab(panel);
  await byText(panel, "button", "Finish setup").click();
  assert.deepEqual(calls.filter(([m]) => m === "POST"), [["POST", "/api/setup/complete"]]);
  assert.deepEqual(app.flashes.at(-1), ["ok", "setup complete"]);
});

// ---------- the pages: #/settings/<tab> renders that one; a save stays on it ----------

const PROJECT_READS = { projects: [], packs: [], repos: [], templates: [], schedules: [] };

function settingsApp(extra = {}) {
  const app = loadApp();
  const replaced = [];
  const calls = [];
  app.history.replaceState = (_state, _title, url) => replaced.push(url);
  const table = {
    "GET /api/setup/status": SETUP,
    "GET /api/onboard": { relock: null, steps: {} },
    "GET /api/onboard/unlocked": { on: false, offer: false, reason: "register a work directory first (step ②)" },
    "GET /api/llm/config": { configured: false, protocol: "ollama", num_ctx: 8192, num_ctx_source: "default" },
    "GET /api/llm/usage": { last_5h: { total_tokens: 0, requests: 0 }, last_7d: { total_tokens: 0, requests: 0 } },
    "GET /api/provider-presets": { presets: [] },
    "GET /api/settings": { configured: false },
    "GET /api/workers": { castes: [], engines: [] },
    "GET /api/channels": { channels: {} },
    "GET /api/webhooks": { webhooks: [] },
    ...extra,
  };
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    const key = `${method} ${path}`;
    if (key in table) return table[key];
    if (method === "GET" && /^\/api\/(projects|repos|templates|schedules)/.test(path)) return PROJECT_READS;
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  return { app, replaced, calls };
}

// What each page puts first: its one marker, never another page's.
const PAGE_MARK = {
  models: main => texts(main, "settings-block-title").includes("Chat model (the Queen)"),
  workers: main => main.querySelectorAll("h3").some(h => h.textContent === "Castes"),
  projects: main => texts(main, "settings-block-title").includes("Repos"),
  channels: main => texts(main, "settings-block-title").includes("Webhooks"),
  setup: main => Boolean(main.querySelector(".setup-checklist")),
};

test("#/settings/<tab> renders that one page and no tab bar; unknown or missing falls back to Models", async () => {
  for (const tab of Object.keys(PAGE_MARK)) {
    const { app, replaced } = settingsApp();
    const main = app.el("div");
    await app.viewSettings(main, tab);
    await settle();
    assert.equal(main.querySelectorAll(".tab-bar").length + main.querySelectorAll(".tab-button").length, 0, tab);
    assert.equal(main.querySelectorAll(".tab-panel").length, 1, tab);
    for (const [other, shows] of Object.entries(PAGE_MARK)) {
      assert.equal(shows(main), other === tab, `${tab} page shows ${other}?`);
    }
    // The hash already names the page: the view never rewrites it.
    assert.deepEqual(replaced, [], tab);
  }
  for (const tab of [undefined, "nope"]) {
    const { app } = settingsApp();
    const main = app.el("div");
    await app.viewSettings(main, tab);
    await settle();
    assert.ok(PAGE_MARK.models(main), String(tab));
  }
});

test("Models: the chat model and the worker model, one form each, each saying where it is stored", async () => {
  const { app } = settingsApp();
  const main = app.el("div");
  await app.viewSettings(main, "models");
  await settle();
  const panel = main.querySelector(".tab-panel");
  assert.deepEqual(texts(panel, "settings-block-title"), ["Chat model (the Queen)", "Worker model"]);
  assert.equal(panel.querySelectorAll(".settings-block-where").length, 2);
  // No setting editable twice on this page: one protocol select, one save per record.
  const protocolSelects = panel.querySelectorAll("select")
    .filter(s => s.children.some(o => o.getAttribute("value") === "openai-responses"));
  assert.equal(protocolSelects.length, 1);
  assert.equal(panel.querySelectorAll("button").filter(b => b.textContent === "Save provider").length, 1);
  assert.equal(panel.querySelectorAll("button").filter(b => b.textContent === "Test & save connection").length, 1);
});

// The router rewrites #/setup and #/projects to these tabs (shell.test.mjs pins the aliases).
test("the setup and projects tabs render their page in place", async () => {
  for (const tab of ["setup", "projects"]) {
    const { app, replaced } = settingsApp();
    const main = app.el("div");
    await app.viewSettings(main, tab);
    await settle();
    assert.ok(PAGE_MARK[tab](main), tab);
    assert.deepEqual(replaced, [], tab);
  }
});

// A save calls route(); the router re-renders from the hash, which still names
// the page, so the operator stays where they were. One save per former tab
// that has one (Workers is a read-only roster: nothing to save there).
test("a save on each page re-renders that page: route() runs with the hash untouched", async () => {
  const saves = {
    models: ["Test & save connection", { "PUT /api/llm/config": {}, "POST /api/llm/test": { ok: true, models: 2 } }],
    projects: ["Use default workspace", {
      "GET /api/setup/status": { ...SETUP, workspace_project: { ready: false } },
      "POST /api/setup/default-workspace": {},
    }],
    channels: ["Save channel", {
      "GET /api/channels": { channels: { slack: { enabled: false, live: false, allowed_users: [] } } },
      "PUT /api/channels/slack": {},
    }],
    setup: ["Finish setup", {
      "GET /api/setup/status": { ...SETUP, marked_complete: false, completed_at: null },
      "POST /api/setup/complete": { complete: true },
    }],
  };
  for (const [tab, [label, routes]] of Object.entries(saves)) {
    const { app, replaced } = settingsApp(routes);
    const hash = `#/settings/${tab}`;
    app.location.hash = hash;
    const rerendered = [];
    app.route = () => rerendered.push(app.location.hash);
    const main = app.el("div");
    await app.viewSettings(main, tab);
    await settle();
    const button = main.querySelectorAll("button").find(b => b.textContent === label);
    assert.ok(button, `${tab}: ${label}`);
    await button.click();
    await settle();
    assert.deepEqual(rerendered, [hash], tab);
    assert.deepEqual(replaced, [], tab);
  }
});

test("the sidebar's one Settings link is lit for settings, projects (detail too) and setup", () => {
  const app = loadApp();
  const settings = app.el("a");
  settings.dataset.ws = "settings";
  const policies = app.el("a");
  policies.dataset.ws = "policies";
  app.document.querySelectorAll = () => [settings, policies];
  for (const hash of ["#/settings", "#/projects", "#/projects/acme", "#/setup"]) {
    app.setShellActiveRoute(hash);
    assert.ok(settings.classList.contains("active"), hash);
    assert.ok(!policies.classList.contains("active"), hash);
  }
  app.setShellActiveRoute("#/policies");
  assert.ok(!settings.classList.contains("active"));
  assert.ok(policies.classList.contains("active"));
});
