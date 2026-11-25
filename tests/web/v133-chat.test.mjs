// v133 B4 — the web chat (findings G1, G2, G3, G6, G7, G8 of the Alfin live
// test). The chat page must not rebuild itself for a live event, the composer
// must always take typing, and nothing the operator typed is lost or sent
// without their action.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadApp } from "./harness.mjs";

const DECK = JSON.parse(readFileSync(new URL("./fixtures/commands-web.json", import.meta.url), "utf8"));

const settle = async (n = 8) => { for (let i = 0; i < n; i += 1) await new Promise(r => setTimeout(r, 0)); };

const msg = (id, role, content, provenance = role === "user" ? "operator" : "queen") =>
  ({ id, chat_id: "c1", role, content, provenance, created_at: "2026-11-25T10:00:00Z" });
const card = (id, extra = {}) => ({
  action_id: id, chat_id: "c1", tool: "run_shell", args: { command: "ls" },
  status: "proposed", source: "assistant", card: { headline: `run_shell — ${id}` }, ...extra,
});

class FakeSource {
  constructor(url) { this.url = url; this.listeners = {}; FakeSource.last = this; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, data) { for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) }); }
  close() { this.closed = true; }
}

// The chat view for chat c1 over a mutable server: `server.messages` and
// `server.actions` are what the next GET returns. Every POST, every stream
// and every route() (a rebuild of #main) is counted.
async function openChat({ messages = [], actions = [] } = {}) {
  const app = loadApp();
  const server = { messages: [...messages], actions: [...actions] };
  const posts = [];
  const streams = [];
  let routed = 0;
  app.api = async (method, path, body) => {
    if (method === "POST") { posts.push([path, body]); return { detail: "stopping", ok: true }; }
    if (path === "/api/llm/config") return { configured: true, default_model: "m" };
    if (path === "/api/chats") return { chats: [{ chat_id: "c1", title: "t" }] };
    if (path === "/api/chats/c1") {
      return { chat: { chat_id: "c1", title: "t" }, messages: [...server.messages], actions: server.actions.map(a => ({ ...a })) };
    }
    if (path === "/api/roles") return { roles: [], default: null };
    if (path === "/api/llm/models") return { models: [] };
    if (path === "/api/projects") return { projects: [] };
    if (path === "/api/status") return { status: "ok", pending_approvals: 0, machine: "m" };
    if (path === "/api/tasks") return { tasks: [] };
    if (path === "/api/commands") return DECK;
    throw new Error(`unexpected ${method} ${path}`);
  };
  app.streamSse = async (path, body, handlers) => { streams.push([path, body]); };
  app.renderThreads = () => {};
  app.refreshSidebar = async () => {};
  app.setInterval = () => 0;
  app.clearInterval = () => {};
  app.EventSource = FakeSource;
  app.route = () => { routed += 1; };
  const main = app.document.createElement("main");
  await app.viewChat(main, "c1");
  app.location.hash = "#/chat/c1";
  app.document.visibilityState = "visible";
  const input = main.querySelectorAll("textarea")[0];
  const log = main.querySelector(".chat-log");
  const firstChild = main.children[0];
  const press = (key, extra = {}) => {
    const event = { type: "keydown", key, shiftKey: false, prevented: false, preventDefault() { this.prevented = true; }, ...extra };
    input.dispatchEvent(event);
    return event;
  };
  const textCount = (text) => log.querySelectorAll(".chat-message").filter(n => n.textContent.includes(text)).length;
  return { app, main, input, log, server, posts, streams, routed: () => routed, firstChild, press, textCount };
}

// ---------- F19 (G8): the poll reconciles by message id ----------

test("F19: a reply stored by a background turn appears after one poll, without rebuilding #main, drawn once", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello"), msg(2, "assistant", "hi there")] });
  chat.server.messages.push(msg(9, "assistant", "the run finished, here is what it did"));
  await chat.app.poll();
  assert.equal(chat.textCount("the run finished"), 1, "the background reply is drawn");
  assert.equal(chat.routed(), 0, "#main is not rebuilt");
  assert.equal(chat.main.children[0], chat.firstChild);
  await chat.app.poll();
  assert.equal(chat.textCount("the run finished"), 1, "a message already drawn is not drawn twice");
  assert.equal(chat.textCount("hi there"), 1);
});

