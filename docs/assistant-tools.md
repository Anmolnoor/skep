# Assistant tools — first-party mail and calendar (v72-F5)

Two stdlib-only stdio MCP servers ship in the skep package. They hold ZERO
permission logic: the operator registers them like any community server,
and the existing MCP scopes decide what flows and what cards (I5). They
follow the forge contract (single file, JSON-RPC lines, zero-argument
`self_test`), so `python -m skep.mcp_servers.mail` is drivable by the same
harness that trials forged tools.

## Mail (`scope=email` — v41-F3 finally has its server)

Register from chat (a card) or the API:

    register_mcp_server server_id=mail transport=stdio
      command=["python","-m","skep.mcp_servers.mail"] scope=email

Under the email scope: `list_recent` and `read_message` classify as
`email/read` and run in-turn once granted (`allow_mcp_tool`);
`send_message` classifies as `email/send` and **always cards** — nothing
sends without your verdict (I6).

Config, in `skep serve`'s environment:

| variable | meaning |
|---|---|
| `SKEP_MAIL_IMAP_HOST` / `_IMAP_PORT` | IMAP host (reads); port defaults 993 |
| `SKEP_MAIL_SMTP_HOST` / `_SMTP_PORT` | SMTP host (send); port defaults 465 |
| `SKEP_MAIL_USER` | login, and the default From |
| `SKEP_MAIL_FROM` | From override |
| `SKEP_MAIL_PASSWORD` | password — or omit and use the secret file |

The secret file is the house pattern (the `llm-secret` posture): write the
password to `<SKEP_HOME>/supervisor/mail-secret`, `chmod 600` — never
SQLite, never a command argument, never visible in `ps`. Use an
app-specific password where your provider offers one.

## Calendar (plain `mcp` scope — read-only by construction)

    register_mcp_server server_id=calendar transport=stdio
      command=["python","-m","skep.mcp_servers.calendar"]

One env var: `SKEP_CALENDAR_ICS` = a local `.ics` path or an https URL
(e.g. a Google Calendar "secret address" export). `upcoming_events(days)`
is the only real tool; there is no write tool to grant.

Stated ceiling (the v29 posture — honest, with an upgrade path): the
parser handles DTSTART/DTEND/SUMMARY and RRULE `FREQ=DAILY|WEEKLY`; other
recurrences render once, marked `(recurring)`, and TZIDs are treated as
naive local time.

## The pattern for everything else

Email and calendar are the two the daily-driver record demanded. For any
other integration: registry first (an existing community MCP server via
`register_mcp_server`), and where none exists, the forge (`forge_tool`)
authors one under the same trial + card + registration discipline. No
integration ever ships its own permission system.
