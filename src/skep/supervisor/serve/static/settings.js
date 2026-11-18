/* Graphite round A: Settings is five pages, each a plain question —
   Models, Workers, Projects, Channels, Setup — and Setup is one checklist.
   graphite-nav: the sidebar's Settings surface lists the five; #/settings/<tab>
   renders the one it names, so a save's route() re-renders the same page.
   #/projects/<id> stays app.js's detail view. The forms themselves are
   app.js's, unchanged: this module composes them so each record is edited in
   exactly one place.

   app.js boots on load, so its helpers come in through the import cycle.
   Safe because this module only declares at top level: by the time any of
   its functions runs, app.js has finished evaluating. */
import { api, el, flash, fmtTs, header, renderAssistantTab, renderChannelsTab, renderProjectList, renderReposTab, renderWebhooksTab, renderWorkerModel, renderWorkersTab, route, setupMissingLabels, setupStatus } from "./app.js";

export async function viewSettings(main, tab) {
  header(main, "Settings",
    "Which model does what, the workers, your projects and repos, the channels, and first-run setup.");
  // Built here, not at top level: app.js's renderers exist only once it has run.
  const pages = {
    models: renderModelsTab,
    workers: renderWorkersTab,
    projects: renderProjectsTab,
    channels: renderChannelsAndWebhooks,
    setup: renderSetupTab,
  };
  const panel = el("div", { class: "tab-panel" });
  main.append(panel);
  await (pages[tab] || pages.models)(panel);
}

// graphite-nav: compatibility for main's app.js routes (#/setup, #/projects);
// the router's alias normalisation replaces them at merge.
export function viewSetup(main) { return viewSettings(main, "setup"); }

export function viewProjects(main) { return viewSettings(main, "projects"); }

// A block on a tab: what it controls and where it is stored, in one line each.
function block(title, where, body) {
  return el("section", { class: "settings-block" },
    el("h3", { class: "settings-block-title" }, title),
    el("p", { class: "field-help settings-block-where" }, where),
    body);
}

// -- Models: which model does what. Two records, one form each. -------------
async function renderModelsTab(panel) {
  const chat = el("div");
  const worker = el("div");
  panel.append(
    block("Chat model (the Queen)",
      "Answers every chat and plans the work. Stored in skep's settings; the key is a 0600 file, never shown again.",
      chat),
    block("Worker model",
      "The model coding workers call. Stored in profile.json, which keeps only the env var NAME of the key.",
      worker));
  await Promise.all([renderAssistantTab(chat), renderWorkerModel(worker)]);
}

// -- Projects: what skep may work on, and the repos it keeps. ---------------
async function renderProjectsTab(panel) {
  panel.append(el("p", { class: "note" },
    "A repo is a git checkout skep clones and keeps. A project is what skep may do in a repo "
    + "or folder: trusted packs, bindings, and the schedules they seed."));
  const setup = await setupStatus();
  if (!setup.workspace_project.ready) panel.append(defaultWorkspaceCard(setup));
  await renderProjectList(panel);
  panel.append(el("h3", { class: "settings-block-title" }, "Repos"));
  await renderReposTab(panel);
}

// Moved from the old Setup page: one click binds ~/workspace as a project,
// which is what the older setup flag's "workspace/project" asks for.
function defaultWorkspaceCard(setup) {
  const workspace = setup.default_workspace || {};
  const apply = el("button", { class: "primary" }, "Use default workspace");
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      await api("POST", "/api/setup/default-workspace", { apply: true });
      flash("ok", "workspace applied");
      route();
    } catch (e) { flash("bad", e.message); apply.disabled = false; }
  });
  return el("div", { class: "card" },
    el("h3", {}, "Default workspace"),
    el("p", { class: "note" }, `Default path: ${workspace.path || "~/workspace"}`),
    el("p", { class: "note" },
      "Predefined policy: trusted_local_dev, build phase, workspace execution."),
    el("div", { class: "row" }, apply));
}

// -- Channels: the messengers, then the webhooks. ---------------------------
async function renderChannelsAndWebhooks(panel) {
  await renderChannelsTab(panel);
  panel.append(el("h3", { class: "settings-block-title" }, "Webhooks"));
  await renderWebhooksTab(panel);
}

// -- Setup: the older flag in one line, then the five-step checklist. -------

// Where each thing the older flag needs is set — one place each.
const SETUP_WHERE = {
  llm: ["models", "Models"],
  default_model: ["models", "Models"],
  workspace_project: ["projects", "Projects"],
  policy: ["projects", "Projects"],
};

export async function renderSetupTab(panel) {
  const setup = await setupStatus();
  panel.append(olderFlag(setup), await onboardChecklist());
}

