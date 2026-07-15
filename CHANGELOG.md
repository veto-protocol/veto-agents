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

## [0.0.26] — 2026-07-13
### Fixed
- meta_ads: Meta now requires `is_adset_budget_sharing_enabled` on campaign
  creation when the budget lives on the ad set (error 4834011). We send
  `false` — budget sharing between ad sets would blur the agent's ±20%
  per-adset clamp guarantees. Found by the first live write test against
  the real Marketing API.
### Also in 0.0.26
- controller: magnitude clamp could round a budget change UP past ±20% by a
  cent (e.g. +20.005%) — now clamps in integer cents (floor/ceil at the band
  edge). Found by the new simulation harness's invariant sweep.
- controller/meta_env: META_PAGE_ID is now OPTIONAL for the autonomous loop —
  only creative refresh needs a Page; without one the loop runs and refuses
  refresh_creative with a clear reason.
- NEW tests/sim: accelerated stochastic simulation harness — 6 adversarial
  scenarios × seeds × 30 sim-days (900 cycles in ~3.5s), machine-checked
  governance invariants (±20% clamp, cooldowns, learning-phase, spend cap,
  crash-free under injected API errors, no out-of-scope calls).

## [0.0.27] — 2026-07-13
### Added
- **Brand ingestion**: `veto-agents brand set <url-or-file>` — extract a brand
  profile (product, audience, tone, voice rules, colors) from your website or
  an agent-written txt/md dump into an editable `~/.veto/brand.yaml`; the
  creative director and the decide brain use it automatically. Site text is
  treated as untrusted (structured extraction only; no raw page text reaches
  the director), binary/non-HTML rejected, secrets can't round-trip.
### Notes
- Live-verified end to end against real Meta (reads, PAUSED writes, full
  dry-run autonomous cycle) and 900 simulated days of adversarial scenarios.

## [0.0.28] — 2026-07-15
Hardening pass to professional MCP/CLI standard (adversarial-QA driven).
### Fixed (money-safety & robustness)
- `--image-provider` is now enum-locked to {openai, fal} in the CLI **and** the
  MCP schema — a typo/empty value can no longer silently spend on paid OpenAI
  (it's rejected or falls back to free fal).
- No raw tracebacks on common paths: a bad/expired LLM key, a malformed config,
  or any unexpected error now yields a clean one-line message + exit 1 (new
  top-level `_SafeTyper` safety net; wrapped provider calls; fail-soft config).
- Budget validation: negative / zero / inf / nan / absurd caps rejected or
  clamped; the displayed cap now equals what's enforced.
- `--mock` runs for signed-out users (local governance demo) instead of
  dead-ending on a sign-in prompt; non-TTY no longer prints a bare "Aborted."
- Veto `receipt_url` captured/shown on the ALLOW path (studio + loop), not only
  on deny. fal image errors now give an actionable "wallet setup" message with a
  Veto line, never a raw x402 scheme error.
- `brand set` no longer reads extension-less files (exfil surface closed).
- MCP tools return structured, secret-redacted errors and report the package
  version. Added `click>=8.0` as an explicit dependency.
### Added
- Optional, skippable **video (Higgsfield)** + **voice (ElevenLabs)** steps in
  `adbuyer-setup`, saved to `~/.veto/creative.env` (0600); `creds set/list` for
  adding provider keys later (presence-only listing, never prints secrets).
- Higgsfield video provider rewritten to the real Cloud API (image→video job-set
  poll) — verified live end-to-end.

## [0.0.29] — 2026-07-15
### Fixed
- The LLM SDKs (`anthropic`, `openai`) are now CORE dependencies, not extras —
  a bare `pip install veto-agents` no longer fails with "SDK not installed" on
  the first `create`/`adbuyer`. The agent can't think without a brain, so the
  brain ships by default. (`mcp` stays optional — only the `mcp` command needs it.)
- The "SDK not installed" hint now points to the correct remedy
  (`pipx install --force 'veto-agents[all]'` / `pipx inject`).
### Added
- docs/SETUP.md — from-experience credentials guide: what's required vs optional,
  where to get each key (Meta / Higgsfield Cloud / ElevenLabs / OpenAI), the
  wallet-vs-credit-card explanation, the local-only privacy model, and how to
  wire the MCP server into Claude Code / Claude Desktop.

## [0.0.30] — 2026-07-15
### Changed
- The `adbuyer` loop / `--mock` demo output is now plain-English and readable:
  named actions ("▲ Scaling up …" not "adjust_budget adset 238510…"), a `why:`
  line, a clean per-ad-set snapshot, and a "Cycle recap: 2 applied, 1 held back
  for discipline" summary. Dropped raw agent_id/account noise and the confusing
  "(no receipt returned)".
- All example commands with a `$amount` use single quotes so the shell doesn't
  eat "$30" → "up to /day".
