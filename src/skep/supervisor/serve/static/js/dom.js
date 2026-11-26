/* Phase 10 10.1: the safe DOM builder. Every child string goes in as a text
   node, so untrusted text (run output, model replies, diffs, file names) is
   never parsed as HTML. No innerHTML here, and none needed by its callers. */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function iconButton(label, icon, attrs = {}) {
  return el("button", {
    type: "button",
    title: label,
    "aria-label": label,
    ...attrs,
  }, el("span", { class: "button-icon", "aria-hidden": "true" }, icon));
}

// A 404's face (route() renders it): what is missing, and a way back.
export function notFound(detail) {
  return el("div", { class: "empty-state not-found", role: "status" },
    el("h2", {}, "Not found"),
    el("p", {}, detail),
    el("a", { class: "home-link", href: "#/inbox" }, "Back to the Inbox →"));
}

// v134-F21: a card takes one verdict at a time. The first click disables
// every button in its row (a browser then drops the next click); call the
// returned release after a failed request so the operator can retry. A
// success keeps them held: the card redraws resolved.
export function holdButtons(row) {
  const buttons = [...row.querySelectorAll("button")];
  for (const b of buttons) b.disabled = true;
  return () => { for (const b of buttons) b.disabled = false; };
}

// v134-F21 r1: the verdicts in flight, by card key ("gate:<review_id>", …),
// each with the row drawn for it last. A view redrawn mid-request (a filter
// switch, a poll's route(), another row's verdict) draws that card held.
const verdictsInFlight = new Map();

// Hold `row` for the verdict on `key`. landed(): the key is forgotten and
// the row stays held until its card redraws resolved. failed(): the row
// drawn last for the key gets its buttons back so the operator can retry.
export function holdVerdict(key, row) {
  verdictsInFlight.set(key, row);
  holdButtons(row);
  const settle = () => {
    const drawn = verdictsInFlight.get(key);
    verdictsInFlight.delete(key);
    return drawn;
  };
  return {
    landed: settle,
    failed: () => { for (const b of settle()?.querySelectorAll("button") || []) b.disabled = false; },
  };
}

// Call once a card's verdict row holds its buttons: a row drawn while its
// card's verdict is in flight is drawn held.
export function drawVerdictRow(key, row) {
  if (!verdictsInFlight.has(key)) return;
  verdictsInFlight.set(key, row);
  holdButtons(row);
}
