// graphite-nav: the shell — old hashes normalised in route() and a fresh
// home landing on Setup.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { loadApp } from "./harness.mjs";

function shellApp(hash = "#/home", setup = { complete: true, missing: [] }) {
  const app = loadApp();
  const nodes = {};
  app.document.getElementById = id => (nodes[id] ||= app.el("div"));
  app.document.getElementById("app-search").closest = () => null;
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
  return { app, nodes, replaced, calls };
}

test("route() normalises every old hash in place, before anything reads it, and opens the right view", async () => {
  const cases = [
    ["#/settings", "#/settings/models", ["viewSettings", "models"]],
    ["#/projects", "#/settings/projects", ["viewSettings", "projects"]],
    ["#/setup", "#/settings/setup", ["viewSettings", "setup"]],
    ["#/policies", "#/policies/now", ["viewPolicies", "now"]],
  ];
  for (const [from, to, call] of cases) {
    const { app, replaced, calls } = shellApp(from);
    await app.routeImpl();
    assert.deepEqual(replaced, [to], from);
    assert.equal(app.location.hash, to, from);
    assert.deepEqual(calls, [call], from);
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

test("first run: an incomplete setup lands on the Setup section, with no redirect loop", async () => {
  const incomplete = { complete: false, missing: ["llm"] };
  const { app, calls } = shellApp("#/home", incomplete);
  await app.routeImpl();
  assert.equal(app.location.hash, "#/setup"); // the gate's redirect (a hashchange follows)
  assert.deepEqual(calls, []);
  await app.routeImpl(); // that hashchange
  assert.equal(app.location.hash, "#/settings/setup");
  assert.deepEqual(calls, [["viewSettings", "setup"]]);
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
