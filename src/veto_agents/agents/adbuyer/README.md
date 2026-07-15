# adbuyer — an autonomous AI media buyer that literally can't overspend your budget

It runs your Meta ads 24/7 on a standing goal — observing performance, scaling
winners, killing losers — and **every spend decision it makes is gated by
[Veto](https://veto-ai.com) before a single dollar moves.** Deploy it once, walk
away: the agent thinks for itself, but its downside is bounded in code, not in a
prompt.

---

## The wow

Real captured output (a few per-run header lines trimmed). A pure-rules brain
(no LLM), **real Veto authorize** against veto-ai.com, mock Meta account so you
can reproduce it with zero setup:

```
Veto Agents — adbuyer autonomous loop
  MOCK:     mimicking Meta offline — NO real account, NO real spend. Real Veto + discipline
gates still run on every action.
  goal:      grow signups, US, up to $30/day
  brain:     heuristic (pure rules, no LLM)
  discipline: respect_learning=True · >=4d & (>=50 conv or >=2000 impr) · cooldown 3d ·
budget +/-20%
  Veto governs every autonomous decision before Meta is touched. Ctrl-C to stop.

— cycle 1 —  goal: grow signups, US, up to $30/day
  observed · campaigns=1 adsets=3 ads=3 · spent=$128.40 cap=$210.00
    · US · Lookalike 1% (winner) [ACTIVE/SUCCESS] $20.00/day — CTR 3.2958% · $46.80 spent · 14200 impr
    · US · Broad interest (underperformer) [ACTIVE/SUCCESS] $15.00/day — CTR 0.7044% · $79.50 spent · 15900 impr
    · US · New creative test (learning) [ACTIVE/LEARNING] $10.00/day — CTR 1.6667% · $2.10 spent · 540 impr
  brain: heuristic brain: scale the winner, pause the loser, and (naively) try to scale the
learning ad set — the discipline gate will hold that one.
  -> adjust_budget adset 23851000034001: $20.00/day -> $24.00/day
    rationale: Clear winner: 3.30% CTR, out of learning — scale the daily budget ~20% to capture more.
    ready: ready — 8.2d old, 14200 impr, 562 conv, past 3d cooldown
    Veto: allowed ✓
    done.
  -> pause adset 23851000034002
    rationale: Clear loser: only 0.70% CTR on $79.50 spent, out of learning — pause it to stop the bleed.
    ready: pause allowed (exempt from learning/data checks; past cooldown)
    Veto: allowed ✓
    done.
  -> adjust_budget adset 23851000034003: $10.00/day -> $12.00/day
    rationale: New ad set shows an early pulse (1.67% CTR); a naive buyer would scale it — but it is still in LEARNING.
    holding: ad set still in LEARNING phase — waiting for it to stabilize before acting
  cycle 1 summary: executed=2, held=1
```

> **It tried to scale a still-learning ad set. It got held. On real money, that
> guarantee is the product.**

The agent *proposed* scaling the learning ad set — a rookie move that would reset
Meta's learning phase and burn budget. Its own **code** discipline gate refused,
*upstream of Veto*, so the action never even reached the money gate. The winner
scaled +20% and the loser got paused, both cleared with `Veto: allowed ✓`.

Reproduce it right now, no Meta account:

```bash
veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock --once --no-llm
```

---

## Quickstart

Everything below works today. Copy-paste.

```bash
# 1. Install (isolated env via pipx — needs Python 3.10+, macOS/Linux, Windows via WSL)
curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash

# 2. Guided setup: sign in → pick an LLM brain → optional creative providers → optional
#    Meta ad account → budget guardrails. Writes 0600 configs, never prints a secret.
veto-agents adbuyer-setup

# 3. (Optional, recommended) Teach it your brand — drop your site URL (or a txt/md
#    dump from another agent). It extracts product, audience, tone, voice rules and
#    colors into an editable ~/.veto/brand.yaml; every ad it makes matches YOUR brand.
veto-agents brand set https://yourdomain.com

# 4. Make an ad — no Meta account needed. One concept → copy + hero image (+ optional
#    video/voice), each paid asset Veto-gated. Drops into ~/Downloads/veto-studio/.
veto-agents create "premium cold-brew coffee for busy founders, launch week"

# 5. Run the autonomous loop with a mock Meta account (no account, no real spend —
#    but real Veto + real discipline gate on every action).
veto-agents adbuyer --goal 'grow signups, US, up to $30/day' --mock

# 6. Or drop it into Claude Code / Claude Desktop / OpenClaw as MCP tools.
claude mcp add veto-agents -- veto-agents mcp
```

When you're ready for real spend, `adbuyer-setup` wires your **own** Meta ad
account (BYO token) and you drop `--mock`:

```bash
veto-agents adbuyer --goal 'US traffic to https://mysite.com, keep CPC under $1'
```

Scriptable, non-interactive setup:

```bash
veto-agents adbuyer-setup -n --llm-provider claude --daily-budget 25 --creative-cap 0.25
```

---

## The two guarantees (why it's safe to walk away)

### 1. Fail-closed Veto on every spend

For **every** action with a spend implication, the loop calls
`veto_authorize(action="payment", merchant="facebook.com", amount=<usd>, context=<the agent's OWN rationale>)`
*before* touching Meta:

| Verdict | What happens |
|---|---|
| `allow` | execute the Meta write |
| `deny` | log + skip the action |
| `escalate` | log + notify (console + receipt URL) + skip |
| authorize **exception** | skip — **fail-closed, never fails open** |

The loop never freezes on a deny/escalate/error — it skips the one action and
keeps running. And because governance lives *inside* the tool, **a host LLM
calling the MCP tools cannot bypass it.**

### 2. Ad-ops discipline — code-enforced, independent of the LLM

A readiness gate (`_is_actionable`) runs *before* the Veto gate. **HOLD (do
nothing) is the default, valid outcome.** `adjust_budget` / `resume` /
`refresh_creative` are held unless the ad set:

- is **out of Meta's `LEARNING` phase** (touching a learning ad set resets it),
- has run **≥ `min_days_before_action` (4) days** *and* has **≥ `min_conversions_before_action` (50) conversions OR ≥ `min_impressions_before_action` (2000) impressions**, and
- is **past `cooldown_days_per_entity` (3) days** since the last action on it.

Any budget change is **clamped to ±`max_budget_change_pct` (20%)** of current —
never rejected, just made gradual. `pause` is exempt from the learning/data bar
(kill a runaway anytime) but still respects cooldown. Cooldowns persist in
`~/.veto/adbuyer_state.json`, written only after an action executes.

Config lives in `policy.yaml → ad_ops:`; edit via `veto-agents policy edit adbuyer`.

### Proven under chaos — 900 simulated days, zero violations

The repo ships a stochastic simulation harness (`tests/sim/`) that runs the
agent through 6 adversarial scenarios × 5 seeds × 30 simulated days — creative
fatigue, market collapse, pure noise, late bloomers, flaky APIs — **900 cycles
in ~3.5 s**, with every governance invariant machine-checked: never past ±20%
per step, never touched a learning ad set, never broke a cooldown, never
exceeded the spend cap, never raised a budget after a collapse, zero unhandled
exceptions. Run it yourself: `python -m tests.sim.harness --sweep`.

### Non-custodial — Veto governs decisions, never holds your money

Ad spend goes to *your own* Meta ad account (BYO `META_ACCESS_TOKEN` /
`META_AD_ACCOUNT_ID` / `META_PAGE_ID`, read locally from `~/.veto/meta.env`,
never printed, logged, or stored by Veto). Meta's own account spend cap is the
server-side backstop. Creative micro-spends (x402) are funded from a
Veto-guarded Safe you control. Veto holds no keys and moves no money — it only
says allow / deny / escalate.

---

## How it works

### Creative studio — `veto-agents create`

One LLM **director** derives ONE creative concept (theme + tone), then derives
every asset from it so they all match: copy → hero image (+ optional
video/voice).

```bash
veto-agents create "eco running shoe" --image-provider fal --no-video
veto-agents create "SaaS onboarding tool" --all
```

- **Copy is free** — one director call crystallizes the concept and writes
  `copy.md` (headlines, primary texts, CTAs, voiceover script, and derived
  image/video prompts).
- **Each PAID asset is Veto-authorized *inside its provider driver*** before the
  provider is called. Deny/escalate → asset `status:"denied"` with `verdict` +
  `receipt_url`, never generated.
- Providers: image via OpenAI `gpt-image-1` (BYO) or **free fal.ai FLUX over
  x402** (auto-fallback when no OpenAI key); video via Higgsfield (BYO); voice
  via ElevenLabs (BYO). Missing keys degrade to `status:"skipped"`.
- Flags: `--image-provider <openai|fal>`, `--video/--no-video`,
  `--voice/--no-voice`, `--all`, `--out <folder>` (default
  `~/Downloads/veto-studio/`). No Meta account required.

### The autonomous loop — `veto-agents adbuyer`

Every `--interval` minutes (default `0` = use policy
`ad_ops.observe_interval_minutes` = 360 / 6h — a real media buyer observes often,
acts rarely):

```
OBSERVE → DECIDE → DISCIPLINE → GOVERN (Veto) → ACT → RECORD
```

The brain may only propose `adjust_budget`, `pause`, `resume`,
`refresh_creative` on **existing** entities — it may not create new
campaigns/ad sets/ads (rejected before execution). Meta client is Graph API
v25.0. Deep-dive on the control loop and autonomy-scope table is in the sections
below — this is the canonical reference.

Safe ways to try it:

- `--mock` — mimic Meta fully offline against seeded, evolving fake campaigns.
  No account, no spend; **real** Veto + discipline gates still run.
- `--dry-run` — run OBSERVE + DECIDE + the Veto authorize gates (you see every
  verdict + receipt URL), skip all Meta writes.
- `--once` — one cycle, then exit.
- `--no-llm` — pure-rules heuristic brain, no model key (implied by `--mock`
  when no key is present).

### LLM-agnostic brain

One shared `structured_llm` client routes by `cfg.llm_provider` / `cfg.llm_model`
across two wire branches — `anthropic` (Claude, forced tool-use) and
`openai_compat` (OpenAI, OpenRouter, Hermes, Ollama, any local/custom
OpenAI-compatible endpoint). Registered providers: `claude` (default, Sonnet
4.6 / Opus 4.7), `openai`, `grok`, `kimi`, `gemini`, `deepseek`, `openrouter`,
`hermes` (Nous, free tier), `ollama` (self-hosted, no key), `custom`. It
auto-detects from whichever key is present when the provider is unset, and never
hands a non-Claude model to the Anthropic SDK.

### Two policies, by design

Kept distinct so a $0.01 image isn't judged against a $50 ad-budget cap:

| Policy | Caps | Allowlist | Notes |
|---|---|---|---|
| `adbuyer.yaml` (ad spend, v2) | per-tx $50 · per-day $150 · per-month $2000 · human-approval-above $100 | facebook.com, graph.facebook.com | allow `advertising`; block finance/crypto_transfer/gambling; forbid political/housing_discrimination |
| `adbuyer-creative.yaml` (creative, v1) | per-asset $0.50 · per-day $10 · per-month $100 · escalate-above $1 | fal.x402.paysponge.com, api.openai.com, platform.higgsfield.ai, api.elevenlabs.io | forbid nsfw/celebrity_likeness/minors |

---

## Use it your way

- **Standalone CLI daemon.** Run `veto-agents adbuyer --goal "…"` under a process
  manager (`tmux` / `screen`, a `systemd` unit, or a container with a restart
  policy) for always-on operation.
- **Inside an MCP host.** Drop it into Claude Code, Claude Desktop, or OpenClaw
  and let the host's LLM drive it — governance still lives inside every tool,
  un-bypassable. Three tools: `create_ad_creative(...)`, `run_ad_cycle(...)`,
  `get_campaigns(...)`. Full wiring for all three hosts is in
  [../../../../docs/MCP.md](../../../../docs/MCP.md).

```bash
claude mcp add veto-agents -- veto-agents mcp
```

---

## The control loop (deep dive)

Every `--interval` minutes (default **360** — 6h — from
`ad_ops.observe_interval_minutes`; a real media buyer observes often but acts
rarely):

1. **OBSERVE** — pull the account's `amount_spent` / `spend_cap`, the current
   campaigns / ad sets / ads (each ad set's live `daily_budget`, `status`,
   `effective_status`, `learning_stage_info`, `created_time`), and last-7-day
   insights (spend / impressions / conversions / CPC / CTR).
2. **DECIDE** — the LLM brain reads the goal + observed performance and proposes
   actions. It is forced (tool-use with a JSON schema) to return only in-scope
   actions, and is told to respect the learning phase, ignore thin data, treat
   HOLD as a good outcome, and keep budget changes gradual.
3. **DISCIPLINE** — the **CODE-enforced** readiness gate (`_is_actionable`) runs
   **before** Veto. It **HOLDS** unless the ad set has left learning, delivered
   enough days + data, and is past its per-entity cooldown; it **clamps** any
   budget change to ±`max_budget_change_pct` of current. Independent of the LLM.
4. **GOVERN** — for **every** spend-implicated action, the loop calls
   `veto_authorize(action="payment", merchant="facebook.com", amount=<usd>, …, context=<the agent's OWN rationale>)`
   **before** touching Meta. `allow` → execute; `deny` → log + skip; `escalate`
   → log + notify + skip; authorize **exception** → skip (fail-**closed**).
5. **ACT** — apply the one mutation via the Meta client.
6. **RECORD** — print a per-action + per-cycle summary, then sleep and repeat.

Veto governs the agent's autonomous **intent** (its rationale is the authorize
`context`), not a human command. There is **no per-action human consent gate** —
the human deployed once and set the policy; Veto is the guardrail from then on.

