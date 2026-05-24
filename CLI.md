# Veto Agents — CLI

> The primary install + runtime surface. Local-first. Self-host anywhere. Pay-per-use via embedded wallet.

Modeled after Franklin (BlockRun) and Hermes Agent — both proved that a CLI with a one-line install and optional wallet setup is the right shape for *agents that spend money*.

`veto-agents` is a separate package from the core `veto` governance CLI. The two compose — `veto-agents` calls the Veto authorize endpoint for every paid action — but they're shipped, versioned, and installed independently. Same logic as Stripe shipping `stripe` and `stripe-cli` as different surfaces under the same brand.

## Install

```bash
npm install -g @veto-protocol/agents
# or for the curl crowd:
curl -fsSL https://veto-ai.com/install-agents.sh | bash
```

Installs a single binary, `veto-agents`. Works on macOS, Linux, WSL2, Windows (PowerShell installer for native), Termux (Android).

Zero signup. Zero credit card. Zero phone verification.

## First run

```bash
veto-agents setup
```

Walks the user through:

1. **Pick an LLM provider.** `hermes` (default, hosted by Nous), `claude`, `gpt`, `openrouter`, or `custom` (bring your own endpoint). User can switch anytime via `veto-agents model <provider>`.
2. **Provision a wallet.** *(Optional but recommended.)* Privy embedded wallet, on Base. $5 USDC funded by Veto as free credit. User never sees a seed phrase.
3. **Or bring your own wallet.** `veto-agents wallet import <address>` and connect via WalletConnect signature flow.
4. **Confirm default policy posture.** Strict / Balanced / Permissive — affects every agent's default caps and approval thresholds. User can edit later.

Everything is stored in `~/.veto-agents/` (config in YAML, history + receipts in SQLite, secrets in OS keychain).

## Browse + install agents

```bash
veto-agents list
# media     Generate images, video, audio.   Replicate, Runway, ElevenLabs.
# build     Deploy code to cheapest infra.   Vercel, Modal, Fly, Runpod.
# research  Deep research with paid sources. Exa, Tavily, x402-gated content.
# inbox     Email triage + scheduling.       Gmail/Outlook + AssemblyAI + Cal.com.

veto-agents install media
# ✓ Pulled @veto-protocol/agents-media v0.1.0
# ✓ Default policy installed: 'media-agent-default' (per-tx $2, per-month $25)
# ✓ Tool credentials needed: REPLICATE_API_TOKEN (or use Veto's hosted gateway)
# Ready. Try: veto-agents media "make a 6s video of a cat on a slice of bread"
```

Each agent is a published npm package under `@veto-protocol/agents-<name>`, so users can pin versions, audit code, fork freely.

## Use an agent (the plan-then-execute flow)

Per [PRINCIPLES.md](PRINCIPLES.md), every agent surfaces a plan + cost estimate and waits for consent before spending. This is the universal interaction pattern:

```bash
veto-agents media "make a 6-second video of a neon jellyfish in cyberpunk rain"

# Plan:
#   1. Generate 6s video — Runway Gen-3        ~$0.42
#   2. (optional) Generate voiceover            ~$0.05
#                                               ─────
#                                   Estimate:  $0.42
#
# Alternative: use Hailuo for the video → $0.18 total (lower quality)
#
# Proceed?  [y/N/alt]  y
#
# ✓ Veto authorize → allow (receipt: veto-ai.com/r/8b3c-7f29-…)
# ✓ Generating… [████████████] 100%
# ✓ Done in 38s. Actual cost: $0.40 (estimate was $0.42).
#   Output: ~/Downloads/veto-media-2026-05-24-1432.mp4
#   Full breakdown: veto-agents receipt 8b3c-7f29-…
```

For long-running or interactive agents:

```bash
veto-agents inbox
# Welcome back. Last seen: 2h ago. 17 new messages since.
# > triage everything from this week
```

## Manage policies in your editor

```bash
veto-agents policy edit media
# Opens ~/.veto-agents/policies/media.yaml in $EDITOR
# Save & exit → policy is validated, version incremented, content-hashed
# Future receipts will cite the new policy version
```

Plain-English to YAML translation also available via `veto-agents policy describe media` (LLM-assisted).

## Wallet

```bash
veto-agents wallet balance          # USDC balance on Base
veto-agents wallet topup            # Coinbase onramp link
veto-agents wallet receive          # show address for direct deposit
veto-agents wallet export           # encrypted JSON, user-controlled
```

## Receipts

```bash
veto-agents receipts                # last 20, scrollable
veto-agents receipts --agent media  # filter by agent
veto-agents receipts --denied       # see what Veto blocked + why
veto-agents receipt <uuid>          # full JWT + verify link
```

Offline verification is done via the core Veto CLI's mandate-verifier (different package):

```bash
npx @veto-protocol/cli verify <jwt>
```

That way the verifier stays independent of the agents runtime — anyone can verify any Veto receipt without installing the agents package.

## Run modes

The same CLI supports three runtime modes per agent:

1. **Local (default).** Agent runs on your machine, Hermes locally, your data in `~/.veto-agents/`. Wallet is yours. Network calls go directly from your machine to the tool APIs and to Veto's authorize endpoint.
2. **Hosted.** `veto-agents run media --hosted`. Same agent code runs in Veto's cloud. Useful when your laptop sleeps and you want your inbox agent always on. Costs nothing extra; you still pay tool costs.
3. **Bring-your-own infra.** `veto-agents run media --runtime ssh://my-server`. Connect your own VPS / homelab. Veto governs from the cloud; your machine executes.

The choice is per-agent. Inbox agent might run hosted (always on). Media agent might run local (creative work, want files on your disk). Build agent might run on your homelab.

## Open + forkable by design

Every agent is open source MIT under `github.com/veto-protocol/veto-agents`. The CLI is also MIT. Fork an agent, modify it, publish your own variant — `veto-agents install @yourorg/agents-custom`. The Veto governance layer is the only required dependency; everything else is yours.

## Distribution shape

- `npm` and `pip` registries for the runtime
- `brew` formula for macOS
- `winget` for Windows
- `apt` repo for Debian/Ubuntu
- Docker image for self-hosted server installs
- A single `curl | bash` script as the universal fallback

The first three matter on day one. The rest follow.

## Why CLI-first, not PWA-first

- **Self-hostable from the start.** Aligns with crypto-native + open-source values; the kind of user who funds $5 USDC into their agent is also the kind who wants the code on their machine.
- **Faster ship.** A CLI v0 is ~4 weeks; a polished PWA v0 is 8–12 weeks.
- **Pairs naturally with Franklin / Hermes-style adoption patterns.** The audience that installs Franklin will install Veto Agents the same way.
- **PWA comes later, easily.** Once the agent code, runner, and wallet/policy/receipts flow are working in the CLI, wrapping that as a hosted web UI is mostly a frontend job — ~4 weeks on top of the CLI base.

## Build sequence

1. **Weeks 1–4:** CLI v0 + Media agent. `veto-agents setup`, `veto-agents install media`, `veto-agents media "prompt"`, receipts, policy editing.
2. **Weeks 5–8:** Build agent. Same CLI, add `veto-agents install build`.
3. **Weeks 9–10:** Research agent.
4. **Weeks 11–12:** Inbox agent.
5. **Weeks 13–16:** PWA at `app.veto-ai.com` as the hosted convenience layer for non-devs, sharing the same agent backends.

Native iOS/Android wrappers via Capacitor in v0.4, only if the PWA hits a ceiling.
