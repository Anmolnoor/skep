/* Phase 10 10.1: the unified-diff renderer — offline, no dependency. Parses
   `git diff` output into files and hunks, then draws one section per file
   with old/new line numbers. Hunk line counts decide where a hunk ends, so a
   removed line that reads "--- x" is content, never a new file header.
   Phase 10 R1 D2: what is not a line change is still said — a mode change
   (+x loudest), a binary file in either git spelling, a symlink, a submodule
   — and the complete raw diff is one click away. Lines are coloured offline
   by file type; every token is a text node in a span (dom.js el()). */
import { el } from "./dom.js";

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

export function parseDiff(text) {
  const files = [];
  let file = null;
  let oldNo = 0; let newNo = 0; let oldLeft = 0; let newLeft = 0;
  let binaryBody = false; // inside a `GIT binary patch` record: base85, not lines
  const start = () => {
    file = { path: "", oldPath: "", newPath: "", status: "modified", added: 0, removed: 0, lines: [],
      binary: false, oldMode: null, newMode: null, mode: null };
    files.push(file);
    binaryBody = false;
  };
  const strip = p => (p === "/dev/null" ? p : p.replace(/^[ab]\//, ""));
  for (const line of String(text || "").split("\n")) {
    if (oldLeft > 0 || newLeft > 0) { // inside a hunk: the counts say what this line is
      const sign = line[0];
      if (sign === "+") { file.added += 1; file.lines.push({ kind: "add", text: line, newNo: newNo++ }); newLeft -= 1; }
      else if (sign === "-") { file.removed += 1; file.lines.push({ kind: "del", text: line, oldNo: oldNo++ }); oldLeft -= 1; }
      else if (sign === "\\") file.lines.push({ kind: "meta", text: line }); // mid-hunk: counts nothing
      else { file.lines.push({ kind: "ctx", text: line, oldNo: oldNo++, newNo: newNo++ }); oldLeft -= 1; newLeft -= 1; }
      continue;
    }
    if (binaryBody && !line.startsWith("diff --git ")) continue;
    const hunk = line.match(HUNK);
    if (hunk && file) {
      [oldNo, newNo] = [Number(hunk[1]), Number(hunk[3])];
      [oldLeft, newLeft] = [Number(hunk[2] ?? 1), Number(hunk[4] ?? 1)];
      file.lines.push({ kind: "hunk", text: line });
    } else if (line.startsWith("diff --git ")) {
      start();
      const paths = line.match(/^diff --git a\/(.+) b\/(.+)$/);
      if (paths) [file.oldPath, file.newPath] = [paths[1], paths[2]];
    } else if (line.startsWith("--- ")) {
      if (!file || file.lines.length) start(); // a plain (non-git) diff starts here
      file.oldPath = strip(line.slice(4).split("\t")[0]);
    } else if (line.startsWith("+++ ") && file) {
      file.newPath = strip(line.slice(4).split("\t")[0]);
    } else if (!file) continue;
    else if (line.startsWith("\\ ")) file.lines.push({ kind: "meta", text: line }); // "\ No newline at end of file"
    else if (line.startsWith("new file mode ")) { file.status = "added"; file.newMode = line.slice(14); }
    else if (line.startsWith("deleted file mode ")) { file.status = "deleted"; file.oldMode = line.slice(18); }
    else if (line.startsWith("old mode ")) file.oldMode = line.slice(9);
    else if (line.startsWith("new mode ")) file.newMode = line.slice(9);
    else if (line.startsWith("index ")) file.mode = line.split(" ")[2] || file.mode; // "index a..b 100644"
    else if (line.startsWith("rename from ")) { file.status = "renamed"; file.oldPath = line.slice(12); }
    else if (line.startsWith("rename to ")) file.newPath = line.slice(10);
    else if (line.startsWith("Binary files ")) file.binary = true;
    else if (line === "GIT binary patch") { file.binary = true; binaryBody = true; }
  }
  for (const f of files) {
    if (f.oldPath === "/dev/null") f.status = "added";
    if (f.newPath === "/dev/null") f.status = "deleted";
    if (f.binary && f.status === "modified") f.status = "binary";
    f.path = f.newPath && f.newPath !== "/dev/null" ? f.newPath : f.oldPath;
  }
  return files;
}

// ---------- offline syntax colour (D2) ----------
// One diff line at a time — that is all a hunk gives — so a construct that
// spans lines (a block comment, a triple-quoted string) is coloured where it
// is visible and left plain where it is not. Colour is a reading aid; the
// text, untouched, is the truth.

const STR = String.raw`"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'`;
const NUM = String.raw`\b(?:0[xX][\da-fA-F]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b`;
const words = list => String.raw`\b(?:${list.split(" ").join("|")})\b`;

// [class, pattern] in priority order; patterns use no capturing groups.
const LANGS = {
  py: [["com", "#.*"], ["str", STR], ["kw", words("and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield")], ["num", NUM]],
  js: [["com", String.raw`\/\/.*|\/\*.*?\*\/`], ["str", STR + "|`(?:\\\\.|[^`\\\\])*`"], ["kw", words("as async await break case catch class const continue default delete do else enum export extends false finally for from function if implements import in instanceof interface let new null of return static super switch this throw true try type typeof undefined var void while yield")], ["num", NUM]],
  json: [["str", STR], ["kw", words("true false null")], ["num", NUM]],
  sh: [["com", String.raw`(?:^|(?<=\s))#.*`], ["str", STR], ["kw", words("case do done elif else esac exit export fi for function if in local readonly return set then unset until while")], ["num", NUM]],
  conf: [["com", "#.*"], ["str", STR], ["key", String.raw`^\s*[\w.-]+(?=\s*[:=])`], ["kw", words("true false null yes no on off")], ["num", NUM]],
  md: [["kw", String.raw`^#{1,6}\s.*`], ["str", "`[^`]+`"]],
  css: [["com", String.raw`\/\*.*?\*\/`], ["str", STR], ["kw", String.raw`@[\w-]+`], ["key", String.raw`[\w-]+(?=\s*:)`], ["num", String.raw`#[\da-fA-F]{3,8}\b|\b\d+(?:\.\d+)?(?:[a-z]+|%)?`]],
  html: [["com", String.raw`<!--.*?-->`], ["str", STR], ["kw", String.raw`<\/?[A-Za-z][\w-]*|\/?>`], ["key", String.raw`[\w-]+(?==)`]],
};
const EXT = {
  py: "py", pyi: "py", js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js", json: "json",
  sh: "sh", bash: "sh", zsh: "sh", toml: "conf", yaml: "conf", yml: "conf", md: "md", markdown: "md",
  css: "css", html: "html", htm: "html",
};
const compiled = {};
function rules(path) {
  const name = String(path || "").split("/").pop();
  const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  const lang = Object.hasOwn(EXT, ext) ? EXT[ext] : null; // R3 J12: own keys only — never ".constructor"
  if (!lang) return null;
  compiled[lang] ||= {
    re: new RegExp(LANGS[lang].map(([, src]) => `(${src})`).join("|"), "g"),
    classes: LANGS[lang].map(([cls]) => `tok tok-${cls}`),
  };
  return compiled[lang];
}

// The line's code as text and token spans, in order: joined, it is `code`.
export function highlight(code, path) {
  const lang = rules(path);
  if (!lang || !code) return [code];
  const out = [];
  let at = 0;
  for (const match of code.matchAll(lang.re)) {
    if (!match[0]) continue;
    if (match.index > at) out.push(code.slice(at, match.index));
    out.push(el("span", { class: lang.classes[match.slice(1).findIndex(g => g !== undefined)] }, match[0]));
    at = match.index + match[0].length;
  }
  if (at < code.length) out.push(code.slice(at));
  return out;
}

const lineNo = n => el("span", { class: "diff-ln", "aria-hidden": "true" }, n === undefined ? "" : String(n));

const SPECIAL = { 120000: "symlink", 160000: "submodule" };
const executable = mode => /^100[0-7]{3}$/.test(mode || "") && (parseInt(mode.slice(3), 8) & 0o111) !== 0;

function lineNode(line, path) {
  const code = line.kind === "add" || line.kind === "del" || line.kind === "ctx";
  return el("div", { class: line.kind }, lineNo(line.oldNo), lineNo(line.newNo),
    code ? [line.text[0] || " ", ...highlight(line.text.slice(1), path)] : line.text || " ");
}

function fileSection(file) {
  const named = mode => (Object.hasOwn(SPECIAL, mode ?? "") ? SPECIAL[mode] : null);
  const special = named(file.newMode) || named(file.oldMode) || named(file.mode);
  const modeChanged = file.oldMode && file.newMode && file.oldMode !== file.newMode;
  const tags = [
    file.status === "modified" ? null : file.status,
    file.binary && file.status !== "binary" ? "binary" : null,
    special,
    modeChanged ? `mode ${file.oldMode} → ${file.newMode}` : null,
  ].filter(Boolean).map(tag => el("span", { class: "chip tone-muted" }, tag));
  // +x is the mode change that can run: said as an alarm, never as a chip only.
  let plusX = null;
  if (modeChanged && executable(file.newMode) && !executable(file.oldMode)) {
    plusX = `+x — this patch makes ${file.path} executable (mode ${file.oldMode} → ${file.newMode}).`;
  } else if (file.status === "added" && executable(file.newMode)) {
    plusX = `+x — ${file.path} is a new executable file (mode ${file.newMode}).`;
  }
  const label = file.status === "renamed" ? `${file.oldPath} → ${file.newPath}` : file.path;
  const empty = file.binary ? "Binary file — no text diff."
    : modeChanged ? "Mode change only — no line changes." : "No line changes.";
  return el("section", { class: "diff-file", tabindex: "-1", "aria-label": `${label}, +${file.added} −${file.removed}` },
    el("h3", { class: "diff-file-head mono" }, label, " ",
      el("span", { class: "diff-stat" }, el("span", { class: "add" }, `+${file.added}`), " ",
        el("span", { class: "del" }, `−${file.removed}`)),
      tags),
    plusX ? el("p", { class: "banner bad", role: "alert" }, plusX) : null,
    file.lines.length
      ? el("pre", { class: "diff" }, file.lines.map(line => lineNode(line, file.path)))
      : el("p", { class: "note" }, empty));
}

// The whole diff as it came, one text node: what the parser did not model is
// still here (I8).
const rawDiff = text => el("details", { class: "diff-raw" },
  el("summary", {}, "Raw diff (complete)"), el("pre", { class: "diff" }, String(text || "")));

// The file list, then every file. Returns { node, sections } so a view can
// move focus file by file (the review keys).
export function renderDiff(text) {
  const files = parseDiff(text);
  if (!files.length) {
    return { node: el("pre", { class: "diff" }, String(text || "").split("\n").map(line => el("div", {}, line || " "))), sections: [] };
  }
  const sections = files.map(fileSection);
  const list = el("ol", { class: "diff-files" }, files.map((file, i) => el("li", {},
    el("button", { type: "button", class: "ghost diff-file-link", onclick: () => sections[i].focus() },
      el("span", { class: "mono" }, file.path), " ",
      el("span", { class: "diff-stat" }, el("span", { class: "add" }, `+${file.added}`), " ",
        el("span", { class: "del" }, `−${file.removed}`))))));
  const total = files.reduce((sum, f) => [sum[0] + f.added, sum[1] + f.removed], [0, 0]);
  return {
    node: el("div", { class: "diff-view" },
      el("p", { class: "note" }, `${files.length} file${files.length === 1 ? "" : "s"} changed, +${total[0]} −${total[1]}`),
      el("nav", { "aria-label": "Changed files" }, list),
      sections,
      rawDiff(text)),
    sections,
  };
}
