---
name: veto-media-buyer
description: >-
  Use when the user wants to create ad creative (ad copy + image, optionally
  video/voice) or run and manage Meta (Facebook/Instagram) ad campaigns — via
  the open-source `veto-agents` CLI, an autonomous AI media buyer governed by
  Veto so it literally can't overspend. Triggers on requests like "make me an
  ad", "write ad copy and generate the image", "run/optimize my Meta ads",
  "scale my winning ad set / pause the losers", "set a daily budget the agent
  can't exceed", or "manage my ad campaigns for me". Drives the local CLI; every
  spend is gated by Veto before any money moves.
---

# Veto Media Buyer

You drive the **`veto-agents`** CLI on the user's machine. It is an autonomous,
Veto-governed AI media buyer: it writes ad creative, and it observes → decides →
acts on Meta ad campaigns on a schedule. **Governance is enforced inside the CLI**
— every paid action and every budget change is authorized by Veto (and passes a
code "ad-ops discipline" gate) BEFORE it happens. You cannot and must not try to
bypass that; your job is to run the right command and relay the result + receipt.

## 0. Preflight (always do this first)

Check it's installed:
```bash
veto-agents --version
```
If that fails ("command not found"), install it (macOS/Linux):
```bash
curl -fsSL https://raw.githubusercontent.com/veto-protocol/veto-agents/main/install.sh | bash
```
Re-running that same line is also the upgrade. If `veto-agents` still isn't on
PATH afterward, tell the user to open a new terminal (the installer adds
`~/.local/bin` to PATH).

## 1. Core commands (what to run, when)

| The user wants… | Run |
|---|---|
| Make an ad (copy + image) | `veto-agents create '<brief>'` |
| …plus video and/or voice | `veto-agents create '<brief>' --all` (or `--video` / `--voice`) |
| Make ads match their brand | `veto-agents brand set <https-url>` first, then `create` |
| Try the autonomous loop safely | `veto-agents adbuyer -g '<goal>' --mock --once` |
| Preview real decisions, no writes | `veto-agents adbuyer -g '<goal>' --dry-run --once` |
| Run it for real | `veto-agents adbuyer -g '<goal>'` (needs Meta connected) |
| Guided setup (keys, budgets, Meta) | `veto-agents adbuyer-setup` |
| Add a provider key later | `veto-agents creds set <KEY_NAME>` · `veto-agents creds list` |

## 2. Hard rules (read before running anything)

1. **Single-quote every goal/brief.** The shell eats `$` — `-g "…$30/day"`
   silently becomes "…/day". ALWAYS use single quotes: `-g 'grow signups, up to $30/day'`.
2. **Default to safe.** Unless the user explicitly asks to touch a real account
   or spend real money, use **`--mock`** (fake account, zero spend) or
   **`--dry-run`** (real reads, no writes). The `--mock --once --no-llm` form
   needs no keys and no sign-in — perfect for a first demo.
3. **Confirm before anything that costs money or changes a real account:**
   - the non-mock `adbuyer` loop (it will change real budgets / pause real ads),
   - `create` with `--image-provider openai` (~$0.25/image) or `--all` (video/voice cost credits).
   Default images use the **free** `fal` provider — prefer it unless the user asks otherwise.
4. **Never fight the gate.** If Veto returns `deny`/`escalate`, STOP that action
   and relay the reason + the `receipt_url` to the user. Do not retry to force it
   through, do not call any other payment tool. A block is the product working.
5. **Don't invent credentials.** If Meta/creative keys aren't set up, run
   `veto-agents adbuyer-setup` or point the user to `docs/SETUP.md`. Never put a
   real secret on a command line or echo one back.
6. **Relay, don't paraphrase away the governance.** When you report results,
   keep the Veto verdict and the plain outcome ("scaled the winner +20%, paused
   the loser, held the still-learning one") — that's the whole value.

## 3. Typical flows

**"Make me an ad for my product" →**
1. If they gave a website, ground the brand first: `veto-agents brand set 'https://theirsite.com'`.
2. `veto-agents create 'one-line brief in their words'` (free `fal` image by default).
3. Report the output folder (`~/Downloads/veto-studio/…`), the copy, and the Veto
   verdict/cost per asset. Offer `--all` (video+voice) only if they want it (note it costs).

**"Show me how the media buyer works" / "run it on a demo" →**
```bash
veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock --once --no-llm
```
Then explain the story it prints: it scaled the winner, paused the money-waster,
and **held the still-learning ad set on purpose** — every move approved by Veto.

**"Run it on my real ads" →** confirm they understand it will change their live
account, make sure Meta is connected (`veto-agents adbuyer-setup`), then start
with a `--dry-run --once` so they see the proposed decisions before any write.

## 4. What Veto governs (say this if asked "is it safe?")

Veto is a **non-custodial guard**: it decides allow/deny/escalate on every spend
and returns a signed receipt — it never holds the user's money or keys. Meta ad
spend is billed to the user's card **on Meta**; Veto governs the agent's
*decisions* and budgets, and the code discipline gate enforces patience (respect
the learning phase, ±20% budget steps, cooldowns). The agent thinks for itself;
its downside is bounded in code.

## Installing this skill
Drop this folder into your Claude skills directory (e.g. `~/.claude/skills/`),
or point your MCP/agent host at it. Then just ask Claude in plain language —
"make me an ad" or "run my media buyer" — and it will drive the CLI for you.
