# The browser is an MCP server

skep does not ship a browser stack. Browser automation arrives the same way
every external capability does: an MCP server the operator registers, whose
tools decide under the one policy engine (invariant I5). The reference
server is Playwright's official one.

## Register it

From chat (the Queen proposes, you confirm the card), or ask for exactly:

```
register_mcp_server
  server_id: playwright
  transport: stdio
  command: ["npx", "@playwright/mcp@latest"]
  scope: browse
```

Requirements: `npx` on PATH (npm ships it). `skep doctor` warns if a
registered browse server's launcher is missing. No npm? Run the server any
other way that yields a stdio argv (a container wrapper works: podman on
this machine) and register that argv instead.

## What the `browse` scope means

Tools on a browse-bound server decide as `browse/read` or `browse/act`
(v71-F2, mirroring the v41-F3 email precedent):

- **read** — page-STATE reads: `snapshot`, `screenshot`, `console`,
  `network_request`, `wait_for` shaped names that the server itself marks
  read-only (`readOnlyHint` in its tools/list) and that carry no act verb
  (`submit`, `click`, `fill`, …) anywhere in the name (ES-F54). skep asks the
  server's own tools/list for the hint once per registration (ES-F160:
  registering the server again, or restarting serve, asks afresh). A
  Playwright server (`@playwright/mcp`) that sends no hint for a tool falls
  back to skep's list of its page-state reads (ES-F161): `browser_snapshot`,
  `browser_take_screenshot`, `browser_console_messages`,
  `browser_network_requests`, `browser_network_request`, `browser_wait_for`;
  a hint the server does send always wins. Any other server that does not
  send it, or a list that fails, makes every one of its tools an act (a
  card). These run inside the turn
  unless a policy rule asks for them: the builder and operator baselines
  carry `browse-read-gated` (ask), the assistant baseline `browse-read`
  (allow), and a chat's role tightens the same way (ADR 0059 P3-3).
- **act** — everything else, navigation included: `navigate`, `click`,
  `type`, `fill`, `press`, `select`, `evaluate`, `tabs`, uploads. Each
  cards until you allow it. Navigation cards deliberately — it is an
  outbound fetch, and `read_url` set the parity: every fetch is one
  operator decision.

Each call starts the server, waits for its answer, then stops it (one
process per call). A page does not carry over from one call to the next:
`browser_navigate` answers with the snapshot of the page it opened, and a
later `browser_snapshot` starts on `about:blank`. The server runs in
`~/.skep/browser/`, never in the folder serve runs from: Playwright writes
its snapshot and console files to `.playwright-mcp/` there, and a read's
optional `filename` can only land inside that folder.

Teach it your posture with `allow_mcp_tool` (writes a learned
`browse`-scope rule; a policy deny always wins and refuses without a
card), e.g. allow `playwright:browser_navigate` once you trust the flow.

## The walls that do not move

- The Queen still never holds the trigger: an unallowed act is a card,
  never a default (I6).
- Workers get nothing from this: the browse scope is a Queen-side surface;
  worker capabilities and the git guards (I4) are untouched.
- Forged tools (the v71-F1 forge) may NOT shell out to a browser
  themselves — a tool that needs the web goes through this same door.
