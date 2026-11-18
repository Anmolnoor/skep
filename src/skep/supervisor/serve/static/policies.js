/* Graphite round A: the Policies page, by the question the operator asks —
   Now (what may agents do), Decisions (what did skep decide), Standing grants
   (what runs without asking), Projects & groups (what is different here),
   Defaults (what applies when nothing else says) and Tree (every setting and
   which layer decides it, v124). Reads and writes only the
   routes that existed before it (I5); every confirmation the old page had
   stays where the write is.

   app.js boots on load, so its helpers come in through the import cycle.
   Safe because this module only declares at top level: by the time any of
   its functions runs, app.js has finished evaluating. */
import { api, buildFilterBar, el, flash, fmtTs, header, iconButton, relativeTime, route } from "./app.js";

// The engine's verdicts in the operator's words. Explain says allow/ask/deny,
// why and the decision ledger say allow/require_approval/deny: an ask is
// never read as an allow, whichever spelling arrives.
const POLICY_VERDICT_WORD = { allow: "allowed", ask: "asks first", require_approval: "asks first", deny: "denied" };
const POLICY_COLUMNS = [["allow", "Allowed"], ["ask", "Asks first"], ["deny", "Denied"]];
// The shipped templates (policy_templates.TEMPLATE_NAMES); no route lists them,
// and Setup's lock step names the same three.
const POLICY_TEMPLATES = ["assistant", "builder", "operator"];
// Ledger rows that note something rather than decide it (policy_decisions).
const POLICY_NOTE_SOURCES = ["observed", "forwarded", "delegated", "alarm"];
// What a group grants, in words; a key not listed shows its own name.
const GROUP_KEY_LABELS = {
  default_network: "Network hosts",
  allowed_shell_commands: "Shell commands",
  default_env_allowlist: "Environment variable names",
  default_wall_clock_seconds: "Wall clock per run (s)",
  default_max_iterations: "Iterations per run",
  default_max_actions: "Actions per run",
  default_max_provider_calls: "Provider calls per run",
  coding_engine: "Coding engine",
};

const verdictWord = verdict => POLICY_VERDICT_WORD[verdict] || verdict;
const policyNote = text => el("p", { class: "note" }, text);