test("F19: a message the live stream drew is not drawn again by the poll; a worker line stored mid-stream is", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.app.streamSse = async (path, body, handlers) => {
    chat.streams.push([path, body]);
    chat.server.messages.push(msg(5, "user", "do the thing"));
    chat.server.messages.push(msg(6, "user", "run abc completed", "worker:abc"));
    handlers.message({ content: "done, the thing is done" });
    chat.server.messages.push(msg(7, "assistant", "done, the thing is done"));
  };
  chat.input.value = "do the thing";
  chat.press("Enter");
  await settle();
  await chat.app.poll();
  await chat.app.poll();
  assert.equal(chat.streams.length, 1);
  assert.equal(chat.textCount("do the thing"), 1);
  assert.equal(chat.textCount("done, the thing is done"), 1);
  assert.equal(chat.textCount("run abc completed"), 1, "a line another author stored during the stream is drawn");
  assert.equal(chat.routed(), 0);
});

// ---------- F20 (G1): live events update the node they concern ----------

test("F20: a card arriving and a card resolved elsewhere do not rebuild #main; draft and scroll survive", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.input.value = "a draft in progress";
  Object.assign(chat.log, { scrollTop: 100, scrollHeight: 1000, clientHeight: 300 });
  chat.server.actions.push(card("a1"));
  await chat.app.poll();
  const drawn = chat.log.querySelectorAll(".confirm-card");
  assert.equal(drawn.length, 1, "the card arriving is drawn");
  chat.server.actions[0].status = "denied";
  await chat.app.poll();
  assert.ok(drawn[0].classList.contains("resolved"), "the verdict from elsewhere resolves the drawn card");
  assert.equal(chat.log.querySelectorAll(".confirm-card").length, 1, "a card is drawn once");
  assert.equal(chat.routed(), 0);
  assert.equal(chat.main.children[0], chat.firstChild);
  assert.equal(chat.input.value, "a draft in progress");
  assert.equal(chat.log.scrollTop, 100, "the operator's scroll position is kept");
});

test("F20: a run's terminal event draws its stored line in place, with no rebuild", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  const source = FakeSource.last;
  assert.ok(source, "the status stream is open");
  chat.server.messages.push(msg(4, "user", "run abcdef completed — land it?", "worker:abcdef"));
  source.emit("terminal", { task_id: "abcdef0123456789", state: "completed" });
  await settle();
  assert.equal(chat.routed(), 0);
  assert.equal(chat.main.children[0], chat.firstChild);
  assert.equal(chat.textCount("run abcdef completed"), 1);
});


test("F20: a quiet poll touches nothing on screen, not even the context panel", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")], actions: [card("a1")] });
  const panel = chat.main.querySelector(".chat-context");
  const before = [...panel.children];
  const logBefore = [...chat.log.children];
  await chat.app.poll();
  assert.deepEqual([...panel.children], before);
  assert.deepEqual([...chat.log.children], logBefore);
  chat.server.actions[0].status = "confirmed";
  await chat.app.poll();
  assert.notDeepEqual([...panel.children], before, "a card resolved elsewhere redraws the panel");
});

// ---------- F21 (G2): a pending card never disables the textarea ----------

