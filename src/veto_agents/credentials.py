"""Per-agent tool credentials, stored at ~/.veto-agents/credentials.yaml.

A separate file from config.yaml because:
- credentials get tighter file-mode (0600)
- it's a flat dict keyed by env-var name, easy to grep / inspect
- users can edit it directly in $EDITOR without worrying about config schema

Resolution order when a tool needs `REPLICATE_API_TOKEN`:
  1. The actual os.environ — explicit env wins, useful for CI / overrides
  2. credentials.yaml — what `veto-agents install media` saved
  3. None — caller's responsibility to handle the missing-credential case

Saving sets file mode to 0600 since these are secrets. Never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .config import state_dir


def _path() -> Path:
    return state_dir() / "credentials.yaml"


def load() -> dict[str, str]:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save(creds: dict[str, str]) -> None:
    p = _path()
    p.write_text(yaml.safe_dump(creds, sort_keys=True))
    try:
        p.chmod(0o600)
    except OSError:
        # Windows doesn't honor chmod the same way; best-effort.
        pass


def get(env_var: str) -> str | None:
    """Resolve a credential. Env wins; falls back to credentials.yaml."""
    v = os.environ.get(env_var)
    if v:
        return v
    return load().get(env_var)


def set_value(env_var: str, value: str) -> None:
    """Save (or update) a single credential without disturbing others."""
    creds = load()
    creds[env_var] = value
    save(creds)


def remove(env_var: str) -> bool:
    """Delete a credential. Returns True if it existed."""
    creds = load()
    if env_var in creds:
        creds.pop(env_var)
        save(creds)
        return True
    return False
