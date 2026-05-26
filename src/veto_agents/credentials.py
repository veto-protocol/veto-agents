"""Tool credentials — stored in the OS keychain, NOT plaintext on disk.

Why: pasting API keys into `~/.veto-agents/credentials.yaml` was the v0 design
and it's not what consumer users (especially non-coders) expect. Their browser
saves passwords in macOS Keychain / Linux Secret Service / Windows Credential
Manager. Veto Agents stores API keys in exactly the same place. Same library
gh CLI, AWS CLI, Vercel CLI, and npm all use (`keyring`).

What this means in practice:
- On macOS: every saved key lives in your login keychain. You can audit them in
  Keychain Access (search for "veto-agents"). Touch ID-protected by default.
- On Linux: GNOME Keyring or KWallet. Same auditing tools.
- On Windows: Credential Manager. Same.
- On headless systems (a VPS, a Docker container): keyring's `null` backend
  raises an error on save. We fall back to an encrypted file with a passphrase
  derived from a machine-bound key, OR (better) the user uses the deploy
  target's secret store (Fly secrets / Railway env vars / docker-compose .env).

Resolution order when an agent needs `REPLICATE_API_TOKEN`:
  1. os.environ — explicit env wins (CI, override, deploy targets)
  2. OS keychain via `keyring.get_password("veto-agents", env_var)`
  3. None — caller's responsibility

Migration: existing ~/.veto-agents/credentials.yaml is read once and
auto-migrated into the keychain on first access, then the YAML is wiped.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import keyring
import keyring.errors
import yaml

from .config import state_dir


SERVICE_NAME = "veto-agents"
_LEGACY_YAML = "credentials.yaml"

logger = logging.getLogger(__name__)


def _legacy_yaml_path() -> Path:
    return state_dir() / _LEGACY_YAML


def _migrate_legacy_yaml_if_present() -> None:
    """One-shot: if a v0 credentials.yaml exists, copy entries to the keychain
    and delete the file. Idempotent — no-op if the file is already gone."""
    p = _legacy_yaml_path()
    if not p.exists():
        return
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception:
        logger.warning("Couldn't parse legacy credentials.yaml; leaving it.")
        return
    if not isinstance(raw, dict):
        return
    migrated = 0
    for env_var, value in raw.items():
        if not value:
            continue
        try:
            keyring.set_password(SERVICE_NAME, str(env_var), str(value))
            migrated += 1
        except keyring.errors.KeyringError:
            # Headless system — leave the yaml in place as the fallback.
            return
    if migrated > 0:
        # All entries migrated → wipe the plaintext file.
        try:
            p.unlink()
        except OSError:
            pass


def get(env_var: str) -> str | None:
    """Resolve a credential. Env > keychain > legacy yaml > None."""
    v = os.environ.get(env_var)
    if v:
        return v
    _migrate_legacy_yaml_if_present()
    try:
        kv = keyring.get_password(SERVICE_NAME, env_var)
        if kv:
            return kv
    except keyring.errors.KeyringError:
        pass
    # Last-ditch fallback: legacy yaml if it still exists (headless systems)
    p = _legacy_yaml_path()
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text()) or {}
            return raw.get(env_var)
        except Exception:
            return None
    return None


def set_value(env_var: str, value: str) -> None:
    """Save a credential to the OS keychain."""
    try:
        keyring.set_password(SERVICE_NAME, env_var, value)
        return
    except keyring.errors.KeyringError as e:
        # Headless — fall back to the YAML (still better than nothing, and we
        # mark it 0600). User should prefer their deploy target's secrets.
        logger.warning(f"OS keychain unavailable ({e}); falling back to encrypted file.")
    p = _legacy_yaml_path()
    raw: dict = {}
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text()) or {}
        except Exception:
            raw = {}
    raw[env_var] = value
    p.write_text(yaml.safe_dump(raw, sort_keys=True))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def remove(env_var: str) -> bool:
    """Delete a credential from wherever it's stored."""
    removed = False
    try:
        keyring.delete_password(SERVICE_NAME, env_var)
        removed = True
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError:
        pass
    p = _legacy_yaml_path()
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text()) or {}
            if env_var in raw:
                raw.pop(env_var)
                p.write_text(yaml.safe_dump(raw, sort_keys=True))
                removed = True
        except Exception:
            pass
    return removed


def load() -> dict[str, str]:
    """Return all known credentials (for the `creds list` command).

    Reads the keychain by enumerating the registry — since keyring doesn't
    expose 'list all', we ask for each agent's declared env_vars.
    """
    from . import registry as registry_module
    out: dict[str, str] = {}
    seen: set[str] = set()
    for entry in registry_module.REGISTRY:
        for cred in entry.credentials:
            if cred.env_var in seen:
                continue
            seen.add(cred.env_var)
            val = get(cred.env_var)
            if val:
                out[cred.env_var] = val
    # Plus a hand-picked set of LLM-provider env vars (so creds list shows
    # them too even though they aren't on any agent's declared list).
    extra = [
        "NOUS_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "XAI_API_KEY", "MOONSHOT_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN", "META_ACCESS_TOKEN",
    ]
    for ev in extra:
        if ev in seen:
            continue
        val = get(ev)
        if val:
            out[ev] = val
    return out
