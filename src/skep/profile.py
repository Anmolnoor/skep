from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_FILE = "profile.json"

# v48-F2: api_key_env is the NAME of an env var. A pasted key (hex/JWT/dotted
# token) silently corrupted the profile in the field: the worker saw it truthy,
# skipped the llm-secret fallback, and every run failed authentication.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    model: str
    endpoint: str | None = None
    api_key_env: str | None = None
    role: str = "coding"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderProfile:
        return cls(
            name=str(data.get("name", "")),
            model=str(data.get("model", "")),
            endpoint=data.get("endpoint") or None,
            api_key_env=data.get("api_key_env") or None,
            role=str(data.get("role", "coding")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
        }


@dataclass(frozen=True)
class PersonalProfile:
    user_id: str
    hive_id: str
    queen_id: str
    storage_root: str
    provider: ProviderProfile

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalProfile:
        return cls(
            user_id=str(data.get("user_id", "local-owner")),
            hive_id=str(data.get("hive_id", "personal-hive")),
            queen_id=str(data.get("queen_id", "personal-queen")),
            storage_root=str(data.get("storage_root", "")),
            provider=ProviderProfile.from_dict(data.get("provider", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "personal",
            "user_id": self.user_id,
            "hive_id": self.hive_id,
            "queen_id": self.queen_id,
            "storage_root": self.storage_root,
            "provider": self.provider.to_dict(),
        }


@dataclass(frozen=True)
class SetupResult:
    created: bool
    updated: bool
    profile: PersonalProfile


def profile_path(home: Path) -> Path:
    return home / PROFILE_FILE


def load_profile(home: Path) -> PersonalProfile:
    data = json.loads(profile_path(home).read_text(encoding="utf-8"))
    return PersonalProfile.from_dict(data)


def run_personal_setup(
    home: Path,
    *,
    provider: str,
    model: str,
    endpoint: str | None = None,
    api_key_env: str | None = None,
) -> SetupResult:
    api_key_env = api_key_env.strip() if api_key_env else None
    if api_key_env and not _ENV_VAR_NAME_RE.match(api_key_env):
        raise ValueError(
            f"api_key_env must be an environment variable NAME (e.g. OLLAMA_API_KEY), "
            f"got what looks like a key: {api_key_env[:8]}… — export the key under that "
            "name instead, or configure it in the web UI Settings"
        )
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory").mkdir(exist_ok=True)
    (home / "runs").mkdir(exist_ok=True)
    (home / "artifacts").mkdir(exist_ok=True)

    path = profile_path(home)
    created = not path.exists()
    existing = load_profile(home) if path.exists() else None
    profile = PersonalProfile(
        user_id=existing.user_id if existing else "local-owner",
        hive_id=existing.hive_id if existing else "personal-hive",
        queen_id=existing.queen_id if existing else "personal-queen",
        storage_root=str(home),
        provider=ProviderProfile(
            name=provider.strip(),
            model=model.strip(),
            endpoint=endpoint.strip() if endpoint else None,
            api_key_env=api_key_env,
        ),
    )
    old_payload = existing.to_dict() if existing else None
    new_payload = profile.to_dict()
    path.write_text(json.dumps(new_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SetupResult(created=created, updated=old_payload != new_payload, profile=profile)
