// v124-F2 (DD-10): the policy tree — every registry key and every scope·action
// once, each leaf in Group Policy words, read from the endpoints that already
// exist (registry, rsop, explain, schedules). Read-only.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { loadApp } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const REGISTRY = {
  layers: ["floor", "global", "template", "role", "phase", "group", "project", "session", "dispatch"],
  keys: [
    { kind: "knob", system: "run_policy", name: "auto_approve", type: "bool", default: false, layers: ["global", "project"], doc: "land verified fixes", invariant: "I1" },
    { kind: "knob", system: "run_policy", name: "sandbox_required", type: "bool", default: false, layers: ["global", "project"], doc: "always sandbox", invariant: "I4" },
    { kind: "knob", system: "run_policy", name: "default_network", type: "list[str]", default: [], layers: ["global", "template", "project"], doc: "hosts a worker may reach", invariant: "I12" },
    { kind: "knob", system: "run_policy", name: "default_max_actions", type: "int", default: 200, layers: ["global", "group", "project"], doc: "actions per run", invariant: "I4" },
    { kind: "rule", system: "scope_document", name: "shell.run", type: "rule", default: "require_approval", layers: ["template", "global", "project", "session"], doc: "a worker's shell command", invariant: "I5" },
    { kind: "rule", system: "scope_document", name: "shell.run_repo", type: "rule", default: "deny", layers: ["template"], doc: "a repo script", invariant: "I5" },
    { kind: "rule", system: "scope_document", name: "network.fetch", type: "rule", default: "require_approval", layers: ["template"], doc: "the Queen's read_url", invariant: "I5" },
    { kind: "rule", system: "auto_approval", name: "auto_approval.deps-safe", type: "rule", default: "off", layers: ["dispatch"], doc: "land a manifest-only patch", invariant: "I1" },
    { kind: "rule", system: "floor", name: "floor.worker-git-hard-denies", type: "rule", default: "deny", layers: [], doc: "no push or pull", invariant: "I4" },
  ],
};
const PROJECTS = {
  projects: [
    { project_id: "app", name: "app", bindings: [{ kind: "repo_path", value: "/r/app" }] },
    { project_id: "loose", name: "loose", bindings: [] },
  ],
};
const RSOP = {
  repo: "/r/app",
  keys: [
    { key: "auto_approve", value: false, source_layer: null, source_name: null, overridden: [], note: "not configured at any layer — the resolver's fallback" },
    { key: "sandbox_required", value: true, source_layer: "project", source_name: "app", overridden: [] },
    { key: "default_network", value: ["pypi.org"], source_layer: "template", source_name: "builder", overridden: [] },
    { key: "default_max_actions", value: 300, source_layer: "group", source_name: "python-bootstrap", overridden: [{ layer: "global", name: null, value: 200 }] },
  ],
  scopes: {
    shell: [
      { verdict: "allow", rule_id: "shell-uv-sync", action: "run", pattern: "uv sync", exact: false, layer: "template:builder", matcher: null, source: "compiled" },
      { verdict: "require_approval", rule_id: "shell:uv", action: "run", pattern: "uv", exact: false, layer: null, matcher: null, source: "allowed_shell_commands@group:python-bootstrap" },
      { verdict: "deny", rule_id: "shell-run-repo", action: "run_repo", pattern: "*", exact: false, layer: "template:builder", matcher: null, source: "compiled" },
    ],
  },
};
const EXPLAIN = {
  subject: "baseline", kind: "baseline", name: "builder",
  rules: [
    { scope: "shell", verdict: "allow", action: "run", pattern: "uv sync", rule_id: "shell-uv-sync", description: "Lets a worker install the locked dependencies." },
    { scope: "shell", verdict: "deny", action: "run_repo", pattern: "*", rule_id: "shell-run-repo" },
  ],
  floor: [],
};
const SCHEDULES = {
  schedules: [
    { name: "nightly", worker_kind: "coding", role: "builder", role_note: "the role narrows shell prefixes", network: ["pypi.org"], repo: "/r/app" },
    { name: "backup", worker_kind: "script", role: null, network: [], repo: "/r/app" },
    { name: "morning", worker_kind: "prompt", role: null, network: [], repo: "/r/app" },
  ],
};

function withTree(routes = {}) {
  const app = loadApp();
  const calls = [];
  const table = {
    "GET /api/policy/registry": REGISTRY,
    "GET /api/projects": PROJECTS,
    "GET /api/schedules": SCHEDULES,
    "GET /api/policy/explain": EXPLAIN,
    "GET /api/repos/%2Fr%2Fapp/rsop": RSOP,
    "GET /api/roles": { default: "builder", roles: [{ name: "builder" }, { name: "assistant" }] },
    ...routes,
  };
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
const state = main => detail(main).querySelector(".policy-tree-state").textContent;

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
  assert.deepEqual(branches, ["Scopes", "scope:network", "scope:shell", "Settings", "area:auto_approval", "area:floor", "area:run_policy", "Schedules"]);
  for (const node of main.querySelectorAll(".policy-tree-branch")) assert.equal(node.getAttribute("aria-expanded"), "false");
  // Leaves sit under their scope: shell holds run and run_repo.
  const shell = main.querySelectorAll(".policy-tree-branch").find(n => n.getAttribute("data-key") === "scope:shell");
  assert.deepEqual(shell.querySelectorAll(".policy-tree-leaf").map(n => n.getAttribute("data-key")), ["shell.run", "shell.run_repo"]);
});

