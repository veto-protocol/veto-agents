# Veto Agents

**AI agents that pay for things, on your behalf, with the safety built in.**

A curated set of consumer AI agents — each one designed from day zero to spend money to do real work for you, every action governed by [Veto](https://veto-ai.com), every spend signed, every verdict verifiable.

---

## ⭐ Flagship: `adbuyer` — an autonomous AI media buyer that literally can't overspend your budget

Runs your Meta ads 24/7 on a standing goal — scaling winners, killing losers — with **every spend decision gated by Veto before a dollar moves**, plus a code-enforced ad-ops discipline gate that holds still-learning ad sets and clamps budget swings to ±20%. Deploy once, walk away.

Reproduce the demo with zero setup — no Meta account, no real spend, real Veto:

```bash
veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock --once --no-llm
```

**→ Read the full README: [src/veto_agents/agents/adbuyer/README.md](src/veto_agents/agents/adbuyer/README.md)** · MCP wiring: [docs/MCP.md](docs/MCP.md)

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash
```

Then:

```bash
veto-agents                                             # first-time setup (sign in, optional wallet + funding)
veto-agents adbuyer-setup                               # guided setup for the flagship media buyer
veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock   # run it — no Meta account, no real spend
```

Veto authorizes every spend before a dollar moves; you get a signed receipt at `veto-ai.com/r/<uuid>`. That's the loop. Prefer a one-off creative or image instead? `veto-agents create "…"` (keyless over x402 — no provider accounts).

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

The flagship, plus a set of forkable sample agents — each Hermes-core with Veto governance preconfigured:

- **[adbuyer](src/veto_agents/agents/adbuyer/README.md)** — ⭐ *flagship.* An autonomous Meta media buyer: a creative studio (`veto-agents create`) plus a 24/7 observe → decide → discipline → govern → act loop. Every spend is Veto-gated before a dollar moves, and a code-enforced ad-ops discipline gate holds still-learning ad sets and clamps budget swings to ±20%. Runs standalone or as MCP tools.
- **[media](agents/media/SPEC.md)** — generates images (and more) for you, **keyless over x402** (fal.ai FLUX). No provider accounts — fund the wallet and go.
- **[build](agents/build/SPEC.md)** — deploys your code on the cheapest infra it can find.
- **[research](agents/research/SPEC.md)** — deep research over paid search + content (Exa / Tavily / x402 sources).
- **[inbox](agents/inbox/SPEC.md)** — handles email, calendar, and scheduling.
- **[groups](agents/groups/SPEC.md)** — runs a Telegram community.

Each agent ships with a default Veto policy (caps, allowlists, intent rules) and a receipts feed showing every action it took and why. Wallet setup is **opt-in** (Reown-connected or an embedded wallet, guarded by Safe + VetoGuard) — never required to try the mock demos.

## How this connects to Veto

Veto already ships the trust substrate:
- **Engine** — 8-stage policy + risk evaluation
- **Receipts** — Ed25519-signed verdicts at `veto-ai.com/r/<uuid>`
- **APPS** — open policy schema
- **Safe + VetoGuard** — on-chain hard-stop: a Veto co-signer on your own Safe (block, never move)

Veto Agents is the **consumer surface** that surfaces all of that. The agents are the front door; Veto is the load-bearing wall behind them. Same primitives, packaged for a non-developer to install and use.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the layers compose.

## Status

Shipping. The flagship `adbuyer` runs end-to-end today (mock or live Meta): the creative studio, the autonomous loop, the LLM-agnostic brain, and the MCP server are all live, with governance fail-closed on every action. The other agents are forkable templates.

## License

MIT. Each agent is a forkable template. Self-host on your own machine using vanilla Hermes, or run via the hosted Veto Agents PWA at `app.veto-ai.com`.

## Where the credit goes

- **Hermes Agent** — Nous Research. The core runtime every agent runs on.
- **Reown + Safe** — wallet connect and the guarded Safe the agents spend from.
- **Veto Protocol** — the governance, receipts, and on-chain enforcement layer.

---

*Veto governs. The rail executes. The agent works.*