### Autonomy scope (v1)

The brain may ONLY propose these action types, and only on **existing** entities:

| Action | What it does | Spend gated by Veto? |
|---|---|---|
| `adjust_budget` | raise / lower an ad set's `daily_budget` | yes — amount = the new daily budget |
| `pause` | pause a campaign / ad set / ad | yes — amount = $0 (still logged) |
| `resume` | resume a campaign / ad set / ad | yes — amount = $0 (still logged) |
| `refresh_creative` | generate a new image over x402 + swap it into an ad's creative | yes — twice: the authorize gate **and** inside `fal_image` |

It may **not** create brand-new campaigns / ad sets / ads. Any such proposed
action is rejected before it can execute.

### Patience / ad-ops discipline (configure it)

```yaml
ad_ops:
  respect_learning_phase: true         # never touch an ad set Meta marks LEARNING
  min_days_before_action: 4            # let it deliver >= 4 days before judging it
  min_conversions_before_action: 50    # …or gate on volume instead of age…
  min_impressions_before_action: 2000  # …meet EITHER conversions OR impressions
  cooldown_days_per_entity: 3          # >= 3 days between agent actions on one entity
  max_budget_change_pct: 20            # clamp any budget change to +/- 20% of current
  observe_interval_minutes: 360        # default OBSERVE cadence
```

