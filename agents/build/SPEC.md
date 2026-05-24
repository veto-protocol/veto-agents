# Build Agent — Spec

> The agent that deploys your code on the cheapest infra it can find, governed by you. Set a monthly cap, give it a repo, it ships. Every dollar of cloud spend is signed.

**Status:** v0 — second agent, ships weeks 7–10. Headline-tier for the dev audience.

---

## What it does

Takes a code repository (yours) and a job (deploy / run / benchmark / batch-process) and executes it across the cheapest available infra provider — Vercel, Modal, Replicate, Fly.io, GitHub Actions — within your monthly budget. Returns deployment URLs, run logs, and cost breakdowns. Every cloud purchase is a Veto-signed verdict.

**Example prompts:**
- *"Deploy this Next.js repo. Pick the cheapest provider. Budget: $20/month."*
- *"Run this fine-tuning job on a single A100, max $50."*
- *"Tear down anything I deployed last month that hasn't been hit in 14 days."*
- *"Find me the cheapest GPU for stable diffusion inference under 2-second latency."*

## Who it's for

- **Independent devs / indie hackers** shipping side projects who don't want to think about Vercel-vs-Render-vs-Modal pricing.
- **Small startup teams** without DevOps headcount who need someone (something) watching cloud spend.
- **AI researchers** running training/inference jobs who want budget guardrails on GPU rentals.

Differentiator vs. Vercel CLI, Devin, Cursor, GitHub Copilot Workspace: **none of them govern cloud spend.** They generate code, deploy, write tickets. They don't *bound* what they cost. Build Agent's only point is bounded autonomous infra management. Veto is the reason.

## First 60 seconds

1. User signs in.
2. Wallet provisioned (Privy, Base, $5 USDC).
3. Connect GitHub via OAuth (one click).
4. Default policy: *"Max $5/deploy, $50/month total, only Vercel + Modal + Fly, ask before any single charge over $10."*
5. User types: *"Deploy `tommy/landing-page` to Vercel."*
6. Agent thinks. Calls Vercel deploy API. Veto authorize fires (`merchant=vercel.com, amount=$0.00 first month, $0.20/month thereafter`). Allowed.
7. Deploy URL streams back. Cost: $0.00 (free tier). Receipt: `veto-ai.com/r/<uuid>`.
8. User shares URL. Done in 90 seconds.

## Tools (paid APIs)

| Tool                          | Used for                                    | Approx. cost          |
|-------------------------------|---------------------------------------------|-----------------------|
| `vercel.deploy`               | Static + serverless deployments             | $0–$20/mo per project |
| `modal.run`                   | GPU jobs, serverless Python                 | per-second compute    |
| `replicate.run_model`         | Hosted model inference                      | per-call              |
| `fly.deploy`                  | Always-on apps (with sleep)                 | $1.94+/mo per machine |
| `github_actions.trigger`      | CI runs                                     | free up to limit      |
| `runpod.gpu_rent`             | On-demand GPU                               | $0.30+/hr             |
| `cloudflare.deploy`           | Workers, Pages, R2                          | free + usage          |

Each is a Veto-gated wrapper. The cost estimator is the tricky bit: cloud pricing is often unit-rate × usage, so we have to estimate per-call and reconcile actual usage on a periodic settlement basis.

## Default Veto policy (APPS YAML)

```yaml
policy_name: "build-agent-default"
caps:
  per_transaction_usd: 5.00
  per_day_usd: 15.00
  per_month_usd: 50.00
  human_approval_above_usd: 10.00

allowlist_merchants:
  - vercel.com
  - modal.com
  - replicate.com
  - fly.io
  - runpod.io
  - cloudflare.com

blocklist_merchants:
  - aws.amazon.com           # opt-in only; AWS spend explodes fast
  - gcp.google.com
  - azure.microsoft.com

rate_limits:
  txs_per_hour: 20
  txs_per_day: 50

categories:
  allow: ["cloud_infra", "ai_inference", "deploy"]
  block: ["finance", "shopping", "media_generation"]

intent_keywords:
  required: ["deploy", "run", "host", "build", "compute"]
  forbidden: ["bitcoin miner", "mining", "scrape"]
```

## Demoability

- **Dev-share-worthy.** "My agent deployed my repo and it cost $0.02 — receipt verifiable here." Shareable on dev Twitter and Hacker News.
- **Tangible savings.** A demo showing "I asked the agent to deploy this, it picked Cloudflare Pages over Vercel because it's free for this use case, here's the receipt for $0." That's a story.
- **Budget guardrails as the headline.** "Even if my agent goes rogue, it can't burn more than $50 this month. The wallet runs out, end of story." Hard cap as a feature.

## Pricing posture

- **Free tier:** $5 of USDC, refilled $2/week up to $10. Casual side-project deployers stay free forever.
- **Pro tier:** $29/month, $100 of governed cloud spend included, then pass-through + 10%. Targeted at indie hackers / small startups.
- **Team tier (later):** SSO, shared budgets, per-engineer caps.

## Build sketch

```
agents/build/
├── SPEC.md
├── README.md
├── agent.py
├── policy.yaml
├── tools/
│   ├── vercel_deploy.py
│   ├── modal_run.py
│   ├── replicate_run.py
│   ├── fly_deploy.py
│   ├── github_actions.py
│   ├── runpod_gpu.py
│   └── cloudflare_deploy.py
└── prompts/
    └── system.md            (cost-conscious, prefers cheapest provider, explains tradeoffs)
```

The system prompt instructs the agent to always compare ≥2 providers before deploying, choose the cheapest that meets requirements, and report the tradeoff in the chat. Veto enforces the spend cap; the prompt enforces good behavior.

## v0 scope cut

Ships in v0.1 (weeks 7–10):
- ✅ Vercel deploy (no-config, supports Next, Vite, plain HTML)
- ✅ Modal run (Python jobs only)
- ✅ Replicate run_model (any public Replicate model)
- ✅ GitHub OAuth for repo access
- ❌ Fly.io, Runpod, Cloudflare (v0.2)
- ❌ Multi-repo orchestration / monorepo handling (v0.2)
- ❌ Automatic teardown / sleep policies (v0.2 — high value but needs more thought)

## Success criteria

- A dev with GitHub + a repo can deploy + get a live URL in under 2 minutes.
- 100 deploys in week 1 after soft launch.
- 10 of those produce a screenshot the dev shares on X or HN.
- One Hacker News front-page mention.

## Open questions

1. Do we ship our own GitHub App or use OAuth-only? Lean: OAuth-only for v0, App for v0.2 to get webhook support.
2. How do we handle reconciliation when a tool's actual cost differs from our estimate (e.g., Modal job ran for 8 minutes vs. our 4-minute estimate)? Lean: settle on hourly basis; if reconciliation exceeds policy, post a deny-after-the-fact alert.
3. Do we expose the agent's provider-comparison reasoning to the user, or just the decision? Lean: collapsible "why I picked this" section.

## How this composes with the rest of Veto

- Every deploy / run / GPU rental is a Veto authorize → on the public receipts feed.
- The Build Agent's spending data feeds aggregate "what AI agents are deploying in 2026" research that becomes blog content / press / lens-of-the-category positioning.
- Default policy.yaml is an excellent APPS demonstration for cloud-spend governance — a different shape from Media's per-call API governance.
- Public `/r/<uuid>` receipts for deploys become a kind of "Vercel-receipts-on-blast" — devs love sharing infra costs publicly.
