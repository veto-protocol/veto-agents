"""Config + state for veto-agents.

Everything lives under ~/.veto-agents/ (XDG-conformant on Linux). The CLI
reads/writes:
  - config.yaml          user-level settings (LLM provider, wallet address, defaults)
  - agents/<name>/       installed agent code + policy
  - policies/<name>.yaml editable policy per agent (mirrors what the agent ships
                         with at install time, but reflects user edits)
  - receipts.sqlite      local cache of receipts (the source of truth lives at
                         veto-ai.com; this is a convenience)
  - secrets via OS keychain (never on disk)

If the user has already signed in via the main `veto` CLI (which writes to
~/.veto/config.json), we detect it and reuse those credentials — no second
magic-link round-trip. See try_import_main_cli_credentials().
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from platformdirs import user_data_dir


APP_NAME = "veto-agents"
APP_AUTHOR = "veto-protocol"


def state_dir() -> Path:
    """Where ~/.veto-agents/ (or platform equivalent) lives."""
    d = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return state_dir() / "config.yaml"


def agents_dir() -> Path:
    d = state_dir() / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def policies_dir() -> Path:
    d = state_dir() / "policies"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    """User-level config persisted at ~/.veto-agents/config.yaml."""

    # LLM brain. "hermes" is the default; the runner translates that to
    # whichever Hermes endpoint is configured below.
    llm_provider: str = "hermes"
    llm_endpoint: str | None = None  # e.g. https://api.nous.ai, or a local URL
    llm_model: str | None = None  # e.g. "hermes-3-405b" or "claude-sonnet-4-7"

    # Wallet — populated by `veto-agents setup`.
    # In v0.0.2 this is a burner address (random 20 bytes hex-encoded). In
    # v0.0.3 it's replaced with a real Privy-provisioned wallet on Base.
    wallet_address: str | None = None
    wallet_chain: str = "base"

    # Veto governance endpoint. Defaults to prod; overridable for local dev.
    veto_api_base: str = "https://veto-ai.com/api/v1"

    # Credentials issued by Veto on first-run register call. These live in
    # the OS keychain (see auth_creds.py), NOT in the yaml — but they're
    # mirrored onto Config in memory so callers don't have to think about
    # storage when reading. `email` is the address we authenticated with;
    # we keep it next to the credentials so the status screen can say
    # "Signed in as you@x".
    api_key: str | None = None
    agent_id: str | None = None
    client_id: str | None = None
    email: str | None = None

    # Default policy posture: "strict" | "balanced" | "permissive". Affects
    # the caps the user gets when installing a new agent (the policy file
    # ships with sensible defaults; this multiplier loosens/tightens them).
    policy_posture: str = "balanced"

    # Agents the user has installed (names, e.g. ["media", "build"]).
    installed_agents: list[str] = field(default_factory=list)


# Fields that hold credentials. These NEVER go in the yaml — they live in
# the OS keychain (see auth_creds.py). Kept on the Config dataclass for
# convenience so callers can still read `cfg.api_key`. `email` is in the
# same bucket because the main CLI stores it alongside the credentials.
_SECRET_FIELDS = ("api_key", "agent_id", "client_id", "email")


def load() -> Config:
    p = config_path()
    raw: dict = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}

    # One-shot migration: if a legacy plaintext yaml has api_key (pre-keychain
    # behaviour), move it to the keychain and strip from the yaml. After this
    # runs once the yaml never holds secrets again.
    legacy_has_secrets = any(raw.get(f) for f in _SECRET_FIELDS)
    if legacy_has_secrets:
        from . import auth_creds  # lazy — avoid circular at import time

        if auth_creds.migrate_from_yaml(raw):
            for f in _SECRET_FIELDS:
                raw.pop(f, None)
            # Rewrite the yaml without secrets.
            p.write_text(yaml.safe_dump(raw, sort_keys=False))

    cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})

    # Pull credentials from the keychain (or fallback file).
    from . import auth_creds  # lazy

    creds = auth_creds.load()
    if creds.api_key:
        cfg.api_key = creds.api_key
    if creds.agent_id:
        cfg.agent_id = creds.agent_id
    if creds.client_id:
        cfg.client_id = creds.client_id
    if creds.email:
        cfg.email = creds.email

    return cfg


def save(cfg: Config) -> None:
    """Persist config. Non-secret fields go to ~/.veto-agents/config.yaml,
    credentials (api_key/agent_id/client_id) go to the OS keychain."""
    # Write yaml without secrets.
    data = asdict(cfg)
    for f in _SECRET_FIELDS:
        data.pop(f, None)
    p = config_path()
    p.write_text(yaml.safe_dump(data, sort_keys=False))

    # Write secrets to keychain.
    from . import auth_creds  # lazy

    auth_creds.save(
        auth_creds.AuthCreds(
            api_key=cfg.api_key,
            agent_id=cfg.agent_id,
            client_id=cfg.client_id,
            email=cfg.email,
        )
    )


# ── Cross-CLI credential reuse ────────────────────────────────────────────

# Where the main `veto` (Python veto-cli) saves state. The npm
# @veto-protocol/cli uses the OS keychain instead, which we don't read —
# user is far less likely to install both the npm CLI AND veto-agents on
# the same machine. Python veto-cli is the realistic overlap.
MAIN_CLI_STATE_PATH = os.path.expanduser("~/.veto/config.json")


def read_main_cli_state() -> dict | None:
    """Return the main veto CLI's saved state (~/.veto/config.json) or None.

    Used by veto-agents setup to detect "user already signed in via the
    main CLI" so we can reuse credentials and skip the second sign-in.
    """
    p = Path(MAIN_CLI_STATE_PATH)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("api_key"):
        return None
    return data


def import_from_main_cli(cfg: Config, main_state: dict) -> Config:
    """Copy api_key + agent_id + client_id + email from the main CLI's
    state into a veto-agents Config. Leaves wallet/posture/LLM alone —
    those are veto-agents-specific concerns the user still picks once."""
    cfg.api_key = main_state.get("api_key") or cfg.api_key
    cfg.agent_id = main_state.get("default_agent") or main_state.get("agent_id") or cfg.agent_id
    cfg.client_id = main_state.get("client_id") or cfg.client_id
    cfg.email = main_state.get("email") or cfg.email
    base = main_state.get("base_url")
    if base and "/api/v1" not in cfg.veto_api_base:
        cfg.veto_api_base = f"{base.rstrip('/')}/api/v1"
    return cfg