test("F21: with a card pending, typing works and Enter posts nothing until the card is answered", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")], actions: [card("a1")] });
  assert.equal(chat.input.disabled, false, "the textarea takes typing under a pending card");
  assert.equal(chat.main.querySelector(".composer-send").disabled, true, "sending to the model is withheld");
  chat.input.value = "next, do this";
  chat.press("Enter");
  await settle();
  assert.equal(chat.streams.length, 0);
  assert.deepEqual(chat.posts, []);
  chat.input.value = "/runs 5";
  const event = chat.press("Enter");
  await settle();
  assert.ok(event.prevented);
  assert.equal(chat.input.value, "/runs 5", "a slash command under a pending card stays in the box");
  assert.deepEqual(chat.posts, []);
  assert.equal(chat.streams.length, 0);
  // Answering the card sends the queued message, once.
  chat.app.streamSse = async (path, body) => {
    chat.streams.push([path, body]);
    if (path.includes("/confirm")) chat.server.actions[0].status = "confirmed";
  };
  await chat.log.querySelector(".confirm-card").querySelectorAll("button").find(b => b.textContent === "Approve").click();
  await settle();
  assert.deepEqual(chat.streams.map(([path, body]) => [path, body.content]), [
    ["/api/chats/c1/actions/a1/confirm?surface=web", undefined],
    ["/api/chats/c1/messages?surface=web", "next, do this"],
  ]);
});

// ---------- F22 (G3): nothing typed is lost ----------

test("F22: queue one message, type a new draft, answer the card: the queued one is sent once and the draft stays", async () => {
  const chat = await openChat({ actions: [card("a1")] });
  chat.input.value = "queued first";
  chat.press("Enter");
  chat.input.value = "queued second";
  chat.press("Enter");
  await settle();
  const chips = () => chat.main.querySelectorAll(".queued-steer-item");
  assert.equal(chips().length, 2, "a second queued message does not overwrite the first");
  assert.deepEqual(chips().map(c => c.querySelector(".queued-steer-text").textContent), ["queued first", "queued second"]);
  // Cancel the second; edit nothing yet.
  await chips()[1].querySelectorAll("button").find(b => b.textContent === "Cancel").click();
  assert.equal(chips().length, 1);
  chat.input.value = "a draft I am still writing";
  chat.app.streamSse = async (path, body) => {
    chat.streams.push([path, body]);
    if (path.includes("/confirm")) chat.server.actions[0].status = "confirmed";
  };
  await chat.log.querySelector(".confirm-card").querySelectorAll("button").find(b => b.textContent === "Approve").click();
  await settle();
  const sent = chat.streams.filter(([path]) => path.includes("/messages")).map(([, body]) => body.content);
  assert.deepEqual(sent, ["queued first"], "the queued message is sent once; the cancelled one never");
  assert.equal(chat.input.value, "a draft I am still writing", "the draft is still in the box");
  assert.equal(chips().length, 0);
});

test("F22: Edit moves a queued message back into the box without losing the draft", async () => {
  const chat = await openChat({ actions: [card("a1")] });
  chat.input.value = "fix the typo";
  chat.press("Enter");
  await settle();
  chat.input.value = "and more";
  await chat.main.querySelector(".queued-steer-item").querySelectorAll("button").find(b => b.textContent === "Edit").click();
  assert.equal(chat.input.value, "fix the typo\nand more");
  assert.equal(chat.main.querySelectorAll(".queued-steer-item").length, 0);
});

test("F22: a message refused with 409 comes back to the box", async () => {
  const chat = await openChat();
  chat.app.streamSse = async () => {
    throw Object.assign(new Error("a turn is already running in this chat"), { status: 409 });
  };
  chat.input.value = "please do X";
  chat.press("Enter");
  await settle();
  assert.equal(chat.input.value, "please do X", "the refused message is back in the box");
  assert.equal(chat.textCount("please do X"), 0, "and not shown as sent");
  assert.ok(chat.app.flashes.some(([, text]) => /already running/.test(text)));
});


// ---------- F23 (G6): Stop holds the local queue ----------

