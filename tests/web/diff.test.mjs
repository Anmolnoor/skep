// Phase 10 R1 D2: the diff tells the whole truth. A mode change (+x loudest),
// a binary file in either git spelling, a symlink or a submodule is said, not
// rendered as "No line changes"; the complete raw diff is one click away as a
// text node; and lines are coloured offline by file type — spans of text
// nodes, never innerHTML, no library.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, texts } from "./harness.mjs";

const plain = value => JSON.parse(JSON.stringify(value));

const TRUTH = [
  "diff --git a/run.sh b/run.sh",
  "old mode 100644",
  "new mode 100755",
  "diff --git a/tool b/tool",
  "new file mode 100755",
  "index 0000000..1111111",
  "--- /dev/null",
  "+++ b/tool",
  "@@ -0,0 +1 @@",
  "+echo hi",
  "diff --git a/img.png b/img.png",
  "new file mode 100644",
  "index 0000000..2222222",
  "GIT binary patch",
  "literal 12",
  "TcmZ?wbhEHbWMp7q00031",
  "",
  "literal 0",
  "HcmV?d00001",
  "",
  "diff --git a/logo.gif b/logo.gif",
  "index 3333333..4444444 100644",
  "Binary files a/logo.gif and b/logo.gif differ",
  "diff --git a/latest b/latest",
  "new file mode 120000",
  "index 0000000..5555555",
  "--- /dev/null",
  "+++ b/latest",
  "@@ -0,0 +1 @@",
  "+releases/v2",
  "\\ No newline at end of file",
  "diff --git a/vendor/lib b/vendor/lib",
  "index 6666666..7777777 160000",
  "--- a/vendor/lib",
  "+++ b/vendor/lib",
  "@@ -1 +1 @@",
  "-Subproject commit 6666666",
  "+Subproject commit 7777777",
  "diff --git a/old.sh b/old.sh",
  "old mode 100755",
  "new mode 100644",
  "",
].join("\n");

const byPath = (app, text) => Object.fromEntries(app.parseDiff(text).map(f => [f.path, f]));
const alerts = node => node.querySelectorAll(".banner").filter(b => b.getAttribute("role") === "alert").map(b => b.textContent);
const chips = node => texts(node, "chip");

test("parseDiff keeps modes and both binary spellings; a binary patch's body is not a file", () => {
  const app = loadApp();
  const files = app.parseDiff(TRUTH);
  assert.deepEqual(plain(files.map(f => f.path)),
    ["run.sh", "tool", "img.png", "logo.gif", "latest", "vendor/lib", "old.sh"]);
  const f = byPath(app, TRUTH);
  assert.deepEqual(plain([f["run.sh"].oldMode, f["run.sh"].newMode]), ["100644", "100755"]);
  assert.equal(f.tool.newMode, "100755");
  assert.deepEqual(plain([f["img.png"].binary, f["img.png"].status, f["img.png"].lines.length]), [true, "added", 0]);
  assert.deepEqual(plain([f["logo.gif"].binary, f["logo.gif"].status]), [true, "binary"]);
  assert.equal(f.latest.newMode, "120000");
  assert.equal(f["vendor/lib"].mode, "160000");
});

test("a mode change says old → new, and +x is loud — never 'No line changes'", () => {
  const app = loadApp();
  const { sections } = app.renderDiff(TRUTH);
  const [modeOnly, newExec] = sections;
  assert.ok(alerts(modeOnly).some(a => /\+x/.test(a) && /100644 → 100755/.test(a)), alerts(modeOnly));
  assert.match(modeOnly.textContent, /Mode change only/);
  assert.doesNotMatch(modeOnly.textContent, /No line changes/);
  assert.ok(alerts(newExec).some(a => /\+x/.test(a) && /100755/.test(a)), alerts(newExec));
  const dropped = sections[6]; // 100755 → 100644: said, but not an alarm
  assert.deepEqual(alerts(dropped), []);
  assert.ok(chips(dropped).includes("mode 100755 → 100644"), chips(dropped));
});

