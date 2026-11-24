// Phase 10 R1 (E4): the chat model's connection is staged, probed, then saved —
// three steps, never one. The probe (POST /api/llm/probe) saves nothing, so a
// failed test can never leave a new connection active (I8); Save is its own
// button, live only for the exact values that just passed. The Provider Shelf
// lists every configured profile with its last health check.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const LLM = { configured: true, protocol: "ollama", base_url: "http://old:11434", api_key_set: true, num_ctx: 8192, num_ctx_source: "default", default_model: "llama3.2" };
const PROVIDERS = {
  providers: [
    { provider_id: "default", protocol: "ollama", base_url: "http://old:11434", model: "llama3.2", active: true, source: "manual", api_key_set: true },
    { provider_id: "zai", protocol: "openai-compat", base_url: "https://api.z.ai/api/paas/v4", model: "glm-5.2", active: false, source: "preset:zai", api_key_set: false },
    { provider_id: "gpt", protocol: "openai-responses", base_url: "https://chatgpt.com/backend-api/codex", model: "gpt-6", active: false, source: "manual", api_key_set: false, chatgpt_login: { plan: "plus" } },
  ],
};
const HEALTH = {
  health: [
    { provider_id: "default", reachable: true, model_found: true, completion_ok: true, error: null, checked_at: "2026-11-24T08:00:00Z", latency_ms: 120 },
    { provider_id: "zai", reachable: false, model_found: false, completion_ok: null, error: "HTTP 401: invalid key", checked_at: "2026-11-24T08:00:00Z", latency_ms: null },
  ],
};
const GOOD = { ok: true, models: 3, model: "qwen3:14b", context_window: { window: 131072, max_output: null, source: "provider-api" } };

function modelsApp(routes = {}) {
  const app = loadApp();
  const calls = [];
  const table = {
    "GET /api/llm/config": LLM,
    "GET /api/llm/usage": { last_5h: { total_tokens: 0, requests: 0 }, last_7d: { total_tokens: 0, requests: 0 } },
    "GET /api/llm/models": { models: ["llama3.2"] },
    "GET /api/provider-presets": { presets: [] },
    "GET /api/providers": PROVIDERS,
    "GET /api/providers/health": HEALTH,
    "POST /api/llm/probe": GOOD,
    "PUT /api/llm/config": LLM,
    ...routes,
  };
  app.api = async (method, path, body) => {
    calls.push([method, path, body === undefined ? undefined : JSON.parse(JSON.stringify(body))]);
    const key = `${method} ${path}`;
    if (!(key in table)) throw new Error(`unstubbed api ${key}`);
    const out = table[key];
    return typeof out === "function" ? out(body) : out;
  };
  let routed = 0;
  app.route = () => { routed += 1; };
  return { app, calls, routed: () => routed };
}

async function models(routes) {
  const ctx = modelsApp(routes);
  const panel = ctx.app.el("div");
  await ctx.app.renderAssistantTab(panel);
  await flush();
  const button = label => panel.querySelectorAll("button").find(b => b.textContent === label);
  const baseUrl = panel.querySelectorAll("input").find(i => /ollama\.com/.test(i.getAttribute("placeholder") || ""));
  const key = panel.querySelectorAll("input").find(i => i.getAttribute("type") === "password");
  return { ...ctx, panel, button, baseUrl, key };
}
const edit = async (input, value) => { input.value = value; await Promise.all((input.listeners.input || []).map(fn => fn())); };

test("Test probes the staged values and saves nothing; Save is a separate button", async () => {
  const { panel, button, baseUrl, key, calls } = await models();
  assert.equal(button("Test & save connection"), undefined);
  assert.equal(button("Save connection").disabled, true);
  await edit(baseUrl, "http://new:11434");
  await edit(key, "sk-new");
  await button("Test connection").click();
  await flush();
  assert.deepEqual(calls.filter(([m]) => m !== "GET"), [
    ["POST", "/api/llm/probe", { base_url: "http://new:11434", protocol: "ollama", api_key: "sk-new" }],
  ]); // no PUT: a probe is not a save
  const result = panel.querySelector(".probe-result");
  assert.match(result.textContent, /✓ reachable — 3 models; qwen3:14b answered the 1-token probe/);
  assert.match(result.textContent, /context window: 131,072 tokens, reported by the endpoint/);
  assert.match(result.textContent, /Nothing is saved yet/);
  assert.equal(button("Save connection").disabled, false);
});

test("Save writes exactly the values that passed, then re-renders", async () => {
  const { app, button, baseUrl, key, calls, routed } = await models();
  await edit(baseUrl, "http://new:11434");
  await edit(key, "sk-new");
  await button("Test connection").click();
  await flush();
  await button("Save connection").click();
  await flush();
  assert.deepEqual(calls.filter(([m]) => m === "PUT"), [
    ["PUT", "/api/llm/config", { base_url: "http://new:11434", protocol: "ollama", api_key: "sk-new" }],
  ]);
  assert.equal(routed(), 1);
  assert.match(app.flashes.at(-1)[1], /saved — the Queen now uses http:\/\/new:11434/);
});

