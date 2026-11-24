// Phase 10 R1 (E3): the Library's two new views on Templates & Skills —
// the Safe Skill Capture review queue (captured SKILL.md drafts, in full,
// with source and taint, approved or rejected through the existing skill
// verbs) and Forged Tools (each tool's binding, its copy and its seal, read
// from GET /api/forge/tools). Read-only except the two skill verbs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

const candidate = (name, extra = {}) => ({
  name, signature: name, status: "draft", occurrences: 1, created_at: "2026-11-20T10:00:00Z",
  source_task_ids: ["chat:abc"], taint: [], test_outcome: null, registry_name: null,
  template: { name, instructions: "# do it\n\n## Steps\n1. uv sync", description: `captured procedure: ${name}`, worker_kind: "coding", provenance: "captured" },
  ...extra,
});
const SKILLS = {
  skills: [
    candidate("clean-capture"),
    candidate("tainted-capture", { taint: ["untrusted"], source_task_ids: ["run-1234"] }),
    candidate("learned-x", { template: { name: "learned-x", instructions: "x", provenance: "learned", worker_kind: "coding" } }),
    candidate("done-capture", { status: "approved" }),
  ],
};
const TOOLS = {
  tools: [
    {
      plugin_id: "word-count", name: "word-count", purpose: "counts words", state: "active", provenance: "forge",
      task_id: "t1", landed_branch: "skep/t1", registered: true, approved_hosts: [], source_sha256: "a".repeat(64),
      lock_sha256: "", venv_sha256: "", installed: "matches", seal: "none", locked: [],
      lock_note: "no lock landed beside the tool: stdlib only", trial: { offline: "passed" },
    },
    {
      plugin_id: "pdf", name: "pdf", purpose: "reads pdfs", state: "active", provenance: "forge",
      task_id: "t2", landed_branch: "skep/t2", registered: true, approved_hosts: ["pypi.org"], source_sha256: "b".repeat(64),
      lock_sha256: "c".repeat(64), venv_sha256: "d".repeat(64), installed: "differs", seal: "broken",
      locked: ["pypdf==4.0.0 --hash=sha256:00"], lock_note: "the landed lock is the approved one", trial: null,
    },
  ],
};

function libraryApp(routes = {}) {
  const app = loadApp();
  app.header = () => {};
  const calls = [];
  const table = {
    "GET /api/templates": { templates: [] },
    "GET /api/workers": { castes: [] },
    "GET /api/repos": { repos: [] },
    "GET /api/skills": SKILLS,
    "GET /api/forge/tools": TOOLS,
    "POST /api/skills/": {},
    ...routes,
  };
  app.api = async (method, path, body) => {
    calls.push([method, path, body === undefined ? undefined : JSON.parse(JSON.stringify(body))]);
    const key = Object.keys(table).filter(k => `${method} ${path}`.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`unstubbed api ${method} ${path}`);
    const out = table[key];
    return typeof out === "function" ? out(path, body) : out;
  };
  let routed = 0;
  app.route = () => { routed += 1; };
  return { app, calls, routed: () => routed };
}

async function openTab(label, routes) {
  const ctx = libraryApp(routes);
  const main = ctx.app.el("main");
  await ctx.app.viewTemplates(main);
  await flush();
  const tab = main.querySelectorAll(".tab-button").find(b => b.textContent === label);
  assert.ok(tab, label);
  await tab.click();
  await flush();
  const panel = main.querySelectorAll(".tab-panel").find(p => !p.classList.contains("hidden"));
  return { ...ctx, main, panel };
}

test("Templates & Skills carries the capture queue and the forged tools as tabs of their own", async () => {
  const { app } = libraryApp();
  const main = app.el("main");
  await app.viewTemplates(main);
  await flush();
  assert.deepEqual(texts(main, "tab-button"), ["Templates", "Skills", "Captured skills", "Forged tools"]);
});

