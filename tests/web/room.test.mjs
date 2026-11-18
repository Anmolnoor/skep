// H1 (hive-rooms C10): the chat view's context panel (room.js), executed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const panelOf = app => app.el("aside", { class: "chat-context" });

test("context panel: a new thread says so and reads nothing", async () => {
  const app = loadApp();
  const panel = panelOf(app);
  await app.renderContextPanel(panel, { chat: null, defaultModel: "glm-5.2", actions: [] });
  assert.deepEqual(texts(panel, "context-label"),
    ["Role and model", "Waiting on you", "Runs here", "Spend, this thread"]);
  assert.match(panel.textContent, /A new thread/);
  assert.deepEqual(texts(panel, "note").slice(1), ["Nothing.", "None yet.", "None yet."]);
});

test("context panel: role, model, only the cards still waiting, this thread's runs and spend", async () => {
  const app = loadApp();
  const calls = [];
  app.api = async (method, path) => {
    calls.push(path);
    if (path.startsWith("/api/runs")) {
      return { runs: [{ task_id: "abcdef1234567890", state: "running", updated_at: "2026-11-17T00:00:00Z" }] };
    }
    const span = n => ({ total_tokens: n, requests: 1 });
    return { chat: { last_5h: span(1200), last_7d: span(45000) } };
  };
  const panel = panelOf(app);
  await app.renderContextPanel(panel, {
    chat: { chat_id: "c 1", role: "builder", model: "deepseek-v4.1" },
    defaultModel: "glm-5.2",
    actions: [
      { action_id: "a1", tool: "dispatch_run", status: "proposed", source: "assistant", created_at: "2026-11-17T00:00:00Z" },
      { action_id: "a2", tool: "set_policy", status: "confirmed", source: "assistant", created_at: "2026-11-17T00:00:00Z" },
    ],
  });
  assert.deepEqual(calls, ["/api/runs?chat_id=c%201&limit=100", "/api/llm/usage?chat_id=c%201"]);
  const [role] = texts(panel, "context-mini");
  assert.equal(role, "role builderdeepseek-v4.1");
  const cards = panel.querySelectorAll(".context-card");
  assert.equal(cards.length, 1);
  assert.match(cards[0].textContent, /^dispatch_run/);
  const [run] = panel.querySelectorAll(".context-run");
  assert.equal(run.getAttribute("href"), "#/runs/abcdef1234567890");
  assert.match(run.textContent, /^abcdef12running/);
  assert.match(panel.querySelector(".context-spend").textContent, /5h 1,200 tok · 7d 45,000 tok/);
});

test("context panel: a chat with no stored role runs as assistant; the model falls back to the default", async () => {
  const app = loadApp();
  app.api = async (method, path) => {
    if (path.startsWith("/api/runs")) return { runs: [] };
    throw new Error("down");
  };
  const panel = panelOf(app);
  await app.renderContextPanel(panel, { chat: { chat_id: "c2", role: null, model: null }, defaultModel: "glm-5.2", actions: [] });
  assert.equal(texts(panel, "context-mini")[0], "role assistant (none stored)glm-5.2");
  assert.equal(panel.querySelector(".context-runs").textContent, "None yet.");
  // A failed read says it failed rather than showing an empty spend (I8).
  assert.equal(panel.querySelector(".context-spend").textContent, "Could not load spend.");
});

test("context panel: a failed runs read says so", async () => {
  const app = loadApp();
  app.api = async (method, path) => {
    if (path.startsWith("/api/runs")) throw new Error("down");
    const span = { total_tokens: 0 };
    return { chat: { last_5h: span, last_7d: span } };
  };
  const panel = panelOf(app);
  await app.renderContextPanel(panel, { chat: { chat_id: "c3" }, defaultModel: null, actions: [] });
  assert.equal(panel.querySelector(".context-runs").textContent, "Could not load this thread's runs.");
  assert.equal(texts(panel, "context-mini")[0], "role assistant (none stored)no model");
});
