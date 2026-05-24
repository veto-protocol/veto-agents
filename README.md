# Veto Agents

**AI agents that pay for things, on your behalf, with the safety built in.**

A curated set of consumer AI agents — each one designed from day zero to spend money to do real work for you, every action governed by [Veto](https://veto-ai.com), every spend signed, every verdict verifiable.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash
```

Then:

```bash
veto-agents                           # walks first-time setup (email, wallet, QR funding)
veto-agents install media             # add your first agent + paste the Replicate token it asks for
veto-agents media "make a neon jellyfish in cyberpunk rain"
```

Three commands. The agent generates the image, Veto authorizes the spend, you get a signed receipt at `veto-ai.com/r/<uuid>`. That's the loop.

<details>
<summary>Other install paths</summary>

- **Already have pipx:** `pipx install veto-agents`
- **Already have Python + want to be reckless:** `pip install veto-agents` (not recommended — pollutes global)
- **Manual:** clone the repo, `pip install -e .` in a venv

</details>

## The bet

Agents are about to spend a lot of money. Today's general agent frameworks (Hermes, OpenClaw, n8n) treat payments as an afterthought — a tool the agent *might* call if you wire it up. Veto Agents inverts that: every agent in this catalog is built around the assumption that **it has money and will spend it.** Veto governance is not a feature — it's the only way the agent works at all.

## How every agent behaves

Five non-negotiable principles every Veto Agent inherits — see [PRINCIPLES.md](PRINCIPLES.md) for the full version.

1. **Plan-then-execute.** Show the plan + cost estimate first. Wait for explicit consent. Never auto-spend.
2. **Cost transparency at every step.** Show actuals as they happen, with the receipt URL inline.
3. **Receipts for everything spendable.** Every paid action produces a Veto-signed verdict at `veto-ai.com/r/<uuid>`.
4. **Veto is the only spend gate.** Every paid call authorizes through Veto, every time, no caching.
5. **Always offer cheaper alternatives when they exist.** Cost-conscious by default.

That predictability is the product. Every other consumer agent in 2026 is "agent just goes." Veto Agents is *the agent that asks first*.

## What's in the box

Four agents, each Hermes-core with Veto governance preconfigured:

- **[Media](agents/media/SPEC.md)** — generates images, video, and audio for you. Pays Replicate / Runway / ElevenLabs per call. *Headline agent.*
- **[Build](agents/build/SPEC.md)** — deploys your code on the cheapest infra it can find. Pays Vercel / Modal / Replicate for compute. *Dev headline.*
- **[Research](agents/research/SPEC.md)** — does deep research using paid search and content. Pays Exa / Tavily / x402-gated sources.
- **[Inbox](agents/inbox/SPEC.md)** — handles email, calendar, and scheduling using paid AI and scheduling tools.

Each agent ships with a default Veto policy (caps, allowlists, intent rules), a wallet provisioned via Privy on first run, and a receipts feed showing every action it took and why.

## How this connects to Veto

Veto already ships the trust substrate:
- **Engine** — 8-stage policy + risk evaluation
- **Receipts** — Ed25519-signed verdicts at `veto-ai.com/r/<uuid>`
- **APPS** — open policy schema
- **VetoGuardedAccount** — on-chain hard-stop contract

Veto Agents is the **consumer surface** that surfaces all of that. The agents are the front door; Veto is the load-bearing wall behind them. Same primitives, packaged for a non-developer to install and use.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the layers compose.

## Status

v0 in design. Build sequence:
1. **Media** — weeks 1–6, ship first
2. **Build** — weeks 7–10
3. **Research + Inbox** — weeks 11–14

## License

MIT. Each agent is a forkable template. Self-host on your own machine using vanilla Hermes, or run via the hosted Veto Agents PWA at `app.veto-ai.com`.

## Where the credit goes

- **Hermes Agent** — Nous Research. The core runtime every agent runs on.
- **Privy** — embedded wallet provisioning so users never see a seed phrase.
- **Veto Protocol** — the governance, receipts, and on-chain enforcement layer.

---

*Veto governs. The rail executes. The agent works.*
