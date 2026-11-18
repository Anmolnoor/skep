// Graphite round A: the Policies page (policies.js), executed — five tabs,
// each answering one question, built from reads the server already serves.
import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { loadApp, texts } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const BASELINE_OK = {
  state: "ok", template: "builder", approved_by: "operator", approved_at: "2026-11-10T09:00:00Z",
  sha256: "a".repeat(64), diff: [], tampered_since: null, relock: null, line: "baseline: builder approved",
};
const ROLES = {
  default: "builder",
  roles: [{ name: "builder", policy_layer: "builder" }, { name: "assistant", policy_layer: null }],
};
const FLOOR = [
  { scope: "shell", verdict: "deny", action: "run", pattern: "git push / pull / fetch", rule_id: "floor.worker-git-hard-denies", label: "always denied" },
  { scope: "*", verdict: "ask", action: "*", pattern: "a post, send or publish", rule_id: "floor.outbound-content", label: "always asks" },
];
const explain = subject => ({
  subject, kind: subject === "baseline" ? "baseline" : "template", note: subject.startsWith("role:") ? "it only tightens" : null,
  rules: [
    { scope: "shell", verdict: "allow", action: "run", pattern: "uv sync", rule_id: "shell-uv-sync" },
    { scope: "shell", verdict: "ask", action: "run", pattern: "*", rule_id: "shell-ask" },
    { scope: "network", verdict: "deny", action: "fetch", pattern: "*", rule_id: "net-deny" },
  ],
  settings: [], floor: FLOOR,
});
const POLICY = {
  auto_approve: false, worker_cmd: "skep-worker", default_network: ["pypi.org"], default_env_allowlist: ["HOME"],
  default_execution_mode: "sandbox", trusted_workspace_roots: ["/w"], sandbox_required_for: [],
  allowed_shell_commands: [["uv", "sync"]], ticker_interval_seconds: 30, default_wall_clock_seconds: 6000,
  default_max_iterations: 20, default_max_actions: 200, default_max_provider_calls: 50, policy_engine_v2_scopes: ["shell"],
};
const REGISTRY = {
  keys: [
    { kind: "knob", system: "run_policy", name: "default_network", type: "list[str]", layers: ["global", "project"], doc: "hosts", invariant: "D1" },
    { kind: "knob", system: "run_policy", name: "default_max_actions", type: "int", layers: ["global", "project"], doc: "actions", invariant: "I4" },
    { kind: "rule", system: "scope_document", name: "shell.run" },
    { kind: "rule", system: "scope_document", name: "network.fetch" },
  ],
};
const GROUPS = {
  groups: [{
    name: "python-bootstrap", builtin: true, edited: false, attached_projects: ["app", "lib"],
    policy: { default_network: ["pypi.org", "files.pythonhosted.org"], allowed_shell_commands: [["uv", "sync"], ["python3", "-m", "venv"]], default_max_actions: 300 },
  }],
};
const PROJECTS = { projects: [{ project_id: "app", name: "app", bindings: [{ kind: "repo_path", value: "/r/app" }] }] };

// One stub table per test: path prefix → response (or a function of the path).
function withPolicies(routes = {}) {
  const app = loadApp();
  const calls = [];
  const table = {
    "GET /api/policy/baseline": BASELINE_OK,
    "GET /api/roles": ROLES,
    "GET /api/policy/explain": path => explain(decodeURIComponent(path.split("subject=")[1])),
    "GET /api/policy/registry": REGISTRY,
    "GET /api/projects": PROJECTS,
    "GET /api/policy-groups": GROUPS,
    "GET /api/policy/rules": { rules: [], operator_rules: [] },
    "GET /api/policy/decisions": { rows: [], total: 0, summary: { allow: 0, deny: 0, require_approval: 0, observed: 0, forwarded: 0, delegated: 0 } },
    "GET /api/policy": POLICY,
    ...routes,
  };
  app.api = async (method, path, body) => {
    calls.push([method, path, body]);
    const key = Object.keys(table).filter(k => `${method} ${path}`.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`unstubbed api ${method} ${path}`);
    const out = table[key];
    return typeof out === "function" ? out(path, body) : out;
  };
  app.history.replaceState = (_s, _t, url) => { app.location.hash = url; };
  return { app, calls, main: app.el("main") };
}

const panel = (main, i) => main.querySelectorAll(".tab-panel")[i];
const visible = main => main.querySelectorAll(".tab-panel").findIndex(p => !p.classList.contains("hidden"));

