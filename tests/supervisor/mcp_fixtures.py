"""Real MCP servers for the v135 Wave 2a tests (ADR 0069): one in Python, one
in Node, both stdlib only, and the local packages and registry they install
from — no public internet.

Each server answers ``initialize``, then refuses every request until
``notifications/initialized`` arrives (so a test proves the handshake), and
offers four tools: ``probe`` (read paths; try a direct socket), ``fetch``
(GET urls through whatever proxy the environment names), ``env`` (named
variables) and ``leak`` (echo one variable, as a result or as an error).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PY_SERVER = r"""
import json, os, socket, sys, urllib.request


def _text(value):
    return {"content": [{"type": "text", "text": value}]}


def _call(name, args):
    if name == "probe":
        seen = {}
        for path in args.get("paths", []):
            try:
                os.listdir(path) if os.path.isdir(path) else open(path).read()
                seen[path] = "read"
            except OSError as exc:
                seen[path] = type(exc).__name__
        if args.get("port"):
            try:
                socket.create_connection(("127.0.0.1", args["port"]), timeout=3).close()
                seen["direct"] = "reached"
            except OSError as exc:
                seen["direct"] = "blocked:" + type(exc).__name__
        return _text(json.dumps(seen))
    if name == "fetch":
        out = {}
        for url in args.get("urls", []):
            try:
                with urllib.request.urlopen(url, timeout=8) as reply:
                    out[url] = "reached:" + reply.read().decode()
            except Exception as exc:
                out[url] = "blocked:" + type(exc).__name__ + ":" + str(exc)
        return _text(json.dumps(out))
    if name == "env":
        return _text(json.dumps({n: os.environ.get(n) for n in args.get("names", [])}))
    if name == "write":
        out = {}
        for path in args.get("paths", []):
            try:
                with open(os.path.expandvars(path), "a") as handle:
                    handle.write("x")
                out[path] = "wrote"
            except OSError as exc:
                out[path] = type(exc).__name__
        return _text(json.dumps(out))
    if name == "leak":
        value = "x" * int(args.get("pad", 0)) + "value=" + str(os.environ.get(args.get("name", "")))
        if args.get("stderr"):
            sys.stderr.write(value + "\n")
            sys.stderr.flush()
            os._exit(4)
        return {**_text(value), "isError": bool(args.get("as_error"))}
    if name == "die":
        os._exit(3)
    raise ValueError("no tool " + name)


def main():
    initialized = False
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "notifications/initialized":
            initialized = True
            continue
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fx-py", "version": "1"}}
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        elif not initialized:
            reply = {"jsonrpc": "2.0", "id": mid,
                     "error": {"code": -32002, "message": "not initialized"}}
        elif method == "tools/list" and os.environ.get("FX_LIST_ERROR"):
            said = "bad key " + os.environ["FX_LIST_ERROR"]
            sys.stderr.write(said + "\n")
            sys.stderr.flush()
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": said}}
        elif method == "tools/list":
            tools = [{"name": n, "inputSchema": {"type": "object"}}
                     for n in ("probe", "fetch", "env", "leak", "die", "write")]
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        else:
            params = msg.get("params") or {}
            result = _call(params.get("name"), params.get("arguments") or {})
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
"""

NODE_SERVER = r"""#!/usr/bin/env node
const fs = require("fs");
const net = require("net");
const readline = require("readline");
let initialized = false;
const send = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const text = (t) => ({ content: [{ type: "text", text: t }] });

async function call(name, args) {
  if (name === "probe") {
    const seen = {};
    for (const p of args.paths || []) {
      try {
        fs.statSync(p).isDirectory() ? fs.readdirSync(p) : fs.readFileSync(p);
        seen[p] = "read";
      } catch (e) { seen[p] = e.code || String(e); }
    }
    if (args.port) {
      seen.direct = await new Promise((done) => {
        const s = net.connect({ host: "127.0.0.1", port: args.port, timeout: 3000 });
        s.on("connect", () => { s.destroy(); done("reached"); });
        s.on("error", (e) => done("blocked:" + e.code));
        s.on("timeout", () => { s.destroy(); done("blocked:timeout"); });
      });
    }
    return text(JSON.stringify(seen));
  }
  if (name === "fetch") {
    const out = {};
    for (const u of args.urls || []) {
      try {
        const r = await fetch(u, { signal: AbortSignal.timeout(8000) });
        out[u] = (r.ok ? "reached:" : "blocked:" + r.status + ":") + (await r.text());
      } catch (e) { out[u] = "blocked:" + ((e.cause && e.cause.code) || e.message); }
    }
    return text(JSON.stringify(out));
  }
  if (name === "env") {
    const out = {};
    for (const n of args.names || []) out[n] = process.env[n] === undefined ? null : process.env[n];
    return text(JSON.stringify(out));
  }
  if (name === "leak") {
    const value = "value=" + process.env[args.name];
    return { ...text(value), isError: !!args.as_error };
  }
  if (name === "die") process.exit(3);
  throw new Error("no tool " + name);
}

