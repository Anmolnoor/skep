/* Phase 10 10.1: the Library pages — Notes & Tasks, curated Memory, and
   Templates & Skills — moved out of app.js as they were.

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el, iconButton } from "../dom.js";
import { buildTabBar, casteSelect, confirmTemplateSuggestion, flash, fmtTs, header, previewTemplateSuggestion, relativeTime, renderMarkdown, renderSuggestionGrant, repoOptions, route, stateChip } from "../../app.js";

// ---------- Notes & Tasks (v76-F7: markdown notes + due-grouped tasks) ----------

// v76-F7: the EXPLICIT run:<task-id> token links to the run page.
// Deliberately not bare-hex autolinking — prose full of hex-looking words
// would false-positive, and a link to a non-run is a lie (I8).
function linkifyRunTokens(text) {
  const nodes = [];
  let last = 0;
  for (const match of String(text || "").matchAll(/run:([0-9a-f]{6,})/gi)) {
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    nodes.push(el("a", { class: "mono run-token", href: `#/runs/${match[1]}` }, match[0]));
    last = match.index + match[0].length;
  }
  if (last < String(text || "").length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

function linkifyRunTokensInPlace(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) {
    if (!/run:[0-9a-f]{6,}/i.test(node.textContent)) continue;
    node.replaceWith(el("span", {}, linkifyRunTokens(node.textContent)));
  }
}

function noteTags(content) {
  return [...new Set([...(content || "").matchAll(/\B#([\w-]+)/g)].map(m => m[1]))];
}

export async function viewNotesTasks(main) {
  header(main, "Notes & Tasks", "Capture local context and keep lightweight todos.");
  const [{ notes }, { tasks }] = await Promise.all([
    api("GET", "/api/notes"),
    api("GET", "/api/tasks"),
  ]);

  const noteText = el("textarea", { placeholder: "New note" });
  const addNote = el("button", { class: "primary" }, "Add note");
  addNote.addEventListener("click", async () => {
    if (!noteText.value.trim()) return;
    addNote.disabled = true;
    try { await api("POST", "/api/notes", { content: noteText.value.trim() }); route(); }
    catch (e) { flash("bad", e.message); addNote.disabled = false; }
  });

  // v76-F7: notes read as markdown (renderMarkdown — rung-2 reuse); edit
  // swaps to the raw textarea with the same PATCH/DELETE verbs.
  const notesList = el("div", { class: "stack" });
  const noteFilter = el("input", {
    type: "search", placeholder: "Filter notes by text or #tag",
  });
  noteFilter.addEventListener("input", () => {
    const q = noteFilter.value.trim().toLowerCase();
    for (const item of notesList.children) {
      item.classList.toggle("search-hidden",
        Boolean(q) && !item.textContent.toLowerCase().includes(q));
    }
  });
  for (const note of notes) {
    const body = el("div", { class: "note-md" }, renderMarkdown(note.content));
    linkifyRunTokensInPlace(body);
    const tags = noteTags(note.content);
    const content = el("textarea", { class: "hidden" }, note.content);
    const save = el("button", { class: "ghost hidden" }, "Save");
    const remove = el("button", { class: "danger hidden" }, "Delete");
    const edit = el("button", { class: "ghost" }, "Edit");
    edit.addEventListener("click", () => {
      body.classList.add("hidden");
      edit.classList.add("hidden");
      for (const node of [content, save, remove]) node.classList.remove("hidden");
    });
    save.addEventListener("click", async () => {
      try { await api("PATCH", `/api/notes/${note.note_id}`, { content: content.value.trim() }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    remove.addEventListener("click", async () => {
      try { await api("DELETE", `/api/notes/${note.note_id}`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    // v126-F10: the existing propose verb, one click from the note; the
    // server picks the class and the proposal waits on the Memory page.
    const propose = el("button", { class: "ghost" }, "Propose to memory");
    propose.addEventListener("click", async () => {
      propose.disabled = true;
      try {
        const p = await api("POST", `/api/notes/${note.note_id}/propose`, {});
        flash("ok", `proposed as ${p.memory_class} — approve it on the Memory page`);
      } catch (e) { flash("bad", e.message); propose.disabled = false; }
    });
    notesList.append(el("div", { class: "item-card searchable" },
      body,
      content,
      tags.length
        ? el("div", { class: "note-tags" },
          tags.map(tag => el("span", { class: "note-tag" }, `#${tag}`)))
        : null,
      el("div", { class: "row" },
        el("span", { class: "note grow" }, fmtTs(note.updated_at)),
        propose, edit, save, remove)));
  }
  if (!notes.length) notesList.append(el("p", { class: "empty-state" }, "No notes yet."));

  const taskTitle = el("input", { placeholder: "New task" });
  const taskDue = el("input", { type: "datetime-local" });
  const addTask = el("button", { class: "primary" }, "Add task");
  addTask.addEventListener("click", async () => {
    if (!taskTitle.value.trim()) return;
    addTask.disabled = true;
    const due = taskDue.value ? `${taskDue.value}:00Z` : null;
    try { await api("POST", "/api/tasks", { title: taskTitle.value.trim(), due_at: due }); route(); }
    catch (e) { flash("bad", e.message); addTask.disabled = false; }
  });

  // v76-F7: tasks group by urgency; the checkbox is the daily verb (the
  // same PATCH the old Complete button used), the full editor stays one
  // toggle away — the raw record remains editable (I8).
  const dueTone = (task) => {
    if (!task.due_at) return null;
    const ms = new Date(task.due_at).getTime() - Date.now();
    if (ms < 0) return "overdue";
    if (ms < 24 * 60 * 60 * 1000) return "today";
    return "upcoming";
  };
  // v101-F8: the tone lives beside the vocabulary that produces it, not spelled
  // out longhand per variant in the stylesheet.
  const DUE_TONE = { overdue: "tone-bad", today: "tone-warn", upcoming: "tone-muted" };
  const TASK_GROUPS = [
    { label: "Overdue", test: t => t.status !== "done" && dueTone(t) === "overdue" },
    { label: "Due today", test: t => t.status !== "done" && dueTone(t) === "today" },
    { label: "Upcoming", test: t => t.status !== "done" && dueTone(t) === "upcoming" },
    { label: "No due date", test: t => t.status !== "done" && !t.due_at },
    { label: "Done", test: t => t.status === "done", collapsed: true },
  ];
  const taskRow = (task) => {
    const checkbox = el("input", { type: "checkbox", "aria-label": "toggle done" });
    checkbox.checked = task.status === "done";
    checkbox.addEventListener("change", async () => {
      try {
        await api("PATCH", `/api/tasks/${task.task_id}`,
          { status: checkbox.checked ? "done" : "todo" });
        route();
      } catch (e) { flash("bad", e.message); }
    });
    const tone = dueTone(task);
    const duePill = task.due_at
      ? el("span", { class: `chip ${DUE_TONE[tone] || "tone-muted"}`, title: fmtTs(task.due_at) },
        tone === "overdue" ? "overdue" : relativeTime(task.due_at))
      : null;
    const title = el("input", { value: task.title });
    const due = el("input", { value: task.due_at || "", placeholder: "YYYY-MM-DDTHH:MM:SSZ" });
    const status = el("select", {},
      el("option", { value: "todo" }, "todo"),
      el("option", { value: "done" }, "done"));
    status.value = task.status;
    const save = el("button", { class: "ghost" }, "Save");
    const remove = el("button", { class: "danger" }, "Delete");
    save.addEventListener("click", async () => {
      try {
        await api("PATCH", `/api/tasks/${task.task_id}`, {
          title: title.value.trim(),
          status: status.value,
          due_at: due.value.trim() || null,
        });
        route();
      } catch (e) { flash("bad", e.message); }
    });
    remove.addEventListener("click", async () => {
      try { await api("DELETE", `/api/tasks/${task.task_id}`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    const editor = el("div", { class: "task-editor hidden" },
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "task"), title),
        el("div", { class: "field" }, el("label", {}, "status"), status)),
      el("div", { class: "row" },
        el("div", { class: "field grow" }, el("label", {}, "due"), due),
        save, remove));
    return el("div", { class: "item-card searchable" },
      el("div", { class: "task-row" },
        checkbox,
        el("span", { class: `task-title${task.status === "done" ? " done" : ""}` },
          linkifyRunTokens(task.title)),
        duePill,
        iconButton("edit task", "✎", {
          class: "ghost icon-button",
          onclick: () => editor.classList.toggle("hidden"),
        })),
      editor);
  };
  const tasksList = el("div", { class: "stack" });
  for (const group of TASK_GROUPS) {
    const items = tasks.filter(group.test);
    if (!items.length) continue;
    tasksList.append(group.collapsed
      ? el("details", { class: "task-group" },
        el("summary", {}, `${group.label} (${items.length})`),
        items.map(taskRow))
      : el("section", { class: "task-group" },
        el("h4", {}, `${group.label} (${items.length})`),
        items.map(taskRow)));
  }
  if (!tasks.length) tasksList.append(el("p", { class: "empty-state" }, "No tasks yet."));

  main.append(el("div", { class: "nt-grid" },
    el("section", {}, el("h3", {}, "Notes"),
      el("p", { class: "note" },
        "Markdown renders; #tags become pills; run:<task-id> links the run."),
      el("div", { class: "composer inline" }, noteText, addNote),
      noteFilter,
      notesList),
    el("section", {}, el("h3", {}, "Tasks"),
      el("div", { class: "composer stacky" },
        el("div", { class: "row" },
          el("div", { class: "field grow" }, el("label", {}, "task"), taskTitle),
          el("div", { class: "field" }, el("label", {}, "due"), taskDue),
          addTask)),
      tasksList)));
}

// ---------- Curated memory (v13; v76-F8 class chips + counts) ----------

// v76-F8: the 7 REAL memory classes (MEMORY_CLASSES, memory.py) — a class
// added server-side must be added here consciously (a lockstep test compares
// this map to the store's set); unknown falls back muted. The build spec's
// user/environment/convention list had no data source and is not used (I8).
const MEMORY_CLASS_COLORS = {
  durable_preference: "var(--accent)",
  project_fact: "var(--info)",
  todo: "var(--warn)",
  not_to_do: "var(--bad)",
  reminder: "var(--accent-2)",
  policy_hint: "var(--ok)",
  observation: "var(--muted)",
};

function memoryClassChip(cls) {
  const color = MEMORY_CLASS_COLORS[cls] || "var(--muted)";
  return el("span", {
    // v101-F8: the shared .chip primitive; the tone is this chip's own colour
    // rather than one of the named tones, so it still sets --chip-color itself.
    class: "chip upper",
    style: `--chip-color: ${color}`,
  }, cls);
}

export async function viewMemory(main) {
  header(main, "Memory", "Curated durable memory — a proposal becomes durable only when approved; `skep memory add` is the operator's direct write.");
  const [{ proposals }, { items }] = await Promise.all([
    api("GET", "/api/memory/proposals?state=pending_review"),
    api("GET", "/api/memory"),
  ]);

  const propList = el("div", { class: "stack" });
  for (const p of proposals) {
    const approve = el("button", { class: "primary" }, "Approve");
    const reject = el("button", { class: "danger" }, "Reject");
    const clarify = el("button", { class: "ghost" }, "Needs info");
    approve.addEventListener("click", async () => {
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/approve`); route(); }
      catch (e) { flash("bad", e.message); }
    });
    reject.addEventListener("click", async () => {
      const reason = window.prompt("Why reject this proposal?");
      if (!reason) return;
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/reject`, { reason }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    clarify.addEventListener("click", async () => {
      const reason = window.prompt("What needs clarification?");
      if (!reason) return;
      try { await api("POST", `/api/memory/proposals/${p.proposal_id}/clarify`, { reason }); route(); }
      catch (e) { flash("bad", e.message); }
    });
    const sources = (p.sources || []).map(s => `${s.kind}:${s.source_id}`).join(", ");
    propList.append(el("div", { class: "item-card" },
      el("div", { class: "row" },
        memoryClassChip(p.memory_class),
        el("span", { class: "note grow" }, sources ? `from ${sources}` : "manual"),
        // v76-F8: an old proposal means the operator is the bottleneck.
        el("span", { class: "note", title: fmtTs(p.created_at) },
          `proposed ${relativeTime(p.created_at)}`)),
      el("p", {}, p.content),
      // v126-F6 (review): the approver sees what the filing chat had read.
      Array.isArray(p.taint) && p.taint.length
        ? el("p", { class: "note" }, `filed from a chat that read: ${p.taint.slice().sort()
          .map(bit => ({ private: "private data", untrusted: "outside content" })[bit] || bit).join(", ")}`)
        : null,
      el("div", { class: "row" }, approve, clarify, reject)));
  }
  if (!proposals.length) propList.append(el("p", { class: "empty-state" }, "No proposals to review."));

  const memList = el("div", { class: "stack" });
  function renderItems(list) {
    memList.replaceChildren();
    for (const item of list) {
      const forget = el("button", { class: "danger" }, "Forget");
      forget.addEventListener("click", async () => {
        try { await api("DELETE", `/api/memory/${item.memory_id}`); route(); }
        catch (e) { flash("bad", e.message); }
      });
      memList.append(el("div", { class: "item-card" },
        el("div", { class: "row" },
          memoryClassChip(item.memory_class),
          el("span", { class: "note grow" }, item.project_id || "global",
            item.provenance ? ` · written by ${item.provenance}` : ""),
          forget),
        el("p", {}, item.content)));
    }
    if (!list.length) memList.append(el("p", { class: "empty-state" }, "No durable memory yet."));
  }
  renderItems(items);

  const search = el("input", { placeholder: "Search durable memory" });
  search.addEventListener("input", async () => {
    const q = search.value.trim();
    if (!q) { renderItems(items); return; }
    try {
      const res = await api("GET", `/api/memory/search?q=${encodeURIComponent(q)}`);
      renderItems(res.items);
    } catch (e) { flash("bad", e.message); }
  });

  // v76-F8: how much the Queen knows — count + summed content size.
  const memKb = (items.reduce((n, item) => n + (item.content || "").length, 0) / 1024)
    .toFixed(1);
  main.append(el("div", { class: "nt-grid" },
    el("section", {}, el("h3", {}, "Proposals"), propList),
    el("section", {},
      el("h3", {}, "Durable memory"),
      el("p", { class: "note" }, `${items.length} items · ${memKb} KB`),
      el("div", { class: "composer inline" }, search),
      memList)));
}

// ---------- Templates & Skills (v75-F7: two tabs — authored vs learned) ----------

let activeTemplatesTab = "templates";

export async function viewTemplates(main) {
  header(main, "Templates & Skills",
    "Saved recipes — hand-authored, or learned from confirmed runs and human-approved.");
  const tabs = buildTabBar([
    { key: "templates", label: "Templates", render: renderTemplatesTab },
    { key: "skills", label: "Skills", render: renderSkillsTab },
  ], { initial: activeTemplatesTab, onActivate: (key) => { activeTemplatesTab = key; } });
  main.append(tabs.bar, tabs.content);
}

async function renderTemplatesTab(panel) {
  const [{ templates }, repos, roster] = await Promise.all([
    api("GET", "/api/templates"),
    repoOptions(),
    api("GET", "/api/workers"),
  ]);

  if (templates.length) {
    panel.append(el("div", { class: "template-cards" }, templates.map(t => {
      const remove = el("button", { class: "danger ghost" }, "remove");
      remove.addEventListener("click", async () => {
        try { await api("DELETE", `/api/templates/${t.name}`); route(); }
        catch (e) { flash("bad", e.message); }
      });
      return el("div", { class: "template-card searchable" },
        el("div", { class: "template-card-header" },
          el("span", { class: "mono" }, t.name),
          stateChip(t.worker_kind),
          stateChip(t.provenance)),
        el("div", { class: "template-card-params note" },
          (t.params || []).length
            ? `params: ${(t.params || []).map(p => p.name).join(", ")}`
            : "no params"),
        el("div", { class: "actions" },
          // v75-F7 (C9): Use navigates to Assign; the ?template= prefill
          // parsing ships in v76-F4 — until then this is a plain navigation.
          el("a", {
            class: "template-card-use",
            href: `#/assign?template=${encodeURIComponent(t.name)}`,
            title: "opens Assign",
          }, "Use →"),
          remove));
    })));
  } else panel.append(el("p", { class: "empty-state" }, "No templates yet."));

  const name = el("input", { placeholder: "name" });
  const caste = casteSelect(roster.castes);
  const params = el("input", { placeholder: "target, level=high" });
  const instructions = el("textarea", { placeholder: "Audit {{target}} dependencies…" });
  const add = el("button", { class: "primary" }, "Save template");
  add.addEventListener("click", async () => {
    try {
      await api("POST", "/api/templates", {
        name: name.value.trim(), instructions: instructions.value, worker_kind: caste.value,
        params: params.value.split(",").map(s => s.trim()).filter(Boolean).map(spec => {
          const [n, d] = spec.split("=");
          return d === undefined ? { name: n } : { name: n, default: d };
        }),
      });
      route();
    } catch (e) { flash("bad", e.message); }
  });
  panel.append(el("h3", {}, "new template"),
    el("div", { class: "composer stacky" },
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "name"), name),
        el("div", { class: "field" }, el("label", {}, "caste"), caste),
        el("div", { class: "field grow" }, el("label", {}, "params (name or name=default)"), params)),
      el("div", { class: "field" }, el("label", {}, "instructions ({{param}} placeholders)"), instructions),
      add));

  const suggestionName = el("input", { placeholder: "web-feature" });
  const suggestionRepo = el("input", { list: "suggestion-repo-list", placeholder: "repo slug or host path" });
  const suggestionInstructions = el("textarea", { placeholder: "Task that should reuse remembered approvals" });
  const suggestionCaste = casteSelect(roster.castes);
  const suggestionPreview = el("div", { class: "suggestion-preview" });
  const preview = el("button", { class: "ghost" }, "Preview suggestion");
  const confirm = el("button", { class: "primary", disabled: true }, "Confirm suggestion");
  let currentSuggestion = null;
  const resetSuggestion = () => {
    currentSuggestion = null;
    confirm.disabled = true;
    suggestionPreview.replaceChildren();
  };
  for (const input of [suggestionName, suggestionRepo, suggestionInstructions, suggestionCaste]) {
    input.addEventListener("input", resetSuggestion);
    input.addEventListener("change", resetSuggestion);
  }
  const suggestionInput = () => {
    const payload = {
      name: suggestionName.value.trim(),
      repo: suggestionRepo.value.trim(),
      instructions: suggestionInstructions.value.trim(),
      caste: suggestionCaste.value,
    };
    if (!payload.name) throw new Error("suggestion name is required");
    if (!payload.repo) throw new Error("repo is required");
    if (!payload.instructions) throw new Error("instructions are required");
    return payload;
  };
  preview.addEventListener("click", async () => {
    preview.disabled = true;
    resetSuggestion();
    try {
      const payload = suggestionInput();
      const { suggestions } = await previewTemplateSuggestion(payload);
      if (!suggestions.length) {
        suggestionPreview.append(el("p", { class: "empty-state" }, "No matching remembered approvals."));
        return;
      }
      currentSuggestion = suggestions[0];
      confirm.disabled = false;
      suggestionPreview.append(el("div", { class: "suggestion-result" },
        el("p", {}, el("span", { class: "mono" }, currentSuggestion.template.name),
          ` ${currentSuggestion.template.worker_kind}`),
        el("p", { class: "note" }, currentSuggestion.template.instructions),
        renderSuggestionGrant(currentSuggestion)));
    } catch (e) { flash("bad", e.message); } finally { preview.disabled = false; }
  });
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      const payload = suggestionInput();
      await confirmTemplateSuggestion(payload.name, {
        repo: payload.repo,
        instructions: payload.instructions,
        caste: payload.caste,
      });
      flash("ok", `saved template ${payload.name}`);
      route();
    } catch (e) { flash("bad", e.message); confirm.disabled = currentSuggestion === null; }
  });
  panel.append(el("h3", {}, "suggest from approvals"),
    el("div", { class: "composer stacky" },
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "name"), suggestionName),
        el("div", { class: "field grow" }, el("label", {}, "repo"), suggestionRepo,
          el("datalist", { id: "suggestion-repo-list" }, repos.map(r => el("option", { value: r })))),
        el("div", { class: "field" }, el("label", {}, "caste"), suggestionCaste)),
      el("div", { class: "field" }, el("label", {}, "instructions"), suggestionInstructions),
      el("div", { class: "actions" }, preview, confirm),
      suggestionPreview));
}

// v75-F7: the lifecycle stepper renders the states that EXIST in skills.py
// (draft → tested → approved; rejected is the terminal failure branch) — the
// spec's six-step pipeline invented states with no data source (I8).
const SKILL_STEPS = ["draft", "tested", "approved"];

function buildSkillStepper(status) {
  const currentIdx = SKILL_STEPS.indexOf(status);
  return el("div", { class: "skill-stepper" },
    SKILL_STEPS.map((step, i) => el("div", {
      class: `skill-step${i <= currentIdx ? " reached" : ""}${i === currentIdx ? " current" : ""}`,
    },
      el("div", { class: "skill-step-dot", "aria-hidden": "true" }),
      el("div", { class: "skill-step-label" }, step))),
    status === "rejected" ? el("span", { class: "state rejected" }, "rejected") : null);
}

async function renderSkillsTab(panel) {
  const { skills } = await api("GET", "/api/skills");
  const propose = el("button", { class: "ghost" }, "Propose from confirmed runs");
  propose.addEventListener("click", async () => {
    try {
      const { proposed } = await api("POST", "/api/skills/propose");
      flash("ok", `${proposed.length} new draft(s)`); route();
    } catch (e) { flash("bad", e.message); }
  });
  panel.append(el("div", { class: "actions" }, propose));
  if (!skills.length) {
    panel.append(el("p", { class: "empty-state" },
      "No candidates — propose after a few confirmed runs."));
    return;
  }
  for (const skill of skills) {
    const actions = el("div", { class: "actions" });
    const decide = (label, cls, path, body) => {
      const button = el("button", { class: cls }, label);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await api("POST", `/api/skills/${skill.name}/${path}`, body); route(); }
        catch (e) { flash("bad", e.message); button.disabled = false; }
      });
      return button;
    };
    if (skill.status === "draft") {
      const repo = el("input", { placeholder: "test repo (slug or path)", style: "width:160px" });
      const go = el("button", { class: "ghost" }, "Test");
      go.addEventListener("click", async () => {
        go.disabled = true;
        try {
          const result = await api("POST", `/api/skills/${skill.name}/test`,
            { repo: repo.value.trim(), params: {} });
          flash(result.passed ? "ok" : "bad",
            result.passed ? "passed the G10 test gate" : "failed the test gate");
          route();
        } catch (e) { flash("bad", e.message); go.disabled = false; }
      });
      actions.append(repo, go);
    }
    if (skill.status === "tested") actions.append(decide("Approve", "primary", "approve", { actor: "operator" }));
    if (skill.status === "draft" || skill.status === "tested") {
      actions.append(decide("Reject", "danger", "reject", { actor: "operator" }));
    }
    panel.append(el("div", { class: "item-card searchable" },
      el("div", { class: "row" },
        el("span", { class: "mono" }, skill.name),
        skill.registry_name ? el("span", { class: "note" }, `→ ${skill.registry_name}`) : null),
      buildSkillStepper(skill.status),
      el("div", { class: "note" }, skill.template.instructions.slice(0, 90)),
      el("div", { class: "note" }, `${skill.occurrences} runs; test: ${skill.test_outcome || "-"}`),
      actions));
  }
}
