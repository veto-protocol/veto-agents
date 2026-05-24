# Media Agent — Spec

> The agent that creates images, video, and audio for you. Set a budget, give it a brief, watch it work. Every dollar it spends is signed, gated, auditable.

**Status:** v0 — lead agent, ships first.
**Headline.**

---

## What it does

Takes a creative brief in plain English and produces media artifacts — images, short videos, voiceovers, music — by orchestrating calls to paid generative-AI APIs within a budget you set. Returns the artifacts to you in chat with a per-asset cost breakdown and Veto receipt for every API call.

**Example prompts a user might type:**
- *"Make a 6-second video of a neon jellyfish drifting through cyberpunk city rain, square aspect, for $1."*
- *"Generate 5 product-shot variants of a ceramic mug with steam, white background, photoreal, $2 budget."*
- *"Voice this script in a calm British male voice and an upbeat American female voice. 45 seconds each."*

## Who it's for

- **Solo creators** running TikTok / Instagram / YouTube shorts, who'd otherwise pay per-tool subscriptions to Runway + ElevenLabs + Midjourney + Replicate.
- **Founders & marketers** generating landing-page assets, product shots, ad variants, voiceovers.
- **Hobbyists / experimenters** who want to play with generative AI without standing up four accounts and credit cards.

The unifying buyer profile: someone who values their time more than the per-call cost, and is *acutely* nervous about leaving an AI agent unsupervised with their money. The Veto budget cap and receipts feed are the only reason they'd trust this at all.

## First 60 seconds (the user experience)

1. User lands on `app.veto-ai.com/media` from a tweet, install prompt, or referral.
2. **Sign in** — email magic link (5 seconds, no password).
3. **Wallet is provisioned** — Privy creates an embedded wallet on Base, funded with $5 USDC by us as the free-tier credit. User sees: *"Your agent has $5 to start. Add more anytime."*
4. **Default policy preset is applied** — *"Max $2 per generation, $10/month total, ask before any single spend over $1.50."* Editable in plain English.
5. User types: *"Make a 6-second video of a cat surfing on a slice of bread."*
6. Agent thinks, calls `replicate.video_gen(model=runway-gen3, prompt=…)` — Veto authorize fires before the call. Allowed.
7. Video streams back in 30–45 seconds. Cost: $0.42. Receipt: `veto-ai.com/r/<uuid>`.
8. User sees the video, the cost, and the signed receipt with one click.

That whole sequence is the demo video on the marketing site.

## Tools (paid APIs)

| Tool                          | What it produces                       | Approx. cost          |
|-------------------------------|----------------------------------------|-----------------------|
| `replicate.image_gen`         | Image (Flux, SDXL, etc.)               | $0.005 – $0.03 / image |
| `replicate.video_gen`         | Video (Runway Gen-3, Luma, Hailuo)     | $0.30 – $1.50 / 6s clip |
| `runway.video_gen` (direct)   | Video via Runway API                   | ~$0.10 / second of Gen-3 |
| `elevenlabs.voice_synth`      | Speech-to-voice                        | ~$0.30 / 1,000 chars  |
| `elevenlabs.music`            | Music tracks                           | varies                |
| `replicate.audio_gen`         | Music / SFX via Replicate              | varies                |
| `openai.image_edit` (DALL-E)  | Image edits / variations               | $0.04 / image         |

Each is implemented as a Veto-gated wrapper in `tools/`. The wrapper:
1. Computes the per-call cost from the tool's pricing.
2. Calls Veto `authorize` with `{action_type: "api_call", merchant: <tool>, amount: <cost>, …}`.
3. On `allow`, calls the tool, captures the artifact, attaches the receipt URL to the result.
4. On `deny` or `escalate`, refuses or pauses.

## Default Veto policy (APPS YAML)

```yaml
policy_name: "media-agent-default"
caps:
  per_transaction_usd: 2.00
  per_day_usd: 5.00
  per_month_usd: 25.00
  human_approval_above_usd: 1.50

allowlist_merchants:
  - replicate.com
  - api.runwayml.com
  - api.elevenlabs.io
  - api.openai.com

blocklist_merchants: []

time_windows:
  active_hours: "always"   # Media agent runs whenever you ask

rate_limits:
  txs_per_hour: 30
  txs_per_day: 100

categories:
  allow: ["ai_inference", "media_generation"]
  block: ["finance", "crypto_transfer", "shopping"]

intent_keywords:
  forbidden: ["nsfw", "celebrity", "child"]   # block illegal / brand-unsafe generations
  required: []
```