test("binary (both spellings), symlink and submodule are named on the file", () => {
  const app = loadApp();
  const { sections } = app.renderDiff(TRUTH);
  const [, , img, gif, link, sub] = sections;
  for (const section of [img, gif]) {
    assert.ok(chips(section).includes("binary"), chips(section));
    assert.match(section.textContent, /Binary file — no text diff\./);
  }
  assert.ok(chips(link).includes("symlink"), chips(link));
  assert.ok(chips(sub).includes("submodule"), chips(sub));
});

test("the complete raw diff is one text node in a <pre> — never parsed as HTML", () => {
  const app = loadApp();
  const hostile = TRUTH + "diff --git a/x.html b/x.html\n--- a/x.html\n+++ b/x.html\n@@ -0,0 +1 @@\n+<img src=x onerror=alert(1)>\n";
  const { node } = app.renderDiff(hostile);
  const raw = node.querySelector(".diff-raw");
  assert.ok(raw, "a raw-diff disclosure");
  const pre = raw.querySelector("pre");
  assert.equal(pre.children.length, 1);
  assert.equal(pre.children[0].nodeType, 3);
  assert.equal(pre.textContent, hostile);
  // Nowhere in the tree did the diff's text become an element.
  assert.deepEqual(node.querySelectorAll("img"), []);
});

test("highlight: tokens by extension, text nodes in spans, the text unchanged", () => {
  const app = loadApp();
  const kinds = (code, path) => app.highlight(code, path)
    .filter(n => typeof n === "object").map(n => [n.className, n.textContent]);
  const py = 'def f(x): return "s"  # c';
  assert.deepEqual(plain(kinds(py, "src/a.py")), [
    ["tok tok-kw", "def"], ["tok tok-kw", "return"], ["tok tok-str", '"s"'], ["tok tok-com", "# c"]]);
  assert.equal(app.highlight(py, "a.py").map(n => (typeof n === "object" ? n.textContent : n)).join(""), py);
  assert.deepEqual(plain(kinds("const n = 42; // why", "web/x.mjs")),
    [["tok tok-kw", "const"], ["tok tok-num", "42"], ["tok tok-com", "// why"]]);
  assert.deepEqual(plain(kinds('{"on": true}', "a.json")), [["tok tok-str", '"on"'], ["tok tok-kw", "true"]]);
  assert.deepEqual(plain(kinds("name = 'x' # n", "pyproject.toml")),
    [["tok tok-key", "name"], ["tok tok-str", "'x'"], ["tok tok-com", "# n"]]);
  assert.deepEqual(plain(kinds("## Usage", "README.md")), [["tok tok-kw", "## Usage"]]);
  assert.deepEqual(plain(kinds("a { color: #fff; }", "s.css")), [["tok tok-key", "color"], ["tok tok-num", "#fff"]]);
  assert.deepEqual(plain(kinds('<a href="x">', "i.html")), [["tok tok-kw", "<a"], ["tok tok-key", "href"], ["tok tok-str", '"x"'], ["tok tok-kw", ">"]]);
  assert.deepEqual(plain(kinds("if [ -f x ]; then echo; fi", "run.sh")), [["tok tok-kw", "if"], ["tok tok-kw", "then"], ["tok tok-kw", "fi"]]);
  assert.deepEqual(plain(app.highlight("plain words", "notes.txt")), ["plain words"]);
});

test("renderDiff colours a line by its file's type and keeps sign and text intact", () => {
  const app = loadApp();
  const { sections } = app.renderDiff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+def f(): pass\n");
  const add = sections[0].querySelector(".diff").querySelector(".add"); // the line, not the stat
  assert.equal(add.textContent, "1+def f(): pass");
  assert.deepEqual(texts(add, "tok-kw"), ["def", "pass"]);
});
