/* Phase 10 10.1: the review flow and the forensics panel.
   #/review/<review_id> walks one pending gate: what it asks → the run's diff,
   file by file → its forensics (G10 + V1 verdict, the ledger, the policy
   trail) → the verdict. The verdict is the same approve/deny the Approvals
   page and run detail give (no new verb); keys only move focus, so no single
   keypress lands a patch (I7: the operator's explicit press is the decision).

   Run detail itself still lives in app.js (other suites pin its text there);
   it embeds forensicsSection() from here.

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el, notFound } from "../dom.js";
import { renderDiff } from "../diff.js";
import { reviewQueue } from "./inbox.js";
import { flash, header, offersRemember, relativeTime, shortTaskId, stateChip } from "../../app.js";

// Where G10's command came from (I2): the pin, the weaker fallback, or no record.
const COMMANDS_SOURCE = {
  project: "project pin — the project's pinned verify_command",
  worker: "worker fallback — the worker's own verify step (no project pin)",
  unknown: "unknown — the record does not say",
};

const countsLine = c => (c
  ? `${c.total_executed} executed (${c.passed} passed, ${c.failed} failed, ${c.errors} errors)`
  : "not readable");

// The G10 box: what the supervisor re-ran, whose command it was (I2), what
// it returned, and V1's verdict on the test counts. Phase 10 R1 D3 (I8): "ran"
// and exit codes only when a command executed; nothing to re-verify is
// neutral, never "do not trust".
// Phase 10 R2 I10: whose command G10 re-ran and the test counts, as rows —
// the review page's G10 box and the run page say them the same way.
export function g10Provenance(g10) {
  const rows = [];
  if ((g10.commands || []).length) {
    rows.push(["command from", COMMANDS_SOURCE[g10.commands_source] || COMMANDS_SOURCE.unknown]);
  }
  const counts = g10.test_counts;
  if (counts?.baseline || counts?.patched) {
    rows.push(["tests", `baseline ${countsLine(counts.baseline)} → patched ${countsLine(counts.patched)}`]);
  }
  return rows;
}

// v133-F34 (F-I5, I8): a G10 that could not install the dependencies ran no
// check, and its record's lead line says so (reverify.INSTALL_FAILED); a check
// that failed on a network lookup carries reverify.OFFLINE_NOTE. Each is
// quoted from the record's detail, on its own row.
export function g10Notes(g10) {
  const detail = String(g10.detail || "");
  const lead = detail.split("\n")[0];
  const offline = detail.match(/the replay runs offline[^;\n]*/);
  return [
    g10.outcome === "unavailable" && lead.includes("could not install dependencies")
      ? ["re-verify did not run", lead] : null,
    offline ? ["offline replay", offline[0]] : null,
  ].filter(Boolean);
}

