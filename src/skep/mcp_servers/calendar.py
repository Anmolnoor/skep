"""calendar — first-party read-only ICS MCP server (v72-F5), stdlib only.

Reads ONE ICS source (a local file or an https URL — e.g. a Google/Proton
"secret address" export) and answers "what's coming up". Read-only by
construction: there is no write tool. Register under the plain ``mcp``
scope; the tools are read-shaped and flow as reads.

Config (environment):
  SKEP_CALENDAR_ICS  path or https URL of the .ics source (required)

Parser ceiling, stated (the v29 naive-tag-strip precedent): DTSTART/DTEND/
SUMMARY plus RRULE FREQ=DAILY|WEEKLY only; other RRULEs surface as a single
occurrence at DTSTART with a "(recurring)" marker, TZIDs are treated as
naive local time. Upgrade path: a real rrule expander if the field record
shows misses.

Run: python -m skep.mcp_servers.calendar
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from typing import Any

_FETCH_CAP_BYTES = 512 * 1024
_FETCH_TIMEOUT = 10.0
_MAX_DAYS = 60
_MAX_LINES = 40

TOOLS: list[dict[str, Any]] = [
    {
        "name": "upcoming_events",
        "description": "List calendar events in the next N days, one per line: "
        "'YYYY-MM-DD HH:MM — summary' (all-day events show 'all day'). "
        "Arguments: {days?: int (default 7, max 60)}. Example: {name: "
        "upcoming_events, arguments: {days: 3}}. Read-only.",
        "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}},
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check: parses an embedded "
        "fixture calendar (including a daily recurrence) with no network.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@dataclass(frozen=True)
class VEvent:
    start: dt.datetime
    all_day: bool
    summary: str
    rrule_freq: str | None  # DAILY | WEEKLY | other-marker | None


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a line starting with space/tab continues the
    previous line."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str) -> tuple[dt.datetime, bool]:
    value = value.strip().rstrip("Z")
    if "T" in value:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%S"), False
    return dt.datetime.strptime(value, "%Y%m%d"), True


def parse_ics(text: str) -> list[VEvent]:
    events: list[VEvent] = []
    start: dt.datetime | None = None
    all_day = False
    summary = ""
    rrule: str | None = None
    in_event = False
    for line in _unfold(text):
        name, _, value = line.partition(":")
        key = name.split(";", 1)[0].upper()
        if key == "BEGIN" and value.strip().upper() == "VEVENT":
            in_event, start, all_day, summary, rrule = True, None, False, "", None
        elif key == "END" and value.strip().upper() == "VEVENT":
            if in_event and start is not None:
                events.append(
                    VEvent(
                        start=start,
                        all_day=all_day,
                        summary=summary or "(no title)",
                        rrule_freq=rrule,
                    )
                )
            in_event = False
        elif not in_event:
            continue
        elif key == "DTSTART":
            try:
                start, all_day = _parse_dt(value)
            except ValueError:
                start = None
        elif key == "SUMMARY":
            summary = value.strip()
        elif key == "RRULE":
            fields: dict[str, str] = {}
            for part in value.upper().split(";"):
                field_name, _, field_value = part.partition("=")
                if field_value:
                    fields[field_name] = field_value
            rrule = fields.get("FREQ") or "OTHER"
    return events


def occurrences(
    event: VEvent, window_start: dt.datetime, window_end: dt.datetime
) -> list[tuple[dt.datetime, str]]:
    """Project one event into the window. DAILY/WEEKLY step from DTSTART;
    other RRULEs degrade honestly to one '(recurring)' line at DTSTART."""
    if event.rrule_freq in ("DAILY", "WEEKLY"):
        step = dt.timedelta(days=1 if event.rrule_freq == "DAILY" else 7)
        current = event.start
        if current < window_start:  # jump near the window in one hop
            behind = (window_start - current) // step
            current = current + behind * step
        found: list[tuple[dt.datetime, str]] = []
        while current <= window_end:
            if current >= window_start:
                found.append((current, event.summary))
            current += step
        return found
    label = event.summary if event.rrule_freq is None else f"{event.summary} (recurring)"
    if window_start <= event.start <= window_end:
        return [(event.start, label)]
    return []


def _now() -> dt.datetime:  # overridable in tests
    return dt.datetime.now()


def _load_source() -> str:
    source = os.environ.get("SKEP_CALENDAR_ICS", "").strip()
    if not source:
        raise ValueError(
            "calendar is not configured: set SKEP_CALENDAR_ICS to an .ics path or "
            "https URL (docs/assistant-tools.md)"
        )
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=_FETCH_TIMEOUT) as response:
            raw: bytes = response.read(_FETCH_CAP_BYTES)
            return raw.decode("utf-8", errors="replace")
    with open(os.path.expanduser(source), encoding="utf-8", errors="replace") as handle:
        return handle.read(_FETCH_CAP_BYTES)


def upcoming_events(arguments: dict[str, Any]) -> dict[str, Any]:
    days = min(max(int(arguments.get("days") or 7), 1), _MAX_DAYS)
    try:
        text = _load_source()
    except (OSError, ValueError) as exc:
        return {"content": str(exc), "isError": True}
    window_start = _now()
    window_end = window_start + dt.timedelta(days=days)
    rows: list[tuple[dt.datetime, str, bool]] = []
    for event in parse_ics(text):
        window_floor = (
            window_start.replace(hour=0, minute=0, second=0, microsecond=0)
            if event.all_day
            else window_start
        )
        for when, label in occurrences(event, window_floor, window_end):
            rows.append((when, label, event.all_day))
    rows.sort(key=lambda row: row[0])
    lines = [
        f"{when:%Y-%m-%d} {'all day' if all_day else f'{when:%H:%M}'} — {label}"
        for when, label, all_day in rows[:_MAX_LINES]
    ]
    if len(rows) > _MAX_LINES:
        lines.append(f"(+{len(rows) - _MAX_LINES} more in this window)")
    return {"content": "\n".join(lines) or f"nothing in the next {days} day(s)"}


_FIXTURE = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260101T090000
SUMMARY:standup
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
DTSTART:20260103
SUMMARY:launch day
END:VEVENT
END:VCALENDAR
"""


def self_test(_arguments: dict[str, Any]) -> dict[str, Any]:
    events = parse_ics(_FIXTURE)
    window = (dt.datetime(2026, 1, 2), dt.datetime(2026, 1, 5))
    daily = occurrences(events[0], *window)
    single = occurrences(events[1], *window)
    if len(events) == 2 and len(daily) == 3 and len(single) == 1:
        return {"content": "self_test passed: ICS parse + daily recurrence intact"}
    return {
        "content": f"parse broken: {len(events)} events, {len(daily)} daily, {len(single)} single",
        "isError": True,
    }


_HANDLERS = {"upcoming_events": upcoming_events, "self_test": self_test}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {
            "content": f"no tool named {name!r}; tools: {', '.join(_HANDLERS)}",
            "isError": True,
        }
    return handler(arguments)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        result: dict[str, Any]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(str(params.get("name")), params.get("arguments") or {})
            except Exception as exc:  # a tool error is a reply, never a crash
                result = {"content": f"{type(exc).__name__}: {exc}", "isError": True}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)


if __name__ == "__main__":
    main()
