# veto-agents MCP server

Drop the **media buyer** into any MCP host — Claude Code, Claude Desktop, or
OpenClaw — as a set of Model Context Protocol tools. The host's LLM can then
create ad creative and run autonomous media-buying cycles, and **Veto governs
every spend inside each tool, fail-closed**.

The server speaks **STDIO** and launches with one command:

```bash
veto-agents mcp
```

## Install

The MCP SDK ships as an optional extra so the base install stays light:

```bash
pip install 'veto-agents[mcp]'
```

(If you already have veto-agents, this just adds the `mcp` dependency.) Then
sign in once so tools can produce signed receipts and real verdicts:

```bash
veto-agents setup
```

## The three tools

Every tool returns a plain JSON object. Governance is not a separate step you
can forget — it lives **inside** each tool, before any provider HTTP call or any
Meta write. A host LLM calling these tools **cannot bypass Veto**.

### `create_ad_creative(brief, image_provider="openai", want_video=False, want_voice=False)`

Turns a brief into a coherent ad package: one creative concept → ad copy + a
hero image (+ optional video/voice). No Meta account needed.

- **Veto gate:** each PAID asset (image/video/voice) is authorized *inside its
  provider driver* before the provider is called. A deny/escalate makes that
  asset `status:"denied"` with a `verdict` + `receipt_url` on its row — it is
  never generated. Copy is free.
- Missing provider keys (OpenAI / Higgsfield / ElevenLabs) degrade gracefully to
  `status:"skipped"`. `image_provider:"openai"` auto-falls back to the free
  `fal` x402 image when no OpenAI key is present.
- **Returns** the manifest: `concept`, `assets[]` (with `path`, `cost_usd`,
  `verdict`, `receipt_url` per asset), `totals`, and `providers_available`.

### `run_ad_cycle(goal, mock=True, dry_run=False)`

Runs ONE autonomous cycle over a Meta ad account:
`OBSERVE → DECIDE → DISCIPLINE → VETO → ACT`.

- **Two gates, both inside, both un-bypassable:** a CODE discipline gate (respect
  learning phase, require enough days + data, per-entity cooldown, clamp budget
  change magnitude) runs **before** the fail-closed **Veto authorize**, which
  runs **before** any Meta write.
- **`mock=True` (default)** mimics Meta entirely offline (seeded fake campaigns,
  no real account, no real spend) so it is demoable with zero Meta setup — yet
  the **real** Veto authorize (free, decision-only) and the discipline gate still
  run on every action. Set `mock=False` to run against your real account
  (requires `META_ACCESS_TOKEN` / `META_AD_ACCOUNT_ID` / `META_PAGE_ID`).
- **Returns** `{observed, proposals, actions[], summary_counts}`. Each action row
  carries `outcome` (executed/held/denied/escalated/skipped/failed/dry-run),
  `verdict`, `applied`, `reason` (the discipline HOLD explanation), `receipt_url`,
  and `reason_codes`.

### `get_campaigns(mock=True)`

Read-only snapshot of the ad account (account + campaigns + ad sets + ads +
last-7d insights). No spend, so no Veto gate. Use it to inspect state before
proposing changes.

- **Returns** `{ad_account_id, account, campaigns, adsets, ads, insights,
  errors, currency}`.

All three return `{"error": "not_signed_in", ...}` or
`{"error": "meta_credentials_missing", "missing": [...]}` instead of prompting —
the server is non-interactive (there is no TTY over stdio).

## Wiring it into a host

All three hosts use the identical stdio invocation: the `veto-agents` binary
with the arg `mcp`.

### Claude Code

```bash
claude mcp add veto-agents -- veto-agents mcp
```

With a scope and env vars:

```bash
claude mcp add veto-agents --scope user -e VETO_API_KEY=sk_... -- veto-agents mcp
```

(The `--` separates Claude's own flags from the server command.)

### Claude Desktop

Edit `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "veto-agents": {
      "command": "veto-agents",
      "args": ["mcp"],
      "env": { "VETO_API_KEY": "sk_..." }
    }
  }
}
```

If `veto-agents` is not on Desktop's PATH, use an absolute path to the console
script, or:

```json
{
  "mcpServers": {
    "veto-agents": {
      "command": "python",
      "args": ["-m", "veto_agents.cli", "mcp"]
    }
  }
}
```

### OpenClaw

```json
{
  "mcp": {
    "servers": {
      "veto-agents": {
        "command": "veto-agents",
        "args": ["mcp"],
        "env": { "VETO_API_KEY": "sk_..." }
      }
    }
  }
}
```

## Credentials & config

Resolution order matches the rest of veto-agents:

- **Veto sign-in** — from the OS keychain / `~/.veto-agents` (via
  `config.load()`), or `VETO_API_KEY` in the host `env`.
- **Provider keys** (BYO) — `OPENAI_API_KEY`, `HIGGSFIELD_API_KEY` +
  `HIGGSFIELD_API_SECRET`, `ELEVENLABS_API_KEY` — from env, `~/.veto/creative.env`,
  or the keychain.
- **Meta creds** (only for `mock=False`) — `META_ACCESS_TOKEN`,
  `META_AD_ACCOUNT_ID`, `META_PAGE_ID` — from env, `~/.veto/meta.env`, or the
  keychain.

Config is reloaded on every tool call, so a sign-in or key change made while the
host is running is picked up without a restart.

## Why this is safe by construction

No tool reaches a provider HTTP endpoint or a Meta write directly. Each one calls
the agent's governed core (`studio.run` / `controller.run_cycle` /
`controller.observe_structured`), and the Veto authorize (and the CODE
discipline gate) live *inside* that core, before any side effect. There is no
ungoverned path for a host LLM to take. **Veto governs; the provider / Meta
executes.**