test("Captured skills: only pending captures, each in full with its source and taint", async () => {
  const { panel } = await openTab("Captured skills");
  const cards = panel.querySelectorAll(".capture-card");
  assert.deepEqual(cards.map(c => c.getAttribute("data-name")), ["clean-capture", "tainted-capture"]);
  const md = cards[0].querySelector(".capture-md").textContent;
  assert.equal(md, '---\nname: "clean-capture"\ndescription: "captured procedure: clean-capture"\ncaptured_from: "chat:abc"\n---\n\n# do it\n\n## Steps\n1. uv sync\n');
  assert.match(cards[0].textContent, /captured from chat:abc/);
  assert.equal(cards[0].querySelector(".banner"), null);
  const taint = cards[1].querySelector(".banner");
  assert.match(taint.textContent, /⚠ tainted — captured from run-1234, which carries: outside content/);
  assert.match(cards[1].querySelector(".capture-md").textContent, /taint: "untrusted"/);
  assert.match(panel.textContent, /grants nothing/);
});

test("Captured skills: Approve sends accept_taint only after the taint is accepted; Reject rejects", async () => {
  const { panel, calls, routed } = await openTab("Captured skills");
  const [clean, tainted] = panel.querySelectorAll(".capture-card");
  const approve = card => card.querySelectorAll("button").find(b => b.textContent.startsWith("Approve"));
  await approve(clean).click();
  assert.deepEqual(calls.at(-1), ["POST", "/api/skills/clean-capture/approve", { accept_taint: false }]);
  const box = tainted.querySelector("input");
  assert.equal(approve(tainted).disabled, true);
  assert.match(tainted.textContent, /skep skill approve tainted-capture --accept-taint/);
  box.checked = true;
  await Promise.all(box.listeners.change.map(fn => fn()));
  assert.equal(approve(tainted).disabled, false);
  await approve(tainted).click();
  assert.deepEqual(calls.at(-1), ["POST", "/api/skills/tainted-capture/approve", { accept_taint: true }]);
  await tainted.querySelectorAll("button").find(b => b.textContent === "Reject").click();
  assert.deepEqual(calls.at(-1), ["POST", "/api/skills/tainted-capture/reject", {}]);
  assert.equal(routed(), 3);
});

test("Captured skills: none waiting says so", async () => {
  const { panel } = await openTab("Captured skills", { "GET /api/skills": { skills: [] } });
  assert.match(panel.textContent, /No captured skill is waiting/);
});

test("Forged tools: each binding — digests, pinned wheels, hosts, seal — and a copy or seal gone wrong is loud", async () => {
  const { panel, calls } = await openTab("Forged tools");
  const cards = panel.querySelectorAll(".forge-card");
  assert.equal(cards.length, 2);
  const [word, pdf] = cards;
  assert.match(word.textContent, /source sha256 a{64}/);
  assert.match(word.textContent, /installed copy matches the approved digest/);
  assert.match(word.textContent, /stdlib only: no lock, no venv/);
  assert.match(word.textContent, /network: none \(deny-all egress\)/);
  assert.match(pdf.textContent, /lock sha256 c{64} · 1 pinned wheel/);
  assert.match(pdf.querySelector(".forge-pins").textContent, /pypdf==4\.0\.0/);
  assert.match(pdf.textContent, /network: pypi\.org/);
  assert.match(pdf.textContent, /the landed lock is the approved one/);
  const loud = pdf.querySelectorAll(".banner").map(b => b.textContent);
  assert.ok(loud.some(t => /installed copy is NOT the approved source/.test(t)), loud);
  assert.ok(loud.some(t => /venv no longer matches its seal/.test(t)), loud);
  assert.equal(panel.querySelectorAll("button").length, 0); // read-only: promote/suspend stay carded verbs
  assert.ok(calls.some(([, p]) => p === "/api/forge/tools"));
});

test("Forged tools: none forged says so", async () => {
  const { panel } = await openTab("Forged tools", { "GET /api/forge/tools": { tools: [] } });
  assert.match(panel.textContent, /No forged tools yet/);
});
