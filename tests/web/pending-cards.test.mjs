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
  app.clearInterval = () => {};
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
  // v133-F21: "locks" withholds sending; the textarea still takes typing.
  assert.equal(main.querySelectorAll(".composer-send")[0].disabled, true);
  assert.equal(input.disabled, false);
});

test("the web's message says it is the web, so the server applies the same rule", async () => {
  const { app, main, input } = await openChat([modelCard()]);
  const streamed = [];
  app.streamSse = async (path, body) => { streamed.push([path, body.content]); };
  input.value = "hello";
  await main.querySelectorAll(".composer-send")[0].click();
  assert.deepEqual(streamed, [["/api/chats/c1/messages?surface=web", "hello"]]);
});

// R4 K1: the thread's confirm card is where a landing or a PR is approved, so
// it lists every run it lands or publishes — single, grouped, branch mode —
// each with its own G10 warning and V1 tamper.
test("R4 K1: a land_run / open_pr card lists each run with its G10 warning and V1 tamper", async () => {
  const runs = [
    { task_id: "aaaaaaaaaaaa1111", warning: null, tamper: null },
    { task_id: "bbbbbbbbbbbb2222", warning: "re-verification RAN and FAILED", tamper: null },
    { task_id: "cccccccccccc3333", warning: "TEST-TAMPER: tests changed", tamper: "TEST-TAMPER: tests changed" },
  ];
  const pr = { action_id: "a-pr", chat_id: "c1", tool: "open_pr", args: { branch: "feat" },
    status: "proposed", source: "assistant", card: { headline: "open_pr — feat", runs } };
  const land = { action_id: "a-land", chat_id: "c1", tool: "land_run", args: { task_id: "x" },
    status: "proposed", source: "operator", card: { headline: "land_run — x", runs: runs.slice(1, 2) } };
  const { main } = await openChat([pr, land]);
  const [prCard, landCard] = main.querySelectorAll(".confirm-card");
  const lines = prCard.querySelector(".card-runs").querySelectorAll("li");
  assert.equal(lines.length, 3);
  assert.match(texts(prCard, "card-runs-note")[0], /^3 runs this card lands or publishes:/);
  assert.match(lines[0].textContent, /^run aaaaaaaaaaaa1 · no G10 or V1 warning$/);
  assert.match(lines[1].textContent, /run bbbbbbbbbbbb2 · ⚠ re-verification RAN and FAILED/);
  assert.equal(lines[2].querySelector(".banner").className, "banner bad");
  assert.match(lines[2].textContent, /V1 · TEST-TAMPER: tests changed/);
  assert.equal(lines[2].textContent.match(/TEST-TAMPER/g).length, 1, "a warning that IS the tamper is said once");
  assert.match(landCard.querySelector(".card-runs").textContent, /run bbbbbbbbbbbb2 · ⚠ re-verification RAN and FAILED/);
});

// R5-2: the web confirm binds to the items the card rendered (K2's server
// check) — 1 included — and a card that changed since is reloaded, not approved.
test("R5-2: the chat confirm posts the rendered item count; a changed card reloads and nothing is approved", async () => {
  const fetch = { action_id: "a-url", chat_id: "c1", tool: "read_url", args: { url: "https://x.dev" },
    status: "proposed", source: "assistant", card: { headline: "read_url — https://x.dev" } };
  const { app, main } = await openChat([fetch]);
  const sent = [];
  app.streamSse = async (path, body) => {
    sent.push([path, body]);
    // api.js's ApiError: the detail as the message, the HTTP status beside it
    throw Object.assign(new Error("this card now holds 2 items, not the 1 you were shown. Nothing ran. Open it in its thread to read every item"), { status: 409 });
  };
  let routed = 0;
  app.route = () => { routed += 1; };
  const card = main.querySelector(".confirm-card");
  await card.querySelectorAll("button").find(b => b.textContent === "Approve").click();
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], "/api/chats/c1/actions/a-url/confirm?surface=web");
  assert.equal(sent[0][1].items, 1);
  assert.equal(routed, 1, "the thread reloads with the card as it is now");
  assert.deepEqual(app.flashes.at(-1), ["bad", "this card changed — review it again; nothing ran"]);
  assert.ok(!card.classList.contains("resolved"), "nothing is shown as approved");
});

test("R5-2: a folded card's confirm posts every item it rendered", async () => {
  const fold = { action_id: "a-fold", chat_id: "c1", tool: "read_url", args: { url: "https://x.dev/a" },
    status: "proposed", source: "assistant",
    card: { headline: "read_url — https://x.dev/a (+1 more)", items: ["read_url — https://x.dev/a", "read_url — https://x.dev/b"] } };
  const { app, main } = await openChat([fold]);
  const sent = [];
  app.streamSse = async (path, body) => { sent.push(body); };
  await main.querySelector(".confirm-card").querySelectorAll("button").find(b => b.textContent === "Approve").click();
  assert.equal(sent[0].items, 2);
});