// One argv as a shell reader would type it; a part with a space keeps quotes.
const argvText = argv => argv.map(part => (part === "" || /[\s"']/.test(part) ? JSON.stringify(part) : part)).join(" ");

// A policy value as words: a list is its items, never JSON broken per letter.
function plainValue(value) {
  if (value === undefined || value === null) return "—";
  if (Array.isArray(value)) {
    if (!value.length) return "none";
    return value.map(item => (Array.isArray(item) ? argvText(item) : String(item))).join(", ");
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// v75-F5: every field teaches (I9) — a one-line "what this does" hint. An
// internal tag (D1, G2, an ADR) rides the label's hover text, not the label.
function policyField(label, input, help, { grow = false, title = null } = {}) {
  return el("div", { class: `field${grow ? " grow" : ""}` },
    el("label", { title }, label),
    help ? el("p", { class: "field-help" }, help) : null,
    input);
}

function policySection(title, open, ...body) {
  return el("details", { class: "policy-section", open: open ? "" : null },
    el("summary", { class: "policy-section-header" }, title),
    el("div", { class: "policy-section-body" }, body));
}

const projectRepoBinding = project => (project?.bindings || [])
  .find(b => b.kind === "repo_path" || b.kind === "repo_slug");

async function scopeNames() {
  const registry = await api("GET", "/api/policy/registry");
  return [...new Set((registry.keys || [])
    .filter(k => k.kind === "rule" && k.system === "scope_document")
    .map(k => k.name.split(".")[0]))];
}

export async function viewPolicies(main, tab) {
  // v97-F5: a project page hands a group over to edit (sessionStorage, read
  // once, on the groups page). graphite-nav: the hand-off lands on
  // #/policies/groups, so the sidebar lights that page and a save stays there.
  let groupEditHandoff = null;
  try {
    const raw = sessionStorage.getItem("skep-group-edit");
    if (raw && tab !== "groups") {
      location.hash = "#/policies/groups"; // its hashchange re-renders this view
      return;
    }
    sessionStorage.removeItem("skep-group-edit");
    groupEditHandoff = raw ? JSON.parse(raw) : null;
  } catch { groupEditHandoff = null; }
  header(main, "Policies",
    "Policy and scopes: autonomy and defaults — what agents may do now, what skep decided, "
    + "what runs without asking, what differs per project. A template (skep setup --template) "
    + "sets the baseline in one move.");
  // One page per #/policies/<tab>: the sidebar's list is the tab bar.
  const pages = {
    now: renderNowTab,
    decisions: renderDecisionsTab,
    grants: renderGrantsTab,
    groups: panel => renderGroupsTab(panel, groupEditHandoff),
    defaults: renderDefaultsTab,
    tree: renderTreeTab,
  };
  const panel = el("div", { class: "tab-panel" });
  main.append(panel);
  await (pages[tab] || pages.now)(panel);
}

// ---- Now: what may agents do right now? ----

function baselineStatus(b) {
  if (b.state === "ok") {
    return {
      tone: "ok",
      text: `Baseline locked: the ${b.template} template, approved ${fmtTs(b.approved_at)} by ${b.approved_by}.`
        + (b.relock ? ` Re-locked from ${b.relock.source || "an unrecorded source"} by ${b.relock.approved_by}.` : ""),
    };
  }
  if (b.state === "reapprove") {
    return {
      tone: "warn", setup: true,
      text: "Baseline needs re-approval: it was locked under an older schema. Its content is unchanged and still applies.",
    };
  }
  if (b.state === "tampered") {
    return {
      tone: "bad", setup: true,
      text: `Baseline tampered since ${b.tampered_since ? fmtTs(b.tampered_since) : "now"}: it no longer matches its lock. `
        + "Every outside decision asks, the governor's hard ring denies, and dispatch refuses until it is re-approved.",
    };
  }
  return { tone: "warn", setup: true, text: "No baseline locked: every decision asks first until one is." };
}

function ruleColumns(rules) {
  return el("div", { class: "policy-columns" }, POLICY_COLUMNS.map(([verdict, label]) => {
    const mine = rules.filter(r => r.verdict === verdict);
    const scopes = [...new Set(mine.map(r => r.scope))];
    return el("section", { class: `policy-col policy-col-${verdict}` },
      el("h4", { class: `policy-col-head verdict-${verdict}` }, label, el("span", { class: "filter-count" }, String(mine.length))),
      mine.length
        ? scopes.map(scope => el("div", { class: "policy-scope" },
          el("div", { class: "policy-scope-name" }, scope),
          el("ul", { class: "policy-rules" }, mine.filter(r => r.scope === scope).map(rule =>
            el("li", { title: rule.rule_id },
              `${rule.action} `,
              rule.pattern === "*" ? "anything" : el("code", { class: "mono" }, rule.pattern))))))
        : policyNote("Nothing."));
  }));
}

function floorBlock(floor) {
  const line = row => el("li", { title: row.rule_id },
    `${row.scope === "*" ? "any scope" : row.scope} ${row.action === "*" ? "" : `${row.action} `}`,
    el("span", { class: "mono" }, row.pattern));
  const denied = floor.filter(r => r.verdict === "deny");
  const asks = floor.filter(r => r.verdict !== "deny");
  return el("section", { class: "policy-floor" },
    el("h4", { class: "policy-col-head verdict-deny" }, "Always denied"),
    el("p", { class: "field-help" },
      "Built into skep, outside every layer: no baseline, role, group or grant can lift these."),
    el("ul", { class: "policy-rules" }, denied.map(line)),
    asks.length ? el("h4", { class: "policy-col-head verdict-ask" }, "Always asks first") : null,
    asks.length ? el("ul", { class: "policy-rules" }, asks.map(line)) : null);
}

async function renderNowTab(panel) {
  const [baseline, roles, scopes, { projects }, { schedules }] = await Promise.all([
    api("GET", "/api/policy/baseline"),
    api("GET", "/api/roles"),
    scopeNames(),
    api("GET", "/api/projects"),
    api("GET", "/api/schedules"),
  ]);
  const status = baselineStatus(baseline);
  const viewAs = el("select", { class: "policy-view-as", "aria-label": "view the policy as" },
    el("option", { value: "baseline" }, "the locked baseline"),
    ...(roles.roles || []).map(r => el("option", { value: `role:${r.name}` }, `role: ${r.name}`)),
    ...POLICY_TEMPLATES.map(t => el("option", { value: t }, `template: ${t}`)));
  viewAs.value = "baseline";
  const table = el("div", { class: "policy-now-table" });
  const renderTable = async () => {
    table.replaceChildren(policyNote("loading…"));
    try {
      const view = await api("GET", `/api/policy/explain?subject=${encodeURIComponent(viewAs.value)}`);
      table.replaceChildren(...[
        view.kind !== "baseline" && view.note ? el("p", { class: "field-help" }, view.note) : null,
        ruleColumns(view.rules || []),
        floorBlock(view.floor || []),
      ].filter(Boolean));
    } catch (e) { table.replaceChildren(policyNote(e.message)); }
  };
  viewAs.addEventListener("change", renderTable);
  panel.append(
    el("div", { class: "policy-status", title: baseline.line || "" },
      el("p", {},
        el("span", { class: `policy-dot tone-${status.tone}`, "aria-hidden": "true" }),
        status.text,
        status.setup ? [" ", el("a", { href: "#/setup" }, "Lock it in Setup →")] : null),
      baseline.state === "tampered" && baseline.diff?.length
        ? el("ul", { class: "policy-diff mono" }, baseline.diff.map(d => el("li", {}, d))) : null,
      el("p", { class: "note" }, `New chats open as the ${roles.default} role.`)),
    el("div", { class: "row policy-view-row" }, el("label", {}, "View as"), viewAs),
    table,
    testAnAction(scopes, projects, roles, schedules));
  await renderTable();
}

// The why box: one question, both answers (workers + the Queen), the rule named.
// v124-F3: it sends what the live engine applies — a role and a schedule's
// delegation hop (schedule:<name>@<role>, v123) — and shows every rule why considered.
function testAnAction(scopes, projects, roles, schedules, preset = {}) {
  const bound = projects.filter(projectRepoBinding);
  const project = el("select", { "aria-label": "project" },
    ...bound.map(p => el("option", { value: p.project_id }, p.name)));
  const scope = el("select", { "aria-label": "scope" }, ...scopes.map(s => el("option", { value: s }, s)));
  const role = el("select", { "aria-label": "role" },
    el("option", { value: "" }, "no role"),
    ...(roles.roles || []).map(r => el("option", { value: r.name }, r.name)));
  const delegating = schedules.filter(s => s.role);
  const schedule = el("select", { "aria-label": "schedule" },
    el("option", { value: "" }, "no schedule"),
    ...delegating.map(s => el("option", { value: s.name }, `${s.name} (as ${s.role})`)));
  const action = el("input", { class: "mono", placeholder: "run", "aria-label": "action" });
  const value = el("input", { class: "mono", placeholder: "uv sync", "aria-label": "value" });
  if (preset.project) project.value = preset.project;
  if (preset.scope) scope.value = preset.scope;
  if (preset.action) action.value = preset.action;
  // A schedule's tick sends no role, only its hop, against its own repo
  // (scheduler.run_due): picking one takes the role and the project with it.
  const ownProject = hop => bound.find(p => projectRepoBinding(p).value === hop.repo);
  schedule.addEventListener("change", () => {
    const hop = delegating.find(s => s.name === schedule.value);
    role.disabled = project.disabled = Boolean(hop);
    if (!hop) return;
    role.value = "";
    project.value = ownProject(hop)?.project_id ?? "";
  });
  const out = el("div", { class: "policy-test-out" });
  const check = el("button", { type: "button" }, "Check");
  check.addEventListener("click", async () => {
    const hop = delegating.find(s => s.name === schedule.value);
    const picked = hop ? ownProject(hop) : bound.find(p => p.project_id === project.value);
    const repo = hop ? hop.repo : projectRepoBinding(picked)?.value;
    if (!repo) { flash("bad", "pick a project with a repo binding first"); return; }
    try {
      const why = await api("GET", `/api/policy/why?repo=${encodeURIComponent(repo)}`
        + `&scope=${encodeURIComponent(scope.value)}`
        + `&action=${encodeURIComponent(action.value.trim())}`
        + `&value=${encodeURIComponent(value.value.trim())}`
        + (role.value && !hop ? `&role=${encodeURIComponent(role.value)}` : "")
        + (hop ? `&delegation_chain=${encodeURIComponent(`schedule:${hop.name}@${hop.role}`)}` : ""));
      const considered = why.run.considered || [];
      out.replaceChildren(...[
        el("p", {}, `Workers in ${picked?.name || repo}: `, why.run.error
          ? `could not decide — ${why.run.error}`
          : [el("strong", {}, verdictWord(why.run.verdict)), " — decided by ", el("span", { class: "mono" }, why.run.decided_by)]),
        // The deciding rule's sentence, when a shipped baseline rule decided (_deciding_text).
        why.run.description ? el("p", { class: "field-help" }, why.run.description) : null,
        el("p", {}, "The Queen: ", el("strong", {}, verdictWord(why.operator.verdict)),
          " — decided by ", el("span", { class: "mono" }, why.operator.decided_by)),
        why.operator.description ? el("p", { class: "field-help" }, why.operator.description) : null,
        why.role ? el("p", {}, `Role: ${why.role.note}`) : null,
        why.delegation ? el("p", {}, `Delegation: ${why.delegation.note}`) : null,
        considered.length ? el("p", { class: "note" }, "Rules considered:") : null,
        considered.length ? el("ul", { class: "policy-rules" },
          considered.map(r => treeRule(r, r.layer, r.description))) : null,
        why.baseline ? el("p", { class: "note" }, why.baseline) : null,
      ].filter(Boolean));  // replaceChildren(null) would render the text "null"
    } catch (e) { out.replaceChildren(policyNote(e.message)); }
  });
  return policySection("Test an action", true,
    el("p", { class: "field-help" },
      "What skep would decide for one action in a project, and which rule decides it. "
      + "Nothing runs and nothing is recorded."),
    bound.length
      ? el("div", { class: "policy-test" },
        el("div", { class: "row" },
          policyField("project", project, null), policyField("scope", scope, null),
          policyField("action", action, null), policyField("value", value, null, { grow: true })),
        el("div", { class: "row" },
          policyField("role", role, "The role a chat runs as; it only tightens."),
          policyField("schedule", schedule, "Ask as this schedule's workers, held to its role."),
          el("div", { class: "field" }, el("label", {}, " "), check)),
        out)
      : policyNote("No project has a repo yet: the answer depends on a project's policy."));
}

// ---- Decisions: what did skep decide? ----

async function renderDecisionsTab(panel) {
  const { rows, total, summary } = await api("GET", "/api/policy/decisions?limit=200");
  const kindOf = row => (POLICY_NOTE_SOURCES.includes(row.source) ? "note"
    : row.verdict === "require_approval" ? "ask" : row.verdict);
  const counts = [
    `${summary.allow} allowed`, `${summary.require_approval} asked`, `${summary.deny} denied`,
    ...POLICY_NOTE_SOURCES.filter(s => summary[s]).map(s => `${summary[s]} ${s}`),
  ].join(" · ");
  panel.append(el("p", { class: "policy-summary" }, counts,
    // v121-F4: the read window is the newest rotated log file + the live one.
    el("span", { class: "note" }, total > rows.length
      ? ` — the newest ${rows.length} of ${total} in the last two log files shown`
      : " — from the last two log files")));
  if (!rows.length) {
    panel.append(el("p", { class: "empty-state" },
      "Nothing decided yet. Every policy decision skep records (decisions.jsonl in the audit "
      + "trail) shows here, newest first."));
    return;
  }
  const nodes = rows.map(row => {
    const kind = kindOf(row);
    return el("div", { class: "policy-decision searchable", "data-kind": kind },
      el("span", { class: `policy-verdict verdict-${kind}` }, kind === "note" ? row.source : verdictWord(row.verdict)),
      el("span", { class: "note", title: fmtTs(row.at) }, relativeTime(row.at)),
      el("span", { class: "mono" }, row.actor || "-"),
      el("span", {}, `${row.scope} ${row.action} `, el("code", { class: "mono" }, row.value)),
      el("small", { class: "note", title: row.rule_id || "" }, row.decided_by || ""));
  });
  const filters = [
    { key: "all", label: "All" }, { key: "allow", label: "Allowed" },
    { key: "ask", label: "Asked" }, { key: "deny", label: "Denied" }, { key: "note", label: "Notes" },
  ];
  const tally = Object.fromEntries(filters.map(f => [f.key,
    f.key === "all" ? nodes.length : nodes.filter(n => n.getAttribute("data-kind") === f.key).length]));
  panel.append(
    buildFilterBar(filters, tally, (key) => {
      for (const node of nodes) node.classList.toggle("hidden", key !== "all" && node.getAttribute("data-kind") !== key);
    }),
    el("div", { class: "policy-decisions" }, nodes));
}

// ---- Standing grants: what runs without asking me? ----

async function renderGrantsTab(panel) {
  const ruleData = await api("GET", "/api/policy/rules");
  // v109-F9: learned rules — the standing grants that auto-run things.
  const learnedRules = ruleData.rules || [];
  const ruleRow = (rule) => {
    const revoke = el("button", { type: "button", class: "danger ghost" }, "Revoke");
    revoke.addEventListener("click", async () => {
      if (!window.confirm(
        `Revoke ${rule.rule_id}? The next matching action cards again.`)) return;
      try {
        await api("DELETE", `/api/policy/rules/${encodeURIComponent(rule.rule_id)}`);
        flash("ok", `revoked ${rule.rule_id} — the next matching action cards again`);
        route();
      } catch (e) { flash("bad", e.message); }
    });
    return el("div", { class: "policy-grant searchable", title: rule.rule_id },
      el("span", {}, `${rule.scope} ${rule.action}`),
      el("code", { class: "mono" }, rule.pattern),
      el("span", { class: "note" },
        (rule.tier === "session" ? "until serve restarts" : "always")
        + ` · ${rule.provenance}`
        + (rule.created_at ? ` · granted ${relativeTime(rule.created_at)}` : "")),
      revoke);
  };
  // v109-F9: the Queen's standing operator document — read-only (its edits
  // stay the carded set_operator_policy chat verb).
  const operatorRules = ruleData.operator_rules || [];
  panel.append(
    policySection("Standing grants", true,
      el("p", { class: "field-help" },
        "Written by allow-always confirmations and approved cards — each lets its exact "
        + "action run without a card. Session grants end when serve restarts; Revoke ends any now."),
      learnedRules.length
        ? el("div", { class: "stack" }, learnedRules.map(ruleRow))
        : el("p", { class: "empty-state" },
          "No standing grants — nothing runs without asking beyond the policy itself.")),
    policySection("The Queen's own rules", true,
      el("p", { class: "field-help" },
        "The Queen's standing document (the set_operator_policy chat verb). It governs the "
        + "Queen's own reads and fetches, never workers. Read-only here."),
      operatorRules.length
        ? el("ul", { class: "policy-rules policy-operator" }, operatorRules.map(r =>
          el("li", { title: r.rule_id },
            el("strong", {}, verdictWord(r.verdict)), ` ${r.scope} ${r.action} `,
            r.pattern === "*" ? "anything" : el("code", { class: "mono" }, r.pattern))))
        : el("p", { class: "empty-state" }, "The Queen has no rules of her own stored.")));
}

// ---- Projects & groups: what is different for this project? ----

// v75-F5 (C8): each row holds ONE command as its exact JSON array, so argv
// semantics survive — ["bash", "-c", "a b"] never becomes four words. The
// raw-JSON toggle is the whole-list escape hatch; the server-side validation
// stays the authority either way.
function buildShellCommandEditor(commands) {
  const rows = el("div", { class: "shell-cmd-list" });
  const rawBox = el("textarea", { class: "mono hidden" });
  let raw = false;
  const renderRows = () => {
    rows.replaceChildren(...commands.map((command, i) => {
      const input = el("input", { class: "mono", value: JSON.stringify(command) });
      input.addEventListener("change", () => {
        try {
          const parsed = JSON.parse(input.value || "[]");
          if (!Array.isArray(parsed) || !parsed.every(part => typeof part === "string")) {
            throw new Error("not a string array");
          }
          commands[i] = parsed;
        } catch {
          flash("bad", 'each command is a JSON string array, e.g. ["ruff", "check"]');
          input.value = JSON.stringify(commands[i]);
        }
      });
      return el("div", { class: "shell-cmd-row" },
        input,
        iconButton("remove command", "×", {
          class: "danger ghost",
          onclick: () => { commands.splice(i, 1); renderRows(); },
        }));
    }));
  };
  renderRows();
  const addRow = el("button", { type: "button", class: "ghost" }, "+ Add command");
  addRow.addEventListener("click", () => { commands.push([]); renderRows(); });
  const structured = el("div", {}, rows, addRow);
  const toggle = el("button", { type: "button", class: "ghost" }, "raw JSON");
  toggle.addEventListener("click", () => {
    if (!raw) {
      rawBox.value = JSON.stringify(commands, null, 2);
      structured.classList.add("hidden");
      rawBox.classList.remove("hidden");
      toggle.textContent = "row editor";
      raw = true;
      return;
    }
    try {
      const parsed = JSON.parse(rawBox.value || "[]");
      commands.splice(0, commands.length, ...parsed);
    } catch {
      flash("bad", "raw JSON does not parse — fix it, or save straight from raw mode");
      return;
    }
    renderRows();
    rawBox.classList.add("hidden");
    structured.classList.remove("hidden");
    toggle.textContent = "raw JSON";
    raw = false;
  });
  return {
    node: el("div", { class: "shell-cmd-editor" }, structured, rawBox,
      el("div", { class: "actions" }, toggle)),
    value: () => {
      if (!raw) return commands;
      try { return JSON.parse(rawBox.value || "[]"); }
      catch { throw new Error("allowed shell commands: the raw JSON does not parse"); }
    },
  };
}

// P2 (ADR 0053): one typed control per registry knob. The registry's `type`
// string decides the widget; `value()` returns the JSON the routes expect.
function knobInput(knob, current) {
  const type = knob.type || "str";
  if (type === "bool") {
    const box = el("input", { type: "checkbox" });
    box.checked = current === true;
    return { node: box, value: () => box.checked };
  }
  if (type === "int") {
    const input = el("input", { type: "number", class: "mono", value: String(current ?? knob.default ?? 0) });
    return { node: input, value: () => Number(input.value) };
  }
  if (type.startsWith("enum[")) {
    const options = type.slice(5, -1).split("|");
    const select = el("select", {}, ...options.map(o => el("option", { value: o }, o)));
    select.value = options.includes(current) ? current : options[0];
    return { node: select, value: () => select.value };
  }
  if (type === "list[argv]") {
    const editor = buildShellCommandEditor((Array.isArray(current) ? current : []).map(c => [...c]));
    return { node: editor.node, value: () => editor.value() };
  }
  if (type === "list[str]") {
    const input = el("input", { class: "mono", value: (Array.isArray(current) ? current : []).join(", ") });
    return { node: input, value: () => input.value.split(",").map(s => s.trim()).filter(Boolean) };
  }
  const input = el("input", { class: "mono", value: current == null ? "" : String(current) });
  return { node: input, value: () => input.value.trim() };
}

// P2 (ADR 0053): the registry-driven editor. Every knob the registry names
// renders with the layer that decided it (RSoP), everything it overrode, and
// a tri-state control at the layer being edited — inherit (absent from the
// overlay) or set. Writes go through the routes that already exist (I5): a
// project overlay is the definition re-POSTed (the upsert), global knobs are
// PUT /api/policy. Explicit "cleared" has no stored form yet (D3/D7); the
// hint says so.
function effectivePolicyEditor(policy, registry, projects) {
  const knobs = (registry.keys || []).filter(k =>
    k.kind === "knob" && k.system === "run_policy" && k.name !== "policy_engine_v2_scopes");
  const picker = el("select", { "aria-label": "project" },
    el("option", { value: "" }, "no project: the global defaults"),
    ...projects.map(p => el("option", { value: p.project_id }, `${p.name} (${p.project_id})`)));
  const editorBody = el("div", { class: "stack" });
  const chipFor = (layer, name) => el("span", { class: "chip tone-info" },
    name ? `${layer}:${name}` : layer);
  const renderEditor = async () => {
    editorBody.replaceChildren(policyNote("resolving…"));
    const projectId = picker.value;
    let project = null;
    let rsop = null;
    try {
      project = projectId
        ? await api("GET", `/api/projects/${encodeURIComponent(projectId)}`) : null;
      const binding = projectRepoBinding(project);
      rsop = binding
        ? await api("GET", `/api/repos/${encodeURIComponent(binding.value)}/rsop`)
          .catch(exc => ({ error: String(exc) }))
        : null;
    } catch (e) { editorBody.replaceChildren(policyNote(e.message)); return; }
    const byKey = new Map((rsop?.keys || []).map(k => [k.key, k]));
    const editLayer = project ? "project" : "global";
    const overlay = project ? { ...(project.policy || {}) } : null;
    const rows = [];
    const table = el("table", { class: "policy-effective" },
      el("thead", {}, el("tr", {},
        ["Setting", "Value", "Decided by", "Overrode", "Change here"].map(h => el("th", {}, h)))),
      el("tbody", {}, knobs.map(knob => {
        const entry = byKey.get(knob.name);
        const effective = project ? entry?.value : policy[knob.name];
        const decided = project
          ? (entry && entry.source_layer ? chipFor(entry.source_layer, entry.source_name)
            : el("span", { class: "chip tone-muted" }, "unresolved"))
          : el("span", { class: "chip tone-info" }, "global");
        const overrode = el("div", { class: "row" },
          (entry?.overridden || []).map(o => el("span", { class: "chip tone-muted", title: JSON.stringify(o.value) },
            o.name ? `${o.layer}:${o.name}` : o.layer)));
        const canEdit = (knob.layers || []).includes(editLayer)
          && (project || Object.hasOwn(policy, knob.name));
        const control = el("div", { class: "editor-ctl" });
        if (canEdit) {
          const inOverlay = project ? Object.hasOwn(overlay, knob.name) : true;
          const state = el("select", { "aria-label": `${knob.name}: inherit or set` },
            el("option", { value: "inherit" }, "inherit"),
            el("option", { value: "set" }, "set"));
          state.value = inOverlay ? "set" : "inherit";
          const input = knobInput(knob, inOverlay ? (project ? overlay[knob.name] : policy[knob.name])
            : (entry?.value ?? knob.default));
          input.node.classList.toggle("hidden", !inOverlay);
          state.addEventListener("change", () => {
            input.node.classList.toggle("hidden", state.value !== "set");
          });
          if (project) control.append(state);
          control.append(input.node);
          rows.push({ key: knob.name, state: () => (project ? state.value : "set"), value: input.value });
        } else {
          control.append(el("span", { class: "note" }, project ? "not settable here" : "read-only"));
        }
        return el("tr", {},
          el("td", { class: "mono", title: `${knob.doc} (${knob.invariant}) · may be set at: ${(knob.layers || []).join(", ")}` }, knob.name),
          el("td", { class: "mono policy-value", title: effective === undefined ? "" : JSON.stringify(effective) },
            effective === undefined ? "—" : plainValue(effective)),
          el("td", {}, decided),
          el("td", {}, overrode),
          el("td", {}, control));
      })));
    const save = el("button", { class: "primary" },
      project ? `Save ${project.name} overlay` : "Save global knobs");
    save.addEventListener("click", async () => {
      try {
        if (project) {
          const nextPolicy = { ...overlay };
          for (const row of rows) {
            if (row.state() === "set") nextPolicy[row.key] = row.value();
            else delete nextPolicy[row.key];
          }
          await api("POST", "/api/projects", { ...project, policy: nextPolicy });
          flash("ok", `${project.name}: overlay saved — the next dispatch resolves it`);
          await renderEditor();  // stay on this project; the RSoP re-resolves
          return;
        }
        const body = {};
        for (const row of rows) {
          const next = row.value();
          if (JSON.stringify(next) !== JSON.stringify(policy[row.key])) body[row.key] = next;
        }
        if (!Object.keys(body).length) { flash("ok", "nothing changed"); return; }
        await api("PUT", "/api/policy", body);
        flash("ok", "global policy saved — applies to the next run");
        route();
      } catch (e) { flash("bad", e.message); }
    });
    const engineNote = rsop?.engine
      ? el("p", { class: "field-help" },
        `engine: v2 on [${(rsop.engine.v2_scopes || []).join(", ") || "none"}]`
        + (rsop.engine.routed_keys ? ` · routed ${rsop.engine.routed_keys.length} knob(s)` : "")
        + (rsop.engine.rule_refusals?.length ? ` · ${rsop.engine.rule_refusals.length} rule refusal(s)` : ""))
      : null;
    editorBody.replaceChildren(...[
      rsop?.error ? policyNote(`policy did not resolve: ${rsop.error}`)
        : el("div", { class: "policy-table-wrap" }, table),
      engineNote,
      el("p", { class: "field-help" },
        "inherit = the setting is absent from this layer (the layer below decides); set = this "
        + "layer decides. An explicit \"cleared\" state has no stored form yet — remove the "
        + "setting here and edit the layer below."),
      el("div", { class: "actions" }, save),
    ].filter(Boolean));  // replaceChildren(null) would render the text "null"
  };
  picker.addEventListener("change", renderEditor);
  return { picker, editorBody, renderEditor };
}

// What a group grants, as a definition list — the JSON waits behind Edit.
function groupGrants(policy) {
  return el("dl", { class: "policy-group-grants" }, Object.entries(policy).flatMap(([key, value]) => [
    el("dt", { title: key }, GROUP_KEY_LABELS[key] || key),
    el("dd", { class: Array.isArray(value) ? "mono" : "" }, plainValue(value)),
  ]));
}

// v97-F5 (ADR 0048): policy groups — named convenience-grant bundles,
// live-composed into every attached project's run policy. Edit once, all
// follow; "Save as new group" is the copy-on-write escape hatch.
function groupRow(group, groupEditHandoff) {
  const fromProject = groupEditHandoff?.group === group.name
    ? groupEditHandoff.project : null;
  const textarea = el("textarea", { class: "mono", rows: "7", "aria-label": `${group.name} as JSON` });
  textarea.value = JSON.stringify(group.policy, null, 2);
  const forkBox = el("input", { type: "checkbox" });
  // Context-sensitive default: opened FROM a project's view while the
  // group serves more than one project → the protective choice (fork)
  // starts checked; on this global page editing the shared thing is the
  // point, so it starts unchecked. The click decides either way.
  forkBox.checked = Boolean(fromProject && group.attached_projects.length > 1);
  const forkName = el("input", { class: "mono", value: `${group.name}-2`, "aria-label": "new group name" });
  const repoint = el("select", { "aria-label": "repoint a project to the new group" },
    el("option", { value: "" }, "repoint: none"),
    ...group.attached_projects.map(p =>
      el("option", { value: p }, `repoint ${p}`)));
  if (fromProject && group.attached_projects.includes(fromProject)) {
    repoint.value = fromProject;
  }
  const syncFork = () => { forkName.hidden = repoint.hidden = !forkBox.checked; };
  forkBox.addEventListener("change", syncFork);
  syncFork();
  const saveGroup = el("button", { type: "button" }, "Save");
  const removeGroup = el("button", { type: "button", class: "danger ghost" }, "Delete");
  saveGroup.addEventListener("click", async () => {
    let policy;
    try { policy = JSON.parse(textarea.value); }
    catch { flash("bad", "policy must be valid JSON"); return; }
    try {
      if (forkBox.checked) {
        const newName = forkName.value.trim();
        await api("POST", `/api/policy-groups/${encodeURIComponent(group.name)}/fork`, {
          new_name: newName,
          policy,
          repoint_project: repoint.value || null,
        });
        flash("ok", `forked ${group.name} → ${newName}`
          + (repoint.value ? ` and repointed ${repoint.value}` : "")
          + " — the source group is untouched");
      } else {
        const affected = group.attached_projects.length
          ? `every attached project follows: ${group.attached_projects.join(", ")}`
          : "no projects attach it yet";
        if (!window.confirm(`Update ${group.name} IN PLACE — ${affected}. Continue?`)) return;
        await api("PUT", `/api/policy-groups/${encodeURIComponent(group.name)}`, policy);
        flash("ok", `updated ${group.name} — ${affected}`);
      }
      route();
    } catch (e) { flash("bad", e.message); }
  });
  removeGroup.addEventListener("click", async () => {
    try {
      await api("DELETE", `/api/policy-groups/${encodeURIComponent(group.name)}`);
      flash("ok", `deleted ${group.name}`);
      route();
    } catch (e) { flash("bad", e.message); }  // attached → the refusal names the projects
  });
  const editor = el("div", { class: `policy-group-editor${fromProject ? "" : " hidden"}` },
    el("p", { class: "field-help" },
      "Save updates IN PLACE (every attached project follows on its next "
      + "dispatch). “Save as new group” forks copy-on-write: the source and "
      + "its other projects stay untouched."),
    textarea,
    el("div", { class: "row" },
      el("label", {}, forkBox, " Save as new group"),
      forkName, repoint, saveGroup));
  const edit = el("button", { type: "button", class: "ghost" }, "Edit");
  edit.addEventListener("click", () => editor.classList.toggle("hidden"));
  const badge = group.builtin ? (group.edited ? "built in, edited" : "built in") : "custom";
  const attachedNote = group.attached_projects.length
    ? `used by ${group.attached_projects.join(", ")}` : "used by no project";
  return el("details", { class: "policy-group-row", open: fromProject ? "" : null },
    el("summary", {},
      el("span", { class: "mono" }, group.name),
      el("span", { class: "note" }, ` ${badge} · ${attachedNote}`)),
    groupGrants(group.policy),
    el("div", { class: "row" }, edit, removeGroup),
    editor);
}

async function renderGroupsTab(panel, groupEditHandoff) {
  const [policy, { groups }, registry, { projects }] = await Promise.all([
    api("GET", "/api/policy"),
    api("GET", "/api/policy-groups"),
    api("GET", "/api/policy/registry"),
    api("GET", "/api/projects"),
  ]);
  const editor = effectivePolicyEditor(policy, registry, projects);

  const newGroupName = el("input", { class: "mono", placeholder: "new-group-name", "aria-label": "new group name" });
  const newGroupBtn = el("button", { type: "button" }, "Create group");
  newGroupBtn.addEventListener("click", async () => {
    try {
      await api("PUT", `/api/policy-groups/${encodeURIComponent(newGroupName.value.trim())}`,
        { default_network: [] });
      flash("ok", `created ${newGroupName.value.trim()} — edit its grants below`);
      route();
    } catch (e) { flash("bad", e.message); }
  });
  // v109-F9: within Groups, filter by attached project — the same chip
  // pattern, one level down.
  const attachedProjects = [...new Set(groups.flatMap(g => g.attached_projects))].sort();
  const groupList = el("div", { class: "stack" });
  const renderGroupRows = (projectKey) => {
    groupList.replaceChildren(...groups
      .filter(g => projectKey === "all" || g.attached_projects.includes(projectKey))
      .map(g => groupRow(g, groupEditHandoff)));
  };
  const projectBar = attachedProjects.length
    ? buildFilterBar(
      [{ key: "all", label: "All projects" },
        ...attachedProjects.map(p => ({ key: p, label: p }))],
      Object.fromEntries([
        ["all", groups.length],
        ...attachedProjects.map(p =>
          [p, groups.filter(g => g.attached_projects.includes(p)).length]),
      ]),
      renderGroupRows)
    : null;
  renderGroupRows("all");
  panel.append(
    policySection("Effective policy", true,
      el("p", { class: "field-help", title: "the registry-driven RSoP editor (ADR 0053)" },
        "Every setting skep has: its value for the picked project, which layer decided it, "
        + "what it overrode, and a control where this layer may change it. Edits go through "
        + "the same routes the CLI and chat use."),
      policyField("project", editor.picker, "Pick a project to resolve against its repo; none = the global defaults."),
      editor.editorBody),
    policySection("Policy groups", true,
      el("p", { class: "field-help" },
        "Reusable grant bundles (network hosts, shell commands, environment names, "
        + "budgets, engine) attached to projects and composed live — the project's own "
        + "policy always wins single values; trust-ramp settings can never ride a group."),
      projectBar,
      groupList,
      el("div", { class: "row" }, newGroupName, newGroupBtn)));
  await editor.renderEditor();
}

// ---- Defaults: what applies when nothing else says? ----

async function renderDefaultsTab(panel) {
  const [policy, scopeList] = await Promise.all([api("GET", "/api/policy"), scopeNames()]);
  const list = input => input.value.split(",").map(s => s.trim()).filter(Boolean);
  const auto = el("input", { type: "checkbox", "aria-label": "auto-apply safe dependency fixes" });
  auto.checked = policy.auto_approve;
  const workerCmd = el("input", { value: policy.worker_cmd, class: "mono" });
  const network = el("input", { value: policy.default_network.join(", ") });
  const env = el("input", { value: policy.default_env_allowlist.join(", ") });
  const mode = el("select", {},
    el("option", { value: "ask" }, "ask each dispatch"),
    el("option", { value: "workspace" }, "workspace"),
    el("option", { value: "sandbox" }, "sandbox"));
  mode.value = policy.default_execution_mode;
  const trustedRoots = el("input", { value: policy.trusted_workspace_roots.join(", ") });
  const sandboxRequired = el("input", { value: policy.sandbox_required_for.join(", ") });
  const shellEditor = buildShellCommandEditor(
    (policy.allowed_shell_commands || []).map(command => [...command]));
  const tick = el("input", { type: "number", min: "1", value: String(policy.ticker_interval_seconds) });
  const defaultWall = el("input", { type: "number", min: "1", value: String(policy.default_wall_clock_seconds) });
  const defaultIterations = el("input", { type: "number", min: "1", value: String(policy.default_max_iterations) });
  const defaultActions = el("input", { type: "number", min: "1", value: String(policy.default_max_actions) });
  const defaultProviderCalls = el("input", { type: "number", min: "0", value: String(policy.default_max_provider_calls) });
  const save = el("button", { type: "button", class: "primary" }, "Save defaults");
  save.addEventListener("click", async () => {
    try {
      await api("PUT", "/api/policy", {
        auto_approve: auto.checked,
        worker_cmd: workerCmd.value.trim(),
        default_network: list(network),
        default_env_allowlist: list(env),
        default_execution_mode: mode.value,
        trusted_workspace_roots: list(trustedRoots),
        sandbox_required_for: list(sandboxRequired),
        ticker_interval_seconds: Number(tick.value),
        default_wall_clock_seconds: Number(defaultWall.value),
        default_max_iterations: Number(defaultIterations.value),
        default_max_actions: Number(defaultActions.value),
        default_max_provider_calls: Number(defaultProviderCalls.value),
        allowed_shell_commands: shellEditor.value(),
      });
      flash("ok", "policy saved — applies to the next run");
    } catch (e) { flash("bad", e.message); }
  });

  // The engine switch (P1, ADR 0053 D6); every flip is a line in
  // <audit>/policy/engine-flips.jsonl.
  const switchBoxes = scopeList.map(s => {
    const box = el("input", { type: "checkbox" });
    box.checked = (policy.policy_engine_v2_scopes || []).includes(s);
    return [s, box];
  });
  const switchSave = el("button", { type: "button" }, "Apply engine switch");
  switchSave.addEventListener("click", async () => {
    const scopes = switchBoxes.filter(([, box]) => box.checked).map(([s]) => s);
    if (!window.confirm(`Route ${scopes.length ? scopes.join(", ") : "no scope"} through policy `
      + "engine v2? The flip is recorded in the audit trail.")) return;
    try {
      await api("PUT", "/api/policy", { policy_engine_v2_scopes: scopes });
      flash("ok", "engine switch saved — recorded in the audit trail");
      route();
    } catch (e) { flash("bad", e.message); }
  });

  panel.append(
    el("p", { class: "field-help" },
      "What a run gets when no baseline, role, project or group says otherwise. "
      + "Changes apply to the next run."),
    policySection("Limits", true,
      el("div", { class: "row" },
        policyField("Wall clock per run (s)", defaultWall,
          "Maximum seconds a worker may run before it is stopped."),
        policyField("Iterations per run", defaultIterations,
          "Plan/act rounds a worker gets before it must finish."),
        policyField("Actions per run", defaultActions,
          "Total capability calls (files, shell, …) per run."),
        policyField("Provider calls per run", defaultProviderCalls,
          "LLM requests a run may spend — the cost ceiling."))),
    policySection("Network and environment", true,
      el("div", { class: "row" },
        policyField("Network hosts workers may reach", network,
          "Domains, comma-separated. Empty = deny all network.",
          { grow: true, title: "default network allowlist (D1)" }),
        policyField("Environment variables passed to workers", env,
          "Variable NAMES, comma-separated — never values.",
          { grow: true, title: "default env allowlist (G2)" }))),
    policySection("Where workers run", true,
      el("div", { class: "row" },
        policyField("Default place to run", mode,
          "workspace = a checkout under a trusted folder; sandbox = bubblewrap/seatbelt "
          + "walls; ask = choose per dispatch.", { title: "default execution" }),
        policyField("Trusted workspace folders", trustedRoots,
          "Where workspace (non-sandbox) runs are allowed, comma-separated.",
          { grow: true, title: "trusted workspace roots" }),
        policyField("Always sandbox these", sandboxRequired,
          "Repos or paths that always run sandboxed, whatever the default says.",
          { grow: true, title: "sandbox required for" }))),
    policySection("Auto-apply", true,
      el("div", { class: "field" },
        el("label", { title: "auto-approval (D3: verified + re-verified manifest-only fixes)" },
          "Auto-apply verified dependency fixes"),
        el("p", { class: "field-help" },
          "Auto-applies only verified, re-verified, manifest-only fixes on the "
          + "constrained skep/ integration branch — never on main, never for "
          + "other change classes."),
        el("div", {}, auto, " auto-apply safe dependency fixes"))),
    policySection("Advanced", false,
      el("div", { class: "row" },
        policyField("Worker command", workerCmd,
          "The command skep uses to spawn the coding worker.", { grow: true }),
        policyField("Ticker interval (s)", tick,
          "How often the in-process scheduler checks for due schedules.")),
      policyField("Allowed shell commands", shellEditor.node,
        "Commands workers may run without an approval card. One row per "
        + "command, as a JSON array — quoted arguments survive exactly."),
      el("div", { class: "field" },
        el("label", { title: "policy engine v2 — scopes routed through the new engine (ADR 0053 D6)" },
          "Scopes on the new policy engine"),
        el("p", { class: "field-help" },
          "Cut over one scope at a time, shell first, after the golden parity suite is green. "
          + "Flip back is one save; both are recorded."),
        el("div", { class: "row" }, switchBoxes.map(([s, box]) => el("label", { class: "note" }, box, " ", s))),
        el("div", { class: "actions" }, switchSave))),
    el("div", { class: "actions" }, save));
}

// ---- Tree: every setting, where it stands (v124-F2, DD-10) ----
// Group Policy's shape over the reads that already exist (I5): the registry
// names every key, the RSoP says which layer decided it in one project, the
// baseline's explain rows carry rule descriptions. Read-only; each leaf links
// to the editor that already changes it.

const RULE_WORD = { allow: "allow", require_approval: "ask", ask: "ask", deny: "deny" };
// A baseline that does not verify fails every decision closed (policy_layers.
// baseline_guard): the guard's reason by baseline state; ok and reapprove apply.
const BASELINE_GUARD = { none: "no_baseline", tampered: "baseline_tampered" };
// What a roleless schedule runs as (v123: only worker schedules take a role).
const SCHEDULE_NO_ROLE = {
  script: "the script runs as you",
  prompt: "the prompt runs in its chat's role",
  note: "a note dispatches no worker",
  digest: "a digest dispatches no worker",
};

// A setting's value in Group Policy words: true/false reads Enabled/Disabled.
const settingWord = value => (typeof value === "boolean" ? (value ? "Enabled" : "Disabled") : plainValue(value));
const layerName = (layer, name) => (name ? `${layer}:${name}` : layer);

// Scopes → scope → action; Settings → area → key; Schedules → name.
function treeModel(registry, schedules) {
  const keys = registry.keys || [];
  const branch = (key, label, children) => ({ key, label, children });
  const byFirst = (list, of) => [...new Set(list.map(of))].sort().map(name => [name, list.filter(k => of(k) === name)]);
  const scopeKeys = keys.filter(k => k.system === "scope_document");
  const settingKeys = keys.filter(k => k.system !== "scope_document");
  return [
    branch("Scopes", "Scopes", byFirst(scopeKeys, k => k.name.split(".")[0]).map(([scope, list]) =>
      branch(`scope:${scope}`, scope, list.map(k => ({ key: k.name, label: k.name.slice(scope.length + 1), registry: k }))))),
    branch("Settings", "Settings", byFirst(settingKeys, k => k.system).map(([area, list]) =>
      branch(`area:${area}`, area, list.map(k => ({ key: k.name, label: k.name, registry: k }))))),
    branch("Schedules", "Schedules", schedules.map(s => ({ key: `schedule:${s.name}`, label: s.name, schedule: s }))),
  ];
}

// The WAI-ARIA tree pattern: one tab stop (roving tabindex), arrows move,
// Right opens / steps in, Left closes / steps out, Enter opens or selects.
function buildTree(model, onSelect) {
  const tree = el("ul", { class: "policy-tree", role: "tree", "aria-label": "policy settings" });
  let current = null;
  let selected = null;
  const focusOn = item => {
    current.node.setAttribute("tabindex", "-1");
    current = item;
    item.node.setAttribute("tabindex", "0");
    item.node.focus();
  };
  const setOpen = (item, open) => {
    item.open = open;
    item.node.setAttribute("aria-expanded", String(open));
    item.group.hidden = !open;
  };
  const activate = item => {
    focusOn(item);
    if (item.group) { setOpen(item, !item.open); return; }
    if (selected) selected.node.setAttribute("aria-selected", "false");
    selected = item;
    item.node.setAttribute("aria-selected", "true");
    onSelect(item.data);
  };
  const make = (data, parent) => {
    const label = el("span", { class: "policy-tree-label" }, data.label);
    const node = el("li", {
      role: "treeitem", tabindex: "-1", "data-key": data.key,
      class: data.children ? "policy-tree-branch" : "policy-tree-leaf",
      "aria-expanded": data.children ? "false" : null,
      "aria-selected": data.children ? null : "false",
    }, label);
    const item = { node, parent, data, open: false, kids: [] };
    if (data.children) {
      item.group = el("ul", { role: "group" });
      item.group.hidden = true;
      item.kids = data.children.map(child => make(child, item));
      item.group.append(...item.kids.map(kid => kid.node));
      node.append(item.group);
    }
    // A branch toggles from its label, so a click inside its open group never closes it.
    (data.children ? label : node).addEventListener("click", e => { e.stopPropagation(); activate(item); });
    return item;
  };
  const roots = model.map(data => make(data, null));
  tree.append(...roots.map(item => item.node));
  current = roots[0];
  current.node.setAttribute("tabindex", "0");
  const visible = () => {
    const out = [];
    const walk = items => { for (const item of items) { out.push(item); if (item.open) walk(item.kids); } };
    walk(roots);
    return out;
  };
  tree.addEventListener("keydown", e => {
    const list = visible();
    const at = list.indexOf(current);
    const moves = {
      ArrowDown: () => list[at + 1] && focusOn(list[at + 1]),
      ArrowUp: () => at > 0 && focusOn(list[at - 1]),
      ArrowRight: () => current.group && (current.open ? current.kids[0] && focusOn(current.kids[0]) : setOpen(current, true)),
      ArrowLeft: () => (current.group && current.open ? setOpen(current, false) : current.parent && focusOn(current.parent)),
      Enter: () => activate(current),
    };
    if (!moves[e.key]) return;
    e.preventDefault();
    moves[e.key]();
  });
  return tree;
}

const fact = (label, ...value) => el("p", {}, el("strong", {}, `${label}: `), ...value);
const editLink = (href, text) => el("p", {}, el("a", { href }, text));

// One rule: verdict, pattern, id, the layer that stated it, its explain text.
function treeRule(rule, layer, description) {
  return el("li", { class: "policy-tree-rule" },
    el("strong", {}, RULE_WORD[rule.verdict] || rule.verdict), " ",
    rule.pattern === "*" ? "anything" : el("code", { class: "mono" }, rule.pattern),
    " · ", el("span", { class: "mono" }, rule.rule_id),
    " · ", el("span", { class: "chip tone-info" }, layer || "compiled"),
    el("span", { class: description ? "field-help" : "note" }, description || "No description"));
}

// policy_v3.OPERATOR_LANES: decided on the Queen's lane even from a checkout.
const QUEEN_LANES = ["network.search", "network.fetch"];

function scopeLeafDetail(key, ctx) {
  const [scope, action] = [key.name.split(".")[0], key.name.slice(key.name.indexOf(".") + 1)];
  const unmatched = key.default === "deny" ? "denies" : "asks";
  if (!ctx.guard && ctx.rsop?.error) return [policyNote(`policy did not resolve: ${ctx.rsop.error}`)];
  // A description is the baseline's, so it shows only on a rule the baseline
  // stated, matched whole — never by id alone (I8, policy_templates.shipped_descriptions).
  const baselineLayer = ctx.explain.name ? `template:${ctx.explain.name}` : null;
  const whole = r => [r.scope ?? scope, RULE_WORD[r.verdict], r.action, r.pattern, r.rule_id].join("\u0000");
  const explained = new Map((ctx.explain.rules || []).filter(r => r.description).map(r => [whole(r), r.description]));
  // Whose answer it is: the workers' compiled run lane in the project, and the
  // Queen's lane (the baseline without its shell allows, plus her documents);
  // with no project, the locked baseline's layer as stated — no lane's answer.
  const lanes = ctx.rsop
    ? [
      QUEEN_LANES.includes(key.name) ? null : [`Workers in ${ctx.rsop.project?.name || ctx.rsop.repo}`, ctx.rsop.scopes?.[scope]],
      ["The Queen", ctx.rsop.operator?.[scope]],
    ].filter(Boolean)
    : [[ctx.guard ? "Every decision" : "The locked baseline",
      (ctx.explain.rules || []).filter(r => r.scope === scope).map(r => ({ ...r, layer: baselineLayer || "baseline" }))]];
  const lane = ([who, all]) => {
    const rules = (all || []).filter(r => r.action === action);
    const verdicts = ["allow", "ask", "deny"].filter(v => rules.some(r => RULE_WORD[r.verdict] === v));
    // The guard answers before any rule (I8): nothing below it is live, so no rule shows.
    const said = ctx.guard ? `Asks: baseline ${ctx.guard}`
      : verdicts.length ? `Configured: ${verdicts.join(" / ")}` : `Not configured → ${unmatched}`;
    return [
      el("p", { class: "policy-tree-state" }, `${who}: ${said}`),
      !ctx.guard && verdicts.length ? policyNote(`anything no rule matches ${unmatched}`) : null,
      !ctx.guard && rules.length ? el("ul", { class: "policy-rules" }, rules.map(r => treeRule(r, r.layer || r.source,
        r.description || (r.layer && r.layer === baselineLayer ? explained.get(whole(r)) : null)))) : null,
    ];
  };
  const slot = el("div");
  const testThis = el("button", { type: "button" }, "Test this");
  testThis.addEventListener("click", () => slot.replaceChildren(testAnAction(
    ctx.scopes, ctx.projects, ctx.roles, ctx.schedules, { scope, action, project: ctx.project })));
  return [
    ...lanes.flatMap(lane),
    editLink("#/setup", "The baseline changes in Setup →"),
    el("div", { class: "actions" }, testThis),
    slot,
  ];
}

function keyLeafDetail(key, ctx) {
  if (ctx.rsop?.error) {
    return [
      policyNote(`policy did not resolve: ${ctx.rsop.error}`),
      fact("May be set at", (key.layers || []).join(", ") || "no layer: built into skep"),
      editLink("#/policies/groups", "Change it in Projects & groups →"),
    ];
  }
  const entry = (ctx.rsop?.keys || []).find(k => k.key === key.name);
  // No project: what the locked baseline sets — unless it does not verify, when nothing of it is live.
  const baselineSets = !ctx.rsop && !ctx.guard && (ctx.explain.settings || []).find(s => s.key === key.name);
  if (baselineSets) {
    return [
      el("p", { class: "policy-tree-state" }, settingWord(baselineSets.value)),
      // A template layer is the locked baseline, every setting enforced (ADR 0053 D4).
      fact("Decided by", el("span", { class: "chip tone-info policy-tree-decider", title: `template:${ctx.explain.name}` },
        "Enforced (locked baseline)")),
      fact("May be set at", (key.layers || []).join(", ")),
      editLink("#/setup", "The baseline changes in Setup →"),
    ];
  }
  if (!entry) {
    return [
      fact("Default", settingWord(key.default)),
      fact("May be set at", (key.layers || []).join(", ") || "no layer: built into skep"),
      key.system === "run_policy" && !ctx.rsop ? policyNote("Pick a project to see which layer decides it.") : null,
      key.system === "run_policy" ? editLink("#/policies/groups", "Change it in Projects & groups →") : null,
    ];
  }
  // A template layer is the locked baseline, every setting enforced (ADR 0053 D4).
  const decider = entry.source_layer === "template" ? "Enforced (locked baseline)"
    : entry.source_layer ? layerName(entry.source_layer, entry.source_name) : null;
  return [
    el("p", { class: "policy-tree-state" }, entry.source_layer
      ? settingWord(entry.value) : `Not configured → ${settingWord(entry.value ?? key.default)}`),
    decider ? fact("Decided by", el("span", {
      class: "chip tone-info policy-tree-decider", title: layerName(entry.source_layer, entry.source_name),
    }, decider)) : null,
    entry.overridden?.length ? fact("Overrode", entry.overridden.map(o => `${layerName(o.layer, o.name)} (${settingWord(o.value)})`).join(", ")) : null,
    fact("May be set at", (key.layers || []).join(", ")),
    editLink("#/policies/groups", "Change it in Projects & groups →"),
  ];
}

// What a schedule's network really is (scheduler.run_due): a script runs via
// /bin/sh as you; a worker in a bound repo adds its hosts to the project's
// default_network (none listed: that list alone); an unbound repo's worker
// gets only its own list. Notes, digests and prompts reach no network here.
function scheduleNetwork(s, project) {
  if (s.worker_kind === "script") return fact("Network", "yours — the script runs via /bin/sh as you, outside every policy");
  if (SCHEDULE_NO_ROLE[s.worker_kind]) return null;
  if (project) {
    return fact("Extra hosts requested", s.network.length
      ? `${plainValue(s.network)}, on top of the project's default_network`
      : "none (the project's default_network applies)");
  }
  return fact("Network", `${plainValue(s.network)} — no project adds any`);
}

function scheduleLeafDetail(s) {
  const project = s.project_context?.name;
  const roleless = SCHEDULE_NO_ROLE[s.worker_kind]
    || (project ? `its workers run under ${project}'s policy alone`
      : "its repo is bound to no project, so its workers run on the global defaults");
  return [
    el("h3", { class: "mono" }, s.name),
    fact("Kind", s.worker_kind),
    s.role ? fact("Role", el("strong", {}, s.role)) : el("p", {}, `No role: ${roleless}`),
    s.role_note ? el("p", { class: "field-help" }, s.role_note) : null,
    scheduleNetwork(s, project),
    fact("Repo", el("span", { class: "mono" }, s.repo)),
    editLink("#/schedules", "Manage it in Schedules →"),
  ];
}

function treeLeafDetail(leaf, ctx) {
  if (leaf.schedule) return scheduleLeafDetail(leaf.schedule);
  const key = leaf.registry;
  return [
    el("h3", { class: "mono" }, key.name),
    key.doc ? el("p", {}, key.doc) : null,
    ...(key.system === "scope_document" ? scopeLeafDetail(key, ctx) : keyLeafDetail(key, ctx)),
  ];
}

async function renderTreeTab(panel) {
  const [registry, { schedules }, { projects }, explain, roles] = await Promise.all([
    api("GET", "/api/policy/registry"),
    api("GET", "/api/schedules"),
    api("GET", "/api/projects"),
    api("GET", "/api/policy/explain?subject=baseline"),
    api("GET", "/api/roles"),
  ]);
  const scopes = [...new Set((registry.keys || []).filter(k => k.system === "scope_document").map(k => k.name.split(".")[0]))];
  const baselineState = explain.status?.state;
  const guard = ["ok", "reapprove"].includes(baselineState) ? null : BASELINE_GUARD[baselineState] || baselineState;
  const bound = projects.filter(projectRepoBinding);
  const picker = el("select", { class: "policy-tree-project", "aria-label": "project" },
    el("option", { value: "" }, "no project: the locked baseline"),
    ...bound.map(p => el("option", { value: p.project_id }, p.name)));
  picker.value = bound[0]?.project_id || "";
  const detail = el("div", { class: "policy-tree-detail" }, policyNote("Pick a setting on the left."));
  let rsop = null;
  let shown = null;
  const show = leaf => {
    shown = leaf;
    const ctx = { rsop, explain, guard, scopes, projects, roles, schedules, project: picker.value };
    detail.replaceChildren(...treeLeafDetail(leaf, ctx).filter(Boolean));
  };
  const resolve = async () => {
    const binding = projectRepoBinding(bound.find(p => p.project_id === picker.value));
    rsop = binding
      ? await api("GET", `/api/repos/${encodeURIComponent(binding.value)}/rsop`).catch(exc => ({ error: String(exc) }))
      : null;
    if (shown) show(shown);
  };
  picker.addEventListener("change", resolve);
  await resolve();
  panel.append(...[
    el("p", { class: "field-help" },
      "Every setting skep has, as a tree: what it does, its state in the picked project and "
      + "which layer decided it. Read-only; each setting links to where it changes."),
    guard ? el("p", { class: "policy-tree-guard" },
      el("span", { class: "policy-dot tone-bad", "aria-hidden": "true" }),
      explain.note, ` — every decision asks (${guard}) until a baseline is locked and verifies. `,
      el("a", { href: "#/setup" }, "Lock it in Setup →")) : null,
    policyField("project", picker, "The answer is per project; none = the locked baseline alone."),
    el("div", { class: "policy-tree-layout" }, buildTree(treeModel(registry, schedules), show), detail),
  ].filter(Boolean));  // append(null) would render the text "null"
}