test("F23: Stop with a message queued sends nothing after; the chip is held with Send and Discard", async () => {
  const chat = await openChat();
  let finish;
  chat.app.streamSse = (path, body) => {
    chat.streams.push([path, body]);
    return new Promise(resolve => { finish = resolve; });
  };
  chat.input.value = "start working";
  chat.press("Enter");
  await settle();
  assert.equal(chat.streams.length, 1);
  chat.input.value = "then this";
  chat.press("Enter");
  await settle();
  await chat.log.querySelector(".chat-stop").click();
  assert.deepEqual(chat.posts.map(([path]) => path), ["/api/chats/c1/stop"]);
  finish();
  await settle();
  assert.equal(chat.streams.length, 1, "nothing queued is sent because Stop was pressed");
  const chip = chat.main.querySelector(".queued-steer-item");
  assert.ok(chip, "the queued message is kept");
  assert.match(chip.textContent, /held/);
  const labels = chip.querySelectorAll("button").map(b => b.textContent);
  assert.ok(labels.includes("Send") && labels.includes("Discard"), labels.join(","));
  chat.app.streamSse = async (path, body) => { chat.streams.push([path, body]); };
  await chip.querySelectorAll("button").find(b => b.textContent === "Send").click();
  await settle();
  assert.deepEqual(chat.streams.map(([, body]) => body.content), ["start working", "then this"]);
  assert.equal(chat.main.querySelectorAll(".queued-steer-item").length, 0);
});

test("F23: Discard drops a held message and nothing is sent", async () => {
  const chat = await openChat();
  let finish;
  chat.app.streamSse = (path, body) => { chat.streams.push([path, body]); return new Promise(r => { finish = r; }); };
  chat.input.value = "go";
  chat.press("Enter");
  await settle();
  chat.input.value = "later";
  chat.press("Enter");
  await chat.log.querySelector(".chat-stop").click();
  finish();
  await settle();
  await chat.main.querySelector(".queued-steer-item").querySelectorAll("button").find(b => b.textContent === "Discard").click();
  await settle();
  assert.equal(chat.main.querySelectorAll(".queued-steer-item").length, 0);
  assert.equal(chat.streams.length, 1);
});


// ---------- F24 (G7): ↑ recalls this chat's own messages ----------

test("F24: ↑ in an empty composer walks this chat's own messages back, ↓ forward; a draft is left alone", async () => {
  const chat = await openChat({ messages: [
    msg(1, "user", "first ask"),
    msg(2, "assistant", "a reply"),
    msg(3, "user", "run x completed", "worker:x"),
    msg(4, "user", "This is the run we were working on.", "queen"),
    msg(5, "user", "second ask"),
  ] });
  const { input, press } = chat;
  input.selectionStart = input.selectionEnd = 0;
  assert.ok(press("ArrowUp").prevented);
  assert.equal(input.value, "second ask");
  press("ArrowUp");
  assert.equal(input.value, "first ask", "walks back past rows other authors wrote");
  press("ArrowUp");
  assert.equal(input.value, "first ask", "stops at the oldest");
  press("ArrowDown");
  assert.equal(input.value, "second ask");
  press("ArrowDown");
  assert.equal(input.value, "", "past the newest is the empty box again");
  // A draft — multi-line, caret at the start — is never replaced.
  input.value = "line one\nline two";
  input.selectionStart = input.selectionEnd = 0;
  const event = press("ArrowUp");
  assert.equal(event.prevented, false);
  assert.equal(input.value, "line one\nline two");
  // Caret not at the start of an empty-looking recall: nothing either.
  input.value = "typed";
  input.selectionStart = input.selectionEnd = 5;
  assert.equal(press("ArrowUp").prevented, false);
  assert.equal(input.value, "typed");
});

test("F24: a message sent in this session is the first ↑ recalls", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "old ask")] });
  chat.input.value = "just sent";
  chat.press("Enter");
  await settle();
  chat.input.selectionStart = chat.input.selectionEnd = 0;
  chat.press("ArrowUp");
  assert.equal(chat.input.value, "just sent");
  chat.press("ArrowUp");
  assert.equal(chat.input.value, "old ask");
});


// ---------- v133 R1 (Sol review of F19–F24) ----------

