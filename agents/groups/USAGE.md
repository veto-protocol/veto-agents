# Groups Agent — Usage Guide

For the admin running the bot in their community. This is what the bot DMs you after install — also viewable anytime with `veto-agents groups usage`.

---

## Getting your bot into the group

1. Open Telegram, search `@BotFather` → `/newbot` → pick a name → save the token.
2. On the machine running the bot: `export TELEGRAM_BOT_TOKEN=<token>` (or run `veto-agents creds set TELEGRAM_BOT_TOKEN <token>`).
3. Start the bot — `veto-agents groups run` (local) or deploy it (see DEPLOY.md).
4. Add the bot to your group. Open Telegram → Group → Add Member → search by `@yourbot_name` → Add.
5. **Make the bot a group admin** with at least: "Delete Messages," "Pin Messages," and the read-history permission. Without these the bot can't function.
6. In the group, type `/start` once. The bot DMs you to begin configuration.

---

## `/setup` — first-time configuration (DM only)

The bot walks you through these in a DM thread. You can re-run anytime.

### 1. Caps (most important)

```
/setcap monthly 50      # total $/mo the bot can spend on paid APIs
/setcap per-question 0.10
/setcap escalate-above 1
```

Veto enforces these. If the bot tries to spend more, the call refuses and the bot replies "I'd need approval for this — capped at $0.10."

### 2. Tone

```
/settone friendly        # default: friendly, helpful, concise
/settone formal          # formal, professional, longer replies
/settone playful         # casual, emojis, shorter
/settone custom "<paste your style guide>"
```

The bot's system prompt incorporates this. Reply samples adapt within a few interactions.

### 3. Topics the bot should care about

```
/addtopic onboarding
/addtopic product-faqs
/addtopic deadlines

/dropTopic gambling      # explicitly refuse to engage
/dropTopic finance       # refuse anything money-shaped
```

The bot prioritizes replies on `addtopic` topics and refuses cleanly on `dropTopic`.

### 4. Knowledge source

The bot can read three kinds of context:

```
/source channel-history       # default — last 7 days of group messages
/source pinned-only           # only pinned messages
/source url https://...       # canonical doc/wiki the bot pulls from
/source file <upload>         # upload a PDF/Markdown the bot indexes
```

This is how you make the bot "yours" — point it at your community's canonical source of truth (your wiki, your docs, your pinned FAQ thread).

### 5. Voice transcription threshold

```
/transcribe over 30s      # default: transcribe voice notes > 30s
/transcribe off
/transcribe all
```

Off = $0 transcription spend. All = $0.04/hour on every voice note.

### 6. Alerts (who pings you when something matches)

```
/alert "outage|down|broken" → DM me when these words appear
/alert "@admin"            → standard mention
/alert "scam|spam|phishing" → DM + pin a warning message
```

### 7. Quiet hours

```
/quiet 23:00-06:00 local  # don't reply at night
/quiet none               # 24/7
```

Veto's `time_windows` policy enforces this — agents *can't* spend during quiet hours unless an alert overrides it.

---

## Day-to-day commands (any group member or admin)

| Command            | Who    | What it does                                  |
|--------------------|--------|-----------------------------------------------|
| `@bot <question>`  | anyone | Bot answers in-thread, with sources           |
| `/summary today`   | anyone | Summary of last 24h activity                  |
| `/summary week`    | anyone | Weekly digest                                 |
| `/find <query>`    | anyone | Search the group's history for prior threads  |
| `/usage`           | admin  | Per-day, per-month spend so far               |
| `/receipts`        | admin  | Last 20 receipts (link to veto-ai.com/r/...)  |
| `/pause`           | admin  | Bot stops replying (still listens)            |
| `/resume`          | admin  | Resume replying                               |

---

## Making your bot better for YOUR community

The bot improves three ways:

### 1. Curate the knowledge source

The single biggest lever. Point `/source` at the canonical FAQ / wiki / pinned thread that has your community's real answers. Update it weekly — the bot re-indexes within an hour.

### 2. Use `/feedback` after replies

Reply to any bot message with:
- `/feedback good` → reinforces that pattern
- `/feedback wrong: <correction>` → bot remembers + does better next time
- `/feedback off-topic` → bot adjusts its topic-relevance threshold

The bot uses these to refine its retrieval + reply style for YOUR community over time. Stored locally (per-deployment), never sent to Veto's servers.

### 3. Custom system prompt for your niche

If your community is technical (e.g., crypto traders), the default friendly tone may be wrong. Use `/settone custom` and write a prompt like:

```
You are the bot for the "Base USDC traders" community. Respond like
a calm, experienced trader — short, factual, no emojis. Cite block
explorer links when discussing on-chain activity. If asked for trade
advice, refuse and redirect to the pinned disclaimer.
```

This becomes the system prompt prepended to every reply. The bot adapts immediately.

---

## How to know it's working

- **Reply quality.** Check `/feedback good` ratio — should trend above 70% after the first 50 replies.
- **Cost per useful answer.** Check `/usage` — typical: $0.02–0.08 per reply. If it's higher, your knowledge source isn't tight enough (bot is doing too much external search).
- **Coverage.** Run `/find <random question>` weekly. If you can't find at least one prior good thread for any reasonable question, the bot doesn't have enough material to learn from yet.

---

## When to switch off / replace

- Active members > 1000 and bot replies > 100/day: graduate to Pro tier (`veto-agents account upgrade --tier=pro`) for higher rate limits + priority Veto-side support.
- Voice notes are most of your spend but most useful: drop the transcription threshold to `all`.
- Members complain the bot is too chatty: drop `/transcribe`, drop `/summary` from auto, force `@-mention required for replies` via `/strict on`.

---

## Receipts + audit

Every paid action (LLM call, search, transcription) creates a receipt. As admin:

```
veto-agents receipts --agent groups --tail
```

Or visit `veto-ai.com/r/<uuid>` — public URL anyone with the link can verify the action happened, exactly as recorded, against the policy version in effect at the time. Use this to show your community treasurer or board: *here's exactly what the bot spent and why.*
