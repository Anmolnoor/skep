/* Phase 10 10.1: the Schedules page, moved out of app.js as it was. The
   project list and detail it sat beside stay in app.js (Settings → Projects).

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el } from "../dom.js";
import { flash, fmtTs, header, relativeTime, route } from "../../app.js";

// ---------- Schedules ----------

export async function viewSchedules(main) {
  header(main, "Schedules", "Recurring work the in-process ticker dispatches — no cron, no terminal.");
  // v76-F6: one fetch — the health facts render ABOVE the table as banners,
  // so a failing schedule is visible (and actionable) without scrolling.
  const [
    { schedules }, { templates },
    { health: schedHealth }, { health: provHealth }, { nodes },
  ] = await Promise.all([
    api("GET", "/api/schedules"),
    api("GET", "/api/templates"),
    api("GET", "/api/schedules/health"),
    api("GET", "/api/providers/health"),
    api("GET", "/api/nodes"),
  ]);

  // Banners fire only for actual failures (I8/v65) — a clean page shows none.
  const failing = schedHealth.filter(h => h.consecutive_failures >= 3);
  if (failing.length) {
    main.append(el("div", { class: "health-banner warn" },
      el("span", {}, `${failing.length} schedule(s) failing:`),
      failing.map(h => {
        const entry = el("span", { class: "health-banner-item" },
          el("span", { class: "mono" }, h.name),
          ` ${h.consecutive_failures} consecutive failures`);
        // The disable goes through the EXISTING toggle (no new mutation
        // path, I5); the ticker itself auto-disables at 5 — the button is
        // early action, not the only guard (I8).
        if (h.consecutive_failures >= 5 && h.enabled) {
          entry.append(el("button", {
            class: "ghost",
            onclick: async () => {
              try { await api("PATCH", `/api/schedules/${h.name}`, { enabled: false }); route(); }
              catch (e) { flash("bad", e.message); }
            },
          }, "disable"));
        }
        return entry;
      }),
      el("span", { class: "note" },
        "the ticker auto-disables a schedule after 5 consecutive failures")));
  }
  const unreachable = provHealth.filter(h => !h.reachable);
  if (unreachable.length) {
    main.append(el("div", { class: "health-banner bad" },
      el("span", {}, `${unreachable.length} provider(s) unreachable:`),
      unreachable.map(h => el("span", { class: "health-banner-item mono" }, h.provider_id))));
  }

  if (schedules.length) {
    main.append(el("table", {},
      el("thead", {}, el("tr", {},
        ["name", "caste", "project", "every", "on", "next run", "last run", "last outcome", "source", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, schedules.map(s => {
        const toggle = el("button", { class: "ghost" }, s.enabled ? "disable" : "enable");
        toggle.addEventListener("click", async () => {
          await api("PATCH", `/api/schedules/${s.name}`, { enabled: !s.enabled }); route();
        });
        const remove = el("button", { class: "danger" }, "remove");
        remove.addEventListener("click", async () => {
          await api("DELETE", `/api/schedules/${s.name}`); route();
        });
        const project = s.project_context
          ? `${s.project_context.project_id} (${s.project_context.phase})`
          : "-";
        // v76-F6: next-run is a countdown ("in 4h 12m"; a past stamp reads
        // "overdue"), the absolute time one hover away (I8). Last-run stays
        // absolute — it is a record, not a countdown.
        const nextRun = s.next_run_at && new Date(s.next_run_at).getTime() < Date.now()
          ? "overdue" : relativeTime(s.next_run_at);
        return el("tr", {},
          el("td", { class: "mono" }, s.name),
          el("td", {}, s.worker_kind),
          el("td", {}, project),
          el("td", {}, `${s.interval_seconds}s`),
          el("td", {}, s.enabled ? "yes" : "no"),
          el("td", { title: fmtTs(s.next_run_at) }, nextRun),
          el("td", {}, fmtTs(s.last_run_at)),
          el("td", {}, s.last_state || "-"),
          el("td", {}, s.template_name ? `template ${s.template_name}` : "inline"),
          el("td", { class: "actions" }, toggle, remove));
      }))));
  } else main.append(el("p", { class: "empty-state" }, "No schedules yet."));

  // v14: schedule + provider health. v15: ops nodes. (Fetched up top since
  // v76-F6 — the banners summarize; this table stays the record.)
  if (schedHealth.length || provHealth.length || nodes.length) {
    const rows = [];
    for (const n of nodes) {
      rows.push(el("tr", {},
        el("td", {}, `node: ${n.node_id}`),
        el("td", {}, n.trust_tier),
        el("td", {}, `${n.allowed_capabilities.length} ops capabilities`)));
    }
    for (const h of schedHealth) {
      const rate = h.success_rate == null ? "-" : `${Math.round(h.success_rate * 100)}%`;
      rows.push(el("tr", {},
        el("td", {}, `schedule: ${h.name}`),
        el("td", {}, h.enabled ? "enabled" : "disabled"),
        el("td", {}, `success ${rate}, consecutive fails ${h.consecutive_failures}`)));
    }
    for (const h of provHealth) {
      rows.push(el("tr", {},
        el("td", {}, `provider: ${h.provider_id}`),
        el("td", {}, h.reachable ? "reachable" : "unreachable"),
        el("td", {}, h.model_found ? "model ok" : (h.error || "model missing"))));
    }
    main.append(el("section", {}, el("h3", {}, "Health"),
      el("table", {}, el("tbody", {}, rows))));
  }

  const name = el("input", { placeholder: "nightly-audit" });
  const repo = el("input", { placeholder: "repo slug or path" });
  const every = el("input", { placeholder: "30s / 5m / 2h / 1d", value: "1d" });
  const templateSel = el("select", {}, el("option", { value: "" }, "(inline instructions)"),
    templates.map(t => el("option", { value: t.name }, t.name)));
  const params = el("input", { placeholder: "key=value, key=value" });
  const instructions = el("input", { placeholder: "instructions (inline schedules)" });
  const add = el("button", { class: "primary" }, "Add schedule");
  add.addEventListener("click", async () => {
    try {
      const body = {
        name: name.value.trim(), repo: repo.value.trim(), every: every.value.trim(),
      };
      if (templateSel.value) {
        body.template = templateSel.value;
        body.params = Object.fromEntries(params.value.split(",")
          .map(s => s.trim()).filter(Boolean).map(pair => pair.split("=").map(x => x.trim())));
      } else body.instructions = instructions.value;
      await api("POST", "/api/schedules", body);
      route();
    } catch (e) { flash("bad", e.message); }
  });
  main.append(el("div", { class: "composer stacky" },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "name"), name),
      el("div", { class: "field grow" }, el("label", {}, "repo"), repo),
      el("div", { class: "field" }, el("label", {}, "every"), every)),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "template"), templateSel),
      el("div", { class: "field grow" }, el("label", {}, "params"), params),
      el("div", { class: "field grow" }, el("label", {}, "instructions"), instructions),
      add)));
}