// `/api/setup/status` is the older record (and the gate in route()); the
// checklist below is `/api/onboard`. They are separate in the backend, so the
// older one is stated, never drawn as a card that contradicts the checklist.
function olderFlag(setup) {
  if (!setup.complete) {
    const labels = setupMissingLabels(setup);
    return el("div", { class: "setup-flag" },
      el("p", { class: "note" },
        "The older setup flag: not complete. Every page but Settings waits for it. Still missing:"),
      el("ul", { class: "setup-missing" }, setup.missing.map((key, i) => {
        const [tab, name] = SETUP_WHERE[key] || ["setup", "Setup"];
        return el("li", {}, `${labels[i]} — `,
          el("button", { type: "button", class: "ghost", onclick: () => { location.hash = `#/settings/${tab}`; } }, `Open ${name}`));
      })));
  }
  if (setup.marked_complete && setup.completed_at) {
    return el("p", { class: "note setup-flag" },
      `The older setup flag: complete since ${fmtTs(setup.completed_at)}.`);
  }
  const finish = el("button", { class: "primary" }, "Finish setup");
  finish.addEventListener("click", async () => {
    finish.disabled = true;
    try {
      const done = await api("POST", "/api/setup/complete");
      if (done.complete) { flash("ok", "setup complete"); route(); }
    } catch (e) { flash("bad", e.message); finish.disabled = false; }
  });
  return el("div", { class: "setup-flag row" },
    el("p", { class: "note" }, "The older setup flag: everything it needs is set, not yet recorded."),
    finish);
}

const STEP_LABELS = {
  model: "Model", workdir: "Work folder", sandbox: "Sandbox", template: "Policy baseline", role: "Role",
};

