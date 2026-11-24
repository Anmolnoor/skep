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
