from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any


def render_dashboard(status: Mapping[str, Any]) -> str:
    provider = status.get("provider", {})
    queen = status.get("queen", {})
    workers = status.get("workers", {})
    approvals = status.get("approvals", {})
    memory = status.get("memory", {})
    required = status.get("required", {})
    planned = status.get("planned", {})

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Skep</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18202a;
      --muted: #607080;
      --line: #d7dde4;
      --panel: #ffffff;
      --bg: #f6f4ef;
      --ready: #167c55;
      --blocked: #b23636;
      --planned: #6d5fa8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
        sans-serif;
    }}
    main {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0 32px;
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
      margin-bottom: 18px;
    }}
    h1 {{
      font-size: 28px;
      line-height: 1.1;
      margin: 0 0 6px;
      letter-spacing: 0;
    }}
    p {{ margin: 0; color: var(--muted); }}
    .status {{
      min-width: 124px;
      text-align: center;
      padding: 10px 14px;
      border: 1px solid var(--line);
      background: var(--panel);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 13px;
    }}
    .status.ready {{ color: var(--ready); }}
    .status.blocked {{ color: var(--blocked); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 14px;
      min-height: 148px;
    }}
    h2 {{
      margin: 0 0 10px;
      font-size: 14px;
      letter-spacing: 0;
    }}
    dl {{
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr);
      gap: 8px 10px;
      margin: 0;
      font-size: 14px;
    }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    .planned {{ color: var(--planned); font-weight: 650; }}
    .checks {{
      grid-column: 1 / -1;
      min-height: auto;
    }}
    .check-list {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .check {{
      border: 1px solid var(--line);
      padding: 10px;
      min-height: 86px;
    }}
    .check strong {{ display: block; margin-bottom: 4px; }}
    @media (max-width: 760px) {{
      header {{ align-items: stretch; flex-direction: column; }}
      .status {{ text-align: left; }}
      .grid, .check-list {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Skep</h1>
        <p>Local manager status for one owner, one hive, and one Queen.</p>
      </div>
      <div class="status {_esc(status.get("overall"))}">{_esc(status.get("overall"))}</div>
    </header>
    <div class="grid">
      <section>
        <h2>Provider</h2>
        <dl>
          <dt>Name</dt><dd>{_esc(provider.get("name") or "unconfigured")}</dd>
          <dt>Model</dt><dd>{_esc(provider.get("model") or "unconfigured")}</dd>
          <dt>Health</dt><dd>{_esc(required.get("provider", {}).get("status", "blocked"))}</dd>
        </dl>
      </section>
      <section>
        <h2>Queen</h2>
        <dl>
          <dt>User</dt><dd>{_esc(queen.get("user_id") or "not set")}</dd>
          <dt>Hive</dt><dd>{_esc(queen.get("hive_id") or "not set")}</dd>
          <dt>Queen</dt><dd>{_esc(queen.get("queen_id") or "not set")}</dd>
        </dl>
      </section>
      <section>
        <h2>Workers</h2>
        <dl>
          {_worker_rows(workers)}
        </dl>
      </section>
      <section>
        <h2>Approvals</h2>
        <dl>
          <dt>Pending</dt><dd>{_esc(approvals.get("pending", 0))}</dd>
          <dt>Status</dt><dd>{_esc(approvals.get("status", "ready"))}</dd>
        </dl>
      </section>
      <section>
        <h2>Memory</h2>
        <dl>
          <dt>Status</dt><dd>{_esc(memory.get("status", "blocked"))}</dd>
          <dt>Path</dt><dd>{_esc(memory.get("path", "not set"))}</dd>
        </dl>
      </section>
      <section class="checks">
        <h2>Required Checks</h2>
        <div class="check-list">
          {_required_checks(required)}
        </div>
      </section>
      {_planned_section(planned)}
    </div>
  </main>
</body>
</html>
"""


def _worker_rows(workers: Mapping[str, Any]) -> str:
    rows = []
    for name, item in workers.items():
        label = item.get("label", "unknown")
        css = "planned" if item.get("status") == "planned" else ""
        rows.append(f'<dt>{_esc(name)}</dt><dd class="{css}">{_esc(label)}</dd>')
    return "\n          ".join(rows)


def _required_checks(required: Mapping[str, Any]) -> str:
    rows = []
    for name, check in required.items():
        rows.append(
            '<div class="check">'
            f"<strong>{_esc(name)}: {_esc(check.get('status'))}</strong>"
            f"<p>{_esc(check.get('detail'))}</p>"
            "</div>"
        )
    return "\n          ".join(rows)


def _planned_section(planned: Mapping[str, Any]) -> str:
    if not planned:
        return ""

    rows = []
    for name, item in planned.items():
        rows.append(
            '<div class="check">'
            f"<strong>{_esc(name)}: {_esc(item.get('status'))}</strong>"
            f"<p>{_esc(item.get('detail'))}</p>"
            "</div>"
        )
    return (
        '<section class="checks">'
        "<h2>Not connected yet</h2>"
        '<div class="check-list">'
        f"{''.join(rows)}"
        "</div>"
        "</section>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)