// S16: the queue drains only after the server's card state is read.
test("R1 S16: a card the stream never announced holds the queue — nothing is posted into it", async () => {
  const chat = await openChat();
  let finish;
  chat.app.streamSse = (path, body) => { chat.streams.push([path, body]); return new Promise(r => { finish = r; }); };
  chat.input.value = "start";
  chat.press("Enter");
  await settle();
  chat.input.value = "queued while it runs";
  chat.press("Enter");
  await settle();
  chat.server.actions.push(card("a1")); // stored server-side; its `action` event was lost
  finish();
  await settle();
  assert.deepEqual(chat.streams.map(([, body]) => body.content), ["start"], "the queued message waits for the card");
  assert.equal(chat.log.querySelectorAll(".confirm-card").length, 1, "the missed card is drawn");
  assert.equal(chat.main.querySelector(".composer-send").disabled, true);
  assert.equal(chat.main.querySelectorAll(".queued-steer-item").length, 1, "the queued message is kept");
});

test("R1 S16: a POST refused while a card waits leaves Send locked", async () => {
  const chat = await openChat();
  chat.app.streamSse = async () => {
    chat.server.actions.push(card("a1")); // a background turn raised a card first
    throw Object.assign(new Error("a card is waiting"), { status: 409 });
  };
  chat.input.value = "please do X";
  chat.press("Enter");
  await settle();
  assert.equal(chat.input.value, "please do X");
  assert.equal(chat.main.querySelector(".composer-send").disabled, true, "the card's lock survives the refusal");
});

// S6: a row is drawn exactly once, whichever of the stream and the poll
// carries it — the stream's own rows matched by the ids it named (or, from
// an older server that names none, by what it showed), never by provenance.
async function turn(chat, { ids = false, background = null } = {}) {
  chat.app.streamSse = async (path, body, handlers) => {
    chat.streams.push([path, body]);
    chat.server.messages.push(msg(5, "user", "do the thing"));
    handlers.message({ content: "the answer" });
    chat.server.messages.push(msg(7, "assistant", "the answer"));
    if (ids) handlers.message({ content: "", message_id: 7 }); // F38: the event after the stored row
  };
  if (background) chat.server.messages.push(background);
  chat.input.value = "do the thing";
  chat.press("Enter");
  await settle();
}

test("R1 S6: with no message ids, a Queen reply stored since the last poll is drawn after the turn, once", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  await turn(chat, { background: msg(3, "assistant", "a background turn's reply") });
  assert.equal(chat.textCount("a background turn's reply"), 1, "a plain-provenance row the stream never showed is drawn");
  assert.equal(chat.textCount("the answer"), 1);
  assert.equal(chat.textCount("do the thing"), 1);
  await chat.app.poll();
  assert.equal(chat.textCount("a background turn's reply"), 1);
  assert.equal(chat.textCount("the answer"), 1);
});

for (const ids of [true, false]) {
  test(`R1 S6: a poll while the stream is finishing draws none of its rows again (ids: ${ids})`, async () => {
    const chat = await openChat({ messages: [msg(1, "user", "hello")] });
    const api = chat.app.api;
    let release;
    let gated = false;
    chat.app.api = async (method, path, body) => {
      if (!gated && chat.streams.length && method === "GET" && path === "/api/chats") {
        gated = true;
        await new Promise(r => { release = r; });
      }
      return api(method, path, body);
    };
    await turn(chat, { ids });
    assert.ok(gated, "the stream's finish is waiting on its refresh");
    await chat.app.poll();
    release();
    await settle();
    await chat.app.poll();
    assert.equal(chat.textCount("do the thing"), 1);
    assert.equal(chat.textCount("the answer"), 1);
  });

  test(`R1 S6: a refresh that fails after the stream leaves the message sent, and the next poll draws none of its rows again (ids: ${ids})`, async () => {
    const chat = await openChat({ messages: [msg(1, "user", "hello")] });
    const api = chat.app.api;
    let failed = false;
    chat.app.api = async (method, path, body) => {
      if (!failed && chat.streams.length && method === "GET" && path === "/api/chats") {
        failed = true;
        throw new Error("network down");
      }
      return api(method, path, body);
    };
    await turn(chat, { ids, background: msg(3, "assistant", "a background turn's reply") });
    assert.ok(failed);
    assert.equal(chat.input.value, "", "a message the server took is not handed back to the box");
    await chat.app.poll();
    assert.equal(chat.textCount("do the thing"), 1);
    assert.equal(chat.textCount("the answer"), 1);
    assert.equal(chat.textCount("a background turn's reply"), 1);
    assert.equal(chat.main.querySelector(".composer-send").disabled, false);
  });
}

