// v124-F2 (DD-10): the policy tree — every registry key and every scope·action
// once, each leaf in Group Policy words, read from the endpoints that already
// exist (registry, rsop, explain, schedules). Read-only.
// v124 review: every payload here is the engine's real answer on a throwaway
// home (tests/web/fixtures/, generated and pinned by
// tests/supervisor/test_policy_tree_fixtures.py) — never a hand-made row.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { loadApp } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));

// A locked builder baseline, project app bound to /fixture/app, seven schedules.
const LOCKED = fixture("policy-locked");
// The same home with nothing approved (the zero state binds), and with the lock tampered.
const NONE = fixture("policy-none");
const TAMPERED = fixture("policy-tampered");
const REGISTRY = LOCKED["GET /api/policy/registry"];
const SCHEDULES = LOCKED["GET /api/schedules"];
const RSOP_ROUTE = "GET /api/repos/%2Ffixture%2Fapp/rsop";

function withTree(routes = {}) {
  const app = loadApp();
  const calls = [];
  const table = { ...LOCKED, ...routes };
  app.api = async (method, path) => {
    calls.push([method, path]);
    const key = Object.keys(table).filter(k => `${method} ${path}`.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`unstubbed api ${method} ${path}`);
    const out = table[key];
    return typeof out === "function" ? out(path) : out;
  };
  return { app, calls, main: app.el("main") };
}

async function openTree(routes) {
  const ctx = withTree(routes);
  await ctx.app.viewPolicies(ctx.main, "tree");
  await flush();
  return ctx;
}

const leaves = main => main.querySelectorAll(".policy-tree-leaf");
const leaf = (main, key) => leaves(main).find(n => n.getAttribute("data-key") === key);
const detail = main => main.querySelector(".policy-tree-detail");
async function pick(main, key) {
  const node = leaf(main, key);
  assert.ok(node, key);
  await node.click();
  await flush();
  return detail(main).textContent;
}
const states = main => detail(main).querySelectorAll(".policy-tree-state").map(n => n.textContent);
const state = main => states(main)[0];
async function noProject(main) {
  const picker = main.querySelector(".policy-tree-project");
  picker.value = "";
  await Promise.all(picker.listeners.change.map(fn => fn()));
  await flush();
}

test("the tree lists every registry key once and every scope·action once, plus one leaf per schedule", async () => {
  const { main } = await openTree();
  const keys = leaves(main).map(n => n.getAttribute("data-key"));
  const expected = [...REGISTRY.keys.map(k => k.name), ...SCHEDULES.schedules.map(s => `schedule:${s.name}`)];
  assert.deepEqual([...keys].sort(), [...expected].sort());
  assert.equal(new Set(keys).size, keys.length);
  const tree = main.querySelector(".policy-tree");
  assert.equal(tree.getAttribute("role"), "tree");
  // The three roots, then scope nodes and settings areas.
  const branches = main.querySelectorAll(".policy-tree-branch").map(n => n.getAttribute("data-key"));
  const scopes = [...new Set(REGISTRY.keys.filter(k => k.system === "scope_document").map(k => k.name.split(".")[0]))].sort();
  const areas = [...new Set(REGISTRY.keys.filter(k => k.system !== "scope_document").map(k => k.system))].sort();
  assert.deepEqual(branches, ["Scopes", ...scopes.map(s => `scope:${s}`), "Settings", ...areas.map(a => `area:${a}`), "Schedules"]);
  for (const node of main.querySelectorAll(".policy-tree-branch")) assert.equal(node.getAttribute("aria-expanded"), "false");
  const shell = main.querySelectorAll(".policy-tree-branch").find(n => n.getAttribute("data-key") === "scope:shell");
  assert.deepEqual(shell.querySelectorAll(".policy-tree-leaf").map(n => n.getAttribute("data-key")), ["shell.run", "shell.run_background", "shell.run_repo"]);
});

test("the project picker lists bound projects and the baseline; the first bound project resolves its RSoP", async () => {
  const { main, calls } = await openTree();
  const picker = main.querySelector(".policy-tree-project");
  assert.deepEqual(picker.children.map(o => o.getAttribute("value")), ["", "app"]);
  assert.equal(picker.value, "app");
  assert.ok(calls.some(([, p]) => p === "/api/repos/%2Ffixture%2Fapp/rsop"));
});

test("a scope·action that denies when unmatched says so", async () => {
  const { main } = await openTree();
  await pick(main, "shell.run_repo");
  assert.match(state(main), /Not configured → denies$/);
});