User edits these in the PWA via a plain-English form. We translate UI to YAML on the backend. APPS schema is the source of truth.

## Demoability — why this leads the marketplace

- **Outputs are visual and shareable.** A 6-second video the agent made for $0.40 is a tweet that travels. The screenshot of the cost + receipt + artifact is the marketing material itself.
- **Veto is visible without being intrusive.** "Your agent spent $0.40 to make this. Here's the signed receipt." That sentence sells the trust thesis without jargon.
- **First-run wow factor.** Most users have never had an AI agent with its own funded wallet. Watching the agent generate, pay, and produce in 45 seconds is a *moment*. We need that.

## Pricing posture

- **Free tier:** $5 of pre-funded USDC on signup, refilled $1/week up to $10 total — so a casual user can use the agent forever without paying us. Marketing cost, not product cost.
- **Pro tier (later):** $19/month flat, $50 of usage included, then pass-through pricing + a 10% Veto fee on top.
- We do NOT charge per-call directly to the user in v0. Friction kills first-run. Pricing comes in v0.3.

## Build sketch

```
agents/media/
├── SPEC.md                  (this file)
├── README.md                (install + run instructions)
├── agent.py                 (Hermes agent module — system prompt, tool registration, skills)
├── policy.yaml              (default Veto policy, as above)
├── tools/
│   ├── __init__.py
│   ├── replicate_image.py   (Veto-gated wrapper)
│   ├── replicate_video.py
│   ├── runway_video.py
│   ├── elevenlabs_voice.py
│   ├── elevenlabs_music.py
│   └── openai_image.py
└── prompts/
    └── system.md            (agent persona + behavior + budget awareness)
```

`agent.py` registers each tool with Hermes's tool-registration API, wrapped through a single `veto_gate(tool_fn, cost_estimator)` decorator that does the authorize call before invocation.

`prompts/system.md` includes explicit instructions to the LLM about budget awareness, asking for permission on big spends, and preferring cheaper models when quality is comparable. This is *prompt-level* discipline; Veto enforces it at the gate.

## v0 scope cut

What ships in v0 (weeks 1–6):
- ✅ image gen via Replicate (Flux model — cheapest, good quality)
- ✅ video gen via Replicate (Runway Gen-3 model)
- ✅ voiceover via ElevenLabs
- ✅ chat UI + receipts + budget bar
- ❌ music generation (v0.1)
- ❌ image editing / inpainting (v0.1)
- ❌ Midjourney (no public API; v0.2 if they ship one)
- ❌ batch jobs / queued workflows (v0.2)

## Success criteria for v0

- A first-time user can sign up, generate a video, and see the receipt in under 90 seconds end-to-end.
- 50 users in week 1 after soft launch.
- 5 of them produce a screenshot they'd be willing to share on X.
- One of those screenshots gets >50 likes from non-followers (i.e., crosses out of our bubble).

## Open questions

1. Do we ship a sample-gallery onboarding ("here are 5 things people made — try one of these prompts") or jump straight to the prompt box? Lean: gallery, lowers cold-start friction.
2. Do we let users export the agent's generations to their own storage (Dropbox, Drive) or keep them in our app? Lean: in-app for v0, export in v0.2.
3. Should the receipts feed be public-by-default for showcase value, or private-by-default for user trust? Lean: private, with one-click "make this generation public" if the user wants to show off.

## How this composes with the rest of Veto

- Every tool call is a Veto authorize → live receipts on `veto-ai.com/receipts`.
- The Media agent's spending activity contributes to the public *State of Agent Commerce* dashboard (aggregate, anonymized).
- The `policy.yaml` is APPS-format, demonstrates APPS in a consumer use case, helps APPS adoption.
- If the user wants to spend on a non-allowlisted merchant, the *escalate* flow lands in the PWA approval UI — same flow Veto already uses elsewhere.
