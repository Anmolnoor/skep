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
  feeds, each read at most 512 KiB and ITEM_CAP items (in feed order); with
  `--topic`, only titles naming one of the words. These and open-meteo are
  the only hosts this file contacts itself. A redirect is not followed (the
  section names the new URL), a feed with a DTD is refused, and every
  remote field prints as one line, links only when http(s);
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
to the ones after it), so a long feed is clipped, not the mail after it, and
a fair share of the lane's 120 s (a source slow to answer or trickling bytes
is cut off at its own deadline, by SIGALRM, so Linux and macOS only). Each
section is flushed as it finishes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import signal
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any
from xml.parsers import expat

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
CAP_NOTE = "(output cap reached: the remaining sections are left out)"
TIME_BUDGET = 100.0  # seconds of the lane's 120 for every network source together
SOCKET_TIMEOUT = 10.0
LABEL_WIDTH = 60  # a section heading's label, at most
NOTE_WIDTH = 120  # a failed section's one "(unavailable: ...)" line, at most


class TimeShareSpent(BaseException):
    """A source's whole fetch (connect, headers, body) passed its time share.
    Raised from SIGALRM. A BaseException, so no layer below takes it for an
    ordinary failure: create_connection retries the next address on OSError,
    and a mail digest reads "one bad message" on Exception."""


def _out_of_time(*_args: object) -> None:
    raise TimeShareSpent("the source did not finish in its time share")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect fails the source, naming where it points, so the operator
    updates the URL; nothing is followed and no redirect body is read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError(f"redirected ({code}) to {_one_line(newurl)}: use that URL")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url: str, what: str) -> bytes:
    body = bytearray()
    with _OPENER.open(url, timeout=SOCKET_TIMEOUT) as response:
        while chunk := response.read1(FETCH_CAP_BYTES + 1 - len(body)):
            body += chunk
            if len(body) > FETCH_CAP_BYTES:
                raise ValueError(f"the {what} answer passed {FETCH_CAP_BYTES // 1024} KiB")
    return bytes(body)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _one_line(text: object) -> str:
    """One printable line: control characters and newlines become spaces."""
    printable = "".join(c if c.isprintable() else " " for c in str(text or ""))
    return " ".join(printable.split())


def _web_link(url: object) -> str:
    link = _one_line(url)
    try:
        return link if urllib.parse.urlsplit(link).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def _news(items: list[tuple[str, str, str]], topics: tuple[str, ...], count: int) -> str:
    """``items`` are (title, suffix, link); ``topics`` match the title only."""
    words = [topic.lower() for topic in topics]
    kept = [item for item in items if not words or any(w in item[0].lower() for w in words)]
    lines = [
        f"- {title}{suffix}" + (f" — {link}" if link else "")
        for title, suffix, link in kept[: max(1, min(count, ITEM_CAP))]
    ]
    return "\n".join(lines) or f"(nothing matching {', '.join(topics)})"


def hacker_news(count: int, topics: tuple[str, ...]) -> str:
    asked = HN_POOL if topics else max(1, min(count, ITEM_CAP))
    hits = json.loads(_get(HN_URL.format(count=asked), "Hacker News"))["hits"]
    items = [
        (
            _one_line(hit.get("title")),
            f" ({_one_line(hit.get('points', 0))} points)",
            _web_link(
                hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            ),
        )
        for hit in hits
    ]
    return _news(items, topics, count)


def _refuse_dtd(body: bytes) -> None:
    """A feed never needs a DTD; refusing one keeps entity expansion from
    growing past the byte cap in memory."""

    def refuse(*_args: object) -> None:
        raise ValueError("the feed declares a DTD (<!DOCTYPE>), which this reader refuses")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.Parse(body, True)


def feed(url: str, topics: tuple[str, ...]) -> str:
    """An RSS 2.0 or Atom feed's first titles, in feed order. ElementTree
    resolves no external entities, the byte cap bounds what expat is handed,
    and a DTD is refused before ElementTree sees it."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"--rss wants an http(s) URL, got {_one_line(url)!r}")
    body = _get(url, "feed")
    _refuse_dtd(body)
    root = ET.fromstring(body)
    items: list[tuple[str, str, str]] = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = link = ""
        for child in entry:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title":
                title = _one_line(child.text)
            elif tag == "link" and not link and child.get("rel", "alternate") == "alternate":
                link = _web_link(child.get("href") or child.text)  # Atom | RSS
        items.append((title, "", link))
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


def _feed_label(url: str) -> str:
    try:
        return f"Feed {_clip(_one_line(urllib.parse.urlsplit(url).hostname or url), LABEL_WIDTH)}"
    except ValueError:  # e.g. an unmatched bracket in the host: feed() says why
        return "Feed (malformed URL)"


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
            (_feed_label(url), lambda u=url: feed(u, topics), url)
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
    print(header, flush=True)
    failed = False
    end = time.monotonic() + TIME_BUDGET
    # Every print adds a newline; CAP_NOTE's line is always kept in reserve.
    left = OUTPUT_CAP - (len(header) + 1) - (len(CAP_NOTE) + 1)
    previous = signal.signal(signal.SIGALRM, _out_of_time)
    try:
        for index, (title, read) in enumerate(wanted):
            remaining = len(wanted) - index
            share = max(0, left // remaining)  # a short section leaves its rest to the next
            if share < len(f"\n## {title}\n\n") + NOTE_WIDTH:  # room to say why it failed
                print(CAP_NOTE, flush=True)
                break
            try:
                try:
                    seconds = max(0.0, end - time.monotonic()) / remaining
                    signal.setitimer(signal.ITIMER_REAL, max(0.01, seconds))
                    body = read()
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
            except (Exception, TimeShareSpent) as exc:  # one dead source never hides the others
                why = _clip(f"{type(exc).__name__}: {_one_line(exc)}", NOTE_WIDTH - 15)
                body, failed = f"(unavailable: {why})", True
            block = f"\n## {title}\n{body}"
            if len(block) + 1 > share:
                block = block[: share - len(" …\n")] + " …"
            print(block, flush=True)
            left -= len(block) + 1
    finally:
        signal.signal(signal.SIGALRM, previous)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
