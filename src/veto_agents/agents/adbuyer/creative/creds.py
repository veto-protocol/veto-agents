"""Resolve creative-provider API keys for the studio (bring-your-own-key).

Mirrors meta_env.py. Veto never provisions or holds these keys. We read them,
in precedence order:

    1. shell environment                 (OPENAI_API_KEY, ...)
    2. ~/.veto/creative.env              (KEY=VALUE lines, shared with main Veto)
    3. veto-agents keychain credentials  (credentials.get / .load)

Keys we resolve:
    OPENAI_API_KEY        — gpt-image-1 (image). Org verification required.
    HIGGSFIELD_API_KEY    — Higgsfield KEY_ID   (video)
    HIGGSFIELD_API_SECRET — Higgsfield KEY_SECRET (video)
    HIGGSFIELD_CREDENTIALS— optional combined "KEY_ID:KEY_SECRET" (overrides split)
    ELEVENLABS_API_KEY    — ElevenLabs text-to-speech (voice, optional)

Keys are NEVER printed or logged. `describe()` returns booleans only, so the
studio can tell the user *which assets are possible* without leaking a secret.
"""

from __future__ import annotations

import os
from pathlib import Path

# Shared with the main Veto CLI, alongside ~/.veto/meta.env.
CREATIVE_ENV_PATH = Path.home() / ".veto" / "creative.env"


def _parse_env_file(p: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Ignores blanks + `#` comments."""
    out: dict[str, str] = {}
    if not p.exists():
        return out
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def resolve(key: str, cfg=None) -> str | None:
    """Resolve one credential: env → ~/.veto/creative.env → veto-agents keychain."""
    v = os.environ.get(key)
    if v:
        return v
    file_env = _parse_env_file(CREATIVE_ENV_PATH)
    if file_env.get(key):
        return file_env[key]
    try:  # pragma: no cover - credentials module optional at import time
        from veto_agents import credentials as creds

        return creds.get(key)
    except Exception:
        return None


def load_creative(cfg=None) -> dict:
    """Return the resolved creative creds (values may be None). Never printed."""
    return {
        "openai_api_key": resolve("OPENAI_API_KEY", cfg),
        "higgsfield_api_key": resolve("HIGGSFIELD_API_KEY", cfg),
        "higgsfield_api_secret": resolve("HIGGSFIELD_API_SECRET", cfg),
        "higgsfield_credentials": resolve("HIGGSFIELD_CREDENTIALS", cfg),
        "elevenlabs_api_key": resolve("ELEVENLABS_API_KEY", cfg),
    }


def higgsfield_credentials(cfg=None) -> tuple[str, str] | None:
    """Return (KEY_ID, KEY_SECRET) for Higgsfield, or None if incomplete.

    Prefers a combined `HIGGSFIELD_CREDENTIALS="KEY_ID:KEY_SECRET"`; otherwise
    combines the split `HIGGSFIELD_API_KEY` + `HIGGSFIELD_API_SECRET`.
    """
    combined = resolve("HIGGSFIELD_CREDENTIALS", cfg)
    if combined and ":" in combined:
        kid, _, ksec = combined.partition(":")
        if kid and ksec:
            return kid.strip(), ksec.strip()
    kid = resolve("HIGGSFIELD_API_KEY", cfg)
    ksec = resolve("HIGGSFIELD_API_SECRET", cfg)
    if kid and ksec:
        return kid, ksec
    return None


def describe(cfg=None) -> dict[str, bool]:
    """Presence-only view of which assets are possible — safe to print."""
    c = load_creative(cfg)
    return {
        "openai_image": bool(c["openai_api_key"]),
        "higgsfield_video": higgsfield_credentials(cfg) is not None,
        "elevenlabs_voice": bool(c["elevenlabs_api_key"]),
    }


def missing(cfg=None) -> list[str]:
    """Env-var names for assets that can't run yet (for a friendly hint)."""
    d = describe(cfg)
    out: list[str] = []
    if not d["openai_image"]:
        out.append("OPENAI_API_KEY")
    if not d["higgsfield_video"]:
        out.append("HIGGSFIELD_API_KEY+HIGGSFIELD_API_SECRET")
    if not d["elevenlabs_voice"]:
        out.append("ELEVENLABS_API_KEY")
    return out
