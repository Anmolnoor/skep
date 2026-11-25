/* H1 (hive-rooms C10): the chat view's context panel — the thread's role and
   model, the cards waiting in it, the runs it dispatched, what it spent.
   Read-only: a card is still resolved on the card, a run on its page.

   app.js boots on load, so its helpers come in through the import cycle.
   Safe because this module only declares functions: by the time any of them
   runs, app.js has finished evaluating. */
import { api, el, liveRunStatus, openRunLink, relativeTime, shortTaskId, stateChip } from "./app.js";

const section = (label, ...body) =>
  el("section", { class: "context-section" }, el("div", { class: "context-label" }, label), ...body);
const none = text => el("p", { class: "note" }, text);
// v134-F7 (N5): who raised a card — "assistant" read like the chat's role.
const PROPOSERS = { assistant: "proposed by skep", operator: "your command", gate: "a worker's gate" };
const proposer = source => PROPOSERS[source] || source;
// TaskState minus TERMINAL_STATES: a gated run (pending_approval) has stopped;
// its approval resumes it as a new run, and its card is under Waiting on you.
const RUNNING = new Set(["created", "dispatched", "running"]);

// The panel's sync half draws at once from what the chat view already holds;
// runs and spend follow from two reads, and a failed read says so (I8).
export function renderContextPanel(panel, { chat, defaultModel, actions }) {
  const runs = el("div", { class: "context-runs" });
  const spend = el("div", { class: "context-spend" });
  const waiting = actions.filter(a => a.status === "proposed");
  panel.replaceChildren(
    section("Role and model",
      chat
        ? el("div", { class: "context-mini" },
          // A chat from before roles were stored runs as assistant (ADR 0059 #4).
          el("span", {}, `role ${chat.role || "assistant (none stored)"}`),
          el("small", { class: "mono" }, chat.model || defaultModel || "no model"))
        : none("A new thread — pick its role and model in the composer.")),
    section("Waiting on you",
      waiting.length
        ? waiting.map(card => el("button", {
          type: "button", class: "context-mini context-card",
          title: "show this card in the thread",
          onclick: () => document.querySelector(`.confirm-card[data-action-id="${card.action_id}"]`)
            ?.scrollIntoView({ block: "center" }),
        }, el("span", {}, card.tool), el("small", {}, `${proposer(card.source)} · ${relativeTime(card.created_at)}`)))
        : none("Nothing.")),
    section("Runs here", runs),
    section("Spend, this thread", spend));
  if (!chat) {
    runs.append(none("None yet."));
    spend.append(none("None yet."));
    return Promise.resolve();
  }
  const id = encodeURIComponent(chat.chat_id);
  return Promise.all([
    api("GET", `/api/runs?chat_id=${id}&limit=100`).then(({ runs: list }) => {
      // v134-F6 (W2, the operator's ask): the runs working now come first,
      // each with its live status (the chat view's status stream writes the
      // span) and Open ↗ to its page in a new tab; the history follows.
      const running = list.filter(run => RUNNING.has(run.state));
      const rest = list.filter(run => !RUNNING.has(run.state));
      runs.replaceChildren(
        ...(running.length ? [el("div", { class: "context-running" },
          el("small", { class: "context-sublabel" }, "Running now"),
          ...running.map(run => el("div", { class: "context-mini context-run running" },
            el("span", { class: "mono" }, shortTaskId(run.task_id)),
            liveRunStatus(run.task_id, run.state), openRunLink(run.task_id))))] : []),
        ...rest.map(run => el("a", {
          class: "context-mini context-run", href: `#/runs/${run.task_id}`,
        }, el("span", { class: "mono" }, shortTaskId(run.task_id)), stateChip(run.state),
        el("small", {}, relativeTime(run.updated_at)))),
        ...(list.length ? [] : [none("None yet.")]));
    }).catch(() => runs.replaceChildren(none("Could not load this thread's runs."))),
    api("GET", `/api/llm/usage?chat_id=${id}`).then(usage => {
      const tok = span => span.total_tokens.toLocaleString();
      spend.replaceChildren(el("div", { class: "context-mini" },
        el("span", {}, `5h ${tok(usage.chat.last_5h)} tok · 7d ${tok(usage.chat.last_7d)} tok`),
        el("small", {}, "counted locally from llm_usage")));
    }).catch(() => spend.replaceChildren(none("Could not load spend."))),
  ]);
}