test("the project picker lists bound projects and the baseline; the first bound project resolves its RSoP", async () => {
  const { main, calls } = await openTree();
  const picker = main.querySelector(".policy-tree-project");
  assert.deepEqual(picker.children.map(o => o.getAttribute("value")), ["", "app"]);
  assert.equal(picker.value, "app");
  assert.ok(calls.some(([, p]) => p === "/api/repos/%2Fr%2Fapp/rsop"));
});

test("scope·action wording: an unmatched one says what happens; a configured one lists its rules", async () => {
  const { main } = await openTree();
  await pick(main, "network.fetch");
  assert.equal(state(main), "Not configured → asks");
  const text = await pick(main, "shell.run");
  assert.equal(state(main), "Configured: allow / ask");
  assert.match(text, /anything no rule matches asks/);
  // Each rule with its id, the layer that stated it and its description.
  const rules = detail(main).querySelectorAll(".policy-tree-rule").map(n => n.textContent);
  assert.equal(rules.length, 2);
  assert.match(rules[0], /allow.*uv sync.*shell-uv-sync.*template:builder.*Lets a worker install the locked dependencies\./s);
  assert.match(rules[1], /ask.*uv.*shell:uv.*allowed_shell_commands@group:python-bootstrap.*No description/s);
  await pick(main, "shell.run_repo");
  assert.equal(state(main), "Configured: deny");
  // A description shows only for a rule the baseline stated, never by id alone.
  assert.match(detail(main).textContent, /No description/);
});

test("a scope·action that denies when unmatched says so", async () => {
  const { main } = await openTree({ "GET /api/repos/%2Fr%2Fapp/rsop": { ...RSOP, scopes: {} } });
  await pick(main, "shell.run_repo");
  assert.equal(state(main), "Not configured → denies");
});

test("setting wording: not configured, enabled, a value, enforced; the deciding layer and what it overrode", async () => {
  const { main } = await openTree();
  await pick(main, "auto_approve");
  assert.equal(state(main), "Not configured → Disabled");
  await pick(main, "sandbox_required");
  assert.equal(state(main), "Enabled");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "project:app");
  const text = await pick(main, "default_max_actions");
  assert.equal(state(main), "300");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "group:python-bootstrap");
  assert.match(text, /Overrode.*global.*200/s);
  assert.match(text, /actions per run/);
  await pick(main, "default_network");
  assert.equal(state(main), "pypi.org");
  assert.equal(detail(main).querySelector(".policy-tree-decider").textContent, "Enforced (locked baseline)");
  // run_policy keys link to the existing editor; nothing on the leaf edits.
  assert.ok(detail(main).querySelectorAll("a").some(a => a.getAttribute("href") === "#/policies/groups"));
  assert.equal(detail(main).querySelectorAll("input").length + detail(main).querySelectorAll("select").length, 0);
});

test("a key the RSoP does not resolve shows its doc, default and the layers that may set it", async () => {
  const { main } = await openTree();
  const text = await pick(main, "auto_approval.deps-safe");
  assert.match(text, /land a manifest-only patch/);
  assert.match(text, /Default: off/);
  assert.match(text, /May be set at: dispatch/);
  // With no project picked, a run_policy key says where its deciding layer comes from.
  const picker = main.querySelector(".policy-tree-project");
  picker.value = "";
  await Promise.all(picker.listeners.change.map(fn => fn()));
  await flush();
  const knob = await pick(main, "default_max_actions");
  assert.match(knob, /Default: 200/);
  assert.match(knob, /Pick a project to see which layer decides it/);
  // Without a project the scope leaf reads the locked baseline's rules.
  await pick(main, "shell.run");
  assert.equal(state(main), "Configured: allow");
});

test("a schedule leaf shows its kind, role and network; no role says what governs it", async () => {
  const { main } = await openTree();
  let text = await pick(main, "schedule:nightly");
  assert.match(text, /coding/);
  assert.match(text, /Role: builder/);
  assert.match(text, /the role narrows shell prefixes/);
  assert.match(text, /pypi\.org/);
  text = await pick(main, "schedule:backup");
  assert.match(text, /No role: the script runs as you/);
  text = await pick(main, "schedule:morning");
  assert.match(text, /No role: the prompt runs in its chat's role/);
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
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["scope:network"]);
  await key("Enter");
  assert.equal(branch("scope:network").getAttribute("aria-expanded"), "true");
  await key("ArrowDown");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["network.fetch"]);
  await key("Enter");
  assert.equal(state(main), "Not configured → asks");
  assert.equal(leaf(main, "network.fetch").getAttribute("aria-selected"), "true");
  await key("ArrowLeft");
  assert.deepEqual(current().map(n => n.getAttribute("data-key")), ["scope:network"]);
  await key("ArrowLeft");
  assert.equal(branch("scope:network").getAttribute("aria-expanded"), "false");
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
