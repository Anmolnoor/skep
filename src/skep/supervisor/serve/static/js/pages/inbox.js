/* Phase 10 10.1: the Inbox — review first. Phase 10 R1 (E2): ONE queue,
   GET /api/review-queue — worker gates, standalone cards, memory proposals
   and skills awaiting the human gate, each once, grouped by the wall it
   crosses (riskiest first). The sidebar badge counts the same queue, so the
   two never disagree. Each verdict button is wired to the item's existing
   route (I5). Phase 10 R4 (K1): Approve is inline ONLY for a worker gate or a
   memory proposal — the items whose whole evidence the row shows (a gate's
   every command, a memory's full text) — and not even then when a landing's
   G10 or V1 is warned about. Every chat card (dispatches, landings, PRs,
   captures, schedules, folds, forge…) and every skill opens where its full
   evidence is; Deny stays inline. Keys only move focus; a button press is
   the only verdict (I7).

   Phase 10 R2: a run `skep review` walks with no gate yet (an unlanded
   patch, a G10 disagreement) is a row that opens the run — no inline land
   (H1). A batch gate lists every command its approve grants (H3); a card the
   web cannot confirm says where to answer it (H5); and a verdict's flash says
   what the operation reported, never just "2xx" (H6, R4 K7).

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el } from "../dom.js";
import { streamSse } from "../sse.js";
import { routeLive } from "./run-detail.js";
import { getState, setState } from "../store.js";
import { chatTitle, flash, header, relativeTime, route, shortTaskId, shortTitle } from "../../app.js";

// cards.py BOUNDARIES, riskiest first; a gate with no label sorts last, as itself.
const BOUNDARY_ORDER = ["tainted-dispatch", "patch-landing", "network-egress", "host-shell",
  "outbound-content", "policy-change", "in-sandbox-locked"];
const SOURCE_TEXT = { assistant: "proposed by the Queen", operator: "your command", gate: "a run's gate" };
// H3: a batch longer than this is listed on its review page, not in a row
// (console.py INLINE_COMMANDS; whether a row approves is the server's call).
const INLINE_COMMANDS = 10;

const boundaryRank = gate => {
  const i = BOUNDARY_ORDER.indexOf(gate.boundary);
  return i < 0 ? BOUNDARY_ORDER.length : i;
};

// The review order: boundary group, then oldest first. The review page walks
// the same order for "next".
export function reviewQueue(approvals) {
  return [...approvals].sort((a, b) => boundaryRank(a) - boundaryRank(b)
    || (a.requested_at || "").localeCompare(b.requested_at || ""));
}

// What each kind of item is called, where it lives, and its evidence line.
const FACE = {
  gate: item => ({
    title: `${item.action} · run ${shortTaskId(item.task_id)}`,
    href: `#/review/${item.id}`,
    evidence: item.reason || "approval",
  }),
  card: item => ({
    title: item.headline || item.tool,
    href: `#/chat/${item.chat_id}`,
    evidence: [shortTitle(chatTitle({ title: item.chat_title })), SOURCE_TEXT[item.source] || item.source, item.risk]
      .filter(Boolean).join(" · "),
  }),
  run: item => ({
    title: `${item.g10} · run ${shortTaskId(item.id)}`,
    href: `#/runs/${item.id}`,
    evidence: [item.summary, item.waiting, filesLine(item)].filter(Boolean).join(" · "),
  }),
  memory: item => ({ title: `memory · ${item.memory_class}`, href: "#/memory", evidence: item.content }),
  skill: item => ({
    title: `skill · ${item.id}`,
    href: "#/templates",
    evidence: [item.description, `${item.status} · from ${item.source}`].filter(Boolean).join(" · "),
  }),
};

function filesLine(item) {
  const files = item.files || [];
  if (!files.length) return null;
  const each = files.map(f => `${f.path} +${f.added} −${f.removed}`).join(", ");
  return `${files.length} file${files.length === 1 ? "" : "s"} changed: ${each}${item.files_note ? ` (${item.files_note})` : ""}`;
}

// The existing route each verdict goes through — [path, body]. R4 K1 (SD-2):
// an inline approve exists only when the server's item.inline_approve says
// so (console.inline_approvable) — never inferred here from the kind; else
// it is null, with where to go instead.
function verdicts(item) {
  if (item.kind === "run") {
    // H1: no gate exists until land_run opens one — the run page has the verdict.
    return { approve: null, elsewhere: [`#/runs/${item.id}`, "Open to review →"], deny: null };
  }
  if (item.kind === "gate") {
    const commands = item.commands || [];
    const why = commands.length > INLINE_COMMANDS ? `Review all ${commands.length} commands →` : "Review →";
    return {
      approve: item.inline_approve ? [`/api/approvals/${item.id}/approve`, {}] : null,
      label: commands.length > 1 ? `Approve all ${commands.length}` : "Approve",
      elsewhere: item.inline_approve ? null : [`#/review/${item.id}`, why],
      deny: [`/api/approvals/${item.id}/deny`, {}],
    };
  }
  if (item.kind === "card") {
    // R4 K1: a chat card is approved in its thread, where the whole card is —
    // the row is its headline and evidence. A typed or gate card denies on the
    // command route; the Queen's on the turn route (a stream, one verdict per
    // item the card holds).
    const operator = item.source !== "assistant";
    const lane = operator ? "commands" : "actions";
    return {
      approve: null,
      elsewhere: [`#/chat/${item.chat_id}`, "Open the full card →"],
      deny: [`/api/chats/${item.chat_id}/${lane}/${item.id}/deny`, operator ? undefined : {}],
      stream: !operator,
      items: (item.items || []).length || 1,
    };
  }
  if (item.kind === "memory") {
    return {
      approve: item.inline_approve ? [`/api/memory/proposals/${item.id}/approve`, undefined] : null,
      elsewhere: item.inline_approve ? null : ["#/memory", "Open it in Memory →"],
      deny: [`/api/memory/proposals/${item.id}/reject`, "reason"],
    };
  }
  // R4 K1: a skill is approved in the Library, from what it would install.
  const where = item.taint ? "Read it in the Library, then accept the taint →"
    : !item.approvable ? "Test it in the Library first →"
      // H4: a captured draft is approved from its SKILL.md, not its description
      : item.provenance === "captured" && item.status !== "tested" ? "Read its SKILL.md in the Library first →"
        : "Approve it in the Library →";
  return {
    approve: null,
    elsewhere: ["#/templates", where],
    deny: [`/api/skills/${encodeURIComponent(item.id)}/reject`, {}],
  };
}

// H6: what a verdict's route reported — { failed, warning } — read the way
// each route answers: the Queen's card lane is an SSE stream (one "tool" event
// per item, or "error"), the command lane can be 200 {ok:false,error}, and an
// approve may carry a warning. A 2xx alone is never taken as success.
// R3 J5: the stream goes on into the Queen's turn after the verdicts — only
// the card's own verdict events count (a "tool" event with no `args`, before
// the continuation, at most one per item); a continuation's error after the
// verdicts is a warning about the follow-up, never a failed verdict.
const warningOf = r => r.warning || r.result?.warning; // a payload is {ok, error | result: {…, warning}}

// R4 K7: the operation's own outcome, not the outer ok — a PR not opened or
// not merged (after a landing that did happen) is partial, never green.
export function outcomeTrouble(r) {
  if (!r || r.denied) return null;
  if (r.ok === false) return r.error || r.note || "failed";
  const inner = r.result;
  if (!inner || typeof inner !== "object") return null;
  const why = inner.detail || inner.error || inner.note;
  if (inner.ok === false) return why || "failed";
  if (inner.opened === false) return `partial — the PR was not opened${why ? `: ${why}` : ""}`;
  if (inner.merged === false) return `partial — not merged${why ? `: ${why}` : ""}`;
  return null;
}

async function send(path, body, stream, items = 1) {
  if (stream) {
    const results = [];
    const errors = [];
    const followUp = [];
    let continued = false;
    await streamSse(path, body, {
      tool: d => {
        if (continued || "args" in d || results.length >= items) continued = true;
        else results.push(d.result || {});
      },
      error: d => (results.length ? followUp : errors).push(d.detail),
    });
    if (errors.length) return { failed: errors.join("; ") };
    if (!results.length) return { failed: "no outcome came back — open its thread to see what happened" };
    const failed = results.map(outcomeTrouble).filter(Boolean);
    // R4 T1: fewer verdicts than items is never read as all of them
    const missing = results.length < items ? `incomplete: ${results.length} of ${items} answered` : null;
    if (failed.length || missing) {
      const why = failed.join("; ");
      const counted = failed.length && results.length > 1 ? `${failed.length} of ${results.length} items failed — ${why}` : why;
      return { failed: [missing, counted].filter(Boolean).join("; ") };
    }
    const notes = results.map(warningOf).filter(Boolean);
    if (followUp.length) notes.push(`the Queen's follow-up failed: ${followUp.join("; ")}`);
    return { warning: notes.join("; ") };
  }
  const reply = await api("POST", path, body);
  const trouble = outcomeTrouble(reply);
  if (trouble) return { failed: trouble };
  return { warning: reply && warningOf(reply) };
}

function reviewRow(item) {
  const face = FACE[item.kind](item);
  const loud = item.tamper || item.warning || item.high;
  const { approve, label, elsewhere, deny, stream, items } = verdicts(item);
  const decide = (text, verb, call) => {
    const node = el("button", { type: "button", class: verb === "approve" ? "primary" : "danger ghost" }, text);
    node.addEventListener("click", async () => {
      let [path, body] = call;
      if (body === "reason") {
        const reason = window.prompt(`Why reject this ${item.kind}?`);
        if (!reason) return;
        body = { reason };
      }
      node.disabled = true;
      let outcome;
      try { outcome = await send(path, body, stream, items); }
      catch (e) { flash("bad", e.message); node.disabled = false; return; }
      const done = `${verb === "approve" ? "approved" : "denied"} ${face.title}`;
      if (outcome.failed) flash("bad", `${face.title}: ${outcome.failed}`);
      else if (outcome.warning) flash("warn", `${done} — ⚠ ${outcome.warning}`);
      else flash("ok", done);
      route(); // resolved either way: the list redraws to say so
    });
    return node;
  };
  const mirrored = item.mirrored_by || [];
  const commands = item.commands || [];
  const folded = item.items || [];
  return el("div", { class: `inbox-item review-row${item.kind === "gate" ? " review-item" : ""}`, "data-kind": item.kind },
    el("span", { class: `thread-dot ${loud ? "bad" : "wait"}`, "aria-hidden": "true" }),
    el("a", { class: "inbox-open inbox-title mono", href: face.href }, face.title),
    el("small", {}, `${face.evidence} · ${relativeTime(item.at)}`),
    item.tamper ? el("p", { class: "banner bad", role: "alert" }, `⚠ V1 · ${item.tamper}`) : null,
    item.taint ? el("p", { class: "banner warn" }, item.taint) : null,
    item.high ? el("small", { class: "review-warning" }, `⚠ HIGH · ${item.high}`) : null,
    item.warning && item.warning !== item.tamper ? el("small", { class: "review-warning" }, `⚠ ${item.warning}`) : null,
    item.stale_base ? el("small", { class: "review-warning" }, `⚠ ${item.stale_base}`) : null,
    item.web_refusal ? el("small", { class: "review-warning" }, item.web_refusal) : null,
    item.branch ? el("small", {}, `lands on ${item.branch}`) : null,
    commands.length && commands.length <= INLINE_COMMANDS
      ? el("ul", { class: "inbox-commands" }, commands.map(c => el("li", {}, el("code", { class: "inbox-command mono" }, c))))
      : null,
    folded.length > 1 && folded.length <= INLINE_COMMANDS
      ? el("ul", { class: "inbox-commands" }, folded.map(line => el("li", {}, el("code", { class: "inbox-command mono" }, line))))
      : null,
    folded.length > INLINE_COMMANDS ? el("small", {}, `${folded.length} items — every one runs when the card is approved`) : null,
    mirrored.length
      ? el("small", {}, `also asked in ${mirrored.length} chat card${mirrored.length === 1 ? "" : "s"} — resolved with it: `,
        mirrored.flatMap((m, i) => [i ? ", " : null, el("a", { href: `#/chat/${m.chat_id}` }, m.tool)]))
      : null,
    el("div", { class: "inbox-actions" },
      approve ? decide(label || "Approve", "approve", approve)
        : el("a", { class: "inbox-elsewhere", href: elsewhere[0] }, elsewhere[1]),
      deny ? decide("Deny", "deny", deny) : null));
}

// j/k (and the arrows) move focus between rows; Enter opens one — it is a
// link. No key approves or denies: only a button press does (I7).
function walkable(list) {
  list.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const step = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    const rows = [...list.querySelectorAll(".inbox-open")];
    const next = rows[Math.min(Math.max(rows.indexOf(event.target) + step, 0), rows.length - 1)];
    if (!next) return;
    event.preventDefault();
    setState("inbox-cursor", next.getAttribute("href"));
    next.focus();
  });
}

export async function viewInbox(main) {
  header(main, "Inbox", "Everything waiting on you, riskiest wall first. A worker gate or memory proposal whose row shows all its evidence is approved here; chat cards and skills open where their full evidence is. Deny works here on everything but a run.");
  const live = routeLive(main); // H7 (D7): a late queue draws nothing once the operator moved on
  const { items } = await api("GET", "/api/review-queue");
  if (!live()) return;
  if (!items.length) {
    main.append(el("p", { class: "note" }, "Nothing is waiting on you."));
    return;
  }
  const list = el("div", { class: "inbox-list" },
    el("h2", { class: "inbox-heading" }, `Waiting on you · ${items.length}`));
  let group;
  for (const item of items) {
    const label = item.boundary || "other";
    if (label !== group) {
      group = label;
      list.append(el("h3", { class: "inbox-group mono" }, label));
    }
    list.append(reviewRow(item));
  }
  walkable(list);
  main.append(el("p", { class: "note keys" }, "j / k move · Enter opens · a verdict is a button"), list);
  // Back from a review: land where the operator was, else on the first row.
  const rows = [...list.querySelectorAll(".inbox-open")];
  const cursor = getState("inbox-cursor", null);
  (rows.find(row => row.getAttribute("href") === cursor) || rows[0]).focus();
}
