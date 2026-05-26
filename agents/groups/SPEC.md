# Groups Agent — Spec

> A 24/7 Telegram (and later WhatsApp) bot that lives in your community group. Answers questions, summarizes threads, transcribes voice notes, escalates to humans when needed. Every paid action governed by Veto — admin sees what the bot spent and why.

**Status:** v0.1 — to ship after Media. Self-hosted (VPS / Fly / Railway / Docker).

---

## What it does

Drops into a Telegram group (200K-member cap) as a bot, reads group history with the admin's consent, and:

1. **Answers questions in-thread.** `@bot what's the deadline for the proposal vote?` → searches recent history + paid sources (Exa / Tavily) → replies, citing the message thread + sources.
2. **Daily / weekly digests.** Scheduled summary of the group's activity. "Top 5 threads from this week, 8 unanswered questions, 3 polls open."
3. **Transcribes voice notes.** Replies with text under any voice message over 30 seconds.
4. **Surfaces urgents.** Pings the admin (DM or pinned message) when someone matches certain keywords ("security issue", "outage", "@admin", member-defined).
5. **Member onboarding.** New member joins → DM with the rules + a primer pulled from the group's pinned content.

All within Veto's policy caps — admin sets `$X/month total, $0.10 per question, transcription only over 30s, no replies after midnight local`. Bot refuses anything outside the cap.

## Who it's for

- **Community admins / moderators.** Anyone running a Telegram (or WhatsApp Business) group with 50+ active members.
- **Founders running customer support communities.** Real-time questions answered without the founder being online 24/7.
- **DAO ops / token communities.** Heavy info flow, lots of repeat questions, scarce admin time.
- **Course / cohort creators.** Each cohort has a group; bot lives there, answers FAQs from the syllabus.

The buyer: someone who's currently doing 20+ replies/day in their group and paying $30+/month on existing bots (MEE6 / Carl-bot equivalents in the Telegram space) that have no payment governance.

## First 5 minutes (the user experience)

1. User runs `veto-agents install groups` on their machine. CLI prompts for:
   - Telegram bot token (created via @BotFather — 60-second free flow, opens browser)
   - Anthropic API key OR Hermes-via-Nous-Portal key (for replies)
   - Optional: Exa API key (for paid search), AssemblyAI key (for transcription)
2. CLI prints a Docker run command + a Fly.io deploy command + a systemd unit. User picks one and runs it (or asks Claude Code to do it for them).
3. Bot starts. User adds the bot to their Telegram group, makes it admin (so it can read messages).
4. Bot DMs the user (the group owner): "Hi! I'm @your_groups_bot. Reply to this DM with `/setup` to configure my behavior."
5. `/setup` walks: monthly cap, per-question cap, allowed merchants (paid APIs), keyword alerts, response style.
6. Bot is live. Members can `@-mention` to ask questions, run `/summary`, etc.

## Tools (paid APIs)

| Tool                          | What it does                          | Approx. cost              |
|-------------------------------|---------------------------------------|---------------------------|
| `anthropic.claude_sonnet`     | Reply drafting + summaries            | ~$3 / Mtok input          |
| `nous.hermes_3_405b`          | Alternative reply brain               | varies                    |
| `exa.neural_search`           | Paid search for question-answering    | ~$5 / 1k queries          |
| `tavily.search`               | Alternative search                    | ~$3 / 1k queries          |
| `assemblyai.transcribe`       | Voice-note → text                     | ~$0.37 / hour audio       |
| `firecrawl.scrape`            | Pull canonical sources cited in chat  | per-page                  |

All paid actions authorize through Veto first. Bot can be capped at `$0.10/question, $5/day, $50/month` and refuses anything over.

## Default Veto policy (APPS YAML)

```yaml
policy_name: "groups-agent-default"
caps:
  per_transaction_usd: 0.50
  per_day_usd: 5.00
  per_month_usd: 50.00
  human_approval_above_usd: 1.00

allowlist_merchants:
  - api.anthropic.com
  - api.nousresearch.com
  - api.exa.ai
  - api.tavily.com
  - api.assemblyai.com
  - api.firecrawl.dev

time_windows:
  active_hours: "06:00-23:00 local"  # don't reply at 3am unless escalated

rate_limits:
  txs_per_hour: 60
  txs_per_day: 200

categories:
  allow: ["ai_inference", "search", "transcription"]
  block: ["finance", "crypto_transfer", "shopping", "media_generation"]

intent_keywords:
  forbidden:
    - "send money to"
    - "transfer funds"
    - "share private key"
    # ↑ if a member asks the bot to do anything money-shaped, refuse
  required: []
```

## Why this leads the consumer audience

- **Universal pain.** Everyone with a Telegram group has the "answer the same question for the 12th time today" problem.
- **Shareable artifact.** The bot's daily digest can be a public message. Other admins see it, ask "what bot is that?" → install.
- **24/7 by definition.** A community bot that goes offline at midnight isn't useful. This justifies the always-on hosting story.
- **Veto's value is obvious.** "My bot's monthly LLM bill is capped at $50, every reply costs a known amount, I have a receipt for everything." Compare to: existing bots where you find out about the bill after the fact.

## Hosting (deploy hints, not orchestration)

The agent ships a `Dockerfile` + deploy recipes for:

- **Fly.io** — `fly launch` + a one-line `fly.toml` (free tier covers small communities)
- **Railway** — push the repo, set env vars, done
- **Docker Compose** — drop into any VPS, `docker compose up -d`
- **Bare VPS + systemd** — `Dockerfile` + a `groups.service` unit file the user copies to `/etc/systemd/system/`

We don't orchestrate the deploy. We give the user (or their Claude Code) the artifacts and clear instructions.

## v0 scope cut

Ships in v0.1:
- ✅ Telegram bot (python-telegram-bot v22+)
- ✅ Question-answering with Exa + Anthropic
- ✅ Daily digest (scheduled)
- ✅ Voice-note transcription via AssemblyAI
- ✅ Admin DM for `/setup`
- ✅ Caps enforced via Veto authorize
- ❌ WhatsApp Business — v0.2 (~3 weeks; Business API approval needed)
- ❌ Discord — v0.2 (similar to Telegram in shape; just another adapter)
- ❌ Multi-language replies — v0.3 (translation is cheap; just a tool addition)

## Success criteria

- 50 communities install in week 1 after launch
- Median bot replies to ≥5 questions/day
- 0 incidents of the bot blowing its monthly cap (Veto governance works)
- 10 of the 50 communities pay $19/mo Pro tier (Veto governance + premium features)

## How this composes with the rest of Veto

- **Same auth flow** — magic-link sign-in, same `cfg.api_key`
- **Same Veto authorize calls** — every paid LLM call goes through `/api/v1/authorize/`
- **Same receipts feed** — Group admin sees per-bot spend in `veto-agents wallet` and at `veto-ai.com/r/<uuid>`
- **Same policy YAML format** — APPS schema, editable via `veto-agents policy edit groups`
- **Composable with Media agent** — the Groups bot can call the Media agent's generation pipeline when a community wants a poster or summary card
