# Creative Studio

The **standalone creative stage** of the ad-buyer agent. Give it a product
brief; it returns a coherent **ad package** — copy + a hero image (+ optional
video + voiceover) — all derived from **one** creative concept so the pieces
match. It is deliberately decoupled from Meta: you can run the whole studio with
**no Meta credentials**. Placing/buying the ad on Meta is a separate, later
stage.

## What it does

1. **DIRECT** — one LLM call (the *creative director*) turns your brief into a
   single unifying concept (theme + message + tone) and derives every asset's
   prompt/script **from that concept**: copy variants, an image prompt, a video
   prompt, and a voiceover script.
2. **COPY** *(free)* — writes `copy.md` (headlines, primary texts, CTAs).
3. **IMAGE** — OpenAI `gpt-image-1` (BYO key) **or** the free fal.ai fallback
   over x402.
4. **VIDEO** *(optional)* — Higgsfield DoP, async-polled.
5. **VOICE** *(optional)* — ElevenLabs text-to-speech.
6. **ASSEMBLE** — a per-run folder with `copy.md`, the image(s)/video/audio, and
   a `manifest.json`, plus a printed summary (what was made, cost per asset, and
   the Veto verdict + receipt for each paid asset).

Every **paid** generation is gated by **Veto** (`VetoClient.authorize`,
`decision_only`, `action="payment"`, `merchant=<provider domain>`) **before** the
provider is called. `deny`/`escalate` → the asset is skipped and its receipt is
logged; `allow` → the provider runs. Copy is free (LLM only). The free fal image
self-gates through x402. The gate is **fail-closed** — if Veto can't be reached,
nothing is spent.

## Bring-your-own keys

Keys are read (in order) from the shell env → `~/.veto/creative.env` →
the veto-agents keychain. They are **never printed or logged**.

`~/.veto/creative.env`:

```dotenv
# Creative director (required — Claude-only in v1)
ANTHROPIC_API_KEY=sk-ant-...

# Image (BYO — optional; without it, the free fal.ai fallback is used)
OPENAI_API_KEY=sk-...

# Video (BYO — optional)
HIGGSFIELD_API_KEY=your_key_id
HIGGSFIELD_API_SECRET=your_key_secret
# or, combined:
# HIGGSFIELD_CREDENTIALS=KEY_ID:KEY_SECRET

# Voice (BYO — optional)
ELEVENLABS_API_KEY=...
```

Or store them in the keychain: `veto-agents creds set OPENAI_API_KEY <key>`.

**Missing keys degrade gracefully** — the asset is skipped with a note and the
studio still delivers everything it can. Free/cheap default: copy + image;
video/voice turn on automatically only when their keys are configured.

Paid assets also need Veto sign-in (`veto-agents setup`) so the spend can be
authorized; the free fal image additionally needs a funded x402 wallet.

## Run it

```bash
# Copy + hero image (OpenAI if OPENAI_API_KEY is set, else free fal.ai):
veto-agents create "premium cold-brew coffee for busy founders, launch week"

# Force the free image path, no video:
veto-agents create "eco running shoe" --image-provider fal --no-video

# Everything available (video/voice skip cleanly if keys are missing):
veto-agents create "SaaS onboarding tool" --all
```

Flags: `--image-provider openai|fal`, `--video/--no-video`, `--voice/--no-voice`,
`--all`, `--out <folder>`.

## Programmatic

```python
from veto_agents.config import load
from veto_agents.agents.adbuyer.creative import studio
from rich.console import Console

cfg = load()
manifest = studio.run(
    "premium cold-brew coffee for busy founders",
    cfg, Console(),
    want=("copy", "image", "video", "voice"),
    image_provider="openai",
)
# manifest -> concept + per-asset {status, path, cost_usd, verdict, receipt_url}
```

## Layout

```
creative/
  director.py                 brief → one concept + derived prompts/scripts (LLM, free)
  studio.py                   orchestrator: direct → gate+generate → assemble → summary
  creds.py                    BYO-key resolver (env → ~/.veto/creative.env → keychain)
  gate.py                     Veto spend-gate for BYO-key providers (fail-closed)
  types.py                    shared ToolResult
  providers/
    openai_image.py           gpt-image-1 (BYO)         — Veto-gated
    fal_image.py              free fal.ai fallback      — self-gated via x402
    higgsfield_video.py       Higgsfield DoP (BYO)      — Veto-gated, async-poll
    elevenlabs_voice.py       ElevenLabs TTS (BYO)      — Veto-gated
```

## Notes / limits

- The director is **Claude-only** in v1 (Anthropic tool-use for structured
  output); it honors `cfg.llm_model`. Multi-provider structured output is a
  future improvement.
- `gpt-image-1` requires OpenAI **org verification** and is scheduled to
  deprecate 2026-10-23 (successors use the same Images API shape — swap `model`).
- Provider prices used for the Veto gate `amount` are estimates; the real
  settled cost for x402 assets comes back on the receipt.