// first-run O3 (ADR 0056 D7): `skep onboard`'s five steps as a thin form over
// the same step functions (POST /api/onboard/<step>). The CLI resumes from the
// records these write, and the other way round. Each step is one row — its
// state and one button — and its form opens in place under the row.
export async function onboardChecklist() {
  const { steps, relock } = await api("GET", "/api/onboard");
  const summaries = {
    model: r => `${r.provider} ${r.model} — ${r.egress}`,
    workdir: r => `${r.path} (project ${r.project_id})`,
    sandbox: r => `${r.backend} — ${r.rings}`,
    template: r => `${r.template} locked, sha256 ${String(r.sha256).slice(0, 12)}`,
    role: r => r.role,
  };
  const field = (label, input) => el("div", { class: "field grow" }, el("label", {}, label), input);
  const select = (values, value) => {
    const node = el("select", {}, ...values.map(v => el("option", { value: v }, v)));
    node.value = value;
    return node;
  };
  const post = async (step, body, button) => {
    button.disabled = true;
    try {
      await api("POST", `/api/onboard/${step}`, body);
      flash("ok", `${step} recorded`);
      route();
    } catch (e) { flash("bad", e.message); button.disabled = false; }
  };

  const model = steps.model || {};
  const provider = el("input", { value: model.provider || "ollama" });
  const modelName = el("input", { value: model.model || "", placeholder: "e.g. qwen3:14b" });
  const endpoint = el("input", { value: model.endpoint || "", placeholder: "http://localhost:11434" });
  const keyEnv = el("input", { value: model.api_key_env || "", placeholder: "env var NAME, never the key" });
  const saveModel = el("button", { class: "primary" }, "Test and save");
  saveModel.addEventListener("click", () => post("model", {
    provider: provider.value.trim(),
    model: modelName.value.trim(),
    endpoint: endpoint.value.trim() || null,
    api_key_env: keyEnv.value.trim() || null,
  }, saveModel));
  // L2: an empty body tests the provider already configured and records it —
  // so a model set in Models is not typed a second time here.
  const keepModel = el("button", { class: "ghost" }, "Record the current model");
  keepModel.addEventListener("click", () => post("model", {}, keepModel));

  const workdir = el("input", { value: (steps.workdir || {}).path || "", placeholder: "~/workspace" });
  const saveWorkdir = el("button", { class: "primary" }, "Register");
  saveWorkdir.addEventListener("click", () => post("workdir", { path: workdir.value.trim() }, saveWorkdir));

  const backend = select(["auto", "podman", "none"], (steps.sandbox || {}).backend || "auto");
  const detected = el("p", { class: "note" }, "");
  const detect = el("button", { class: "ghost" }, "Detect");
  detect.addEventListener("click", async () => {
    try {
      const found = await api("GET", "/api/onboard/sandboxes");
      backend.value = found.recommended;
      detected.textContent = Object.entries(found.backends)
        .map(([name, v]) => `${name}: ${v.usable ? "usable" : "unusable"} (${v.detail})`)
        .concat(`docker: ${found.docker.detail}`).join(" · ");
    } catch (e) { flash("bad", e.message); }
  });
  const saveSandbox = el("button", { class: "primary" }, "Save");
  saveSandbox.addEventListener("click", () => post("sandbox", { backend: backend.value }, saveSandbox));

  const template = select(["assistant", "builder", "operator"], (steps.template || {}).template || relock || "assistant");
  const table = el("pre", { class: "mono worker-command-output onboard-table" }, "");
  const typed = el("input", { placeholder: "type yes to lock this as the policy baseline" });
  const lock = el("button", { class: "primary" }, "Lock baseline");
  lock.disabled = true;
  let shown = null;
  const preview = el("button", { class: "ghost" }, "Preview");
  preview.addEventListener("click", async () => {
    try {
      shown = await api("GET", `/api/onboard/template?template=${encodeURIComponent(template.value)}`);
      table.textContent = shown.locked
        ? "this is already the locked baseline"
        : [shown.table, ...(shown.tightened || []), ...(shown.diff || [])].join("\n");
      lock.disabled = false;
    } catch (e) { flash("bad", e.message); }
  });
  template.addEventListener("change", () => { shown = null; lock.disabled = true; table.textContent = ""; });
  lock.addEventListener("click", () => {
    if (!shown || typed.value.trim() !== "yes") { flash("bad", "preview, then type yes"); return; }
    post("template", { template: template.value, expect_sha256: shown.sha256, approved_by: "web" }, lock);
  });

  const role = select(["assistant", "builder", "operator"],
    (steps.role || {}).role || (steps.template || {}).template || "assistant");
  const saveRole = el("button", { class: "primary" }, "Save");
  saveRole.addEventListener("click", () => post("role", { role: role.value }, saveRole));

  const forms = {
    model: [
      el("p", { class: "field-help" },
        "Sets one provider for both the chat and the workers, with a test call. To change only "
        + "one of them, use Models; to record what Models already has, use Record the current model."),
      el("div", { class: "row" }, field("provider", provider), field("model", modelName),
        field("endpoint", endpoint), field("API key env var", keyEnv), saveModel, keepModel)],
    workdir: [el("div", { class: "row" }, field("path", workdir), saveWorkdir)],
    sandbox: [el("div", { class: "row" }, field("backend", backend), detect, saveSandbox), detected],
    template: [
      el("div", { class: "row" }, field("template", template), preview, field("approval", typed), lock),
      table],
    role: [el("div", { class: "row" }, field("role", role), saveRole)],
  };

  // O3-F1: a sandbox switch after the lock re-opens the baseline — say why.
  const row = (name) => {
    const done = Boolean(steps[name]);
    const reLock = !done && name === "template" && relock;
    const state = done ? `done: ${summaries[name](steps[name])}`
      : reLock ? `re-lock needed: the locked baseline is not what ${relock} locks under this sandbox`
        : "not done yet";
    const action = done ? "Change" : reLock ? "Re-lock" : "Set up";
    const form = el("div", { class: "setup-form hidden" }, ...forms[name]);
    const toggle = el("button", {
      type: "button", class: "ghost setup-toggle",
      "aria-expanded": "false", "aria-label": `${action} ${STEP_LABELS[name]}`,
    }, action);
    toggle.addEventListener("click", () => {
      const open = !form.classList.toggle("hidden");
      toggle.setAttribute("aria-expanded", String(open));
    });
    const mark = done ? "done" : reLock ? "relock" : "open";
    return el("div", { class: "setup-step" },
      el("div", { class: "setup-row" },
        el("span", { class: `setup-mark ${mark}`, "aria-hidden": "true" },
          done ? "✓" : reLock ? "!" : "○"),
        el("span", { class: "setup-label" }, STEP_LABELS[name]),
        el("span", { class: "note setup-state", title: state }, state),
        toggle),
      form);
  };

  const report = el("pre", { class: "mono worker-command-output onboard-table" }, "");
  const check = el("button", { class: "ghost" }, "Check everything");
  check.addEventListener("click", async () => {
    try {
      const result = await api("GET", "/api/onboard/check");
      report.textContent = result.report;
      flash(result.ready ? "ok" : "bad", result.ready ? "ready" : "not ready yet");
    } catch (e) { flash("bad", e.message); }
  });

  return el("div", { class: "setup-checklist" },
    el("h3", { class: "settings-block-title" }, "First run"),
    el("p", { class: "field-help" },
      "The same five steps as `skep onboard` in a terminal; either one picks up where the other stopped."),
    Object.keys(STEP_LABELS).map(row),
    el("div", { class: "row" }, check), report);
}
