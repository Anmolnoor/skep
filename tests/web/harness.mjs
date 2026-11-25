// Load the no-build web UI (app.js) into a node:vm context with a fake DOM,
// so its render functions run under `node --test` — no npm, no jsdom. The
// boot section (connect, route, poll) is cut off; everything above it is
// plain function/const declarations plus three listener registrations.
// H1: the modules app.js imports (sidebar.js, room.js) load into the same
// context with their import/export lines blanked, so both sides of the import
// cycle resolve to the one set of globals a test can stub. Phase 10 10.1: and
// what those import in turn (js/, js/pages/) — every module once, deps first.
// ponytail: the fake DOM covers what app.js's render paths touch (el(),
// append, textContent, listeners, a class/tag querySelectorAll); grow it when
// a test reaches for more.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC = new URL("../../src/skep/supervisor/serve/static/", import.meta.url);
// Blank, never delete, so line numbers in a stack trace stay true.
const unmodule = text => text.replace(/^import .*$/gm, "").replace(/^export /gm, "");
const IMPORT = /^import .* from "(\.{1,2}\/[\w./-]+\.js)";$/gm;

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.style = { setProperty(name, value) { this[name] = String(value); } };
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = ""; // inputs and selects start empty, as in a browser
    const classes = () => new Set(this.className.split(/\s+/).filter(Boolean));
    this.classList = {
      add: (...names) => { this.className = [...new Set([...classes(), ...names])].join(" "); },
      remove: (...names) => { this.className = [...classes()].filter(c => !names.includes(c)).join(" "); },
      contains: name => classes().has(name),
      toggle: (name, on) => {
        const want = on ?? !classes().has(name);
        want ? this.classList.add(name) : this.classList.remove(name);
        return want;
      },
    };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() {}
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatchEvent(event) {
    for (const fn of this.listeners[event.type] || []) fn(event);
    return true;
  }
  append(...nodes) {
    this._text = undefined;
    for (const n of nodes) {
      const node = typeof n === "object" ? n : new FakeText(n);
      if (node.parentNode) node.remove?.(); // appending moves a node, as in the DOM
      node.parentNode = this;
      this.children.push(node);
    }
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this);
    this.parentNode = null;
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get textContent() { return this._text ?? this.children.map(c => c.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  // Fire listeners and wait for any async handler, so a test can assert after.
  async click() {
    await Promise.all((this.listeners.click || []).map(fn => fn({ target: this, preventDefault() {}, stopPropagation() {} })));
  }
  // ".cls" or "tag", optionally with one [attr="value"] — enough to find a
  // card's parts, and a drawn card by its data-action-id (v133).
  querySelectorAll(selector) {
    const [, base, attr, value] = selector.match(/^([^[]*)(?:\[([\w-]+)="([^"]*)"\])?$/);
    const matchBase = base.startsWith(".")
      ? n => n.classList?.contains(base.slice(1))
      : n => n.tagName === base.toUpperCase();
    const match = attr ? n => matchBase(n) && n.getAttribute?.(attr) === value : matchBase;
    const found = [];
    const walk = n => { for (const c of n.children || []) { if (match(c)) found.push(c); walk(c); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class FakeText {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); }
}

export function loadApp() {
  const source = readFileSync(new URL("app.js", STATIC), "utf8");
  const cut = source.indexOf("// ---------- boot ----------");
  if (cut < 0) throw new Error("app.js boot marker moved — update tests/web/harness.mjs");
  const document = {
    createElement: tag => new FakeNode(tag),
    createTextNode: text => new FakeText(text),
    createDocumentFragment: () => new FakeNode("#fragment"),
    // A text-node walker (renderMarkdown's run-token linkify), in tree order.
    createTreeWalker: root => {
      const nodes = [];
      const walk = n => { for (const c of n.children || []) { if (c.nodeType === 3) nodes.push(c); walk(c); } };
      walk(root);
      return { currentNode: null, nextNode() { this.currentNode = nodes.shift() ?? null; return Boolean(this.currentNode); } };
    },
    getElementById: () => new FakeNode("div"),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    body: new FakeNode("body"),
    cookie: "",
  };
  const store = new Map();
  const context = vm.createContext({
    document,
    localStorage: {
      getItem: k => store.get(k) ?? null,
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: { hash: "" },
    history: { replaceState() {} },
    addEventListener() {},
    console, setTimeout, clearTimeout, URL, URLSearchParams,
    Event: class { constructor(type) { this.type = type; } },
    NodeFilter: { SHOW_TEXT: 4 },
  });
  context.window = context;
  // Imported modules first, as the browser evaluates them: app.js's top level
  // (the routes table) names their functions; they name app.js's only in calls.
  // A module importing app.js back (the cycle) stops there — app.js runs last.
  const loaded = new Set([new URL("app.js", STATIC).href]);
  const load = (url) => {
    if (loaded.has(url.href)) return;
    loaded.add(url.href);
    const text = readFileSync(url, "utf8");
    for (const [, spec] of text.matchAll(IMPORT)) load(new URL(spec, url));
    vm.runInContext(unmodule(text), context, { filename: url.href.slice(STATIC.href.length) });
  };
  for (const [, spec] of source.matchAll(IMPORT)) load(new URL(spec, STATIC));
  vm.runInContext(unmodule(source.slice(0, cut)), context, { filename: "app.js" });
  // Every network call fails loudly unless a test stubs the route it expects.
  context.apiImpl = context.api; // the real fetch path (js/api.js), for tests that drive it
  context.api = async (method, path) => { throw new Error(`unstubbed api ${method} ${path}`); };
  context.flashes = [];
  context.flash = (kind, text) => context.flashes.push([kind, text]);
  context.routeImpl = context.route; // the real router, for tests that drive it
  context.route = () => {};
  return context;
}

// Text of every node under `root` carrying class `cls`.
export const texts = (root, cls) => root.querySelectorAll(`.${cls}`).map(n => n.textContent);
