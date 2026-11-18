// Graphite round A: Settings as one page of five tabs, Setup as one checklist
// (settings.js), executed. Run: node --test tests/web/
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const byText = (root, tag, text) =>
  root.querySelectorAll(tag).find(n => n.textContent === text);
const byPlaceholder = (root, placeholder) =>
  root.querySelectorAll("input").find(n => n.getAttribute("placeholder") === placeholder);
// A tab's render() starts inside buildTabBar and is not awaited by the view.
const settle = async () => { for (let i = 0; i < 5; i += 1) await new Promise(r => setTimeout(r, 0)); };

// ---------- the Setup checklist (moved from app.test.mjs's O3 onboard card) ----------

function onboardApp(onboard, calls = []) {
  const app = loadApp();
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    if (method === "GET" && path === "/api/onboard") return onboard;
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
  assert.deepEqual(texts(list, "setup-label"), ["Model", "Work folder", "Sandbox", "Policy baseline", "Role"]);
  assert.deepEqual(texts(list, "setup-state"), [
    "done: ollama qwen3:14b — local only",
    "not done yet",
    "not done yet",
    "done: builder locked, sha256 0123456789ab",
    "not done yet",
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
  const steps = list.querySelectorAll(".setup-step");
  assert.equal(steps.length, 5);
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
    { template: "assistant", expect_sha256: "abc123", approved_by: "web" }]]);
});

test("checklist: changing the template throws away the preview", async () => {
  const app = onboardApp({ relock: null, steps: {} });
  const card = await app.onboardChecklist();
  const lock = byText(card, "button", "Lock baseline");
  await byText(card, "button", "Preview").click();
  const template = card.querySelectorAll("select").find(s => s.value === "assistant");
  template.value = "operator";
  for (const fn of template.listeners.change) fn();
  assert.equal(lock.disabled, true);
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
  await app.renderSetupTab(panel, () => {});
  assert.deepEqual(texts(panel, "setup-flag"), ["The older setup flag: complete since 2026-07-08 09:00:00."]);
  assert.equal(panel.querySelectorAll(".card").length, 0); // no "Ready" card above "not done yet"
  assert.equal(panel.querySelectorAll(".setup-step").length, 5);
  assert.equal(byText(panel, "button", "Finish setup"), undefined);
});

test("older flag: each missing item names where it is set and opens that tab", async () => {
  const opened = [];
  const app = setupApp({ ...SETUP, complete: false, marked_complete: false, completed_at: null,
    missing: ["llm", "default_model", "workspace_project", "policy"] });
  const panel = app.el("div");
  await app.renderSetupTab(panel, key => opened.push(key));
  const flag = panel.querySelector(".setup-flag");
  assert.match(flag.textContent, /^The older setup flag: not complete/);
  const links = flag.querySelectorAll("button");
  assert.deepEqual(links.map(b => b.textContent),
    ["Open Models", "Open Models", "Open Projects", "Open Projects"]);
  assert.ok(flag.textContent.includes("LLM connection"));
  assert.ok(flag.textContent.includes("execution policy"));
  for (const link of links) await link.click();
  assert.deepEqual(opened, ["models", "models", "projects", "projects"]);
});

test("older flag: everything set but not recorded offers Finish, which records it", async () => {
  const calls = [];
  const app = setupApp({ ...SETUP, marked_complete: false, completed_at: null }, calls);
  const panel = app.el("div");
  await app.renderSetupTab(panel, () => {});
  await byText(panel, "button", "Finish setup").click();
  assert.deepEqual(calls.filter(([m]) => m === "POST"), [["POST", "/api/setup/complete"]]);
  assert.deepEqual(app.flashes.at(-1), ["ok", "setup complete"]);
});

// ---------- the page: five tabs, old routes land on theirs ----------

function settingsApp() {
  const app = loadApp();
  const replaced = [];
  app.history.replaceState = (_state, _title, url) => replaced.push(url);
  app.api = async (method, path) => {
    if (path === "/api/setup/status") return SETUP;
    if (path === "/api/onboard") return { relock: null, steps: {} };
    if (path === "/api/llm/config") return { configured: false, protocol: "ollama", num_ctx: 8192, num_ctx_source: "default" };
    if (path === "/api/llm/usage") return { last_5h: { total_tokens: 0, requests: 0 }, last_7d: { total_tokens: 0, requests: 0 } };
    if (path === "/api/provider-presets") return { presets: [] };
    if (path === "/api/settings") return { configured: false };
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  return { app, replaced };
}

test("Settings is five tabs named by plain question, opening on the last one used", async () => {
  const { app } = settingsApp();
  const main = app.el("div");
  await app.viewSettings(main);
  await settle();
  assert.deepEqual(texts(main, "tab-button"), ["Models", "Workers", "Projects", "Channels", "Setup"]);
  const active = () => main.querySelectorAll(".tab-button").find(b => b.classList.contains("active")).textContent;
  assert.equal(active(), "Models");
  await byText(main, "button", "Setup").click();
  await settle();
  const again = app.el("div");
  await app.viewSettings(again);
  await settle();
  assert.equal(again.querySelectorAll(".tab-button").find(b => b.classList.contains("active")).textContent, "Setup");
});

test("Models: the chat model and the worker model, one form each, each saying where it is stored", async () => {
  const { app } = settingsApp();
  const main = app.el("div");
  await app.viewSettings(main);
  await settle();
  const panel = main.querySelectorAll(".tab-panel")[0];
  assert.deepEqual(texts(panel, "settings-block-title"), ["Chat model (the Queen)", "Worker model"]);
  assert.equal(panel.querySelectorAll(".settings-block-where").length, 2);
  // No setting editable twice on this page: one protocol select, one save per record.
  const protocolSelects = panel.querySelectorAll("select")
    .filter(s => s.children.some(o => o.getAttribute("value") === "openai-responses"));
  assert.equal(protocolSelects.length, 1);
  assert.equal(panel.querySelectorAll("button").filter(b => b.textContent === "Save provider").length, 1);
  assert.equal(panel.querySelectorAll("button").filter(b => b.textContent === "Test & save connection").length, 1);
});

test("#/setup and #/projects open their Settings tab and show #/settings", async () => {
  for (const [view, label] of [["viewSetup", "Setup"], ["viewProjects", "Projects"]]) {
    const { app, replaced } = settingsApp();
    app.api = async (method, path) => {
      if (path === "/api/setup/status") return SETUP;
      if (path === "/api/onboard") return { relock: null, steps: {} };
      return { projects: [], packs: [], repos: [], templates: [], schedules: [] };
    };
    const main = app.el("div");
    await app[view](main);
    await settle();
    assert.equal(main.querySelectorAll(".tab-button").find(b => b.classList.contains("active")).textContent, label);
    // A save's route() re-renders #/settings, so it lands back on the tab in use.
    assert.deepEqual(replaced, ["#/settings"]);
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
