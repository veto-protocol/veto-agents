"""Resolve Meta Marketing API credentials for the ad-buyer agent.

Bring-your-own token model — Veto never provisions or holds a Meta token. We
read three values, in precedence order:

    1. shell environment       (META_ACCESS_TOKEN, ...)
    2. ~/.veto/meta.env        (KEY=VALUE lines, shared with the main Veto CLI)
    3. veto-agents credentials (~/.veto-agents/credentials.yaml, set on install)

Values:
    META_ACCESS_TOKEN     — System-User token, scope ads_management + ads_read.
    META_AD_ACCOUNT_ID    — the ad account, e.g. `act_1234567890`.
    META_PAGE_ID          — Facebook Page the creative attaches to (mandatory
                            for any ad creative — no Page, no ad).

Tokens are NEVER printed or logged. `describe()` returns booleans only so the
agent can tell the user *what is missing* without leaking the secret.
"""

from __future__ import annotations

import os
from pathlib import Path

# Shared with the main Veto CLI (see project memory: ~/.veto/meta.env).
META_ENV_PATH = Path.home() / ".veto" / "meta.env"


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


def load_meta(cfg=None) -> dict:
    """Return {access_token, ad_account_id, page_id} — any may be None.

    `cfg` is accepted for symmetry with other tools but not currently needed;
    Meta creds live in env / meta.env / veto-agents credentials, not on cfg.
    """
    file_env = _parse_env_file(META_ENV_PATH)
    try:  # pragma: no cover - credentials module is optional at import time
        from veto_agents import credentials as creds

        saved = creds.load() or {}
    except Exception:
        saved = {}

    def pick(key: str) -> str | None:
        return os.environ.get(key) or file_env.get(key) or saved.get(key)

    ad_account = pick("META_AD_ACCOUNT_ID")
    # Be forgiving: accept a bare numeric id and normalize to `act_...`.
    if ad_account and not ad_account.startswith("act_"):
        ad_account = f"act_{ad_account}"

    return {
        "access_token": pick("META_ACCESS_TOKEN"),
        "ad_account_id": ad_account,
        "page_id": pick("META_PAGE_ID"),
    }


def describe(meta: dict) -> dict[str, bool]:
    """Presence-only view of the creds — safe to print (no secret values)."""
    return {
        "access_token": bool(meta.get("access_token")),
        "ad_account_id": bool(meta.get("ad_account_id")),
        "page_id": bool(meta.get("page_id")),
    }


def missing(meta: dict) -> list[str]:
    """Env-var names that still need to be set (page_id required for creatives)."""
    want = {
        "META_ACCESS_TOKEN": meta.get("access_token"),
        "META_AD_ACCOUNT_ID": meta.get("ad_account_id"),
        "META_PAGE_ID": meta.get("page_id"),
    }
    return [k for k, v in want.items() if not v]
