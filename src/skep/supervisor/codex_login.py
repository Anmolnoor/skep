"""ES-F120 (D20): the operator's Codex ChatGPT login, handed to one codex run.

The ``codex`` engine authenticates from ``OPENAI_API_KEY``; an operator with
only a ChatGPT plan has no key. This module lets such a run use the plan's
login without ever giving the sandbox the operator's refresh token — the
design and its failure modes are in ``docs/codex-chatgpt-login.md``. In short:

- the operator's ``$CODEX_HOME/auth.json`` (default ``~/.codex``) is only ever
  READ here, and stays masked from every worker (W33);
- the run gets a NEW file in the Codex CLI's own externally-managed-token
  mode (``chatgptAuthTokens``): the access token and account id, an EMPTY
  refresh token, read-only. Codex never refreshes that mode, so a run can
  neither rotate the operator's login nor write anything back. The file sits
  in the run's audit dir, which every worker's sandbox masks; the run's own
  ``CODEX_HOME/auth.json`` is a link to it and only that run's sandbox binds
  it back (``sandbox.write_profile(unmasked=)``) — in the worktree, any other
  sandboxed run could read it;
- the access token must outlive the run's budget, or the dispatch refuses and
  says how to refresh it on the host (the Codex CLI does that itself).

Nothing here returns, logs or prints a token: callers see a ``LoginStatus``
(presence, plan, expiry, a problem sentence) and a path.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

POLICY_KEY = "codex_chatgpt_login"
AUTH_SOURCE = "chatgpt_login"
CARD_LINE = "uses your ChatGPT plan login (Codex)"
# ponytail: one fixed margin past the run budget; Codex itself refreshes a
# managed login 5 minutes before expiry, so the same window is kept here.
_MARGIN_SECONDS = 300
_REFRESH_HINT = (
    "use `codex` once on this machine (it refreshes its own login when one is due) "
    "or sign in again with `codex login`, then dispatch again"
)


@dataclass(frozen=True)
class LoginStatus:
    """What skep may say about the login: never a value."""

    present: bool  # a ChatGPT sign-in (not an API key) is saved on this machine
    plan: str | None = None  # the plan the token names, e.g. "plus"
    expires_at: datetime | None = None  # the access token's own expiry
    problem: str | None = None  # why a run cannot use it now; None when it can


def auth_file() -> Path:
    """Where the Codex CLI keeps its login: ``$CODEX_HOME`` else ``~/.codex``."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def _claims(jwt: str) -> dict[str, Any]:
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _load(min_seconds: float) -> tuple[LoginStatus, str, str]:
    """(status, access token, account id); the two strings are empty unless
    the status has no problem. Private: tokens never leave this module except
    into the run's file."""
    path = auth_file()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LoginStatus(False, problem="no Codex login on this machine (`codex login`)"), "", ""
    except (OSError, ValueError):
        return LoginStatus(False, problem=f"the Codex login at {path} could not be read"), "", ""
    doc = doc if isinstance(doc, dict) else {}
    tokens = doc.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    access = tokens.get("access_token")
    if doc.get("auth_mode") not in (None, "chatgpt") or not isinstance(access, str) or not access:
        return (
            LoginStatus(False, problem="the Codex login is not a ChatGPT sign-in (`codex login`)"),
            "",
            "",
        )
    claims = _claims(access)
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    plan = auth.get("chatgpt_plan_type")
    plan = plan if isinstance(plan, str) else None
    account = tokens.get("account_id") or auth.get("chatgpt_account_id")
    exp = claims.get("exp")
    expires_at = datetime.fromtimestamp(exp, UTC) if isinstance(exp, int | float) else None
    status = LoginStatus(True, plan, expires_at)
    if expires_at is None:
        problem = "the Codex login's access token carries no expiry skep can check"
    elif expires_at <= datetime.now(UTC) + timedelta(seconds=min_seconds + _MARGIN_SECONDS):
        problem = (
            f"the Codex login's access token expires {expires_at:%Y-%m-%d %H:%M} UTC, "
            f"before this run could finish — {_REFRESH_HINT}"
        )
    elif not isinstance(account, str) or not account:
        problem = "the Codex login names no ChatGPT account (`codex login`)"
    else:
        return status, access, account
    return LoginStatus(True, plan, expires_at, problem), "", ""


def login_status(min_seconds: float = 0) -> LoginStatus:
    """The login's presence, plan and expiry, and whether a run that lasts
    ``min_seconds`` could use it."""
    return _load(min_seconds)[0]


def run_auth_source(engine: str, policy: dict[str, Any], *, min_seconds: float) -> str | None:
    """``AUTH_SOURCE`` when a dispatch of ``engine`` under ``policy`` would run
    on the plan login now (what the card shows; run_task decides the same way,
    refusing where this returns None)."""
    from .engines import resolve_engine

    chosen = resolve_engine(engine)
    if chosen.login_host is None or any(os.environ.get(var) for var in chosen.auth_env):
        return None  # no plan login for this engine, or an API key wins
    if policy.get(POLICY_KEY) is not True or login_status(min_seconds).problem is not None:
        return None
    return AUTH_SOURCE


def write_run_login(codex_home: Path, path: Path, *, min_seconds: float) -> Path:
    """Write the run's own login to ``path`` (0400, access token only, in a
    directory every other run's sandbox masks) and link ``codex_home/auth.json``
    (the run's redirected ``CODEX_HOME``, 0700, never the real one) to it.
    Returns ``path``. Raises ValueError naming the problem; writes nothing then."""
    status, access, account = _load(min_seconds)
    if status.problem is not None:
        raise ValueError(status.problem)
    codex_home.mkdir(parents=True, exist_ok=True)
    codex_home.chmod(0o700)
    path.unlink(missing_ok=True)
    # Codex's own constructor for external tokens (AuthDotJson::
    # from_external_access_token) derives the id-token claims from the access
    # token the same way; the refresh token stays empty, so nothing refreshes.
    doc = {
        "auth_mode": "chatgptAuthTokens",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": access,
            "access_token": access,
            "refresh_token": "",
            "account_id": account,
        },
        "last_refresh": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    link = codex_home / "auth.json"
    link.unlink(missing_ok=True)
    link.symlink_to(path)  # Codex follows it (auth/storage_file.rs resolve_auth_file)
    return path