test("R1 S6: with ids, an unnamed reply is drawn even in the streamed words; a blank tool-call round is not redrawn", async () => {
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.app.streamSse = async (path, body, handlers) => {
    chat.streams.push([path, body]);
    chat.server.messages.push(msg(5, "user", "do the thing"));
    handlers.thinking({ thinking: "let me look" });
    chat.server.messages.push({ ...msg(6, "assistant", ""), thinking: "let me look", tool_calls: [{ id: "c" }] });
    handlers.message({ content: "the answer" });
    chat.server.messages.push(msg(7, "assistant", "the answer"));
    handlers.message({ content: "", message_id: 7 });
  };
  chat.server.messages.push(msg(3, "assistant", "the answer")); // a background reply, same words
  chat.input.value = "do the thing";
  chat.press("Enter");
  await settle();
  await chat.app.poll();
  assert.equal(chat.textCount("the answer"), 2, "the streamed reply and the unnamed background one");
  assert.equal(chat.textCount("let me look"), 1, "the streamed thinking is not drawn again");
  assert.equal(chat.textCount("do the thing"), 1);
});

// S14: "executing" is a passing state — the next status replaces it.
for (const [status, final] of [["confirmed", /Approved/], ["failed", /Failed — boom/]]) {
  test(`R1 S14: a card seen running elsewhere ends on its real result (${status})`, async () => {
    const chat = await openChat({ actions: [card("a1")] });
    const [drawn] = chat.log.querySelectorAll(".confirm-card");
    chat.server.actions[0].status = "executing";
    await chat.app.poll();
    assert.match(drawn.textContent, /Running — confirmed elsewhere/);
    assert.ok(drawn.classList.contains("resolved"), "its buttons are gone while it runs");
    await chat.app.poll();
    assert.equal(drawn.querySelectorAll(".verdict").length, 1, "a quiet poll adds nothing");
    Object.assign(chat.server.actions[0], { status, result: { error: "boom" } });
    await chat.app.poll();
    assert.doesNotMatch(drawn.textContent, /Running/);
    assert.match(drawn.textContent, final);
    assert.equal(drawn.querySelectorAll(".verdict").length, 1, "one verdict on the card");
    assert.equal(chat.log.querySelectorAll(".confirm-card").length, 1);
  });
}

// S15: an explicitly addressed verdict is the decision, asked once — it goes
// to the resolver now; /btw (read-only, which the server runs beside a
// pending card) goes too; any other slash command stays in the box.
test("R1 S15: /approve <id> typed under the pending card resolves it at once", async () => {
  const chat = await openChat({ actions: [card("a1")] });
  chat.app.streamSse = async (path, body) => {
    chat.streams.push([path, body]);
    if (path.includes("/actions/a1/confirm")) chat.server.actions[0].status = "confirmed";
  };
  chat.input.value = "/approve a1";
  chat.press("Enter");
  await settle();
  assert.deepEqual(chat.streams.map(([path]) => path), ["/api/chats/c1/actions/a1/confirm?surface=web"]);
  assert.equal(chat.input.value, "");
  assert.equal(chat.main.querySelectorAll(".queued-steer-item").length, 0, "not queued");
  assert.ok(chat.log.querySelector(".confirm-card").classList.contains("resolved"));
});

test("R1 S15: /deny <id> under a pending command card denies it on the commands endpoint and the card resolves", async () => {
  const chat = await openChat({ actions: [card("o1", { source: "operator", tool: "set_role" })] });
  const api = chat.app.api;
  chat.app.api = async (method, path, body) => {
    if (method === "POST" && path.endsWith("/commands/o1/deny")) chat.server.actions[0].status = "denied";
    return api(method, path, body);
  };
  chat.input.value = "/deny o1";
  chat.press("Enter");
  await settle();
  assert.deepEqual(chat.posts.map(([path]) => path), ["/api/chats/c1/commands/o1/deny"]);
  assert.ok(chat.log.querySelector(".confirm-card").classList.contains("resolved"), "the card shows the verdict");
  assert.equal(chat.main.querySelector(".composer-send").disabled, false, "nothing waits any more");
});

