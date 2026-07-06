# Changelog

All notable changes to `veto-agents` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.0.25] — 2026-07-05

### Added — the flagship: `adbuyer`, an autonomous Veto-governed media buyer

A new headline agent that runs your Meta ads 24/7 on a standing goal — scaling
winners, killing losers — where **every spend decision is gated by Veto before a
dollar moves**, and a code-enforced ad-ops discipline gate bounds the downside.

- **Autonomous loop** (`veto-agents adbuyer`) — `OBSERVE → DECIDE → DISCIPLINE →
  GOVERN (Veto) → ACT → RECORD`. Fail-closed: any authorize exception skips the
  action, never fails open. `--mock` reproduces the full governed path with no
  Meta account and no real spend; `--dry-run`, `--once`, `--no-llm` included.
- **Code-enforced ad-ops discipline** — a readiness gate (`_is_actionable`) that
  holds still-learning ad sets, waits for enough days + data, respects per-entity
  cooldowns, and clamps any budget change to ±20% — independent of the LLM, and
  it runs *upstream* of the Veto money gate.
- **Creative studio** (`veto-agents create`) — one LLM director derives a single
  concept, then every asset from it (copy → hero image → optional video/voice).
  Each paid asset is Veto-authorized *inside its provider driver*. Image via
  keyless fal.ai FLUX over x402 (or BYO OpenAI), video via Higgsfield, voice via
  ElevenLabs; missing keys degrade gracefully.
- **MCP server** (`veto-agents mcp`) — drops the buyer into Claude Code / Claude
  Desktop / OpenClaw over stdio as three governed tools: `create_ad_creative`,
  `run_ad_cycle`, `get_campaigns`. Governance lives inside the tools, so a host
  LLM cannot bypass it. Wiring documented in `docs/MCP.md`.
- **LLM-agnostic brain** — one shared `structured_llm` client routes across the
  Anthropic SDK (Claude, forced tool-use) and any OpenAI-compatible endpoint
  (OpenAI, OpenRouter, Hermes/Nous, Grok, Kimi, DeepSeek, Ollama, custom),
  auto-detecting from whichever key is present.
- **Guided setup wizard** (`veto-agents adbuyer-setup`) — walks brain (LLM) →
  creative providers → Meta ad account → budget guardrails, one step at a time.
  Writes `0600` configs and never prints a secret. Scriptable with `-n`.
- **Two separate policies** so a $0.01 image isn't judged against a $50 ad-budget
  cap: `adbuyer/policy.yaml` (ad spend) and `adbuyer/creative/policy.yaml`
  (creative micro-spends).

### Changed

- **`media` agent is now keyless.** Dropped the Replicate BYO-token path in favor
  of keyless x402 image generation (fal.ai FLUX). No provider accounts — fund the
  wallet and go.
- **README** now leads with `adbuyer` as the flagship; refreshed the agent
  catalog, wallet model (Reown + Safe/VetoGuard, opt-in), and removed the stale
  four-agent / Privy / VetoGuardedAccount framing and the design-phase roadmap.

### Removed

- `agents/media/tools/replicate_image.py` — superseded by the keyless x402 path.
