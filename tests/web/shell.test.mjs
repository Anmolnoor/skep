// graphite-nav: the shell — old hashes normalised in route(), the Settings
// surface on and off per route, the lit item for every page, the close
// button's return route, the breadcrumb, and a fresh home landing on Setup.
// The sidebar anchors are read from the real index.html, so the markup and
// setShellActiveRoute are tested together.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { loadApp } from "./harness.mjs";

const INDEX = readFileSync(new URL("../../src/skep/supervisor/serve/static/index.html", import.meta.url), "utf8");

// Every data-ws anchor in the sidebar, as fake nodes carrying their attributes.
function sidebarLinks(app) {
  const sidebar = INDEX.slice(INDEX.indexOf('<nav class="sidebar"'), INDEX.indexOf('<div class="workspace">'));
  const pagesFrom = sidebar.indexOf('<nav class="sidebar-pages"');
  const pagesTo = sidebar.indexOf("</nav>", pagesFrom);
  return [...sidebar.matchAll(/<a ([^>]*data-ws="[^"]*"[^>]*)>([^<]*)/g)].map(m => {
    const link = app.document.createElement("a");
    for (const [, name, value] of m[1].matchAll(/([\w-]+)="([^"]*)"/g)) {
      link.setAttribute(name, value);
      if (name.startsWith("data-")) link.dataset[name.slice(5)] = value;
    }
    link.textContent = m[2].replace(/&amp;/g, "&").trim();
    link.inPages = m.index > pagesFrom && m.index < pagesTo;
    return link;
  });
}

function shellApp(hash = "#/home", setup = { complete: true, missing: [] }) {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.document.getElementById("app-search").closest = () => null;
  const links = sidebarLinks(app);
  const pages = links.filter(link => link.inPages);
  app.document.querySelectorAll = selector => (
    selector === ".sidebar a[data-ws]" ? links
      : selector === ".sidebar-pages a[data-tab]" ? pages.filter(link => link.dataset.tab) : []);
  app.location.hash = hash;
  const replaced = [];
  app.history.replaceState = (_state, _title, url) => { replaced.push(url); app.location.hash = url; };
  app.api = async (method, path) => {
    if (path === "/api/setup/status") return setup;
    throw new Error(`unstubbed api ${method} ${path}`);
  };
  // Every view becomes a recorder: [its name, ...what the route passed it].
  const calls = [];
  for (const entry of vm.runInContext("routes", app)) {
    const name = entry[1].name;
    entry[1] = async (_main, ...args) => { calls.push([name, ...args]); };
  }
  return { app, nodes, links, pages, replaced, calls };
}

const lit = pages => pages.filter(link => link.classList.contains("active")).map(link => link.textContent);
const surfaceOf = nodes => nodes.shell.dataset.surface;

// The seventeen pages of the surface: route → the item it lights.
const SURFACE = [
  ["#/runs", "Runs"], ["#/approvals", "Approvals"], ["#/assign", "New run"], ["#/schedules", "Schedules"],
  ["#/templates", "Templates & Skills"], ["#/notes", "Notes & Tasks"], ["#/memory", "Memory"],
  ["#/policies/now", "Now"], ["#/policies/decisions", "Decisions"], ["#/policies/grants", "Standing grants"],
  ["#/policies/groups", "Projects & groups"], ["#/policies/defaults", "Defaults"],
  ["#/settings/models", "Models"], ["#/settings/workers", "Workers"], ["#/settings/projects", "Projects"],
  ["#/settings/channels", "Channels"], ["#/settings/setup", "Setup"],
];

test("the surface lists seventeen pages in four captioned groups, in order", () => {
  const { pages } = shellApp();
  assert.deepEqual(pages.map(link => link.textContent), SURFACE.map(([, label]) => label));
  assert.deepEqual(pages.map(link => link.getAttribute("href")), SURFACE.map(([hash]) => hash));
  assert.deepEqual([...INDEX.matchAll(/class="rail-caption">([^<]*)</g)].map(m => m[1]),
    ["Work", "Library", "Policies", "Setup"]);
});

test("each surface page turns the surface on and lights only its own item, with aria-current", () => {
  const { app, nodes, pages } = shellApp();
  for (const [hash, label] of SURFACE) {
    app.setShellActiveRoute(hash);
    assert.equal(surfaceOf(nodes), "settings", hash);
    assert.deepEqual(lit(pages), [label], hash);
    assert.deepEqual(pages.filter(link => link.getAttribute("aria-current") === "page").map(link => link.textContent),
      [label], hash);
  }
});

