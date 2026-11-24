/* Phase 10 10.1: the Inbox — review first. Every pending gate, grouped by the
   boundary it crosses (riskiest group first), each opening its review page
   (#/review/<id>: diff → forensics → verdict); then the confirm cards, each
   resolved in its own thread. The list itself holds no verb (I6): the verdict
   is given on the review page, with the evidence in view.

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el } from "../dom.js";
import { getState, setState } from "../store.js";
import { chatTitle, header, relativeTime, shortTaskId, shortTitle } from "../../app.js";

// cards.py BOUNDARIES, riskiest first; a gate with no label sorts last, as itself.
const BOUNDARY_ORDER = ["tainted-dispatch", "patch-landing", "network-egress", "host-shell",
  "outbound-content", "policy-change", "in-sandbox-locked"];
const SOURCE_TEXT = { assistant: "proposed by the Queen", operator: "your command", gate: "a run's gate" };

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

function reviewItem(gate) {
  const run = gate.run || {};
  const loud = gate.reverification_warning;
  return el("a", { class: "inbox-item review-item searchable", href: `#/review/${gate.review_id}` },
    el("span", { class: `thread-dot ${loud ? "bad" : "wait"}`, "aria-hidden": "true" }),
    el("span", { class: "inbox-title mono" }, `${gate.action} · run ${shortTaskId(gate.task_id)}`),
    el("small", {}, `${gate.reason || (run.instructions || "").slice(0, 90) || "approval"} · ${relativeTime(gate.requested_at)}`),
    loud ? el("small", { class: "review-warning" }, `⚠ ${loud}`) : null,
    gate.stale_base ? el("small", { class: "review-warning" }, "⚠ the base moved since this run started") : null);
}

const cardItem = card => el("a", { class: "inbox-item searchable", href: `#/chat/${card.chat_id}` },
  el("span", { class: "thread-dot wait", "aria-hidden": "true" }),
  el("span", { class: "inbox-title mono" }, card.tool),
  el("small", {}, `${shortTitle(chatTitle({ title: card.chat_title }))} · ${SOURCE_TEXT[card.source] || card.source} · ${relativeTime(card.created_at)}`));

// j/k (and the arrows) walk the rows; Enter opens one — it is a link.
function walkable(list) {
  list.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const step = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    const rows = [...list.querySelectorAll(".inbox-item")];
    const next = rows[Math.min(Math.max(rows.indexOf(event.target) + step, 0), rows.length - 1)];
    if (!next) return;
    event.preventDefault();
    setState("inbox-cursor", next.getAttribute("href"));
    next.focus();
  });
}

export async function viewInbox(main) {
  header(main, "Inbox", "Everything waiting on you. Review a gate's diff and forensics before the verdict; a card is resolved in its thread.");
  // /api/inbox is what the sidebar badge counts; /api/approvals adds the full
  // gate record (and the gates a chat card mirrors), so every gate is reviewable.
  const [{ cards }, { approvals }] = await Promise.all([api("GET", "/api/inbox"), api("GET", "/api/approvals")]);
  if (!cards.length && !approvals.length) {
    main.append(el("p", { class: "note" }, "Nothing is waiting on you."));
    return;
  }
  const list = el("div", { class: "inbox-list" });
  const gates = reviewQueue(approvals);
  if (gates.length) {
    list.append(el("h2", { class: "inbox-heading" }, `Review queue · ${gates.length}`));
    let group = null;
    for (const gate of gates) {
      const label = gate.boundary || "other";
      if (label !== group) {
        group = label;
        list.append(el("h3", { class: "inbox-group mono" }, label));
      }
      list.append(reviewItem(gate));
    }
  }
  if (cards.length) {
    list.append(el("h2", { class: "inbox-heading" }, `Cards in threads · ${cards.length}`), ...cards.map(cardItem));
  }
  walkable(list);
  main.append(el("p", { class: "note keys" }, "j / k move · Enter opens"), list);
  // Back from a review: land where the operator was, else on the first row.
  const rows = [...list.querySelectorAll(".inbox-item")];
  const cursor = getState("inbox-cursor", null);
  (rows.find(row => row.getAttribute("href") === cursor) || rows[0]).focus();
}