test("setting wording: not configured, enabled, a value, enforced; the deciding layer and what it overrode", async () => {
  const { main } = await openTree();
  await pick(main, "sandbox_required_for");
  assert.equal(state(main), "Not configured → email, browser, secrets, unknown_repo");
  await pick(main, "auto_approve");
  assert.equal(state(main), "Enabled");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "global");
  const text = await pick(main, "default_max_actions");
  assert.equal(state(main), "7");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "project:app");
  assert.match(text, /Overrode.*global.*100/s);
  await pick(main, "default_network");
  assert.equal(state(main), "pypi.org, files.pythonhosted.org, registry.npmjs.org, proxy.golang.org");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "Enforced (locked baseline)");
  // run_policy keys link to the existing editor; nothing on the leaf edits.
  assert.ok(detail(main).querySelectorAll("a").some(a => a.getAttribute("href") === "#/policies/groups"));
  assert.equal(detail(main).querySelectorAll("input").length + detail(main).querySelectorAll("select").length, 0);
});

test("a key the RSoP does not resolve shows its doc, default and the layers that may set it", async () => {
  const { main } = await openTree();
  const deps = REGISTRY.keys.find(k => k.name === "auto_approval.deps-safe");
  const text = await pick(main, "auto_approval.deps-safe");
  assert.ok(text.includes(deps.doc));
  assert.match(text, new RegExp(`Default: ${deps.default}`));
  assert.match(text, new RegExp(`May be set at: ${deps.layers.join(", ")}`));
  await noProject(main);
  const knob = await pick(main, "default_max_actions");
  assert.match(knob, /Default: 100/);
  assert.match(knob, /Pick a project to see which layer decides it/);
});

test("a schedule leaf shows its kind and role", async () => {
  const { main } = await openTree();
  const text = await pick(main, "schedule:nightly");
  assert.match(text, /coding/);
  assert.match(text, /Role: builder/);
  assert.match(text, /the role narrows shell prefixes/);
});

test("keyboard: arrows move, Right opens, Left closes, Enter opens a leaf", async () => {
  const { app, main } = await openTree();
  const tree = main.querySelector(".policy-tree");
  const key = async name => {
    await Promise.all(tree.listeners.keydown.map(fn => fn({ key: name, preventDefault() {} })));
    await flush();
  };
  const current = () => [...main.querySelectorAll(".policy-tree-branch"), ...leaves(main)]
    .filter(n => n.getAttribute("role") === "treeitem" && n.getAttribute("tabindex") === "0");
  const branch = name => main.querySelectorAll(".policy-tree-branch").find(n => n.getAttribute("data-key") === name);
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["Scopes"]);
  await key("ArrowDown");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["Settings"]); // children hidden: skips them
  await key("ArrowUp");
  await key("ArrowRight");
  assert.equal(branch("Scopes").getAttribute("aria-expanded"), "true");
  await key("ArrowDown");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["scope:browse"]);
  await key("Enter");
  assert.equal(branch("scope:browse").getAttribute("aria-expanded"), "true");
  await key("ArrowDown");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["browse.act"]);
  await key("Enter");
  assert.equal(detail(main).querySelector("h3").textContent, "browse.act");
  assert.equal(leaf(main, "browse.act").getAttribute("aria-selected"), "true");
  await key("ArrowLeft");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["scope:browse"]);
  await key("ArrowLeft");
  assert.equal(branch("scope:browse").getAttribute("aria-expanded"), "false");
  assert.ok(vm.runInContext("SECTIONS", app).policies.includes("tree"));
  // An open branch with nothing under it keeps focus where it is.
  const empty = await openTree({ "GET /api/schedules": { schedules: [] } });
  const emptyTree = empty.main.querySelector(".policy-tree");
  for (const name of ["ArrowDown", "ArrowDown", "ArrowRight", "ArrowRight"]) {
    await Promise.all(emptyTree.listeners.keydown.map(fn => fn({ key: name, preventDefault() {} })));
  }
  const schedulesNode = empty.main.querySelectorAll(".policy-tree-branch").find(n => n.getAttribute("data-key") === "Schedules");
  assert.equal(schedulesNode.getAttribute("tabindex"), "0");
});