test("R1 S15: /btw runs beside a pending card; /approve with no id stays in the box", async () => {
  const chat = await openChat({ actions: [card("a1")] });
  chat.input.value = "/btw what does this card do";
  chat.press("Enter");
  await settle();
  assert.deepEqual(chat.streams.map(([path, body]) => [path, body.content, body.read_only]),
    [["/api/chats/c1/messages", "what does this card do", true]]);
  assert.equal(chat.main.querySelector(".composer-send").disabled, true, "the card still holds the composer");
  chat.input.value = "/approve";
  chat.press("Enter");
  await settle();
  assert.equal(chat.input.value, "/approve");
  assert.equal(chat.streams.length, 1);
});

// S17: a held chip's Send sends that message only; the others stay held.
test("R1 S17: Send on the second of two held chips sends only that one; the first stays held", async () => {
  const chat = await openChat();
  let finish;
  chat.app.streamSse = (path, body) => { chat.streams.push([path, body]); return new Promise(r => { finish = r; }); };
  chat.input.value = "go";
  chat.press("Enter");
  await settle();
  for (const text of ["first held", "second held"]) { chat.input.value = text; chat.press("Enter"); }
  await chat.log.querySelector(".chat-stop").click();
  finish();
  await settle();
  const chips = () => chat.main.querySelectorAll(".queued-steer-item");
  assert.equal(chips().length, 2);
  chat.app.streamSse = async (path, body) => { chat.streams.push([path, body]); };
  await chips()[1].querySelectorAll("button").find(b => b.textContent === "Send").click();
  await settle();
  assert.deepEqual(chat.streams.map(([, body]) => body.content), ["go", "second held"]);
  assert.deepEqual(chips().map(c => c.querySelector(".queued-steer-text").textContent), ["first held"]);
  assert.match(chips()[0].textContent, /held/);
});

test("R1 S6: F38's go-ahead line — text and id on one event — is drawn once", async () => {
  const line = "run_shell could not run in this turn: it started on its own. Reply “go ahead”.";
  const chat = await openChat({ messages: [msg(1, "user", "hello")] });
  chat.app.streamSse = async (path, body, handlers) => {
    chat.streams.push([path, body]);
    chat.server.messages.push(msg(5, "user", "do the thing"));
    handlers.message({ content: "the answer" });
    chat.server.messages.push(msg(7, "assistant", "the answer"));
    handlers.message({ content: "", message_id: 7 });
    chat.server.messages.push(msg(8, "assistant", line));
    handlers.message({ content: `\n\n${line}`, message_id: 8 });
  };
  chat.input.value = "do the thing";
  chat.press("Enter");
  await settle();
  await chat.app.poll();
  assert.equal(chat.textCount("the answer"), 1);
  assert.equal(chat.textCount("could not run in this turn"), 1);
  assert.equal(chat.textCount("do the thing"), 1);
});

// ---------- F37: the web says which surface answered a chat card ----------

test("F37: Approve, Deny and a typed /approve <id> on an assistant card send ?surface=web", async () => {
  for (const [how, verb] of [["Approve", "confirm"], ["Deny", "deny"], ["typed", "confirm"]]) {
    const chat = await openChat({ actions: [card("a1")] });
    chat.app.streamSse = async (path, body) => {
      chat.streams.push([path, body]);
      if (path.includes("/actions/a1/")) chat.server.actions[0].status = verb === "confirm" ? "confirmed" : "denied";
    };
    if (how === "typed") { chat.input.value = "/approve a1"; chat.press("Enter"); }
    else await chat.log.querySelector(".confirm-card").querySelectorAll("button").find(b => b.textContent === how).click();
    await settle();
    assert.deepEqual(chat.streams.map(([path]) => path), [`/api/chats/c1/actions/a1/${verb}?surface=web`], how);
  }
});
