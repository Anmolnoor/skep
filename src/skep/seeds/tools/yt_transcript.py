"""yt_transcript — fetch a YouTube video's transcript as plain text.

v83-F14: a shipped seed tool in the forge's single-file stdio MCP format
(stdlib only, JSON-RPC over stdio, self_test offline). Read-only; egress
is youtube.com only, and the tool refuses any other host itself. The
youtubei/timedtext surface is UNOFFICIAL and unversioned — when it
drifts, the error says so plainly instead of mystifying (the tool then
needs re-forging).
"""

import html
import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

UPSTREAM_DRIFT = (
    "the YouTube page no longer matches the shape this tool knows — the "
    "unofficial endpoint changed; the tool needs re-forging (forge_tool a "
    "replacement). If the video simply has no captions, there is nothing "
    "to fetch."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "yt_transcript",
        "description": "Fetch a YouTube video's transcript as plain text. "
        "Arguments: {video: string} — a watch URL, youtu.be link, or bare "
        "11-char video id. Example: {name: yt_transcript, arguments: "
        "{video: dQw4w9WgXcQ}}. Read-only; youtube.com only.",
        "inputSchema": {
            "type": "object",
            "properties": {"video": {"type": "string"}},
            "required": ["video"],
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check; exercises the "
        "caption-track extraction and timedtext parsing on fixtures.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(value: str) -> str:
    value = value.strip()
    if _ID_RE.match(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        if not candidate and parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
    else:
        candidate = ""
    if not _ID_RE.match(candidate or ""):
        raise ValueError(
            "could not extract a video id — pass a youtube.com/youtu.be URL "
            "or the bare 11-character id"
        )
    return candidate


def _fetch(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        raise ValueError("refusing non-youtube host " + repr(host))
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 skep-yt-transcript"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return str(response.read().decode("utf-8", errors="replace"))


def caption_track_url(page: str) -> str | None:
    """The first captionTracks baseUrl in a watch page, or None."""
    marker = page.find('"captionTracks":')
    if marker == -1:
        return None
    start = page.find("[", marker)
    depth = 0
    for index in range(start, min(len(page), start + 200_000)):
        if page[index] == "[":
            depth += 1
        elif page[index] == "]":
            depth -= 1
            if depth == 0:
                try:
                    tracks = json.loads(page[start : index + 1])
                except ValueError:
                    return None
                for track in tracks:
                    base = track.get("baseUrl") if isinstance(track, dict) else None
                    if base:
                        return str(base).replace("\\u0026", "&")
                return None
    return None


def timedtext_to_text(xml: str) -> str:
    """<text ...>chunks</text> -> readable lines (entities unescaped)."""
    chunks = re.findall(r"<text[^>]*>(.*?)</text>", xml, flags=re.DOTALL)
    lines = []
    for chunk in chunks:
        text = html.unescape(html.unescape(chunk)).replace("\n", " ").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


# Offline fixtures for self_test — the parsing logic, exercised for real.
_FIXTURE_PAGE = (
    '... "captions":{"playerCaptionsTracklistRenderer":{"captionTracks":'
    '[{"baseUrl":"https://www.youtube.com/api/timedtext?v=x\\u0026lang=en",'
    '"languageCode":"en"}]}} ...'
)
_FIXTURE_XML = (
    "<transcript><text start=\"0\" dur=\"2\">Hello &amp;amp; welcome</text>"
    "<text start=\"2\" dur=\"3\">to the demo</text></transcript>"
)


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "yt_transcript":
        try:
            vid = video_id(str(arguments.get("video") or ""))
        except ValueError as exc:
            return {"content": str(exc), "isError": True}
        try:
            page = _fetch("https://www.youtube.com/watch?v=" + vid)
        except Exception as exc:
            return {"content": "fetch failed: " + str(exc), "isError": True}
        track = caption_track_url(page)
        if track is None:
            return {"content": UPSTREAM_DRIFT, "isError": True}
        try:
            xml = _fetch(track)
        except Exception as exc:
            return {"content": "caption fetch failed: " + str(exc), "isError": True}
        text = timedtext_to_text(xml)
        if not text:
            return {"content": UPSTREAM_DRIFT, "isError": True}
        return {"content": text}
    if name == "self_test":
        track = caption_track_url(_FIXTURE_PAGE)
        if track != "https://www.youtube.com/api/timedtext?v=x&lang=en":
            return {"content": "captionTracks extraction broken", "isError": True}
        text = timedtext_to_text(_FIXTURE_XML)
        if text != "Hello & welcome\nto the demo":
            return {"content": "timedtext parsing broken: " + repr(text), "isError": True}
        try:
            video_id("https://youtu.be/dQw4w9WgXcQ")
        except ValueError:
            return {"content": "video id extraction broken", "isError": True}
        return {"content": "self_test passed: extraction + parsing intact (offline)"}
    known = ", ".join(str(tool["name"]) for tool in TOOLS)
    return {"content": "no tool named " + repr(name) + "; tools: " + known, "isError": True}


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
                result = {"content": type(exc).__name__ + ": " + str(exc), "isError": True}
        else:
            result = {}
        print(
            json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}),
            flush=True,
        )


if __name__ == "__main__":
    main()