readline.createInterface({ input: process.stdin }).on("line", async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const { id, method, params } = msg;
  if (method === "notifications/initialized") { initialized = true; return; }
  if (method === "initialize") {
    return send({ jsonrpc: "2.0", id, result: { protocolVersion: "2024-11-05",
      capabilities: { tools: {} }, serverInfo: { name: "fx-node", version: "1" } } });
  }
  if (!initialized) {
    return send({ jsonrpc: "2.0", id, error: { code: -32002, message: "not initialized" } });
  }
  if (method === "tools/list") {
    return send({ jsonrpc: "2.0", id, result: { tools: ["probe", "fetch", "env", "leak", "die"]
      .map((name) => ({ name, inputSchema: { type: "object" } })) } });
  }
  send({ jsonrpc: "2.0", id, result: await call(params.name, params.arguments || {}) });
});
"""


def _b64_digest(data: bytes, algo: str) -> str:
    return base64.urlsafe_b64encode(hashlib.new(algo, data).digest()).rstrip(b"=").decode()


def build_wheel(dest: Path, *, name: str = "fxmcp", version: str = "1.0.0") -> Path:
    """A pure-Python wheel whose console script ``fx-mcp`` runs PY_SERVER."""
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": PY_SERVER,
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: skep-test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
        f"{dist}/entry_points.txt": f"[console_scripts]\nfx-mcp = {name}:main\n",
    }
    record = [
        f"{path},sha256={_b64_digest(body.encode(), 'sha256')},{len(body.encode())}"
        for path, body in files.items()
    ]
    record.append(f"{dist}/RECORD,,")
    wheel = dest / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
        archive.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    return wheel


def build_sdist(dest: Path, *, name: str = "fxsrc", version: str = "1.0.0") -> Path:
    """A package that ships only as source: it needs a build to install."""
    sdist = dest / f"{name}-{version}.tar.gz"
    body = f"from setuptools import setup\nsetup(name={name!r}, version={version!r})\n".encode()
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"{name}-{version}/setup.py")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return sdist


def build_npm_package(
    *,
    name: str = "fx-mcp",
    version: str = "1.0.0",
    marker: Path | None = None,
    bins: tuple[str, ...] = ("fx-mcp",),
    dependencies: dict[str, str] | None = None,
) -> bytes:
    """An npm tarball whose bins (``fx-mcp``) run NODE_SERVER. With ``marker``,
    its preinstall, install and postinstall scripts each write that file."""
    manifest: dict[str, object] = {
        "name": name, "version": version, "bin": dict.fromkeys(bins, "server.js"),
        **({"dependencies": dependencies} if dependencies else {}),
    }  # fmt: skip
    if marker is not None:
        script = f"node -e \"require('fs').writeFileSync('{marker}', 'ran')\""
        manifest["scripts"] = {"preinstall": script, "install": script, "postinstall": script}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for path, body, mode in (
            ("package/package.json", json.dumps(manifest).encode(), 0o644),
            ("package/server.js", NODE_SERVER.encode(), 0o755),
        ):
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(body), mode
            archive.addfile(info, io.BytesIO(body))
    return out.getvalue()


class Registry:
    """A loopback package registry: npm packuments and tarballs, and a PEP 503
    simple index for wheels and sdists. ``host`` is what an allowlist names
    (the test's fake DNS resolves ``*.test`` to loopback)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.host = f"registry.test:{port}"
        self.files: dict[str, bytes] = {}
        self.hits: list[str] = []

    @property
    def url(self) -> str:
        return f"http://{self.host}"

    def add_npm(self, tarball: bytes, *, name: str = "fx-mcp", version: str = "1.0.0") -> None:
        path = f"/{name}/-/{name}-{version}.tgz"
        self.files[path] = tarball
        dist = {
            "tarball": f"{self.url}{path}",
            "shasum": hashlib.sha1(tarball).hexdigest(),
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
        }
        with tarfile.open(fileobj=io.BytesIO(tarball)) as archive:
            member = archive.extractfile("package/package.json")
            assert member is not None
            manifest = json.loads(member.read())
        doc = {"name": name, "dist-tags": {"latest": version},
               "versions": {version: {**manifest, "dist": dist}}}  # fmt: skip
        self.files[f"/{name}"] = json.dumps(doc).encode()

    def add_python(self, artifact: Path, *, project: str) -> None:
        body = artifact.read_bytes()
        self.files[f"/files/{artifact.name}"] = body
        digest = hashlib.sha256(body).hexdigest()
        link = f'<a href="{self.url}/files/{artifact.name}#sha256={digest}">{artifact.name}</a>'
        self.files[f"/simple/{project}/"] = f"<html><body>{link}</body></html>".encode()


@contextmanager
def registry() -> Iterator[Registry]:
    holder: list[Registry] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            reg = holder[0]
            reg.hits.append(self.path)
            path = self.path.split("?")[0].replace("%2f", "/").replace("%2F", "/")
            body = reg.files.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            kind = "application/json" if not path.endswith((".tgz", ".whl", ".gz", "/")) else (
                "text/html" if path.endswith("/") else "application/octet-stream")  # fmt: skip
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder.append(Registry(server.server_address[1]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()
