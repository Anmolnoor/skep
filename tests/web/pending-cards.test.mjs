// Phase 10 R1 D1: `?surface=` is a client hint, not authorization — the web
// and skep chat share one token, one operator. A card the web cannot confirm
// (skep chat's /model card) is drawn read-only with where to answer it, keeps
// a working Cancel (a deny only narrows), and never locks the web composer;
// the composer follows the server's own rule (chat.py blocking_cards).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadApp, texts } from "./harness.mjs";

const DECK = JSON.parse(readFileSync(new URL("./fixtures/commands-web.json", import.meta.url), "utf8"));
const ELSEWHERE = "/model is not available on web; it runs on cli — answer it in skep chat";

const modelCard = (extra = {}) => ({
  action_id: "a-model", chat_id: "c1", tool: "set_assistant_model", args: { model: "m2" },
  status: "proposed", source: "operator", card: { headline: "set the model to m2" },
  web_refusal: ELSEWHERE, ...extra,
});
const roleCard = { action_id: "a-role", chat_id: "c1", tool: "set_role", args: { name: "x" },
  status: "proposed", source: "operator", card: { headline: "switch role" } };

// The chat view for chat c1, holding `actions`, with every boot read answered.
async function openChat(actions, posts = []) {
  const app = loadApp();
  app.api = async (method, path, body) => {
    if (method === "POST") { posts.push([path, body]); return { action_id: "a-model", denied: true, ok: false }; }
    if (path === "/api/commands") return DECK;
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") return { chat: { chat_id: "c1", title: "t" }, messages: [], actions };
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path.startsWith("/api/status")) return {};
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.renderThreads = () => {};
  app.setInterval = () => 0;
  app.watchStatus = () => {};
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  return { app, main, input: main.querySelectorAll("textarea")[0] };
}

test("blocksComposer: only a pending, non-gate card the web can confirm holds the composer", () => {
  const app = loadApp();
  assert.equal(app.blocksComposer(roleCard), true);
  assert.equal(app.blocksComposer(modelCard()), false);
  assert.equal(app.blocksComposer({ ...roleCard, source: "gate" }), false);
  assert.equal(app.blocksComposer({ ...roleCard, status: "denied" }), false);
});

test("a card the web cannot confirm is read-only here: where to answer it, and a working Cancel", async () => {
  const posts = [];
  const { main, input } = await openChat([modelCard()], posts);
  const card = main.querySelector(".elsewhere-card");
  assert.ok(card, "the card is drawn");
  assert.deepEqual(card.querySelectorAll("button").map(b => b.textContent), ["Cancel"]);
  assert.ok(texts(card, "note").includes(ELSEWHERE), texts(card, "note"));
  assert.equal(input.disabled, false, "a card the web cannot confirm never locks the composer");
  await card.querySelectorAll("button")[0].click();
  assert.deepEqual(posts.map(([path]) => path), ["/api/chats/c1/commands/a-model/deny"]);
  assert.ok(card.classList.contains("resolved"));
});

test("a card the web can confirm still locks the composer, beside a read-only one", async () => {
  const { main, input } = await openChat([modelCard(), roleCard]);
  assert.equal(main.querySelectorAll(".command-card").length, 2);
  assert.equal(main.querySelectorAll(".elsewhere-card").length, 1);
  assert.equal(input.disabled, true);
});

test("the web's message says it is the web, so the server applies the same rule", async () => {
  const { app, main, input } = await openChat([modelCard()]);
  const streamed = [];
  app.streamSse = async (path, body) => { streamed.push([path, body.content]); };
  input.value = "hello";
  await main.querySelectorAll(".composer-send")[0].click();
  assert.deepEqual(streamed, [["/api/chats/c1/messages?surface=web", "hello"]]);
});