test("deeper pages light their parent item: a run, a template-prefilled assign, a project", () => {
  const { app, nodes, pages } = shellApp();
  for (const [hash, label] of [["#/runs/abcdef12", "Runs"], ["#/assign?template=nightly", "New run"],
    ["#/projects/acme", "Projects"]]) {
    app.setShellActiveRoute(hash);
    assert.equal(surfaceOf(nodes), "settings", hash);
    assert.deepEqual(lit(pages), [label], hash);
  }
});

test("thread-view routes turn the surface off and light no surface item", () => {
  const { app, nodes, pages, links } = shellApp();
  app.setShellActiveRoute("#/runs");
  for (const hash of ["#/home", "#/chat", "#/chat/c1", "#/inbox"]) {
    app.setShellActiveRoute(hash);
    assert.equal(surfaceOf(nodes), undefined, hash);
    assert.deepEqual(lit(pages), [], hash);
    assert.ok(pages.every(link => link.getAttribute("aria-current") === null), hash);
  }
  // The daily links still follow the route.
  const inbox = links.find(link => link.dataset.ws === "inbox");
  assert.equal(inbox.getAttribute("aria-current"), "page");
});

test("the footer is three icon links — Settings, Runs, Policies — each named for a screen reader", () => {
  const { links } = shellApp();
  const footLinks = links.filter(link => !link.inPages && link.getAttribute("aria-label"));
  assert.deepEqual(footLinks.map(link => link.getAttribute("href")), ["#/settings", "#/runs", "#/policies"]);
  for (const link of footLinks) assert.equal(link.getAttribute("title"), link.getAttribute("aria-label"));
});

test("route() normalises every old hash in place, before anything reads it, and opens the right view", async () => {
  const cases = [
    ["#/settings", "#/settings/models", ["viewSettings", "models"]],
    ["#/projects", "#/settings/projects", ["viewSettings", "projects"]],
    ["#/setup", "#/settings/setup", ["viewSettings", "setup"]],
    ["#/policies", "#/policies/now", ["viewPolicies", "now"]],
  ];
  for (const [from, to, call] of cases) {
    const { app, nodes, pages, replaced, calls } = shellApp(from);
    await app.routeImpl();
    assert.deepEqual(replaced, [to], from);
    assert.equal(app.location.hash, to, from);
    assert.deepEqual(calls, [call], from);
    // The active item and the surface read the normalised hash, not the alias.
    assert.equal(surfaceOf(nodes), "settings", from);
    assert.equal(lit(pages).length, 1, from);
  }
});

test("route(): every other old hash lands where it did, unrewritten; an unknown one goes Home", async () => {
  const cases = [
    ["#/home", ["viewHome"]], ["", ["viewHome"]], ["#/chat", ["viewChat"]], ["#/chat/c1", ["viewChat", "c1"]],
    ["#/inbox", ["viewInbox"]], ["#/runs", ["viewRuns"]], ["#/runs/abc", ["viewRunDetail", "abc"]],
    ["#/approvals", ["viewApprovals"]], ["#/assign", ["viewAssign"]], ["#/assign?template=x", ["viewAssign"]],
    ["#/schedules", ["viewSchedules"]], ["#/templates", ["viewTemplates"]], ["#/notes", ["viewNotesTasks"]],
    ["#/memory", ["viewMemory"]], ["#/policies/grants", ["viewPolicies", "grants"]],
    ["#/settings/workers", ["viewSettings", "workers"]], ["#/projects/acme", ["viewProjectDetail", "acme"]],
  ];
  for (const [hash, call] of cases) {
    const { app, replaced, calls } = shellApp(hash);
    await app.routeImpl();
    assert.deepEqual(replaced, [], hash);
    assert.deepEqual(calls, [call], hash);
  }
  const { app, calls } = shellApp("#/nowhere");
  await app.routeImpl();
  assert.deepEqual(calls, []);
  assert.equal(app.location.hash, "#/home");
});