test("five tabs, Now first; a #/policies/<tab> suffix opens that tab and is accepted by the router", async () => {
  const { app, main } = withPolicies();
  await app.viewPolicies(main);
  await flush();
  assert.deepEqual(texts(main, "tab-button"), ["Now", "Decisions", "Standing grants", "Projects & groups", "Defaults"]);
  assert.equal(visible(main), 0);
  const second = withPolicies();
  await second.app.viewPolicies(second.main, "decisions");
  await flush();
  assert.equal(visible(second.main), 1);
  const routes = vm.runInContext("routes", app);
  const hit = routes.find(([pattern]) => pattern.test("#/policies/decisions"));
  assert.ok(hit && hit[1] === app.viewPolicies);
  assert.deepEqual([..."#/policies/decisions".match(hit[0]).slice(1)], ["decisions"]);
  assert.ok(hit[0].test("#/policies"));
});

test("the active tab is remembered for the session and written to the hash", async () => {
  const { app, main } = withPolicies();
  await app.viewPolicies(main);
  await flush();
  await main.querySelectorAll(".tab-button")[4].click();
  await flush();
  assert.equal(app.location.hash, "#/policies/defaults");
  const again = app.el("main");
  await app.viewPolicies(again);
  await flush();
  assert.equal(visible(again), 4);
});

test("Now: three columns by verdict word, grouped by scope; an ask never sits under Allowed", async () => {
  const { main, app } = withPolicies();
  await app.viewPolicies(main);
  await flush();
  const now = panel(main, 0);
  assert.match(now.querySelector(".policy-status").textContent, /Baseline locked: the builder template/);
  assert.match(now.querySelector(".policy-status").textContent, /New chats open as the builder role/);
  const cols = now.querySelectorAll(".policy-col");
  assert.deepEqual(cols.map(c => c.querySelector(".policy-col-head").textContent.replace(/\d+$/, "")), ["Allowed", "Asks first", "Denied"]);
  assert.match(cols[0].textContent, /uv sync/);
  assert.doesNotMatch(cols[0].textContent, /anything/);
  assert.match(cols[1].textContent, /shell/);
  assert.match(cols[1].textContent, /run anything/);
  assert.match(cols[2].textContent, /network/);
  // The fixed floor: deny rows under "Always denied", the ask row apart, no control anywhere in it.
  const floor = now.querySelector(".policy-floor");
  assert.match(floor.textContent, /Always denied/);
  assert.match(floor.textContent, /git push \/ pull \/ fetch/);
  assert.match(floor.textContent, /Always asks first/);
  assert.equal(floor.querySelectorAll("button").length + floor.querySelectorAll("input").length + floor.querySelectorAll("select").length, 0);
  assert.equal(now.querySelectorAll("a").filter(a => a.getAttribute("href") === "#/setup").length, 0);
});

test("Now: View as offers the baseline, each role and each template, and re-reads explain", async () => {
  const { main, app, calls } = withPolicies();
  await app.viewPolicies(main);
  await flush();
  const select = panel(main, 0).querySelector(".policy-view-as");
  const values = select.children.map(o => o.getAttribute("value"));
  assert.deepEqual(values, ["baseline", "role:builder", "role:assistant", "assistant", "builder", "operator"]);
  select.value = "role:assistant";
  await Promise.all(select.listeners.change.map(fn => fn()));
  await flush();
  assert.ok(calls.some(([, path]) => path === "/api/policy/explain?subject=role%3Aassistant"));
  assert.match(panel(main, 0).textContent, /it only tightens/);
});

test("Now: no baseline says so and links to Setup — no lock form here", async () => {
  const { main, app } = withPolicies({
    "GET /api/policy/baseline": { state: "none", diff: [], tampered_since: null, relock: null, line: "baseline: none approved" },
    "GET /api/policy/explain": path => ({ ...explain("baseline"), rules: [], subject: path }),
  });
  await app.viewPolicies(main);
  await flush();
  const now = panel(main, 0);
  assert.match(now.querySelector(".policy-status").textContent, /No baseline locked: every decision asks first until one is/);
  assert.ok(now.querySelectorAll("a").some(a => a.getAttribute("href") === "#/setup"));
  assert.equal(now.querySelectorAll("input").filter(i => /yes/.test(i.getAttribute("placeholder") || "")).length, 0);
});

test("Now: a tampered baseline names since when and what that means", async () => {
  const { main, app } = withPolicies({
    "GET /api/policy/baseline": { state: "tampered", diff: ["- shell: allow run uv sync"], tampered_since: "2026-11-17T08:00:00Z", relock: null, line: "x" },
  });
  await app.viewPolicies(main);
  await flush();
  const status = panel(main, 0).querySelector(".policy-status").textContent;
  assert.match(status, /Baseline tampered since 2026-11-17 08:00:00/);
  assert.match(status, /dispatch refuses until it is re-approved/);
  assert.match(status, /- shell: allow run uv sync/);
});

