// Load the no-build web UI (app.js) into a node:vm context with a fake DOM,
// so its render functions run under `node --test` — no npm, no jsdom. The
// boot section (connect, route, poll) is cut off; everything above it is
// plain function/const declarations plus three listener registrations.
// ponytail: the fake DOM covers what app.js's render paths touch (el(),
// append, textContent, listeners, a class/tag querySelectorAll); grow it when
// a test reaches for more.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const APP = new URL("../../src/skep/supervisor/serve/static/app.js", import.meta.url);

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.style = {};
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
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  append(...nodes) {
    this._text = undefined;
    for (const n of nodes) this.children.push(typeof n === "object" ? n : new FakeText(n));
  }
  appendChild(node) { this.append(node); return node; }
  get textContent() { return this._text ?? this.children.map(c => c.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  // Fire listeners and wait for any async handler, so a test can assert after.
  async click() {
    await Promise.all((this.listeners.click || []).map(fn => fn({ target: this, preventDefault() {} })));
  }
  // ".cls" or "tag" only — enough to find a card's parts.
  querySelectorAll(selector) {
    const match = selector.startsWith(".")
      ? n => n.classList?.contains(selector.slice(1))
      : n => n.tagName === selector.toUpperCase();
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
  const source = readFileSync(APP, "utf8");
  const cut = source.indexOf("// ---------- boot ----------");
  if (cut < 0) throw new Error("app.js boot marker moved — update tests/web/harness.mjs");
  const document = {
    createElement: tag => new FakeNode(tag),
    createTextNode: text => new FakeText(text),
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
  });
  context.window = context;
  vm.runInContext(source.slice(0, cut), context, { filename: "app.js" });
  // Every network call fails loudly unless a test stubs the route it expects.
  context.api = async (method, path) => { throw new Error(`unstubbed api ${method} ${path}`); };
  context.flashes = [];
  context.flash = (kind, text) => context.flashes.push([kind, text]);
  context.route = () => {};
  return context;
}

// Text of every node under `root` carrying class `cls`.
export const texts = (root, cls) => root.querySelectorAll(`.${cls}`).map(n => n.textContent);
