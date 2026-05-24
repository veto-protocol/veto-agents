# Inbox Agent — Spec

> Connects to your email + calendar, triages, drafts, schedules. Pays for the paid tools (transcription, premium scheduling, AI drafting) it needs. Never sends without you saying so.

**Status:** v0 — supporting agent, ships weeks 13–14.

---

## What it does

Reads your inbox (Gmail / Outlook via OAuth), classifies messages, drafts replies, surfaces what actually needs your attention, schedules meetings, and transcribes voice memos. It pays paid backend services to do this well — Cal.com Pro, AssemblyAI transcription, premium AI for drafting. Veto governs every paid call.

**Example user prompts:**
- *"Triage my inbox from this week. Draft replies to anything from a customer; surface anything from an investor."*
- *"Schedule a 30-min call with Erik next Tuesday afternoon. Send the invite once I confirm."*
- *"Transcribe these voice notes from my Slack DMs and summarize."*
- *"Draft a follow-up to everyone who hasn't replied to my last email in 7 days."*

## Who it's for

- **Anyone with an overflowing inbox.** The most universal pain in the working world.
- **Solo founders, consultants, salespeople** who live in email and would pay for an extra hour back per day.

The Veto angle: drafted replies and scheduling actions are reversible until they're sent. The agent NEVER sends mail or accepts calendar invites without explicit user confirmation. The Veto policy makes that a hard rule, not a hopeful one.

## First 60 seconds

1. Sign in.
2. Wallet provisioned, $5 USDC.
3. Connect Gmail or Outlook (OAuth one-tap).
4. Default policy: *"Never send mail without my approval. Never accept calendar invites outside business hours without asking. Max $5/month on premium tools."*
5. Agent ingests last 7 days of inbox.
6. Surfaces 5 messages: 2 customer questions (drafted replies waiting), 1 investor intro (flagged), 2 newsletters (already archived).
7. User reviews drafts, taps "Send" on each one. Veto authorize fires per send (cheap: $0). Receipts logged.

## Tools

| Tool                          | Used for                                | Approx. cost          |
|-------------------------------|-----------------------------------------|-----------------------|
| `gmail.list_messages`         | Read inbox                              | free (Google quota)   |
| `gmail.send_message`          | Send mail (gated by Veto)               | free, but always escalates first |
| `gmail.draft_create`          | Create drafts (no send)                 | free                  |
| `outlook.*`                   | Same as Gmail but for Microsoft         | free                  |
| `calendar.create_event`       | Schedule meetings                       | free                  |
| `assemblyai.transcribe`       | Voice note → text                       | $0.37 / hour audio    |
| `cal_com.find_slots`          | Premium scheduling (Cal.com Pro)        | $15/mo subscription   |
| `apollo.find_contact`         | Contact enrichment                      | per-call              |
| `anthropic.claude_sonnet`     | Drafting + classification               | per-token             |

Sending mail is always Veto-escalated to the user in v0 (no autonomy on outbound), regardless of cap. Reading + drafting + scheduling internal-only events is allowed without escalation.

## Default Veto policy (APPS YAML)

```yaml
policy_name: "inbox-agent-default"
caps:
  per_transaction_usd: 0.50
  per_day_usd: 1.00
  per_month_usd: 5.00
  human_approval_above_usd: 0.25

allowlist_merchants:
  - googleapis.com
  - graph.microsoft.com
  - api.assemblyai.com
  - api.cal.com
  - api.apollo.io
  - api.anthropic.com

time_windows:
  active_hours: "00:00-23:59"
  no_send_outside: "08:00-22:00 local"  # never send mail at 3 AM

categories:
  allow: ["communication", "scheduling", "ai_inference", "transcription"]
  block: ["finance", "shopping", "media_generation"]

intent_keywords:
  forbidden_in_drafts: ["wire transfer", "send me your password", "click this link urgently"]
  # Inbox agent should never draft a phishing-shaped reply, even if asked
```

## Demoability

- **Universal pain, universal demo.** Everyone has email. "Watch the agent triage 40 messages in 30 seconds and propose 5 drafts" is a video anyone understands.
- **Trust through restraint.** The fact that the agent *can't* send without confirmation is the trust story. Show the "agent wanted to send this, asked you, you said yes" loop on-screen.
- **Cost transparency.** "Your agent used $0.04 of transcription to summarize that voice memo. Here's the receipt." Builds the receipts-as-trust habit.

## Pricing posture

- **Free tier:** $5 funded, refilled $1/month. Most users stay free.
- **Pro tier:** $9/month, premium scheduling + unlimited transcription, $5 of other governed spend included.

## Build sketch

```
agents/inbox/
├── SPEC.md
├── README.md
├── agent.py
├── policy.yaml
├── tools/
│   ├── gmail_oauth.py
│   ├── outlook_oauth.py
│   ├── calendar.py
│   ├── transcribe.py
│   ├── scheduling.py
│   ├── contact_lookup.py
│   └── llm_draft.py
└── prompts/
    └── system.md            (never send without explicit confirmation, prefer drafts)
```

## v0 scope cut

Ships in v0.1:
- ✅ Gmail OAuth + read + draft + classify
- ✅ Calendar read + create event
- ✅ Manual approval flow for sends
- ✅ Transcription via AssemblyAI
- ❌ Outlook (Gmail-first; Outlook in v0.2)
- ❌ Automatic follow-ups (v0.2 — needs more trust)
- ❌ Contact enrichment (Apollo) — v0.2

## Success criteria

- A first-time user connects Gmail, sees 5 triaged messages, and sends one drafted reply within 3 minutes.
- 100 inboxes connected in week 1 of soft launch.
- 50% of users return within 7 days.
- Zero "agent sent something it shouldn't have" incidents (the policy enforces this; we monitor).

## How this composes

- Inbox Agent's send-approval flow is a canonical example of Veto *escalation* in a consumer context — different from Media or Build (which are mostly auto-allow within caps).
- Connects to Veto's policy-as-code story: users can edit the policy in plain English to relax / tighten send rules, write their own keywords to flag, etc.
- The "receipt for every outbound mail" pattern starts establishing the norm: every agent action that touches the outside world produces an auditable artifact. Important for the compliance / enterprise version later.
