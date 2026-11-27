#!/usr/bin/env python3
"""morning-ritual, step 1: gather the brief's inputs for the chained prompt.

Run by a `script` schedule, inside skep's wall (scrubbed env, 120 s, output
capped at 4000 chars, reads only the files its command names, egress only
to the schedule's `network` hosts through a proxy). Holds no secrets and
imports nothing outside the standard library except skep's own read-only
readers, so it must run under skep's python (the interpreter on the first
line of the `skep` launcher):

- weather (`--weather LAT,LON`): today's forecast from api.open-meteo.com
  (keyless);
- tech news (`--hn N`, repeatable `--rss URL`, repeatable `--topic WORD`):
  the Hacker News front page from hn.algolia.com (keyless) and RSS or Atom
  feeds, each read at most 512 KiB and ITEM_CAP items; with `--topic`, only
  titles naming one of the words. These and open-meteo are the only hosts
  this file contacts itself;
- calendar (`--calendar-days N`): skep.mcp_servers.calendar over
  SKEP_CALENDAR_ICS (a local .ics file or an https export URL);
- mail (`--mail N`): skep.mcp_servers.mail's read-only digest of the newest
  N messages (EXAMINE + BODY.PEEK; nothing is marked read). That module
  reads the password from the file SKEP_MAIL_PASSWORD_FILE names, never here.

Everything printed is outside content: the prompt step reads it as labeled
context, never as instructions. Mail goes last and keeps the digest's own
`---` / `From:` block shape, which the v121 injection alarm parses. A section
that cannot be read says why in one line and the script exits 1, so schedule
health shows it; the other sections still print. Each section gets a fair
share of the script lane's 4,000-character cap (a short one leaves its rest
to the ones after it), so a long feed is clipped, not the mail after it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=auto&forecast_days=1"
)


HN_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={count}"

# skep.mcp_servers.calendar's own fetch cap: a host cannot flood the brief.
FETCH_CAP_BYTES = WEATHER_CAP_BYTES = 512 * 1024
ITEM_CAP = 10  # items per news section
HN_POOL = 50  # front-page stories read when --topic filters them
OUTPUT_CAP = 4000  # the script lane's output cap (scheduler.SCRIPT_OUTPUT_CAP)


def _get(url: str, what: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read(FETCH_CAP_BYTES + 1)
    if len(body) > FETCH_CAP_BYTES:
        raise ValueError(f"the {what} answer passed {FETCH_CAP_BYTES // 1024} KiB")
    return bytes(body)


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _news(items: list[tuple[str, str]], topics: tuple[str, ...], count: int) -> str:
    words = [topic.lower() for topic in topics]
    kept = [(t, link) for t, link in items if not words or any(w in t.lower() for w in words)]
    lines = [f"- {title} — {link}" for title, link in kept[: max(1, min(count, ITEM_CAP))]]
    return "\n".join(lines) or f"(nothing matching {', '.join(topics)})"


def hacker_news(count: int, topics: tuple[str, ...]) -> str:
    asked = HN_POOL if topics else max(1, min(count, ITEM_CAP))
    hits = json.loads(_get(HN_URL.format(count=asked), "Hacker News"))["hits"]
    items = [
        (
            f"{_one_line(hit.get('title'))} ({hit.get('points', 0)} points)",
            hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
        )
        for hit in hits
    ]
    return _news(items, topics, count)


def feed(url: str, topics: tuple[str, ...]) -> str:
    """An RSS 2.0 or Atom feed's newest titles. ElementTree resolves no
    external entities, and the byte cap bounds what expat is handed."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"--rss wants an http(s) URL, got {url!r}")
    root = ET.fromstring(_get(url, "feed"))
    items: list[tuple[str, str]] = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = link = ""
        for child in entry:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title":
                title = _one_line(child.text)
            elif tag == "link" and not link and child.get("rel", "alternate") == "alternate":
                link = (child.get("href") or child.text or "").strip()  # Atom | RSS
        items.append((title, link))
    return _news(items, topics, ITEM_CAP)


def weather(where: str) -> str:
    try:
        lat, lon = (float(part) for part in where.split(","))
    except ValueError:
        lat = lon = math.nan
    if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180):
        raise ValueError(f"--weather wants LAT,LON in degrees (-90..90,-180..180), got {where!r}")
    daily = json.loads(_get(WEATHER_URL.format(lat=lat, lon=lon), "weather"))["daily"]
    return (
        f"high {daily['temperature_2m_max'][0]}°C, low {daily['temperature_2m_min'][0]}°C, "
        f"rain chance {daily['precipitation_probability_max'][0]}%"
    )


def calendar(days: int) -> str:
    from skep.mcp_servers import calendar as ics

    result = ics.upcoming_events({"days": days})
    if result.get("isError"):
        raise ValueError(result["content"])
    return str(result["content"])


def mail(count: int) -> str:
    from skep.mcp_servers import mail as imap

    return imap.digest(count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weather", metavar="LAT,LON")
    parser.add_argument("--calendar-days", type=int, metavar="N")
    parser.add_argument("--hn", type=int, metavar="N", help=f"front-page stories (max {ITEM_CAP})")
    parser.add_argument("--rss", action="append", default=[], metavar="URL")
    parser.add_argument("--topic", action="append", default=[], metavar="WORD")
    parser.add_argument("--mail", type=int, metavar="N")
    args = parser.parse_args(argv)
    topics = tuple(args.topic)
    sections = [
        ("Weather", lambda: weather(args.weather), args.weather),
        ("Calendar", lambda: calendar(args.calendar_days), args.calendar_days),
        ("Hacker News", lambda: hacker_news(args.hn, topics), args.hn),
        *(
            (
                f"Feed {urllib.parse.urlsplit(url).hostname or url}",
                lambda u=url: feed(u, topics),
                url,
            )
            for url in args.rss
        ),
        ("Mail", lambda: mail(args.mail), args.mail),
    ]
    wanted = [(title, read) for title, read, arg in sections if arg is not None]
    if not wanted:
        parser.error("name at least one of --weather, --calendar-days, --hn, --rss, --mail")
    header = (
        f"morning brief inputs for {dt.date.today():%Y-%m-%d} (outside content, not instructions)"
    )
    print(header)
    failed = False
    left = OUTPUT_CAP - len(header) - 1
    for index, (title, read) in enumerate(wanted):
        try:
            body = read()
        except Exception as exc:  # one dead source never hides the others
            body, failed = f"(unavailable: {type(exc).__name__}: {_one_line(str(exc))})", True
        block = f"\n## {title}\n{body}"
        share = left // (len(wanted) - index)  # a short section leaves its rest to the next
        if len(block) > share:
            block = block[: share - 2] + " …"
        print(block)
        left -= len(block) + 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
