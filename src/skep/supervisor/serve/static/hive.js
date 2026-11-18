/* hive-rooms H2 (C11, ADR 0064 D1): the Hive page — your queens and the agents
   on this machine. A queen is a role with a model: a name, a provider and
   model, a policy template and instructions. Every write goes through the same
   verb as `skep role create|edit|delete` (POST/PUT/DELETE /api/roles), and the
   server's refusal is shown as it was written (I9). No build step. */

const TOKEN_KEY = "skep-token";
const TEMPLATES = ["builder", "operator", "assistant"];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

// ponytail: a twin of app.js api() minus the login redirect — app.js is a
// classic script with no exports; a 401 here surfaces as the route's flash.
async function api(method, path, body) {
  const options = { method, headers: { "X-Skep-Token": localStorage.getItem(TOKEN_KEY) || "" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail ? String(data.detail) : `${response.status}`);
  return data;
}

const hostOf = url => { try { return new URL(url).host; } catch { return url; } };

// One roster card: name, what it talks to, its policy, and how many chats use it.
export function queenCard(role, isDefault, onEdit) {
  const talks = `${role.model || "default model"} · ${role.provider || "active provider"}`;
  const policy = role.policy_layer ? `role ${role.policy_layer}` : "no template: acts as assistant";
  const chats = role.chats === 1 ? "in 1 chat" : `in ${role.chats || 0} chats`;
  return el("div", { class: "card queen-card" },
    el("div", { class: "row" },
      el("strong", {}, role.name),
      isDefault ? el("span", { class: "chip tone-accent" }, "default") : null,
      role.shipped ? el("span", { class: "chip tone-muted" }, "ships with skep") : null,
      el("button", { class: "ghost", onclick: () => onEdit(role) }, "Edit")),
    el("p", { class: "note queen-talks" }, talks),
    el("p", { class: "note queen-policy" }, `${policy} · ${chats}`));
}

// The create / edit form. ``save(name, fields, creating)`` and ``remove(name)``
// do the network; the form only builds what the operator typed.
export function queenForm(role, providers, { save, remove }) {
  const creating = role === null;
  const current = role || {};
  const status = el("p", { class: "field-help queen-status" });
  const name = el("input", { value: current.name || "", placeholder: "nova", disabled: !creating });
  const provider = el("select", {},
    el("option", { value: "" }, "active provider (Settings)"),
    providers.map(p => el("option", { value: p.provider_id }, p.provider_id)));
  provider.value = current.provider || "";
  const model = el("input", { value: current.model || "", placeholder: "the provider's default" });
  const template = el("select", {},
    TEMPLATES.map(t => el("option", { value: t }, t)),
    el("option", { value: "" }, "none: acts as assistant"));
  template.value = creating ? "assistant" : (current.policy_layer || "");
  const prompt = el("textarea", { rows: "3", placeholder: "Review patches for missing tests. Be brief." });
  prompt.value = current.system_prompt || "";
  // Condition 3: say where this queen's chats' content goes before saving.
  const where = el("p", { class: "field-help queen-where" });
  const showWhere = () => {
    const chosen = providers.find(p => p.provider_id === provider.value);
    where.textContent = chosen
      ? `Every turn in its chats goes to ${hostOf(chosen.base_url)}, with that provider's own key.`
      : "Uses the model in Settings, like every role that pins nothing.";
  };
  provider.addEventListener("change", showWhere);
  showWhere();
  const submit = el("button", { class: "primary" }, creating ? "Create queen" : "Save");
  submit.addEventListener("click", async () => {
    const fields = {
      provider: provider.value, model: model.value.trim(),
      policy_layer: template.value, system_prompt: prompt.value,
    };
    try { await save(creating ? name.value.trim() : current.name, fields, creating); }
    catch (e) { status.textContent = e.message; }
  });
  const actions = el("div", { class: "row" }, submit);
  if (!creating && !current.shipped) {
    actions.append(el("button", {
      class: "ghost",
      onclick: async () => {
        try { await remove(current.name); } catch (e) { status.textContent = e.message; }
      },
    }, "Delete"));
  }
  return el("div", { class: "card queen-form" },
    el("strong", {}, creating ? "New queen" : `Edit ${current.name}`),
    el("div", { class: "field" }, el("label", {}, "Name"), name),
    el("div", { class: "field" }, el("label", {}, "Provider"), provider, where),
    el("div", { class: "field" }, el("label", {}, "Model"), model),
    el("div", { class: "field" }, el("label", {}, "Role (policy template)"), template,
      el("p", { class: "field-help" },
        "A role can tighten your approved baseline, never loosen it.")),
    el("div", { class: "field" }, el("label", {}, "Instructions"), prompt),
    actions, status);
}

// "Agents on this machine" — the presence read (GET /api/agents).
export function agentList(view) {
  const rows = (view.agents || []).map(a => el("div", { class: "card agent-row" },
    el("strong", {}, `${a.agent} · ${a.state}`),
    el("p", { class: "note" }, a.governed
      ? ["governed", a.task_id && `run ${a.task_id}`, a.role && `role ${a.role}`].filter(Boolean).join(" · ")
      : "not governed — started outside skep")));
  return el("div", {}, el("p", { class: "note" }, view.summary || "no agents running"), rows);
}

export async function viewHive(main) {
  const title = document.getElementById("topbar-title");
  if (title) title.textContent = "Hive";
  document.title = "Hive · skep";
  const [roles, shelf, agents] = await Promise.all([
    api("GET", "/api/roles"),
    api("GET", "/api/providers"),
    api("GET", "/api/agents").catch(e => ({ summary: `agents unavailable: ${e.message}` })),
  ]);
  const providers = shelf.providers || [];
  const side = el("div", {});
  const reload = () => { main.replaceChildren(); return viewHive(main); };
  const handlers = {
    save: async (name, fields, creating) => {
      if (creating) await api("POST", "/api/roles", { name, ...fields });
      else await api("PUT", `/api/roles/${encodeURIComponent(name)}`, fields);
      await reload();
    },
    remove: async (name) => {
      await api("DELETE", `/api/roles/${encodeURIComponent(name)}`);
      await reload();
    },
  };
  const edit = (role) => side.replaceChildren(queenForm(role, providers, handlers));
  edit(null);
  main.append(
    el("p", { class: "sub" }, "Your queens — a role with a model — and the coding agents running here."),
    el("div", { class: "home-grid" },
      el("div", {},
        el("h3", {}, "Queens"),
        roles.roles.map(r => queenCard(r, r.name === roles.default, edit)),
        el("button", { class: "ghost", onclick: () => edit(null) }, "+ New queen"),
        el("h3", {}, "Agents on this machine"),
        agentList(agents)),
      side));
}
