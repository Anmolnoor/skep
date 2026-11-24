/* Phase 10 10.1: the Inbox — review first. Phase 10 R1 (E2): ONE queue,
   GET /api/review-queue — worker gates, standalone cards, memory proposals
   and skills awaiting the human gate, each once, grouped by the wall it
   crosses (riskiest first). The sidebar badge counts the same queue, so the
   two never disagree. Each row has Approve and Deny buttons wired to the
   item's existing route (I5); an item whose evidence needs more than a row —
   a landing G10 or V1 warned about, a HIGH card, a tainted or untested skill
   — sends you where that evidence is instead of approving inline. Keys only
   move focus; a button press is the only verdict (I7).

   Phase 10 R2: a run `skep review` walks with no gate yet (an unlanded
   patch, a G10 disagreement) is a row that opens the run — no inline land
   (H1). A landing card is warned like a gate (H2); a batch gate lists every
   command its approve grants (H3); a captured draft is approved from its
   SKILL.md (H4); a card the web cannot confirm is read-only (H5); and a
   verdict's flash says what the route reported, never just "2xx" (H6).

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
// H3: a batch longer than this is read on its review page, not in a row.
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

// A landing whose G10 or V1 evidence needs reading before it is approved.
const warnedLanding = item => item.boundary === "patch-landing" && (item.warning || item.tamper);

// The existing route each verdict goes through — [path, body]. An approve
// that needs its evidence in view is null, with where to go instead.
function verdicts(item) {
  if (item.kind === "run") {
    // H1: no gate exists until land_run opens one — the run page has the verdict.
    return { approve: null, elsewhere: [`#/runs/${item.id}`, "Open to review →"], deny: null };
  }
  if (item.kind === "gate") {
    const commands = item.commands || [];
    const long = commands.length > INLINE_COMMANDS;
    const why = warnedLanding(item) ? "Review →" : long ? `Review all ${commands.length} commands →` : null;
    return {
      approve: why ? null : [`/api/approvals/${item.id}/approve`, {}],
      label: commands.length > 1 ? `Approve all ${commands.length}` : "Approve",
      elsewhere: why ? [`#/review/${item.id}`, why] : null,
      deny: [`/api/approvals/${item.id}/deny`, {}],
    };
  }
  if (item.kind === "card") {
    // A typed or gate card resolves on the command route; the Queen's on the
    // turn route (a stream), keep "once": an approve from the Inbox stores no grant.
    const operator = item.source !== "assistant";
    const lane = operator ? "commands" : "actions";
    const base = `/api/chats/${item.chat_id}/${lane}/${item.id}`;
    // H2: the full card keeps its proposed branch; H5: so does the refusal's thread.
    // R3 J1: a card carries G10 / V1 only for runs its execution lands or
    // publishes (land_run, open_pr…) — any of it sends you to the full card.
    // R3 J2: a fold runs every item — each is listed and the approve names
    // how many it saw (the server refuses a card that holds another set);
    // past the batch-gate cap it is read in its thread.
    const count = (item.items || []).length || 1;
    const away = item.high || item.warning || item.tamper || item.web_refusal || count > INLINE_COMMANDS;
    return {
      approve: away ? null : [`${base}/confirm`, operator ? undefined : { keep: "once", items: count }],
      label: count > 1 ? `Approve all ${count}` : "Approve",
      elsewhere: away ? [`#/chat/${item.chat_id}`, "Open the full card in its thread →"] : null,
      deny: [`${base}/deny`, operator ? undefined : {}],
      stream: !operator,
    };
  }
  if (item.kind === "memory") {
    return {
      approve: [`/api/memory/proposals/${item.id}/approve`, undefined],
      deny: [`/api/memory/proposals/${item.id}/reject`, "reason"],
    };
  }
  const blocked = item.taint ? "Read it in the Library, then accept the taint →"
    : !item.approvable ? "Test it in the Library first →"
      // H4: a captured draft is approved from its SKILL.md, not its description
      : item.provenance === "captured" && item.status !== "tested" ? "Read its SKILL.md in the Library first →"
        : null;
  return {
    approve: blocked ? null : [`/api/skills/${encodeURIComponent(item.id)}/approve`, {}],
    elsewhere: blocked ? ["#/templates", blocked] : null,
    deny: [`/api/skills/${encodeURIComponent(item.id)}/reject`, {}],
  };
}

// H6: what a verdict's route reported — { failed, warning } — read the way
// each route answers: the Queen's card lane is an SSE stream (one "tool" event
// per item, or "error"), the command lane can be 200 {ok:false,error}, and an
// approve may carry a warning. A 2xx alone is never taken as success.
async function send(path, body, stream) {
  if (stream) {
    const results = [];
    const errors = [];
    await streamSse(path, body, {
      tool: d => results.push(d.result || {}),
      error: d => errors.push(d.detail),
    });
    const failed = results.filter(r => !r.ok && !r.denied);
    if (errors.length) return { failed: errors.join("; ") };
    if (!results.length) return { failed: "no outcome came back — open its thread to see what happened" };
    if (failed.length) {
      const why = failed.map(r => r.error || r.note || "failed").join("; ");
      return { failed: results.length > 1 ? `${failed.length} of ${results.length} items failed — ${why}` : why };
    }
    return { warning: results.map(r => r.result?.warning).filter(Boolean).join("; ") };
  }
  const reply = await api("POST", path, body);
  if (reply && reply.ok === false && !reply.denied) return { failed: reply.error || reply.note || "failed" };
  return { warning: reply?.warning || reply?.result?.warning };
}

function reviewRow(item) {
  const face = FACE[item.kind](item);
  const loud = item.tamper || item.warning || item.high;
  const { approve, label, elsewhere, deny, stream } = verdicts(item);
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
      try { outcome = await send(path, body, stream); }
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
    folded.length > INLINE_COMMANDS ? el("small", {}, `${folded.length} items — every one runs on approve`) : null,
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
  header(main, "Inbox", "Everything waiting on you, riskiest wall first. Approve or deny here, or open an item for its full evidence.");
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