test("first run: an incomplete setup lands on Settings / Setup in the surface, with no redirect loop", async () => {
  const incomplete = { complete: false, missing: ["llm"] };
  const { app, nodes, pages, calls } = shellApp("#/home", incomplete);
  await app.routeImpl();
  assert.equal(app.location.hash, "#/setup"); // the gate's redirect (a hashchange follows)
  assert.deepEqual(calls, []);
  await app.routeImpl(); // that hashchange
  assert.equal(app.location.hash, "#/settings/setup");
  assert.deepEqual(calls, [["viewSettings", "setup"]]);
  assert.equal(surfaceOf(nodes), "settings");
  assert.deepEqual(lit(pages), ["Setup"]);
  // Setup's links to Models and Projects open before setup is complete; others wait.
  for (const tab of ["models", "projects"]) {
    const next = shellApp(`#/settings/${tab}`, incomplete);
    await next.app.routeImpl();
    assert.deepEqual(next.calls, [["viewSettings", tab]], tab);
  }
  const runs = shellApp("#/runs", incomplete);
  await runs.app.routeImpl();
  assert.equal(runs.app.location.hash, "#/setup");
  assert.deepEqual(runs.calls, []);
});

test("the close button shows only in the surface and returns to where the operator came from", async () => {
  const { app, nodes } = shellApp();
  app.installShellHandlers();
  const close = nodes["surface-close"];
  app.updateShellChrome("#/home");
  assert.equal(close.hidden, true);
  app.updateShellChrome("#/policies/now");
  assert.equal(close.hidden, false);
  app.setShellActiveRoute("#/chat/c1");
  app.setShellActiveRoute("#/runs");
  app.setShellActiveRoute("#/policies/now"); // moving inside the surface keeps the way back
  await close.click();
  assert.equal(app.location.hash, "#/chat/c1");
  // Opened straight into the surface (a reload, a deep link): close goes Home.
  const fresh = shellApp();
  fresh.app.installShellHandlers();
  fresh.app.setShellActiveRoute("#/settings/models");
  await fresh.nodes["surface-close"].click();
  assert.equal(fresh.app.location.hash, "#/home");
});

test("the header reads Settings / <page> in the surface, and the plain title outside it", () => {
  const { app, nodes } = shellApp();
  const title = (hash, text) => {
    app.location.hash = hash;
    app.header(null, text, "subtitle");
    return nodes["topbar-title"].textContent;
  };
  assert.equal(title("#/runs", "Runs"), "Settings / Runs");
  assert.equal(title("#/runs/abc", "run abc"), "Settings / run abc");
  assert.equal(title("#/projects/acme", "project acme"), "Settings / project acme");
  assert.equal(title("#/policies/grants", "Policies"), "Settings / Policies / Standing grants");
  assert.equal(title("#/settings/models", "Settings"), "Settings / Models");
  assert.equal(title("#/settings/workers", "Workers"), "Settings / Workers");
  assert.equal(title("#/home", "Home"), "Home");
  assert.equal(title("", "Home"), "Home");
  assert.equal(nodes["topbar-title"].title, "subtitle");
});

test("the thread list follows the route on hashchange, even while the surface hides it", () => {
  const { app, nodes } = shellApp("#/home");
  const listeners = {};
  app.addEventListener = (type, fn) => { listeners[type] = fn; };
  app.installSidebar();
  app.renderThreads([{ chat_id: "c1", title: "one", source: "web", project_id: null,
    updated_at: "2026-11-18T10:00:00Z", pending_cards: 0, active_runs: 0, turn_active: false }]);
  const row = () => nodes["sidebar-threads"].querySelector(".sidebar-chat-item");
  assert.ok(!row().classList.contains("active"));
  app.location.hash = "#/chat/c1";
  listeners.hashchange();
  assert.ok(row().classList.contains("active"));
});

test("a project's group edit opens the Groups section by name, so the lit item is the one shown", async () => {
  const { app } = shellApp("#/projects/acme");
  const stored = {};
  app.sessionStorage = { setItem: (k, v) => { stored[k] = v; } };
  app.api = async (_method, path) => {
    if (path === "/api/projects") {
      return { projects: [{ project_id: "acme", phase: "maintain", strategy: "x", bindings: [], policy: { policy_groups: ["py"] } }] };
    }
    if (path === "/api/schedules") return { schedules: [] };
    if (path === "/api/runs?limit=100") return { runs: [] };
    return {};
  };
  app.header = () => {};
  const main = app.el("main");
  await app.viewProjectDetail(main, "acme").catch(() => {}); // later reads are not this test's
  const edit = main.querySelectorAll(".project-group-edit")[0];
  await edit.click();
  assert.equal(app.location.hash, "#/policies/groups");
  assert.deepEqual(JSON.parse(stored["skep-group-edit"]), { group: "py", project: "acme" });
});
