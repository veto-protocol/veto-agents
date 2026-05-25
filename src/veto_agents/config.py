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

    # Credentials issued by Veto on first-run register call.
    # api_key is sent as Bearer on every authorize request.
    # agent_id is included in the authorize payload as the acting agent.
    api_key: str | None = None
    agent_id: str | None = None
    client_id: str | None = None

    # Default policy posture: "strict" | "balanced" | "permissive". Affects
    # the caps the user gets when installing a new agent (the policy file
    # ships with sensible defaults; this multiplier loosens/tightens them).
    policy_posture: str = "balanced"

    # Agents the user has installed (names, e.g. ["media", "build"]).
    installed_agents: list[str] = field(default_factory=list)


def load() -> Config:
    p = config_path()
    if not p.exists():
        return Config()
    raw = yaml.safe_load(p.read_text()) or {}
    return Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})


def save(cfg: Config) -> None:
    p = config_path()
    p.write_text(yaml.safe_dump(asdict(cfg), sort_keys=False))


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
    base = main_state.get("base_url")
    if base and "/api/v1" not in cfg.veto_api_base:
        cfg.veto_api_base = f"{base.rstrip('/')}/api/v1"
    return cfg