function g10Box(g10) {
  if (!g10) return el("p", { class: "note" }, "G10: no re-verification recorded for this run.");
  const tampered = String(g10.v1 || "").startsWith("tampered");
  const nothing = g10.outcome === "not_applicable";
  const commands = (g10.commands || []).join(" && ");
  const ran = (g10.exit_codes || []).length > 0;
  const rows = [["re-verify (G10)", nothing
    ? `nothing to re-verify — ${g10.detail || "the run produced no patch"}`
    : `${g10.outcome} — ${g10.confirmed ? "confirmed" : "NOT CONFIRMED, do not trust"}`]];
  if (ran) rows.push(["ran", commands || "-"], ["exit codes", g10.exit_codes.join(", ")]);
  else if (commands) rows.push(["recorded verify (not run)", commands]);
  rows.push(...g10Provenance(g10));
  if (!nothing) rows.push(...g10Notes(g10), ["V1 (test tamper)", g10.v1]);
  if (g10.detail && !nothing) rows.push(["detail", g10.detail]);
  return el("div", {},
    tampered ? el("p", { class: "banner bad", role: "alert" },
      `V1 TEST TAMPER — ${g10.v1}. The patch changed what the tests count; read the diff before landing.`) : null,
    el("dl", { class: "kv" }, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", { class: "mono" }, String(v))])));
}

// D3: the verifier's own status word, never "valid" by default; a checkpoint
// that was never kept degrades coverage and says so.
function ledgerLine(ledger) {
  if (!ledger) return null;
  if (ledger.break) {
    return el("p", { class: "banner bad", role: "alert" },
      `Ledger BROKEN at ${ledger.break.file || "-"}:${ledger.break.line || "-"} — ${ledger.break.reason}`);
  }
  const status = ledger.status || "status not reported";
  const head = ledger.head_hash
    ? `chain head seq ${ledger.head_seq}, sha256 ${String(ledger.head_hash).slice(0, 16)}…`
    : "no chained rows yet";
  const unchained = ledger.unchained_rows
    ? ` · ${ledger.unchained_rows} older rows are not covered by the chain` : "";
  if (ledger.checkpoints_missing) {
    return el("p", { class: "banner warn", role: "status" },
      `Ledger ${status}, coverage degraded: ${ledger.checkpoints_missing} due checkpoints were never kept · ${head}${unchained}`);
  }
  return el("p", { class: "note mono" }, `Ledger ${status}: ${head}${unchained}`);
}

// Phase 10 R1 D8: the receipt itself (GET /api/runs/<id>/receipt, the bytes
// `skep proof export` prints), copied — and "copied" only once the clipboard
// took it. A refused or absent clipboard shows it to select instead, as one
// text node.
async function copyReceipt(taskId, slot) {
  let text;
  try { text = await api("GET", `/api/runs/${taskId}/receipt?format=md`); }
  catch (e) { flash("bad", `receipt unavailable: ${e.message}`); return; }
  try {
    const clipboard = globalThis.navigator?.clipboard;
    if (!clipboard?.writeText) throw new Error("no clipboard on this page");
    await clipboard.writeText(text);
    flash("ok", "receipt copied");
  } catch {
    slot.replaceChildren(el("pre", { class: "log receipt-text", tabindex: "0" }, text));
    flash("bad", "the clipboard refused — the receipt is shown below to select and copy");
  }
}

// The whole report, as read-only panels. report = GET /api/runs/<id>/forensics.
export function forensicsPanel(report) {
  const { identity = {}, outcome = {} } = report;
  const taint = identity.taint && identity.taint !== "unknown"; // forensics' "none recorded"
  const trail = report.policy_trail || [];
  const log = report.worker?.log_tail || [];
  const receiptSlot = el("div", {});
  return el("div", { class: "forensics" },
    taint ? el("p", { class: "banner warn", role: "alert" },
      `TAINTED — this run's chat carried tainted content (${identity.taint}).`) : null,
    (outcome.alarms || []).map(alarm => el("p", { class: "banner bad", role: "alert" }, `ALARM ${alarm}`)),
    el("h3", {}, "G10 & V1"),
    g10Box(outcome.g10),
    el("h3", {}, "Ledger"),
    ledgerLine(report.ledger),
    el("h3", {}, "Identity"),
    el("dl", { class: "kv" }, Object.entries(identity).flatMap(([k, v]) =>
      [el("dt", {}, k.replace(/_/g, " ")), el("dd", { class: "mono" }, String(v))])),
    el("details", {},
      el("summary", {}, `Policy trail (blame) · ${trail.length} decisions · chain ${(report.resume_chain || []).map(shortTaskId).join(" → ")}`),
      trail.length
        ? el("pre", { class: "log" }, trail.map(row => el("div", { class: "event" },
          `${row.at || "?"}  ${row.actor || "?"}  `,
          el("span", { class: "etype" }, row.verdict || row.source || "?"),
          `  ${row.scope || "?"}.${row.action || "?"} ${row.value ?? ""}  decided_by=${row.decided_by || "?"}`)))
        : el("p", { class: "note" }, "No policy decisions recorded.")),
    el("details", {},
      el("summary", {}, `Worker log tail · ${log.length} lines`),
      log.length ? el("pre", { class: "log" }, log.join("\n")) : el("p", { class: "note" }, "No worker log.")),
    el("p", { class: "actions" },
      el("button", { type: "button", class: "ghost", onclick: () => copyReceipt(report.task_id, receiptSlot) },
        "Copy receipt"),
      el("span", { class: "note mono" }, ` or: skep proof export ${report.task_id}`)),
    receiptSlot);
}

// Phase 10 R1 D7: route() stamps the view container on every navigation. A
// view takes the stamp it started under; after each await it asks live() and
// stops if the operator has moved on — a late fetch never paints into, or
// takes focus from, the page they moved to.
export function routeLive(main) {
  const at = main.dataset.route;
  return () => main.dataset.route === at;
}

// Run detail's panel: fetched the first time it is opened — verifying the
// ledger reads the whole decision log, so it is not paid on every page view.
export function forensicsSection(taskId) {
  const body = el("div", {}, el("p", { class: "note" }, "Loading…"));
  let loaded = false;
  return el("details", {
    class: "card forensics-section",
    ontoggle: async (event) => {
      if (!event.target.open || loaded) return;
      loaded = true;
      try { body.replaceChildren(forensicsPanel(await api("GET", `/api/runs/${taskId}/forensics`))); }
      catch (e) { body.replaceChildren(el("p", { class: "error" }, `forensics unavailable: ${e.message}`)); }
    },
  }, el("summary", {}, "Forensics & blame"), body);
}

// The keys the review page answers. They move focus; Enter/Space presses.
const REVIEW_KEYS = "j / k next / previous file · f forensics · a approve · d deny · i back to the Inbox";

export async function viewReview(main, reviewId) {
  const live = routeLive(main);
  const { approvals } = await api("GET", "/api/approvals");
  if (!live()) return;
  const queue = reviewQueue(approvals);
  const at = queue.findIndex(gate => gate.review_id === reviewId);
  if (at < 0) {
    header(main, "Not found");
    main.append(notFound(`No pending approval ${reviewId} — it may already have been resolved.`));
    return;
  }
  const gate = queue[at];
  const next = queue[at + 1] || queue[at - 1] || null;
  const taskId = gate.task_id;
  header(main, `Review · run ${shortTaskId(taskId)}`, gate.reason || gate.action);
  // A missing run is the page's 404; forensics or a diff that fails is said in place.
  const [detail, report] = await Promise.all([
    api("GET", `/api/runs/${taskId}`),
    api("GET", `/api/runs/${taskId}/forensics`).catch(e => ({ error: e.message })),
  ]);
  if (!live()) return;
  let diff = null;
  let diffError = null;
  if ((detail.artifacts || []).some(a => a.kind === "patch")) {
    try { diff = renderDiff(await api("GET", `/api/runs/${taskId}/diff`)); }
    catch (e) { diffError = `the patch could not be read: ${e.message}`; }
    if (!live()) return;
  }

  const root = el("div", { class: "review", tabindex: "-1", "aria-label": "review this approval" });
  // R3 J8: the stored argv this review grants, shlex-joined by the server;
  // the run event's list only for a review that stored none.
  const commands = Array.isArray(gate.command_lines) ? gate.command_lines
    : Array.isArray(gate.commands) ? gate.commands : [];
  root.append(el("section", { class: "card review-summary", "aria-labelledby": "review-ask" },
    el("h2", { id: "review-ask" }, `${gate.action}${gate.boundary ? ` · ${gate.boundary}` : ""}`),
    el("p", {}, gate.reason || "-"),
    el("p", { class: "note" }, stateChip(detail.run.state), ` ${(detail.run.instructions || "").slice(0, 200)}`),
    el("p", { class: "note" }, `waiting ${relativeTime(gate.requested_at).replace(" ago", "")} · `,
      el("a", { href: `#/runs/${taskId}` }, `run ${taskId}`), ` · ${at + 1} of ${queue.length} in the queue`),
    gate.reverification_warning ? el("p", { class: "banner bad", role: "alert" }, `⚠ ${gate.reverification_warning}`) : null,
    gate.stale_base ? el("p", { class: "banner warn" }, "⚠ the base branch moved since this run started — landing re-checks it") : null,
    gate.remediation ? el("p", { class: "note mono" }, gate.remediation) : null,
    commands.length ? el("ul", { class: "command-list mono" }, commands.map(cmd =>
      el("li", {}, el("code", {}, Array.isArray(cmd) ? cmd.join(" ") : String(cmd))))) : null));

  const diffSection = el("section", { class: "review-step", "aria-labelledby": "review-diff" },
    el("h2", { id: "review-diff" }, "1 · Diff"),
    diff ? diff.node
      : el("p", { class: diffError ? "error" : "note" }, diffError || `No patch — this gate asks for ${gate.action}, not a landing.`));
  const forensics = el("section", { class: "review-step", tabindex: "-1", "aria-labelledby": "review-forensics" },
    el("h2", { id: "review-forensics" }, "2 · Forensics"),
    report.error ? el("p", { class: "error" }, `forensics unavailable: ${report.error}`) : forensicsPanel(report));
  const outcome = el("div", { role: "status", "aria-live": "polite" });
  const verdict = (label, cls, path, body, done) => el("button", {
    type: "button", class: cls,
    onclick: async () => {
      const buttons = bar.querySelectorAll("button");
      for (const b of buttons) b.disabled = true;
      try {
        const result = await api("POST", `/api/approvals/${reviewId}/${path}`, body);
        if (!live()) return; // the verdict stands; the page the operator left does not redraw
        if (result.resumed_as) { location.hash = `#/runs/${result.resumed_as}`; return; }
        outcome.replaceChildren(...[el("p", { class: "flash ok" }, done(result)),
          // D8: a landing G10 could not vouch for says so where it landed.
          result.warning ? el("p", { class: "banner warn", role: "alert" }, `⚠ ${result.warning}`) : null,
          el("a", { class: "home-link", href: next ? `#/review/${next.review_id}` : "#/inbox" },
            next ? "Next in the queue →" : "Back to the Inbox →")].filter(Boolean));
        outcome.querySelector("a").focus();
      } catch (e) {
        flash("bad", e.message);
        for (const b of buttons) b.disabled = false;
      }
    },
  }, label);
  const approveLabel = commands.length > 1 ? `Approve all ${commands.length} commands` : "Approve";
  const bar = el("section", { class: "review-verdict actions", "aria-labelledby": "review-verdict" },
    el("h2", { id: "review-verdict" }, "3 · Verdict"),
    verdict(approveLabel, "primary", "approve", {}, r => (r.branch ? `Approved — patch applied on ${r.branch}.` : "Approved.")),
    verdict("Deny", "danger", "deny", { note: "denied from UI review" }, () => "Denied."),
    /^(shell\.run|network\.(fetch|read))$/.test(gate.action) && offersRemember(gate)
      ? el("a", { class: "home-link", href: "#/approvals" }, "Allow & remember: on the Approvals page") : null);
  root.append(diffSection, forensics, bar, outcome, el("p", { class: "note keys" }, REVIEW_KEYS));

  const files = diff ? diff.sections : [];
  let file = -1;
  root.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    const focus = node => { event.preventDefault(); node?.focus(); };
    if (event.key === "j" && files.length) { file = Math.min(file + 1, files.length - 1); focus(files[file]); }
    else if (event.key === "k" && files.length) { file = Math.max(file - 1, 0); focus(files[file]); }
    else if (event.key === "f") focus(forensics);
    else if (event.key === "a") focus(bar.querySelector(".primary"));
    else if (event.key === "d") focus(bar.querySelector(".danger"));
    else if (event.key === "i") { event.preventDefault(); location.hash = "#/inbox"; }
  });
  main.append(root);
  root.focus();
}