Edit with `veto-agents policy edit adbuyer`. The **readiness gate — not the
interval — is what prevents premature action**, so you can observe as often as
you like without the agent becoming twitchy.

## Flags reference

**`adbuyer`** — `--goal`/`-g` (required), `--interval`/`-i <min>` (default `0` =
policy cadence), `--once`, `--dry-run`, `--mock`, `--no-llm`. Requires
`veto-agents install adbuyer` first (unless `--mock`).

**`create`** — `--image-provider <openai|fal>`, `--video/--no-video`,
`--voice/--no-voice`, `--all`, `--out <folder>`.

**`adbuyer-setup`** — `--non-interactive`/`-n`, `--llm-provider <…>`,
`--daily-budget <USD>`, `--creative-cap <USD>`, `--skip-login`, `--skip-wallet`.

## Under the hood
- `controller.py` — the loop: `observe()` / `decide()` (LLM → validated
  `Action`s) / `_is_actionable()` (code-enforced ad-ops discipline) /
  `govern_and_execute()` (discipline gate + magnitude cap + Veto gate + Meta
  write) / `run_loop()`. Ad-ops thresholds load from `policy.yaml`'s `ad_ops:`
  via `load_ad_ops()`; per-entity cooldowns persist in `~/.veto/adbuyer_state.json`.
- `agent.py` — thin `run()` / `run_daemon()` entrypoints + brief-parsing helpers.
- `creative/` — `director.py` (the one concept), `studio.py` (`_render_copy_md`
  + orchestration), per-provider drivers (each Veto-gated).
- `tools/meta_ads.py` — the fail-soft Graph API v25.0 client (reads, mutations,
  the create hierarchy used by creative refresh). Money on write is in **cents**;
  insights come back in **dollars**. `tools/mock_meta.py` mimics it offline.
- `meta_env.py` — resolves the BYO Meta credentials.
- `policy.yaml` — the caps + `human_approval_above_usd` + rate limits that bound
  the loop, plus the `ad_ops:` discipline thresholds.

## Extensible

The loop is provider-shaped: the Meta adapter (`tools/meta_ads.py` real +
`tools/mock_meta.py`) sits behind a stable interface, so **Google / TikTok
adapters slot in** the same way. Each agent is a forkable **MIT** template.

## Not magic

It starts decent and gets better as you tune the policy and the goal. That's the
point: you can *let* it run autonomously, because Veto bounds the downside. The
agent explores; the code caps the blast radius.

## License

MIT. See the repository root `LICENSE`.