test("a scope·action leaf offers Test an action, preset to that scope and action", async () => {
  const { main, calls } = await openTree();
  await pick(main, "shell.run");
  const button = detail(main).querySelectorAll("button").find(b => b.textContent === "Test this");
  assert.ok(button);
  await button.click();
  await flush();
  const box = detail(main).querySelector(".policy-test");
  assert.ok(box);
  const [, scope] = box.querySelectorAll("select");
  const [action, value] = box.querySelectorAll("input");
  assert.equal(scope.value, "shell");
  assert.equal(action.value, "run");
  value.value = "uv sync";
  await box.querySelectorAll("button").find(b => b.textContent === "Check").click();
  assert.ok(calls.some(([, p]) => p === "/api/policy/why?repo=%2Ffixture%2Fapp&scope=shell&action=run&value=uv%20sync"));
});

// v124 review (F2, I8): a baseline that is missing or tampered makes every
// decision ask (baseline/no_baseline, baseline/baseline_tampered) — the tree
// must say so, not "Configured: allow" from rules the guard never reaches.
test("no approved baseline: the note leads and every scope state asks for the baseline's reason", async () => {
  const { main } = await openTree(NONE);
  assert.equal(NONE["GET /api/policy/explain?subject=baseline"].status.state, "none");
  assert.match(main.querySelector(".policy-tree-guard").textContent, /baseline: none approved/);
  // The live engine's answer for an action the RSoP compiles an allow for.
  const why = NONE["GET /api/policy/why?repo=%2Ffixture%2Fapp&scope=shell&action=run&value=uv%20sync"];
  assert.equal(why.run.decided_by, "baseline/no_baseline");
  for (const name of ["coding.edit", "shell.run", "network.search"]) {
    await pick(main, name);
    assert.ok(states(main).length > 0, name);
    for (const line of states(main)) assert.match(line, /Asks: baseline no_baseline$/, `${name}: ${line}`);
    assert.equal(detail(main).querySelectorAll(".policy-tree-rule").length, 0, name);
  }
  await noProject(main);
  await pick(main, "coding.edit");
  for (const line of states(main)) assert.match(line, /Asks: baseline no_baseline$/);
});

test("a tampered lock: every scope asks for baseline_tampered, and its old rules never read as live", async () => {
  const { main } = await openTree(TAMPERED);
  assert.match(main.querySelector(".policy-tree-guard").textContent, /baseline: TAMPERED/);
  const explain = TAMPERED["GET /api/policy/explain?subject=baseline"];
  assert.ok(explain.rules.some(r => r.scope === "shell" && r.verdict === "allow")); // the lock's old layer
  await pick(main, "shell.run");
  for (const line of states(main)) assert.match(line, /Asks: baseline baseline_tampered$/);
  await noProject(main);
  await pick(main, "shell.run");
  assert.ok(states(main).length > 0);
  for (const line of states(main)) assert.match(line, /Asks: baseline baseline_tampered$/);
  assert.equal(detail(main).querySelectorAll(".policy-tree-rule").length, 0);
  assert.doesNotMatch(detail(main).textContent, /Configured/);
});

test("a locked baseline shows no guard note", async () => {
  const { main } = await openTree();
  assert.equal(main.querySelector(".policy-tree-guard"), null);
});

// v124 review (F2, I8): policy_v3.OPERATOR_LANES — network.search and
// network.fetch are decided on the Queen's lane even from a checkout, so the
// tree reads rsop.operator for them; every state names whose answer it is.
test("network.search and network.fetch are the Queen's answer, read from rsop.operator", async () => {
  const { main } = await openTree();
  const why = LOCKED["GET /api/policy/why?repo=%2Ffixture%2Fapp&scope=network&action=search&value=python"];
  assert.equal(why.operator.verdict, "allow"); // what the live engine gives search
  await pick(main, "network.search");
  assert.deepEqual(states(main), ["The Queen: Configured: allow"]);
  const rules = detail(main).querySelectorAll(".policy-tree-rule").map(n => n.textContent);
  assert.ok(rules.some(r => /net-search/.test(r)), rules.join("\n"));
  await pick(main, "network.fetch");
  assert.deepEqual(states(main), ["The Queen: Configured: ask"]);
  assert.match(detail(main).textContent, /net-fetch-gated/);
});