test("a failed test says nothing was saved and leaves Save off", async () => {
  const { panel, button, calls } = await models({
    "POST /api/llm/probe": { ok: false, detail: "HTTP 401: invalid key", http_status: 401 },
  });
  await button("Test connection").click();
  await flush();
  const result = panel.querySelector(".probe-result");
  assert.match(result.textContent, /✗ HTTP 401: invalid key/);
  assert.match(result.textContent, /nothing was saved: the connection in use is unchanged/);
  assert.ok(result.classList.contains("tone-bad"));
  assert.equal(button("Save connection").disabled, true);
  await button("Save connection").click(); // a fake click on a disabled button still sends nothing
  assert.equal(calls.filter(([m]) => m === "PUT").length, 0);
});

test("editing a value after a passing test turns Save off until it is tested again", async () => {
  const { panel, button, baseUrl } = await models();
  await button("Test connection").click();
  await flush();
  assert.equal(button("Save connection").disabled, false);
  await edit(baseUrl, "http://other:11434");
  assert.equal(button("Save connection").disabled, true);
  assert.match(panel.querySelector(".probe-result").textContent, /changed since the test/);
});

test("a window no endpoint reports says it is skep's default", async () => {
  const { panel, button } = await models({
    "POST /api/llm/probe": { ...GOOD, context_window: { window: 16384, max_output: null, source: "default" } },
  });
  await button("Test connection").click();
  await flush();
  assert.match(panel.querySelector(".probe-result").textContent, /context window: 16,384 tokens, skep's default \(nothing reports it\)/);
});

test("the Provider Shelf lists every profile, the active one marked, each with its last check", async () => {
  const { panel } = await models();
  const shelf = panel.querySelector(".provider-shelf");
  const rows = shelf.querySelectorAll(".provider-row");
  assert.deepEqual(rows.map(r => r.getAttribute("data-provider")), ["default", "zai", "gpt"]);
  assert.match(rows[0].textContent, /active/);
  assert.match(rows[0].textContent, /answered the 1-token probe/);
  assert.match(rows[0].textContent, /key saved/);
  assert.match(rows[1].textContent, /HTTP 401: invalid key/);
  assert.match(rows[1].textContent, /no key/);
  assert.match(rows[2].textContent, /ChatGPT plan login/);
  assert.match(rows[2].textContent, /not checked yet/);
  assert.doesNotMatch(rows[1].textContent, /active/);
  assert.equal(shelf.querySelectorAll("button").length, 0); // a list: switching stays a carded verb
});

test("the shelf says so when it cannot be read, and the form still stands", async () => {
  const { panel, button } = await models({ "GET /api/providers": () => { throw new Error("502"); } });
  assert.match(panel.querySelector(".provider-shelf").textContent, /provider shelf could not be read: 502/);
  assert.ok(button("Test connection"));
});

// Phase 10 R2 I7 (N3, O4): Save stays live only for values a probe passed.
test("picking a preset after a passing test turns Save off", async () => {
  const { panel, button } = await models({
    "GET /api/provider-presets": { presets: [{ preset_id: "zai", label: "Z.ai", protocol: "openai_compat", base_url: "https://api.z.ai/api/paas/v4" }] },
  });
  await button("Test connection").click();
  await flush();
  assert.equal(button("Save connection").disabled, false);
  const picker = panel.querySelectorAll("select").find(s => s.children.some(o => o.getAttribute("value") === "zai"));
  picker.value = "zai";
  for (const fn of picker.listeners.change) fn({ target: picker });
  assert.equal(button("Save connection").disabled, true);
  assert.match(panel.querySelector(".probe-result").textContent, /changed since the test/);
});

test("an edit made while the probe is in flight never gets a green check", async () => {
  let release;
  const pending = new Promise(r => { release = r; });
  const { panel, button, baseUrl } = await models({ "POST /api/llm/probe": () => pending });
  const clicked = button("Test connection").click();
  await edit(baseUrl, "http://typed-meanwhile:11434");
  release(GOOD);
  await clicked;
  await flush();
  assert.equal(button("Save connection").disabled, true);
  const result = panel.querySelector(".probe-result").textContent;
  assert.doesNotMatch(result, /✓/);
  assert.match(result, /changed while it was being tested/);
});

test("a probe that names no model says so instead of an empty name", async () => {
  const { panel, button } = await models({ "POST /api/llm/probe": { ...GOOD, models: 0, model: "" } });
  await button("Test connection").click();
  await flush();
  const result = panel.querySelector(".probe-result").textContent;
  assert.doesNotMatch(result, /; {2}answered|; answered/);
  assert.match(result, /no model is named and the endpoint lists none/);
  assert.equal(button("Save connection").disabled, true);
});

test("a probed model is the one Save will activate", async () => {
  const { panel, button } = await models();
  await button("Test connection").click();
  await flush();
  assert.match(panel.querySelector(".probe-result").textContent, /qwen3:14b answered the 1-token probe \(the model Save activates\)/);
});
