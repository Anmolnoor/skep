/* Phase 10 10.1: the unified-diff renderer — offline, no dependency. Parses
   `git diff` output into files and hunks, then draws one section per file
   with old/new line numbers. Hunk line counts decide where a hunk ends, so a
   removed line that reads "--- x" is content, never a new file header. */
import { el } from "./dom.js";

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

export function parseDiff(text) {
  const files = [];
  let file = null;
  let oldNo = 0; let newNo = 0; let oldLeft = 0; let newLeft = 0;
  const start = () => {
    file = { path: "", oldPath: "", newPath: "", status: "modified", added: 0, removed: 0, lines: [] };
    files.push(file);
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
    } else if (file && line.startsWith("\\ ")) file.lines.push({ kind: "meta", text: line }); // "\ No newline at end of file"
    else if (file && line.startsWith("new file mode")) file.status = "added";
    else if (file && line.startsWith("deleted file mode")) file.status = "deleted";
    else if (file && line.startsWith("rename from ")) { file.status = "renamed"; file.oldPath = line.slice(12); }
    else if (file && line.startsWith("rename to ")) file.newPath = line.slice(10);
    else if (file && line.startsWith("Binary files ")) file.status = "binary";
  }
  for (const f of files) {
    if (f.oldPath === "/dev/null") f.status = "added";
    if (f.newPath === "/dev/null") f.status = "deleted";
    f.path = f.newPath && f.newPath !== "/dev/null" ? f.newPath : f.oldPath;
  }
  return files;
}

const lineNo = n => el("span", { class: "diff-ln", "aria-hidden": "true" }, n === undefined ? "" : String(n));

function fileSection(file) {
  const tag = file.status === "modified" ? null : el("span", { class: "chip tone-muted" }, file.status);
  const label = file.status === "renamed" ? `${file.oldPath} → ${file.newPath}` : file.path;
  return el("section", { class: "diff-file", tabindex: "-1", "aria-label": `${label}, +${file.added} −${file.removed}` },
    el("h3", { class: "diff-file-head mono" }, label, " ",
      el("span", { class: "diff-stat" }, el("span", { class: "add" }, `+${file.added}`), " ",
        el("span", { class: "del" }, `−${file.removed}`)),
      tag),
    file.lines.length
      ? el("pre", { class: "diff" }, file.lines.map(line => el("div", { class: line.kind },
        lineNo(line.oldNo), lineNo(line.newNo), line.text || " ")))
      : el("p", { class: "note" }, file.status === "binary" ? "Binary file — no text diff." : "No line changes."));
}

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
      sections),
    sections,
  };
}
