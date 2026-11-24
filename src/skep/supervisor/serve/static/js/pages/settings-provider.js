/* Phase 10 R1 (E4): the chat model's connection, staged → probed → saved.
   Test sends the staged values to POST /api/llm/probe — the same 1-token
   probe /api/llm/test runs, which saves nothing — and shows its verdict and
   the context window the save would get. Save is a separate button, live
   only for the exact values that just passed; it is the existing PUT
   /api/llm/config. So a failed test can never leave a new connection active
   (I8). The Provider Shelf lists every configured profile with its last
   health check; switching profiles stays the carded use_provider verb.

   app.js boots on load, so its helpers come in through the import cycle —
   safe because this module only declares at top level. */
import { api } from "../api.js";
import { el } from "../dom.js";
import { flash, relativeTime, route } from "../../app.js";

const WINDOW_SOURCE = {
  "provider-api": "reported by the endpoint",
  catalog: "from skep's model catalog",
  default: "skep's default (nothing reports it)",
};

// The form's three inputs, as the routes take them; an empty key is not sent
// (the endpoint's saved key is used, as /api/llm/test does).
function staged({ protocol, baseUrl, apiKey }) {
  const body = { base_url: baseUrl.value.trim(), protocol: protocol.value };
  if (apiKey.value.trim()) body.api_key = apiKey.value.trim();
  return body;
}

export function stagedConnection(fields) {
  const test = el("button", { type: "button", class: "primary" }, "Test connection");
  const save = el("button", { type: "button", class: "ghost" }, "Save connection");
  const result = el("p", { class: "probe-result note", role: "status" },
    "Test the connection first: nothing is saved until you press Save.");
  save.disabled = true;
  let passed = null; // the JSON of the values that last passed, or null
  let answered = null; // R3 J11: the model that answered that probe — Save persists it
  const show = (tone, ...text) => {
    result.className = `probe-result note tone-${tone}`;
    result.replaceChildren(...text);
  };
  for (const input of [fields.protocol, fields.baseUrl, fields.apiKey]) {
    const stale = () => {
      if (passed === null || JSON.stringify(staged(fields)) === passed) return;
      passed = null;
      save.disabled = true;
      show("warn", "The values changed since the test: test again before saving.");
    };
    input.addEventListener("input", stale);
    input.addEventListener("change", stale);
  }
  test.addEventListener("click", async () => {
    const body = staged(fields);
    test.disabled = true;
    save.disabled = true;
    passed = null;
    show("muted", "testing…");
    try {
      const verdict = await api("POST", "/api/llm/probe", body);
      if (!verdict.ok) {
        show("bad", `✗ ${verdict.detail} — nothing was saved: the connection in use is unchanged.`);
        return;
      }
      // R2 I7: a value edited while the probe was in flight was never probed.
      if (JSON.stringify(staged(fields)) !== JSON.stringify(body)) {
        show("warn", "The values changed while it was being tested: test again before saving.");
        return;
      }
      const reach = `reachable — ${verdict.models} model${verdict.models === 1 ? "" : "s"}`;
      if (!verdict.model) { // O4: no name to report, and a save would be refused
        show("warn", `${reach}, but no model is named and the endpoint lists none: `
          + "nothing would answer as the Queen, so there is nothing to save.");
        return;
      }
      const ctx = verdict.context_window || {};
      passed = JSON.stringify(body);
      answered = verdict.model;
      save.disabled = false;
      show("ok",
        `✓ ${reach}; ${verdict.model} answered the 1-token probe (the model Save activates); `
        + `context window: ${Number(ctx.window).toLocaleString("en-US")} tokens, `
        + `${WINDOW_SOURCE[ctx.source] || ctx.source}. `
        + "Nothing is saved yet: Save makes it the Queen's connection.");
    } catch (e) {
      show("bad", `✗ ${e.message} — nothing was saved.`);
    } finally { test.disabled = false; }
  });
  save.addEventListener("click", async () => {
    const body = staged(fields);
    if (passed === null || JSON.stringify(body) !== passed) return; // only what passed
    save.disabled = true;
    try {
      // R3 J11: the model the probe said Save activates, saved with it — a
      // fresh profile has no default model of its own to fall back on.
      await api("PUT", "/api/llm/config", { ...body, default_model: answered });
      flash("ok", `saved — the Queen now uses ${body.base_url}`);
      route();
    } catch (e) { flash("bad", e.message); save.disabled = false; }
  });
  return { buttons: el("div", { class: "actions" }, test, save), result };
}

function healthLine(health) {
  if (!health) return ["muted", "not checked yet"];
  const when = health.checked_at ? ` · checked ${relativeTime(health.checked_at)}` : "";
  if (health.error) return ["bad", `${health.error}${when}`];
  if (health.reachable && health.model_found && health.completion_ok) return ["ok", `answered the 1-token probe${when}`];
  if (health.reachable && health.model_found) return ["ok", `reachable, model listed${when}`];
  if (health.reachable) return ["warn", `reachable, but its model is not listed${when}`];
  return ["bad", `unreachable${when}`];
}

// Every configured profile, the active one marked, each with its last
// recorded health check (GET /api/providers + /api/providers/health).
export function providerShelf() {
  const shelf = el("section", { class: "provider-shelf" },
    el("h4", { class: "provider-shelf-title" }, "Provider shelf"),
    el("p", { class: "field-help" },
      "Every configured provider profile and its last health check. Switching the active one is "
      + "the use_provider card in chat, or `skep provider use`."));
  const list = el("div", { class: "stack" }, el("p", { class: "note" }, "loading…"));
  shelf.append(list);
  Promise.all([api("GET", "/api/providers"), api("GET", "/api/providers/health")])
    .then(([{ providers }, { health }]) => {
      const byId = new Map(health.map(h => [h.provider_id, h]));
      list.replaceChildren(...(providers.length ? providers.map((p) => {
        const [tone, status] = healthLine(byId.get(p.provider_id));
        const key = p.chatgpt_login ? "ChatGPT plan login" : p.api_key_set ? "key saved" : "no key";
        return el("div", { class: "provider-row", "data-provider": p.provider_id },
          el("div", { class: "row" },
            el("span", { class: "mono" }, p.provider_id),
            p.active ? el("span", { class: "chip tone-accent" }, "active") : null,
            el("span", { class: "chip tone-info" }, p.protocol),
            el("span", { class: "chip tone-muted" }, key)),
          el("p", { class: "note mono provider-where" }, `${p.base_url} · ${p.model || "no model"} · ${p.source}`),
          el("span", { class: `chip tone-${tone}` }, status));
      }) : [el("p", { class: "empty-state" }, "No provider profiles yet: Save a connection above.")]));
    })
    .catch(e => list.replaceChildren(el("p", { class: "note" }, `The provider shelf could not be read: ${e.message}`)));
  return shelf;
}
