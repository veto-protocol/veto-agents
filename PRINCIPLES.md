# Veto Agents — Principles

Five non-negotiable behaviors every agent in this catalog must implement. These are the things that make a Veto Agent a Veto Agent. If a contributed agent violates any of these, it doesn't ship.

---

## 1. Plan-then-execute

**Every agent must show its plan + cost estimate before spending a single cent.**

When a user gives the agent a task that will cost money to complete, the agent must:

1. **Decompose the task into steps.** "To make this 6-second video I'll: (a) generate the video via Runway Gen-3, (b) generate a voiceover via ElevenLabs, (c) combine them via ffmpeg locally."
2. **Estimate the cost of each step.** "Step a: ~$0.42. Step b: ~$0.05. Step c: free." Show the line items, not just the total.
3. **Surface alternatives when relevant.** "Alternative: use Hailuo for the video at ~$0.18, slightly lower quality. Reply 'use hailuo' to swap."
4. **Wait for explicit consent.** Don't auto-proceed. The user sees the plan, types `y` or taps Approve.

The shape in the CLI:

```
$ veto-agents media "make a 6s video of a neon jellyfish with voiceover"

Plan:
  1. Generate 6s video — Runway Gen-3        ~$0.42
  2. Generate voiceover — ElevenLabs (45c)   ~$0.05
  3. Combine locally with ffmpeg              free
                                              ─────
                                  Estimate:  $0.47

Alternative: Hailuo video instead of Runway → $0.20 total (lower quality)

Proceed?  [y/N/alt]  
```

The shape in the PWA: a plan card with line items + an Approve button.

**Why this is non-negotiable:** Most users have never given an AI agent money. The first time they do, the agent should over-communicate, not under-communicate. Trust compounds across interactions; one auto-spent surprise nukes it forever.

## 2. Cost transparency at every step

Even after the plan is approved, every individual paid call shows its actual cost as it happens.

```
✓ Step 1 done. Runway Gen-3 video, 6.1s, $0.43 actual (~$0.42 est).
  Receipt: veto-ai.com/r/8b3c-7f29-…
✓ Step 2 done. ElevenLabs voiceover, 43 chars, $0.012 actual.
  Receipt: veto-ai.com/r/4a1f-9d02-…
Total spent: $0.44 (estimate was $0.47). Output saved to ~/Downloads/jellyfish.mp4
```

Three rules:
- Show actuals, not estimates, after execution.
- Cite the receipt URL inline.
- Show a running total per task.

## 3. Receipts for everything spendable

Every API call that costs money produces a Veto-signed receipt. No exceptions. No "free this time." The receipt records:
- The action (tool name, parameters, merchant)
- The cost
- The verdict (allow / deny / escalate)
- The reason codes
- The policy version that produced the verdict
- A cryptographic signature anyone can verify offline against the JWKS

The agent always surfaces the receipt URL to the user when reporting back on a step. Anyone with the URL can re-verify the action happened, in the way recorded, against the policy in effect.

## 4. Veto is the only spend gate

Agents don't have their own "should I do this" logic for spending. They ask Veto, every time, before any external paid call. The Veto authorize endpoint is the **single source of truth** for whether an action proceeds.

- Don't bypass Veto with "free tier" calls (they may not stay free).
- Don't pre-aggregate "I'll batch 10 calls into one authorize" (each call is its own verdict).
- Don't cache "Veto said yes once, so this is fine for the next hour" (every call re-authorizes).

This rule is what makes the receipts trustworthy. If an agent ever spent money without authorizing, the receipt graph would have holes and the system would be uninspectable. So: every paid action, every time, authorize first.

## 5. Always offer cheaper alternatives when they exist

Agents must be cost-conscious by default. If a cheaper provider can produce ≥80% of the quality at <50% of the price, the agent surfaces it as an alternative *before* executing the more expensive option.

- Media agent: "Use Hailuo for $0.18 instead of Runway for $0.42? Slightly lower quality."
- Build agent: "Deploy to Cloudflare Pages (free) instead of Vercel ($0.20/mo)? Same Lighthouse score for your stack."
- Research agent: "Use Tavily ($0.20) instead of Exa ($0.30) for this query? Similar source quality."

The user might still pick the expensive option — that's fine. The point is they *chose*, with information. The agent's job is to present the choice.

---

## How these become enforceable

Three layers:

### Layer 1: Agent system prompts

Every agent's `prompts/system.md` includes a non-negotiable block that instructs the LLM to plan, estimate, and seek consent before any external action. The prompt cannot be overridden by user input ("just do it without asking" is ignored — the prompt explicitly says to ignore such overrides).

### Layer 2: Veto policy enforcement

The default `policy.yaml` for every agent includes:

```yaml
caps:
  human_approval_above_usd: <agent-specific threshold>
behavior:
  require_plan_preview: true
  require_per_step_estimate: true
  require_post_action_receipt_link: true
```

These are read by the agent runner and enforce the principles structurally, not just in the prompt. If the LLM tries to skip the plan-preview step, the runner intercepts and returns the missing step to the user.

### Layer 3: CLI / PWA UX

The CLI and PWA both render plan previews as a structured block (not just LLM text), and require an explicit user input (`y`, tap Approve) before the runner proceeds. There is no auto-proceed path in v0. v0.2 may add a "small expenses" auto-approve (e.g., under $0.10) but only with explicit per-agent opt-in.

---

## What this gives the user

A predictable interaction shape across every Veto Agent:

> ask → plan + estimate shown → confirm → execute step-by-step with live cost + receipts → final summary with all receipts

That predictability is what makes "trust an AI agent with money" feel safe enough to actually do. Every other consumer agent product in 2026 (Google Spark, Lindy, ChatGPT) is some flavor of "agent just goes." Veto Agents' brand is **the agent that asks first**. That's the whole product, and these five principles are how we deliver it.