test("Now: Test an action asks /api/policy/why and names both verdicts in words", async () => {
  const { main, app, calls } = withPolicies({
    "GET /api/policy/why": { run: { verdict: "require_approval", decided_by: "template:builder/shell-ask" }, operator: { verdict: "deny", decided_by: "floor" }, baseline: "b" },
  });
  await app.viewPolicies(main);
  await flush();
  const box = panel(main, 0).querySelector(".policy-test");
  const [project, scope] = box.querySelectorAll("select");
  const [action, value] = box.querySelectorAll("input");
  project.value = "app"; scope.value = "shell"; action.value = "run"; value.value = "curl x";
  await box.querySelector("button").click();
  assert.ok(calls.some(([, p]) => p === "/api/policy/why?repo=%2Fr%2Fapp&scope=shell&action=run&value=curl%20x"));
  const out = box.querySelector(".policy-test-out").textContent;
  assert.match(out, /asks first/);
  assert.match(out, /denied/);
  assert.match(out, /template:builder\/shell-ask/);
});

test("Decisions: summary counts, newest-first rows, a verdict filter, and an empty state", async () => {
  const rows = [
    { at: "2026-11-18T10:00:00Z", actor: "acp:claude", scope: "shell", action: "run", value: "rm -rf /", verdict: "deny", decided_by: "floor" },
    { at: "2026-11-18T09:00:00Z", actor: "acp:claude", scope: "shell", action: "run", value: "uv sync", verdict: "allow", decided_by: "template:builder" },
    { at: "2026-11-18T08:00:00Z", actor: "hook", scope: "network", action: "fetch", value: "x.dev", verdict: "require_approval", decided_by: "default" },
    { at: "2026-11-18T07:00:00Z", actor: "hook", scope: "shell", action: "run", value: "ls", source: "observed", verdict: "allow", decided_by: "x" },
  ];
  const { main, app } = withPolicies({
    "GET /api/policy/decisions": { rows, total: 4, summary: { allow: 1, deny: 1, require_approval: 1, observed: 1, forwarded: 0, delegated: 0 } },
  });
  await app.viewPolicies(main, "decisions");
  await flush();
  const dec = panel(main, 1);
  assert.match(dec.querySelector(".policy-summary").textContent, /1 allowed · 1 asked · 1 denied · 1 observed/);
  const rowText = () => dec.querySelectorAll(".policy-decision").filter(r => !r.classList.contains("hidden")).map(r => r.textContent);
  assert.equal(rowText().length, 4);
  assert.match(rowText()[0], /rm -rf/);
  assert.match(rowText()[2], /asks first/);
  assert.match(rowText()[3], /observed/);
  const tabs = dec.querySelectorAll(".filter-tab");
  assert.deepEqual(tabs.map(t => t.textContent), ["All4", "Allowed1", "Asked1", "Denied1", "Notes1"]);
  await tabs[3].click();
  assert.deepEqual(rowText().length, 1);
  assert.match(rowText()[0], /denied/);

  const empty = withPolicies();
  await empty.app.viewPolicies(empty.main, "decisions");
  await flush();
  assert.match(panel(empty.main, 1).querySelector(".empty-state").textContent, /Nothing decided yet/);
});

test("Standing grants: one line per rule; Revoke keeps its confirm wording and DELETEs", async () => {
  const { main, app, calls } = withPolicies({
    "GET /api/policy/rules": {
      rules: [{ rule_id: "shell:run:uv sync", scope: "shell", action: "run", pattern: "uv sync", tier: "session", provenance: "card", created_at: "2026-11-18T09:00:00Z" }],
      operator_rules: [{ rule_id: "op1", scope: "network", action: "fetch", pattern: "docs.python.org", verdict: "allow" }],
    },
    "DELETE /api/policy/rules/": {},
  });
  let asked = null;
  app.confirm = text => { asked = text; return true; };
  await app.viewPolicies(main, "grants");
  await flush();
  const grants = panel(main, 2);
  const row = grants.querySelector(".policy-grant");
  assert.match(row.textContent, /shell run/);
  assert.match(row.textContent, /until serve restarts/);
  await row.querySelector("button").click();
  assert.equal(asked, "Revoke shell:run:uv sync? The next matching action cards again.");
  assert.ok(calls.some(([m, p]) => m === "DELETE" && p === "/api/policy/rules/shell%3Arun%3Auv%20sync"));
  assert.match(grants.querySelector(".policy-operator").textContent, /allowed.*network fetch.*docs\.python\.org/s);
  assert.equal(grants.querySelector(".policy-operator").querySelectorAll("button").length, 0);
});

