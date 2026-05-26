"""Keychain-backed storage for the Veto auth credentials.

Mirrors what the main `@veto-protocol/cli` does in TypeScript:
api_key / agent_id / client_id / email go in the OS keychain
(macOS Keychain, Linux Secret Service via libsecret, Windows Credential
Manager). Falls back to a 0600 file on headless systems where keyring
has no working backend.

Keys live under service name `veto-agents` with an `auth:` prefix so they
don't collide with the LLM-provider keys stored by `credentials.py`.

We keep this module separate from `credentials.py` even though both use
keyring, because the audience is different: credentials.py is "all the
keys the user pasted in for tools" and auth_creds.py is "the credentials
Veto issued for this account". They have different lifecycles.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path

import keyring
import keyring.errors

from .config import state_dir

logger = logging.getLogger(__name__)

SERVICE_NAME = "veto-agents"

# Account keys in the keychain. The `auth:` prefix keeps these separate
# from the LLM provider env-var keys also stored under `veto-agents`.
KEY_API_KEY = "auth:apiKey"
KEY_AGENT_ID = "auth:agentId"
KEY_CLIENT_ID = "auth:clientId"
KEY_EMAIL = "auth:email"

_ALL_KEYS = (KEY_API_KEY, KEY_AGENT_ID, KEY_CLIENT_ID, KEY_EMAIL)


@dataclass
class AuthCreds:
    api_key: str | None = None
    agent_id: str | None = None
    client_id: str | None = None
    email: str | None = None


def _fallback_path() -> Path:
    """Where we stash creds when the OS keychain isn't available.
    Not encrypted — just chmod 0600. The user should prefer their deploy
    target's secret store on headless systems."""
    return state_dir() / "auth.json"


def _read_fallback() -> dict[str, str]:
    p = _fallback_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    p = _fallback_path()
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _keychain_get(key: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, key)
    except keyring.errors.KeyringError:
        return None


def _keychain_set(key: str, value: str) -> bool:
    try:
        keyring.set_password(SERVICE_NAME, key, value)
        return True
    except keyring.errors.KeyringError:
        return False


def _keychain_delete(key: str) -> bool:
    try:
        keyring.delete_password(SERVICE_NAME, key)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError:
        return False


# ── Public API ────────────────────────────────────────────────────────────


def backend_kind() -> str:
    """Human-readable label for where credentials are stored. Probed once
    at call time — the result is what's currently in use, not a fixed
    decision. Same label format the main `veto` CLI uses, expanded with
    the OS so the user can recognize their own machine."""
    if _keychain_set("auth:__probe__", "ok"):
        _keychain_delete("auth:__probe__")
        system = platform.system()
        if system == "Darwin":
            return "macOS Keychain"
        if system == "Linux":
            return "Linux Secret Service"
        if system == "Windows":
            return "Windows Credential Manager"
        return "OS keychain"
    return f"file ({_fallback_path()})"


def load() -> AuthCreds:
    """Read auth credentials from wherever they live. Tries keychain
    first, falls back to the encrypted file. Returns an empty AuthCreds
    when there's nothing stored yet."""
    keychain_data = {
        KEY_API_KEY: _keychain_get(KEY_API_KEY),
        KEY_AGENT_ID: _keychain_get(KEY_AGENT_ID),
        KEY_CLIENT_ID: _keychain_get(KEY_CLIENT_ID),
        KEY_EMAIL: _keychain_get(KEY_EMAIL),
    }
    if any(keychain_data.values()):
        return AuthCreds(
            api_key=keychain_data[KEY_API_KEY],
            agent_id=keychain_data[KEY_AGENT_ID],
            client_id=keychain_data[KEY_CLIENT_ID],
            email=keychain_data[KEY_EMAIL],
        )
    file_data = _read_fallback()
    if not file_data:
        return AuthCreds()
    return AuthCreds(
        api_key=file_data.get(KEY_API_KEY),
        agent_id=file_data.get(KEY_AGENT_ID),
        client_id=file_data.get(KEY_CLIENT_ID),
        email=file_data.get(KEY_EMAIL),
    )


def save(creds: AuthCreds) -> None:
    """Persist auth credentials. Tries keychain first; only falls back
    to file if every keychain write fails (i.e. headless system).
    Idempotent — overwrites whatever was there."""
    pairs = {
        KEY_API_KEY: creds.api_key,
        KEY_AGENT_ID: creds.agent_id,
        KEY_CLIENT_ID: creds.client_id,
        KEY_EMAIL: creds.email,
    }
    keychain_ok = True
    for key, value in pairs.items():
        if value is None:
            # Clear out any stale value.
            _keychain_delete(key)
            continue
        if not _keychain_set(key, value):
            keychain_ok = False
            break

    if keychain_ok:
        # Keychain happy — make sure we don't leave a stale file behind.
        p = _fallback_path()
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        return

    # Keychain unavailable — write the file fallback.
    logger.warning("OS keychain unavailable; writing auth creds to %s", _fallback_path())
    _write_fallback({k: v for k, v in pairs.items() if v is not None})


def clear() -> None:
    """Wipe all auth credentials from wherever they live."""
    for key in _ALL_KEYS:
        _keychain_delete(key)
    p = _fallback_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def migrate_from_yaml(yaml_data: dict) -> bool:
    """One-shot migration: copy api_key/agent_id/client_id from the
    legacy plaintext config.yaml into the keychain. Returns True if
    anything was migrated. Caller should then remove these fields from
    the yaml. No-op if the keychain doesn't have a working backend
    (the yaml stays as-is).
    """
    creds = AuthCreds(
        api_key=yaml_data.get("api_key"),
        agent_id=yaml_data.get("agent_id"),
        client_id=yaml_data.get("client_id"),
        email=yaml_data.get("email"),
    )
    if not creds.api_key:
        return False
    # Probe keychain before committing.
    if not _keychain_set("auth:__probe__", "ok"):
        return False
    _keychain_delete("auth:__probe__")
    save(creds)
    return True


def to_dict(creds: AuthCreds) -> dict:
    return {k: v for k, v in asdict(creds).items() if v is not None}
