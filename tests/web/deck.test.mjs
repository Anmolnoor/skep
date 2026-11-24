// Phase 10 10.3: the web / deck is a view of the one registry
// (serve/commands.py). The chat composer reads the web surface's slice from
// GET /api/commands — fixtures/commands-web.json is that route's real answer,
// pinned by tests/supervisor/test_slash_commands.py — and app.js keeps only
// the executors. A name the web surface does not carry is refused here,
// before any executor or request runs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadApp, texts } from "./harness.mjs";

const DECK = JSON.parse(readFileSync(new URL("./fixtures/commands-web.json", import.meta.url), "utf8"));

// The whole chat view, rendered with every read it makes at boot answered.
async function chat(extra = {}) {
  const app = loadApp();
  const calls = [];
  app.api = async (method, path) => {
    calls.push(`${method} ${path}`);
    if (path === "/api/commands") return DECK;
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [] };
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path in extra) return extra[path];
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.renderThreads = () => {};
  app.setInterval = () => 0;
  const main = app.document.createElement("main");
  await app.viewChat(main, null);
  const input = main.querySelectorAll("textarea")[0];
  const send = main.querySelectorAll(".composer-send")[0];
  const type = async (text) => {
    calls.length = 0;
    input.value = text;
    await send.click();
  };
  // The intro line of the last /help block the composer drew.
  const said = () => texts(main.querySelectorAll(".command-help").at(-1), "note")[0];
  return { app, main, calls, input, type, said };
}

test("/help lists the registry's web rows, in its order", async () => {
  const { main, type } = await chat();
  await type("/help");
  const rows = main.querySelectorAll(".command-help-list")[0].querySelectorAll("li");
  assert.deepEqual(rows.map(li => li.textContent),
    DECK.commands.map(row => `${row.usage} — ${row.help}`));
});

test("a command another surface carries is refused here, before anything runs", async () => {
  const { calls, type, said } = await chat();
  await type("/stop");
  // The one request is the deck itself: no executor, no card, no model turn.
  assert.deepEqual(calls, ["GET /api/commands"]);
  assert.equal(said(), DECK.refusals.stop);
  assert.match(DECK.refusals.stop, /^\/stop is not available on web; it runs on cli/);
  await type("/warp");
  assert.deepEqual(calls, []); // the deck is cached: nothing at all ran
  assert.equal(said(), "unknown command: /warp");
});

test("a web row still reaches its executor", async () => {
  const runs = [{ task_id: "t1" }];
  const { main, calls, type } = await chat({ "/api/runs?limit=5": runs });
  await type("/runs 5");
  assert.deepEqual(calls, ["GET /api/commands", "GET /api/runs?limit=5"]);
  assert.match(main.textContent, /"task_id": "t1"/);
});

test("/status shows the status text every surface answers, read-only", async () => {
  const summary = "skep: 1 running · 2 approval(s) waiting\n⏸ run t-abc… needs your approval";
  const { main, calls, type } = await chat({ "/api/status": { status: "ok", summary } });
  await type("/status");
  // One read, the same route the login check uses: no card, no model turn.
  assert.deepEqual(calls, ["GET /api/commands", "GET /api/status"]);
  const block = main.querySelectorAll(".command-status").at(-1);
  assert.deepEqual(texts(block, "note"), summary.split("\n"));
});

test("completion offers the web rows by prefix, with the registry's help", async () => {
  const { main, input } = await chat();
  const suggest = main.querySelectorAll(".command-suggest")[0];
  input.value = "/pe";
  await Promise.all(input.listeners.input.map(fn => fn()));
  assert.equal(suggest.hidden, false);
  const offered = DECK.commands.filter(row => row.name.startsWith("pe"));
  assert.ok(offered.length >= 2);
  assert.deepEqual(texts(suggest, "command-suggest-item"),
    offered.map(row => `${row.usage}${row.help}`));
  input.value = "/sessions";  // a messenger's command: never offered here
  await Promise.all(input.listeners.input.map(fn => fn()));
  assert.equal(suggest.hidden, true);
});

test("the deck is fetched once, and again after a failed fetch", async () => {
  const app = loadApp();
  let fetches = 0;
  app.api = async () => {
    fetches += 1;
    if (fetches === 1) throw new Error("down");
    return DECK;
  };
  await assert.rejects(app.loadCommandDeck(), /down/);
  assert.equal(await app.loadCommandDeck(), DECK);
  assert.equal(await app.loadCommandDeck(), DECK);
  assert.equal(fetches, 2);
  assert.equal(app.deckRefusal(DECK, "sessions"), DECK.refusals.sessions);
  assert.equal(app.deckRefusal(DECK, "warp"), "unknown command: /warp");
});
