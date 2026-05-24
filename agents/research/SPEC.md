# Research Agent — Spec

> Ask a real question. Get back a real report. The agent spends on paid search, premium content, and any x402-gated source it needs to do the job right.

**Status:** v0 — supporting agent, ships weeks 11–13.

---

## What it does

Takes a research question and produces a synthesized report by orchestrating paid search APIs (Exa, Tavily, Perplexity-style), x402-gated content, paid datasets, and LLM reasoning. Returns a structured report with citations, plus a receipt for every API call.

**Example prompts:**
- *"Research the competitive landscape for AI-driven legal contract review tools in 2026. Budget: $3."*
- *"Find me the 5 most-cited papers on agent governance from the last 6 months."*
- *"Build a market sizing for x402 transaction volume across 2026."*

## Who it's for

- **Founders / operators** doing competitive research, market sizing, due diligence.
- **Analysts** at small funds / firms who don't have an associate to delegate to.
- **Writers / journalists** who need primary-source research.
- **Anyone curious enough** to pay $1–3 for a 20-page synthesized report they can verify.

The buyer: someone who'd otherwise spend an hour on a Perplexity / ChatGPT thread and get a worse result. We're not competing with ChatGPT — we're competing with "I'll do this research myself and waste my morning."

## First 60 seconds

1. Sign in.
2. Wallet provisioned, $5 USDC.
3. Default policy: *"Max $5 per report, $20/month total, prefer cheaper sources."*
4. User types a question.
5. Agent plans: "I'll query Exa (cost ~$0.30), Tavily (~$0.20), pull 5 paywalled articles via x402 (~$0.50), synthesize with Claude ($0.40). Total estimate: $1.40. Proceed?" (Veto authorize fires on each call.)
6. User taps "Go."
7. Streaming progress: queries → sources → synthesis → report.
8. Final report in 2–4 minutes. Cost: $1.32 (under estimate). All sources cited and clickable. Veto receipt per call.

## Tools

| Tool                            | Used for                           | Approx. cost          |
|---------------------------------|------------------------------------|-----------------------|
| `exa.neural_search`             | Semantic web search                | ~$5 / 1k queries      |
| `tavily.search`                 | LLM-optimized search results       | ~$3 / 1k queries      |
| `serpapi.google_search`         | Raw Google search results          | $1 / 1k queries       |
| `firecrawl.scrape`              | Site scraping for content          | per-page              |
| `x402.fetch_paywalled`          | Pay per article (x402-gated)       | $0.01–$0.50 / article |
| `anthropic.claude_sonnet`       | Reasoning + synthesis              | $3 / Mtok input       |
| `nous.hermes_3_405b`            | Synthesis (alt, cheaper)           | varies                |

## Default Veto policy (APPS YAML)

```yaml
policy_name: "research-agent-default"
caps:
  per_transaction_usd: 1.00
  per_day_usd: 5.00
  per_month_usd: 20.00
  human_approval_above_usd: 2.00

allowlist_merchants:
  - api.exa.ai
  - api.tavily.com
  - serpapi.com
  - api.firecrawl.dev
  - api.anthropic.com
  - api.nousresearch.com

categories:
  allow: ["search", "ai_inference", "content_access"]
  block: ["finance", "shopping", "media_generation", "crypto_transfer"]

intent_keywords:
  required: ["research", "find", "summarize", "report", "analyze"]
  forbidden: ["scrape personal data", "doxx"]
```

## Demoability

- **Tangible "this saved me an hour" moment.** Show the user "$1.32 spent in 3 minutes for a 12-page synthesized report with 23 sources." Compare to "an hour on Perplexity for a worse result."
- **The cost breakdown IS the story.** The receipts feed shows "Exa: $0.30, Tavily: $0.20, 5 articles via x402: $0.42, Claude synthesis: $0.40." That's the kind of transparency that builds trust.
- **x402 in production.** This is the most-realistic demo of x402 paying for content — a use case Coinbase / x402.org wants to surface and amplify.

## Pricing posture

- **Free tier:** $5 funded, refilled $0.50/week up to $10. Casual researcher stays free.
- **Pro tier:** $15/month, $20 governed research spend included, then pass-through + 10%.

## Build sketch

```
agents/research/
├── SPEC.md
├── README.md
├── agent.py
├── policy.yaml
├── tools/
│   ├── exa_search.py
│   ├── tavily_search.py
│   ├── serpapi_search.py
│   ├── firecrawl_scrape.py
│   ├── x402_fetch.py        (the most strategically important tool)
│   └── llm_synthesize.py
└── prompts/
    └── system.md            (planning-then-execution, citation-required, cheap-first)
```

System prompt enforces a *plan-then-execute* flow: agent must publish its planned spend before starting. User can edit/approve. This is the "ask before any single spend over $1.50" mechanic made explicit in the agent's behavior, not just the Veto policy.

## v0 scope cut

Ships in v0.1:
- ✅ Exa search
- ✅ Tavily search
- ✅ Firecrawl scrape
- ✅ x402-gated content fetch
- ✅ Anthropic Claude for synthesis (Hermes as fallback)
- ❌ Paid datasets (Bloomberg, FactSet, etc.) — v0.2, needs partnerships
- ❌ Academic database access (JSTOR, etc.) — v0.2
- ❌ Audio source transcription (podcasts as sources) — v0.2

## Success criteria

- First-time user can complete a research task in under 4 minutes for under $2.
- 30 reports generated in week 1 after soft launch.
- Median report has ≥10 cited sources, all clickable.
- 5 users share a report screenshot on X.

## How this composes

- **Research Agent is the canonical x402 consumer demo.** Every paywalled article fetched via x402 is a real on-chain x402 transaction that shows up on x402scan, with a corresponding Veto receipt. This is the cleanest "agent commerce in production" story for any x402 / Coinbase pitch.
- Tightens APPS: Research-agent policy demonstrates per-call cost caps in a content-access context, which is different from media-generation or cloud-deploy. APPS schema gets stronger by serving three distinct shapes.