test("every other scope·action names the workers' answer in the project and the Queen's", async () => {
  const { main } = await openTree();
  const why = LOCKED["GET /api/policy/why?repo=%2Ffixture%2Fapp&scope=shell&action=run&value=uv%20sync"];
  assert.equal(why.run.verdict, "allow");
  assert.equal(why.operator.verdict, "require_approval"); // queen_view drops the baseline's shell allows
  await pick(main, "shell.run");
  assert.deepEqual(states(main), ["Workers in app: Configured: allow", "The Queen: Configured: ask"]);
  assert.match(detail(main).textContent, /anything no rule matches asks/);
  await pick(main, "shell.run_repo");
  assert.deepEqual(states(main), ["Workers in app: Not configured → denies", "The Queen: Not configured → denies"]);
  // No project: the locked baseline's layer as stated, named as that — not a lane's answer.
  await noProject(main);
  await pick(main, "shell.run");
  assert.deepEqual(states(main), ["The locked baseline: Configured: allow / ask"]);
});

// v124 review (F2, I8): what a schedule's network really is (scheduler.run_due):
// a script runs via /bin/sh as you; a bound worker's empty list takes the
// project's default_network; an unbound repo runs on the global defaults.
test("schedule leaves say what network and policy each kind really runs under", async () => {
  const { main } = await openTree();
  let text = await pick(main, "schedule:nightly");
  assert.match(text, /Extra hosts requested: none \(the project's default_network applies\)/);
  assert.doesNotMatch(text, /Network: none/);
  text = await pick(main, "schedule:extra");
  assert.match(text, /Extra hosts requested: example\.com, on top of the project's default_network/);
  assert.match(text, /No role: its workers run under app's policy alone/);
  text = await pick(main, "schedule:stray");
  assert.match(text, /No role: its repo is bound to no project, so its workers run on the global defaults/);
  assert.doesNotMatch(text, /project's policy/);
  assert.match(text, /Network: none — no project adds any/);
  text = await pick(main, "schedule:backup");
  assert.match(text, /No role: the script runs as you/);
  assert.match(text, /Network: yours — the script runs via \/bin\/sh as you, outside every policy/);
  for (const name of ["morning", "memo", "digest"]) {
    text = await pick(main, `schedule:${name}`);
    assert.doesNotMatch(text, /Network|hosts/, name);
  }
});

// v124 review (F1): a description is the baseline's — it shows where a rule
// the baseline layer stated applies (the Queen's lane, the no-project view),
// matched on the whole rule, never by id alone (I8).
test("the Queen's baseline rules carry their description; a changed rule with the same id does not", async () => {
  const { main } = await openTree();
  const explain = LOCKED["GET /api/policy/explain?subject=baseline"];
  const learnable = explain.rules.find(r => r.rule_id === "shell-learnable");
  await pick(main, "shell.run");
  const queen = detail(main).querySelectorAll(".policy-tree-rule").find(n => /shell-learnable/.test(n.textContent));
  assert.ok(queen.textContent.endsWith(learnable.description), queen.textContent);
  // The workers' rules are compiled from knobs: no shipped sentence.
  const worker = detail(main).querySelectorAll(".policy-tree-rule").find(n => /shell:uv sync/.test(n.textContent));
  assert.match(worker.textContent, /No description$/);
  // The same id with another pattern (a lock approved before the rule changed) borrows nothing.
  const rsop = structuredClone(LOCKED[RSOP_ROUTE]);
  rsop.operator.shell = rsop.operator.shell.map(r => (r.rule_id === "shell-learnable" ? { ...r, pattern: "git *" } : r));
  const changed = await openTree({ [RSOP_ROUTE]: rsop });
  await pick(changed.main, "shell.run");
  const moved = detail(changed.main).querySelectorAll(".policy-tree-rule").find(n => /shell-learnable/.test(n.textContent));
  assert.match(moved.textContent, /No description$/);
  // No project: the baseline's own rows, each with its sentence.
  await noProject(main);
  await pick(main, "network.search");
  assert.ok(detail(main).textContent.includes(explain.rules.find(r => r.rule_id === "net-search").description));
});

// v124 review (F2, I8): with no project the settings leaf shows what the locked
// baseline sets (explain.settings), not the registry's default.
test("no project: a setting the locked baseline sets shows its value, enforced", async () => {
  const { main } = await openTree();
  const set = LOCKED["GET /api/policy/explain?subject=baseline"].settings.find(s => s.key === "default_network");
  await noProject(main);
  await pick(main, "default_network");
  assert.equal(state(main), set.value.join(", "));
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "Enforced (locked baseline)");
  assert.doesNotMatch(detail(main).textContent, /Default:/);
  // A tampered lock's settings are not live: no "Enforced" from its old layer.
  const tampered = await openTree(TAMPERED);
  await noProject(tampered.main);
  await pick(tampered.main, "default_network");
  assert.doesNotMatch(detail(tampered.main).textContent, /Enforced/);
});
