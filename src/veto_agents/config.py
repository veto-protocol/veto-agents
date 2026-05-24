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
"""

from __future__ import annotations

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
    wallet_address: str | None = None
    wallet_chain: str = "base"

    # Veto governance endpoint. Defaults to prod; overridable for local dev.
    veto_api_base: str = "https://veto-ai.com/api/v1"

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
