# Veto Agents — Architecture

How the layers compose, and the tooling decisions behind each.

## The stack, top to bottom

```
┌─────────────────────────────────────────────────────────────┐
│  app.veto-ai.com  (PWA — React + Tailwind, mobile-first)    │
│  • chat UI per agent                                        │
│  • receipts feed                                            │
│  • plain-English policy editor                              │
│  • wallet view + funding                                    │
└─────────────────────────────────────────────────────────────┘
                            │
┌─────────────────────────────────────────────────────────────┐
│  Veto Agents API  (FastAPI — agent runner service)          │
│  • spins up + supervises Hermes Agent per user              │
│  • mediates every tool call through Veto                    │
│  • streams chat + tool events via WebSocket                 │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼──────────────────────┐
        │                   │                      │
┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ Hermes Agent │  │  Veto authorize  │  │  Privy embedded  │
│  (per user)  │  │  (every tool     │  │  wallet (per     │
│              │  │   call gated)    │  │   user, on Base) │
│ • LLM brain  │  │                  │  │                  │
│ • toolset    │  │ • policy check   │  │ • USDC balance   │
│ • skills DB  │  │ • risk score     │  │ • sign tx        │
│ • cron       │  │ • signed receipt │  │ • no seed phrase │
└──────────────┘  └──────────────────┘  └──────────────────┘
        │                   │                      │
        ▼                   ▼                      ▼
   Tool APIs           Veto Engine            Base + USDC
   (Replicate,         (Django, prod)         (x402 facilitator
   Vercel, Exa,                                via Coinbase CDP)
   Gmail, etc.)
```

## Tooling decisions

### Agent core: Hermes Agent (Nous Research)

**Why Hermes:** Open-weights, MIT, fastest-growing agent runtime of 2026 (140K+ GitHub stars, most-used agent on OpenRouter). Multi-provider LLM support (Nous Portal, OpenRouter, Anthropic, OpenAI, NVIDIA NIM, Hugging Face) means no vendor lock — users pick the brain. Skills system (procedural memory) means agents get better with use. All data stays local in SQLite.

**Why not OpenClaw:** OpenClaw is brilliant for messaging-first agents (WhatsApp, Telegram, Slack), but its UX assumes a chat-app gateway. Veto Agents is a web-app surface; Hermes fits the model. We will ship an **OpenClaw + Veto plugin** as a v0.2 distribution wedge, not as a v0 dependency.

**How we integrate:**
- Each user gets a dedicated Hermes Agent instance, sandboxed.
- We use Hermes's tool registration API to add Veto-gated wrappers around every tool that spends money or sends external requests.
- The pre-execution hook on each tool call dispatches to Veto's `authorize` endpoint. If denied, the tool refuses; if allowed, it proceeds and the verdict's receipt URL is attached to the chat message.
- The agent's LLM provider is hosted by Veto on a free tier (Hermes 3 via Nous Portal, eaten cost) with an OpenRouter fallback users can configure.
- Persistence: Hermes's SQLite + our own Postgres for the per-user receipts feed, policy versions, and audit log (the same backend as veto-ai.com).

### Wallet: Privy

**Why Privy:** Production-grade embedded wallets with a dedicated `create-privy-pwa` template, Base (chain ID 8453) supported natively, USDC sending built in, login via email / Google / passkey, *users never see a seed phrase*. The whole point of "agents for everyone" is that wallet provisioning is invisible.

**How we use it:**
- On user signup (magic link), Privy provisions an embedded wallet automatically.
- We fund $5 USDC on first run as a free-tier promo (sponsored by Veto, paid out of marketing budget). Users add more via Coinbase onramp or direct USDC deposit.
- Every agent action that costs money signs through the user's Privy wallet — agent has *no key* of its own; it asks Privy (via the user's session) to sign each transaction, which then runs through Veto's policy gate before being broadcast.

### Payment rails

| Rail               | Used for                                | Live in v0? |
|--------------------|-----------------------------------------|-------------|
| x402 (Coinbase)    | Paid APIs that accept HTTP 402          | Yes         |
| Direct USDC on Base| Crypto-native merchants, on-chain swaps | Yes         |
| Anthropic / OpenAI keys | LLM inference billed to operator   | Yes (BYOK or hosted) |
| Stripe Issuing virtual cards | Card-only merchants            | v0.3        |

### Veto governance layer

**Reuses what's already shipped:**
- `gateway/views.py` `authorize` endpoint — every tool call dispatches here.
- `safety/services/engine.py` — 8-stage evaluation.
- Signed receipt at `veto-ai.com/r/<uuid>` — every verdict.
- `policies/models.py` `SecurityPolicy` — per-agent policy lookup.

**New plumbing for agents specifically:**
- Per-agent default policy templates (Media has different defaults than Build).
- Agent-context fields in the authorize request (`agent_type=media`, `tool_name=replicate.video_gen`, `cost_usd`).
- Receipts feed grouped by agent (so the user sees "Media agent's activity" vs "Build agent's activity").

### Frontend

**PWA, not native.** `app.veto-ai.com` as a Progressive Web App built with React + Tailwind (same stack as the landing). Installable to home screen on iOS + Android. Skips App Store gatekeeping for v0, which matters because crypto + payment apps get savaged in Apple review. Native wrappers via Capacitor or Expo come in v0.4 once we have signal.

## Repository shape

```
veto-agents/
├── README.md               (manifesto)
├── ARCHITECTURE.md         (this file)
├── agents/
│   ├── media/SPEC.md       (lead agent)
│   ├── build/SPEC.md
│   ├── research/SPEC.md
│   └── inbox/SPEC.md
└── (future) api/ + app/    (runner service + PWA)
```

Each agent directory will eventually contain:
- `SPEC.md` — what it does, scope, demo
- `agent.py` — Hermes-compatible agent module
- `policy.yaml` — APPS-format default Veto policy
- `tools/` — tool implementations + Veto-gated wrappers
- `README.md` — install + use instructions

## Authorize flow, end to end

For any agent action that spends money or touches external systems:

1. Agent's LLM decides to call a tool (e.g., `replicate.generate_video(prompt="…")`).
2. Hermes tool dispatcher hits our pre-execution hook.
3. Hook builds a Veto authorize request:
   ```json
   {
     "agent_id": "media-user-abc",
     "action_type": "api_call",
     "merchant": "replicate.com",
     "amount": 0.40,
     "currency": "USD",
     "description": "Generate 6s video, model=runway-gen3",
     "context": { "agent_type": "media", "tool_name": "replicate.video_gen" }
   }
   ```
4. POST to `https://veto-ai.com/api/v1/authorize/`.
5. Engine runs 8 stages, returns `{ verdict, reason_codes, receipt_jwt, receipt_url }`.
6. If `allow`: tool proceeds. Receipt URL attached to the chat message.
7. If `deny`: tool refuses. User sees "Veto stopped this — reason: monthly cap exceeded. Adjust policy?"
8. If `escalate`: tool waits. User gets a phone notification with approve/deny.

This is the same authorize flow Veto already serves — we're just adding agent-specific context and the per-agent default policies.

## What we are NOT building

- A new agent framework (Hermes is the core).
- A new LLM (we route to existing providers).
- A custodial wallet (Privy holds keys; we never see them).
- A new payment processor (we ride x402 facilitators + existing card networks).

Veto Agents is a *packaging + governance* layer on proven primitives. The work is in the integration glue, the UX, and making it *feel* trustworthy enough for someone's mom to use.
