"""v44-F5: discord_admin — moderation verbs for the Queen.

Hermes handed its agent a ``discord_admin`` toolset; skep exposes two
moderation verbs at skep's posture. Both are MUTATING chat tools, so a model
call only ever produces a confirm card — and the classes are deliberately NOT
in ``CHANNEL_CONFIRMABLE_ACTIONS``: a hijacked allow-listed Discord account
must never be able to confirm its own moderation action, so the confirm click
lives in the web UI (recorded as a decision in plans/v44, not an oversight).
Transport functions are module-level so tests monkeypatch them; no live
Discord anywhere in the suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

DISCORD_API = "https://discord.com/api/v10"
# 7 days — deliberately below Discord's own 28-day ceiling; a longer exile is
# a ban conversation, not a timeout.
MAX_TIMEOUT_MINUTES = 10080


def delete_message(token: str, channel_id: str, message_id: str) -> bool:
    response = httpx.delete(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 204


def timeout_member(token: str, guild_id: str, user_id: str, minutes: int) -> bool:
    until = (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = httpx.patch(
        f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {token}"},
        json={"communication_disabled_until": until},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 200