test("Projects & groups: a group reads as plain grants; its JSON opens on Edit", async () => {
  const { main, app } = withPolicies({ "GET /api/projects/app": { project_id: "app", name: "app", policy: {}, bindings: [] } });
  await app.viewPolicies(main, "groups");
  await flush();
  const groups = panel(main, 3);
  const row = groups.querySelector(".policy-group-row");
  const summary = row.querySelector(".policy-group-grants").textContent;
  assert.match(summary, /Network hosts/);
  assert.match(summary, /files\.pythonhosted\.org/);
  assert.match(summary, /python3 -m venv/);
  assert.match(summary, /Actions per run300/);
  const editor = row.querySelector(".policy-group-editor");
  assert.ok(editor.classList.contains("hidden"));
  await row.querySelectorAll("button").find(b => b.textContent === "Edit").click();
  assert.ok(!editor.classList.contains("hidden"));
  assert.match(editor.querySelector("textarea").value, /"default_network"/);
});

test("Projects & groups: the hand-off from a project page opens this tab on that group's editor", async () => {
  const { main, app } = withPolicies();
  const store = new Map([["skep-group-edit", JSON.stringify({ group: "python-bootstrap", project: "app" })]]);
  app.sessionStorage = { getItem: k => store.get(k) ?? null, removeItem: k => store.delete(k), setItem: (k, v) => store.set(k, v) };
  await app.viewPolicies(main);
  await flush();
  assert.equal(visible(main), 3);
  assert.equal(store.size, 0);
  const row = panel(main, 3).querySelector(".policy-group-row");
  assert.equal(row.getAttribute("open"), "");
  assert.ok(!row.querySelector(".policy-group-editor").classList.contains("hidden"));
});

test("Projects & groups: the effective table names columns in plain words and shows lists as text", async () => {
  const { main, app } = withPolicies();
  await app.viewPolicies(main, "groups");
  await flush();
  const table = panel(main, 3).querySelector(".policy-effective");
  assert.deepEqual(table.querySelectorAll("th").map(th => th.textContent), ["Setting", "Value", "Decided by", "Overrode", "Change here"]);
  const values = table.querySelectorAll(".policy-value").map(td => td.textContent);
  assert.ok(values.includes("pypi.org"), values.join(" | "));
  assert.ok(!values.some(v => v.includes("[")), values.join(" | "));
  assert.ok(panel(main, 3).querySelector(".policy-table-wrap"));
});

test("Defaults: grouped sections, Advanced closed, tags in hover text, the same PUT body", async () => {
  const { main, app, calls } = withPolicies({ "PUT /api/policy": {} });
  await app.viewPolicies(main, "defaults");
  await flush();
  const defaults = panel(main, 4);
  assert.deepEqual(texts(defaults, "policy-section-header"), ["Limits", "Network and environment", "Where workers run", "Auto-apply", "Advanced"]);
  const advanced = defaults.querySelectorAll(".policy-section").at(-1);
  assert.equal(advanced.getAttribute("open"), null);
  const labels = defaults.querySelectorAll("label").map(l => l.textContent).join(" | ");
  assert.doesNotMatch(labels, /\b(D1|G2|D3)\b/);
  const titled = defaults.querySelectorAll("label").map(l => l.getAttribute("title") || "").join(" | ");
  assert.match(titled, /D1/);
  assert.match(titled, /auto-approval \(D3: verified \+ re-verified manifest-only fixes\)/);
  assert.equal(defaults.querySelectorAll(".primary").length, 1);
  await defaults.querySelector(".primary").click();
  const put = calls.find(([m, p]) => m === "PUT" && p === "/api/policy");
  assert.deepEqual(Object.keys(put[2]).sort(), [
    "allowed_shell_commands", "auto_approve", "default_env_allowlist", "default_execution_mode",
    "default_max_actions", "default_max_iterations", "default_max_provider_calls", "default_network",
    "default_wall_clock_seconds", "sandbox_required_for", "ticker_interval_seconds", "trusted_workspace_roots", "worker_cmd",
  ]);
});

test("Defaults: the engine switch keeps its confirm", async () => {
  const { main, app, calls } = withPolicies({ "PUT /api/policy": {} });
  let asked = null;
  app.confirm = text => { asked = text; return false; };
  await app.viewPolicies(main, "defaults");
  await flush();
  const button = panel(main, 4).querySelectorAll("button").find(b => b.textContent === "Apply engine switch");
  await button.click();
  assert.match(asked, /Route shell through policy engine v2\?/);
  assert.ok(!calls.some(([m]) => m === "PUT"));
});
