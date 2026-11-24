/* Phase 10 10.1: the Inbox — review first. Phase 10 R1 (E2): ONE queue,
   GET /api/review-queue — worker gates, standalone cards, memory proposals
   and skills awaiting the human gate, each once, grouped by the wall it
   crosses (riskiest first). The sidebar badge counts the same queue, so the
   two never disagree. Each row has Approve and Deny buttons wired to the
   item's existing route (I5); an item whose evidence needs more than a row —
   a landing G10 or V1 warned about, a HIGH card, a tainted or untested skill
   — sends you where that evidence is instead of approving inline. Keys only
   move focus; a button press is the only verdict (I7).

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el } from "../dom.js";
import { getState, setState } from "../store.js";
import { chatTitle, flash, header, relativeTime, route, shortTaskId, shortTitle } from "../../app.js";

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
  memory: item => ({ title: `memory · ${item.memory_class}`, href: "#/memory", evidence: item.content }),
  skill: item => ({
    title: `skill · ${item.id}`,
    href: "#/templates",
    evidence: [item.description, `${item.status} · from ${item.source}`].filter(Boolean).join(" · "),
  }),
};

// The existing route each verdict goes through — [path, body]. An approve
// that needs its evidence in view is null, with where to go instead.
function verdicts(item) {
  if (item.kind === "gate") {
    const warned = item.boundary === "patch-landing" && (item.warning || item.tamper);
    return {
      approve: warned ? null : [`/api/approvals/${item.id}/approve`, {}],
      elsewhere: warned ? [`#/review/${item.id}`, "Review →"] : null,
      deny: [`/api/approvals/${item.id}/deny`, {}],
    };
  }
  if (item.kind === "card") {
    // A typed or gate card resolves on the command route; the Queen's on the
    // turn route, keep "once": an approve from the Inbox stores no grant.
    const operator = item.source !== "assistant";
    const lane = operator ? "commands" : "actions";
    const base = `/api/chats/${item.chat_id}/${lane}/${item.id}`;
    return {
      approve: item.high ? null : [`${base}/confirm`, operator ? undefined : { keep: "once" }],
      elsewhere: item.high ? [`#/chat/${item.chat_id}`, "Open the full card in its thread →"] : null,
      deny: [`${base}/deny`, undefined],
    };
  }
  if (item.kind === "memory") {
    return {
      approve: [`/api/memory/proposals/${item.id}/approve`, undefined],
      deny: [`/api/memory/proposals/${item.id}/reject`, "reason"],
    };
  }
  const blocked = item.taint ? "Read it in the Library, then accept the taint →"
    : !item.approvable ? "Test it in the Library first →" : null;
  return {
    approve: blocked ? null : [`/api/skills/${encodeURIComponent(item.id)}/approve`, {}],
    elsewhere: blocked ? ["#/templates", blocked] : null,
    deny: [`/api/skills/${encodeURIComponent(item.id)}/reject`, {}],
  };
}

function reviewRow(item) {
  const face = FACE[item.kind](item);
  const loud = item.tamper || item.warning || item.high;
  const { approve, elsewhere, deny } = verdicts(item);
  const decide = (label, verb, call) => {
    const node = el("button", { type: "button", class: verb === "approve" ? "primary" : "danger ghost" }, label);
    node.addEventListener("click", async () => {
      let [path, body] = call;
      if (body === "reason") {
        const reason = window.prompt(`Why reject this ${item.kind}?`);
        if (!reason) return;
        body = { reason };
      }
      node.disabled = true;
      try {
        await api("POST", path, body);
        flash("ok", `${verb === "approve" ? "approved" : "denied"} ${face.title}`);
        route();
      } catch (e) { flash("bad", e.message); node.disabled = false; }
    });
    return node;
  };
  const mirrored = item.mirrored_by || [];
  return el("div", { class: `inbox-item review-row${item.kind === "gate" ? " review-item" : ""}`, "data-kind": item.kind },
    el("span", { class: `thread-dot ${loud ? "bad" : "wait"}`, "aria-hidden": "true" }),
    el("a", { class: "inbox-open inbox-title mono", href: face.href }, face.title),
    el("small", {}, `${face.evidence} · ${relativeTime(item.at)}`),
    item.tamper ? el("p", { class: "banner bad", role: "alert" }, `⚠ V1 · ${item.tamper}`) : null,
    item.taint ? el("p", { class: "banner warn" }, item.taint) : null,
    item.high ? el("small", { class: "review-warning" }, `⚠ HIGH · ${item.high}`) : null,
    item.warning && item.warning !== item.tamper ? el("small", { class: "review-warning" }, `⚠ ${item.warning}`) : null,
    item.stale_base ? el("small", { class: "review-warning" }, `⚠ ${item.stale_base}`) : null,
    mirrored.length
      ? el("small", {}, `also asked in ${mirrored.length} chat card${mirrored.length === 1 ? "" : "s"} — resolved with it: `,
        mirrored.map(m => el("a", { href: `#/chat/${m.chat_id}` }, m.tool)))
      : null,
    el("div", { class: "inbox-actions" },
      approve ? decide("Approve", "approve", approve)
        : el("a", { class: "inbox-elsewhere", href: elsewhere[0] }, elsewhere[1]),
      decide("Deny", "deny", deny)));
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
  const { items } = await api("GET", "/api/review-queue");
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
