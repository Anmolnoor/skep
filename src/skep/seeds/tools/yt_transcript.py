"""yt_transcript — fetch a YouTube video's transcript as plain text.

v83-F14: a shipped seed tool in the forge's single-file stdio MCP format
(stdlib only, JSON-RPC over stdio, self_test offline). Read-only; egress
is youtube.com only, and the tool refuses any other host itself. The
youtubei/timedtext surface is UNOFFICIAL and unversioned — when it
drifts, the error says so plainly instead of mystifying (the tool then
needs re-forging). v126-F1: captions come from the innertube player
endpoint; no captions, an empty (blocked) caption body and page drift
are three different errors.
"""

# Phase 07: the hosts its promotion card approves; it reaches nothing else.
# skep-hosts: *.youtube.com

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
    "replacement)."
)
# v126-F1: the three failures are told apart, so the model can say which.
NO_CAPTIONS = "NO_CAPTIONS: this video has no caption tracks — there is no transcript to fetch."
EMPTY_CAPTIONS = (
    "EMPTY_CAPTIONS: YouTube listed a caption track but answered with an "
    "empty body — it is blocking this client right now (rate limit or bot "
    "check). Try again later; the tool itself is not broken."
)
# v126-F1: the watch page's own captionTracks URL carries `exp=xpe` and now
# returns 200 with a 0-byte body; the innertube player endpoint asked as the
# ANDROID client returns caption URLs that still answer (the method
# youtube-transcript-api uses; verified live 2026-11-18).
_PLAYER_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_transcript",
        "description": "Fetch a YouTube video's transcript as plain text. "
        "Arguments: {video: string} — a watch URL, youtu.be link, or bare "
        "11-char video id. Example: {name: get_transcript, arguments: "
        "{video: dQw4w9WgXcQ}}. Read-only; youtube.com only.",
        "annotations": {"readOnlyHint": True},  # B09: a read verb + the hint
        "inputSchema": {
            "type": "object",
            "properties": {"video": {"type": "string"}},
            "required": ["video"],
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check; exercises the "
        "player caption-track extraction and timedtext parsing on fixtures.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _is_youtube(host: str) -> bool:
    return host == "youtube.com" or host.endswith(".youtube.com")


def video_id(value: str) -> str:
    value = value.strip()
    if _ID_RE.match(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif _is_youtube(host):
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


def _fetch(url: str, data: bytes | None = None) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "youtu.be" or _is_youtube(host)):
        raise ValueError("refusing non-youtube host " + repr(host))
    headers = {"User-Agent": "Mozilla/5.0 skep-yt-transcript", "Accept-Language": "en-US"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return str(response.read().decode("utf-8", errors="replace"))


def innertube_key(page: str) -> str | None:
    """The watch page's INNERTUBE_API_KEY, or None (page drift)."""
    match = re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', page)
    return match.group(1) if match else None


def caption_track_url(player: Any) -> str | None:
    """An English (else the first) caption track URL from a player response,
    or None when the video has no tracks. Raises ValueError on drift."""
    if not isinstance(player, dict):
        raise ValueError("player response is not an object")
    renderer = (player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    tracks = [t for t in renderer.get("captionTracks") or [] if isinstance(t, dict)]
    tracks = [t for t in tracks if t.get("baseUrl")]
    if not tracks:
        return None
    english = [t for t in tracks if str(t.get("languageCode", "")).startswith("en")]
    return str((english or tracks)[0]["baseUrl"]).replace("&fmt=srv3", "")


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
_FIXTURE_PLAYER = {
    "playabilityStatus": {"status": "OK"},
    "captions": {
        "playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {
                    "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=de",
                    "languageCode": "de",
                },
                {
                    "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en&fmt=srv3",
                    "languageCode": "en",
                },
            ]
        }
    },
}
_FIXTURE_XML = (
    '<transcript><text start="0" dur="2">Hello &amp;amp; welcome</text>'
    '<text start="2" dur="3">to the demo</text></transcript>'
)


def _error(message: str) -> dict[str, Any]:
    return {"content": message, "isError": True}


def get_transcript(vid: str) -> dict[str, Any]:
    try:
        page = _fetch("https://www.youtube.com/watch?v=" + vid)
    except Exception as exc:
        return _error("fetch failed: " + str(exc))
    key = innertube_key(page)
    if key is None:
        return _error(UPSTREAM_DRIFT)
    body = json.dumps({"context": {"client": _PLAYER_CLIENT}, "videoId": vid}).encode()
    try:
        raw = _fetch("https://www.youtube.com/youtubei/v1/player?key=" + key, body)
    except Exception as exc:
        return _error("player request failed: " + str(exc))
    try:
        player = json.loads(raw)
        track = caption_track_url(player)
    except ValueError:
        return _error(UPSTREAM_DRIFT)
    status = player.get("playabilityStatus") or {}
    if track is None and status.get("status") not in (None, "OK"):
        reason = str(status.get("reason") or status.get("status"))
        return _error("YouTube refused this video to the tool: " + reason)
    if track is None:
        return _error(NO_CAPTIONS)
    try:
        xml = _fetch(track)
    except Exception as exc:
        return _error("caption fetch failed: " + str(exc))
    if not xml.strip():
        return _error(EMPTY_CAPTIONS)
    text = timedtext_to_text(xml)
    if not text:
        return _error(UPSTREAM_DRIFT)
    return {"content": text}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_transcript":
        try:
            vid = video_id(str(arguments.get("video") or ""))
        except ValueError as exc:
            return _error(str(exc))
        return get_transcript(vid)
    if name == "self_test":
        track = caption_track_url(_FIXTURE_PLAYER)
        if track != "https://www.youtube.com/api/timedtext?v=x&lang=en":
            return _error("captionTracks extraction broken")
        text = timedtext_to_text(_FIXTURE_XML)
        if text != "Hello & welcome\nto the demo":
            return _error("timedtext parsing broken: " + repr(text))
        try:
            video_id("https://youtu.be/dQw4w9WgXcQ")
        except ValueError:
            return _error("video id extraction broken")
        return {"content": "self_test passed: extraction + parsing intact (offline)"}
    known = ", ".join(str(tool["name"]) for tool in TOOLS)
    return _error("no tool named " + repr(name) + "; tools: " + known)


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
