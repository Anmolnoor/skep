// hive-rooms H2 (C11): the Hive page module, executed under a fake DOM.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fakeDocument, texts } from "./harness.mjs";

globalThis.document = fakeDocument();
globalThis.localStorage = { getItem: () => "tok" };
const hive = await import("../../src/skep/supervisor/serve/static/hive.js");

const byText = (root, tag, text) => root.querySelectorAll(tag).find(n => n.textContent === text);
const PROVIDERS = [{ provider_id: "pa", base_url: "https://api.deepseek.com/v1" }];

test("queenCard: what it talks to, its policy and how many chats run in it", () => {
  const role = { name: "ada", provider: "pa", model: "glm-a1", policy_layer: "operator", chats: 4 };
  const card = hive.queenCard(role, true, () => {});
  assert.deepEqual(texts(card, "queen-talks"), ["glm-a1 · pa"]);
  assert.deepEqual(texts(card, "queen-policy"), ["role operator · in 4 chats"]);
  assert.ok(texts(card, "chip").includes("default"));
  const plain = hive.queenCard({ name: "q", chats: 1, shipped: true }, false, () => {});
  assert.deepEqual(texts(plain, "queen-talks"), ["default model · active provider"]);
  assert.deepEqual(texts(plain, "queen-policy"), ["no template: acts as assistant · in 1 chat"]);
});

test("queenForm: create sends what was typed, names the host, and shows a refusal", async () => {
  const saved = [];
  const form = hive.queenForm(null, PROVIDERS, {
    save: async (...args) => { saved.push(args); throw new Error("provider 'pa' is not on the shelf"); },
    remove: async () => {},
  });
  const [name, model] = form.querySelectorAll("input");
  const [provider] = form.querySelectorAll("select");
  name.value = "nova";
  model.value = " m2 ";
  assert.deepEqual(texts(form, "queen-where"), [
    "Uses the model in Settings, like every role that pins nothing."]);
  provider.value = "pa";
  provider.listeners.change[0]();
  assert.deepEqual(texts(form, "queen-where"), [
    "Every turn in its chats goes to api.deepseek.com, with that provider's own key."]);
  await byText(form, "button", "Create queen").click();
  assert.equal(saved.length, 1);
  const [sentName, fields, creating] = saved[0];
  assert.equal(sentName, "nova");
  assert.equal(creating, true);
  assert.deepEqual({ ...fields }, {
    provider: "pa", model: "m2", policy_layer: "assistant", system_prompt: "" });
  assert.deepEqual(texts(form, "queen-status"), ["provider 'pa' is not on the shelf"]);
  assert.equal(byText(form, "button", "Delete"), undefined);
});

test("queenForm: edit keeps the name; only a custom role offers Delete", async () => {
  const removed = [];
  const handlers = { save: async () => {}, remove: async name => { removed.push(name); } };
  const custom = { name: "nova", provider: "pa", policy_layer: null, shipped: false };
  const form = hive.queenForm(custom, PROVIDERS, handlers);
  assert.equal(form.querySelectorAll("input")[0].getAttribute("disabled"), "true");
  assert.equal(form.querySelectorAll("select")[1].value, "");
  await byText(form, "button", "Delete").click();
  assert.deepEqual(removed, ["nova"]);
  const shipped = hive.queenForm({ name: "builder", shipped: true }, PROVIDERS, handlers);
  assert.equal(byText(shipped, "button", "Delete"), undefined);
  const failing = hive.queenForm(custom, PROVIDERS, {
    save: async () => {}, remove: async () => { throw new Error("in use by 1 chat(s)"); } });
  await byText(failing, "button", "Delete").click();
  assert.deepEqual(texts(failing, "queen-status"), ["in use by 1 chat(s)"]);
});

test("agentList: governed agents name their run and role; others say so", () => {
  const list = hive.agentList({ summary: "2 agent(s) running", agents: [
    { agent: "claude", state: "working", governed: true, task_id: "t_91c", role: "builder" },
    { agent: "codex", state: "idle", governed: false },
  ] });
  assert.deepEqual(texts(list, "note"), [
    "2 agent(s) running", "governed · run t_91c · role builder",
    "not governed — started outside skep"]);
});

test("viewHive: renders the roster and creates through POST /api/roles", async () => {
  const calls = [];
  const replies = {
    "GET /api/roles": { default: "ada", roles: [{ name: "ada", chats: 2 }, { name: "bolt", chats: 0 }] },
    "GET /api/providers": { providers: PROVIDERS },
    "GET /api/agents": { summary: "0 agent(s) running", agents: [] },
    "POST /api/roles": { role: {} },
  };
  globalThis.fetch = async (path, options) => {
    calls.push([options.method, path, options.body && JSON.parse(options.body)]);
    const body = replies[`${options.method} ${path}`];
    return { ok: !!body, status: body ? 200 : 404, json: async () => body || { detail: "nope" } };
  };
  const main = document.createElement("main");
  await hive.viewHive(main);
  assert.equal(main.querySelectorAll(".queen-card").length, 2);
  const form = main.querySelector(".queen-form");
  form.querySelectorAll("input")[0].value = "vera";
  await byText(form, "button", "Create queen").click();
  const post = calls.find(([method]) => method === "POST");
  assert.equal(post[2].name, "vera");
  assert.equal(calls.filter(([, path]) => path === "/api/roles").length, 3); // reloaded
});

test("viewHive: an unreachable agents read does not hide the roster", async () => {
  globalThis.fetch = async (path) => path === "/api/agents"
    ? { ok: false, status: 500, json: async () => { throw new Error("not json"); } }
    : { ok: true, status: 200, json: async () => (path === "/api/roles"
      ? { default: "x", roles: [] } : { providers: [] }) };
  const main = document.createElement("main");
  await hive.viewHive(main);
  assert.ok(texts(main, "note").includes("agents unavailable: 500"));
});
