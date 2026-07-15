# Setup & Credentials — the honest, from-experience guide

Everything veto-agents needs, what's **required** vs **optional**, exactly where to
get each key, the gotchas that cost us hours, and — importantly — **what stays on
your machine vs what touches our servers.**

## TL;DR — what you actually need

| To do this | You need | Required? |
|---|---|---|
| Run anything | A **Veto sign-in** (free email magic-link) | recommended |
| The agent to **think** (write copy, decide) | **one LLM key** (Claude *or* OpenAI *or* a local model) | ✅ required |
| Generate an **image** | nothing extra (free `fal`) — or your OpenAI key | optional |
| Generate a **video** | a **Higgsfield** key | optional |
| Generate a **voiceover** | an **ElevenLabs** key | optional |
| Actually run **Meta ads** | a **Meta** access token (+ a card **on Meta**) | only for real ads |
| Pay for creative **without** provider keys | a funded **wallet** (x402) | optional |

You can skip every "optional" now and add it later — just re-run
`veto-agents adbuyer-setup`, or `veto-agents creds set <KEY_NAME>`. Nothing is lost.

---

## 🔒 Where your credentials live (read this)

**Your provider keys never touch Veto's servers.** Verified in the code:

- Every key (OpenAI, Meta, Higgsfield, ElevenLabs) is stored **locally only** —
  in `~/.veto/*.env` (permissions `600`) and/or your **OS keychain** (the same
  store `gh`, `aws`, and `vercel` use). They are read locally and used to call
  **the provider directly** (e.g. your OpenAI key → `api.openai.com`, your Meta
  token → `graph.facebook.com`). They are never uploaded anywhere.
- The **only** thing that reaches Veto's servers is a **spend-authorization
  request**: the *amount*, the *merchant domain* (e.g. `api.openai.com`), a short
  *description/prompt snippet* for the policy decision, and your *Veto sign-in
  token*. Veto returns allow/deny/escalate + a signed receipt. **Not your keys,
  not your generated images/videos, not your Meta account contents.**
- Your Meta ad data is read **directly by the agent** with your token; it doesn't
  route through Veto (only the decision context — e.g. "raise ad-set X $20→$24" —
  goes to the authorize call).

Short version: **Veto is the brain that says yes/no to a spend. It is not a pipe
your keys or content flow through.**

---

## 1. The LLM brain (required)

Pick ONE. The agent needs a brain to write copy and make decisions.

- **Claude** — [console.anthropic.com](https://console.anthropic.com) → API Keys.
  Env: `ANTHROPIC_API_KEY`.
- **OpenAI** — [platform.openai.com/api-keys](https://platform.openai.com/api-keys).
  Env: `OPENAI_API_KEY`. (This same key also does images via `gpt-image-1` — note
  image gen needs **org verification** on OpenAI's side.)
- **Local / other** — Ollama, OpenRouter, Grok, etc. all work via the
  OpenAI-compatible path; pick "custom" in setup and give the endpoint.

`adbuyer-setup` asks which one and takes the key (or press Enter to paste later).

> Gotcha we hit: a bare `pip install veto-agents` used to miss the LLM SDK →
> "SDK not installed". Fixed — the SDKs now ship in the core install. If you're on
> an old build, `pipx install --force 'veto-agents[all]'`.

## 2. Images (optional — free by default)

- **Free**: `fal` over x402 — no key. Needs a small funded wallet for the
  ~$0.01 micropayment (see Wallet). Or,
- **OpenAI**: `gpt-image-1`, ~$0.25/image, uses your `OPENAI_API_KEY`.

## 3. Video — Higgsfield (optional)

The confusing part, from experience:

- The API keys are **NOT** on the main app `higgsfield.ai` (the pretty video app
  you might subscribe to). They're on the **separate developer platform**:
  **[cloud.higgsfield.ai](https://cloud.higgsfield.ai)** → **API Management**.
- You get a **KEY_ID** and a **KEY_SECRET** (two values).
  Env: `HIGGSFIELD_API_KEY` (=KEY_ID) + `HIGGSFIELD_API_SECRET` (=KEY_SECRET).
- **Credits are separate too** — a subscription on the *app* does NOT fund the
  *Cloud API*. Add credits on cloud.higgsfield.ai or you'll get "Not enough
  credits" even though you paid.
- Note: Higgsfield DoP **animates an image** (image→video), so a video needs a
  source image first.

## 4. Voice — ElevenLabs (optional)

[elevenlabs.io](https://elevenlabs.io) → profile → **API Keys**.
Env: `ELEVENLABS_API_KEY`.

## 5. Meta ads (only when you're ready for real ads)

**Meta ad spend is billed to a credit card on Meta's side — not to Veto, not to a
wallet.** Veto governs the agent's *decisions* (budgets, pause/resume); Meta's own
daily-budget + account spend-cap enforce the ceiling; the loop watches actual
spend each cycle.

Getting a token (the fast path that actually works):

1. **[developers.facebook.com](https://developers.facebook.com)** → your app
   (create one, type **Business**) → add the **Marketing API** product.
2. Top nav → **Tools → Graph API Explorer** → pick your app → add permissions
   **`ads_management`, `ads_read`, `business_management`** → **Generate Access
   Token** → copy it.
3. Get an **Ad Account ID** (`act_…`) and a **Page ID** you admin.

Env: `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID=act_…`, `META_PAGE_ID`.

> Gotchas: Graph-Explorer tokens expire in ~1 hour — for real use, generate a
> **long-lived / System-User** token. Want to test with **zero spend**? Create a
> **Sandbox Ad Account** (app → Marketing API → Tools) — full API, no delivery, no
> card. And you never need any of this to try the agent — just use `--mock`.

## 6. Wallet — what it's actually for (optional)

**A wallet has nothing to do with paying for Meta ads.** It funds a Veto-guarded
Safe that pays for **creative micro-spends over x402** — i.e. the *keyless* image
path (`fal`, ~$0.01/image) so you can generate content **without** bringing your
own OpenAI/provider key. If you BYO your creative keys, you don't need a wallet at
all. (Video/voice are BYO-key only — there is no wallet path for those yet.)

---

## Trying the MCP server from Claude

`veto-agents mcp` exposes the media buyer as MCP tools (`create_ad_creative`,
`run_ad_cycle`, `get_campaigns`) — governance enforced *inside* each tool.

**Claude Code (terminal):**
```bash
claude mcp add veto-agents -- veto-agents mcp
# then in Claude Code:  "use veto-agents to make an ad for a cold-brew brand"
```

**Claude Desktop** — add to `claude_desktop_config.json` (Settings → Developer →
Edit Config):
```json
{
  "mcpServers": {
    "veto-agents": { "command": "veto-agents", "args": ["mcp"] }
  }
}
```
Restart Claude Desktop; the three tools appear. Your keys/config come from the
same local `~/.veto` — the MCP server reads them on your machine, nothing extra to
configure.

**Any MCP host** (Cursor, OpenClaw, …): command `veto-agents`, args `["mcp"]`,
stdio transport.
