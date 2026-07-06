"""veto-agents MCP server (STDIO) — the media buyer, Veto-governed.

Drops the ad-buyer / creative-studio agent into any MCP host (Claude Code,
Claude Desktop, OpenClaw). Launch it with:

    veto-agents mcp

and wire it into your host per docs/MCP.md.

PRINCIPLE — governance lives INSIDE every tool, fail-closed.
Each tool below calls the AGENT'S OWN governed core (studio.run / controller
.run_cycle / controller.observe_structured). Veto authorizes — and the CODE
discipline gate disciplines — every spend or ad-account mutation BEFORE it
happens, inside those functions. A host LLM calling these tools CANNOT reach a
provider HTTP endpoint or a Meta write around Veto: there is no ungoverned path.
A block surfaces as structured data (a `denied` status / a `deny` verdict + a
`receipt_url`), never as a silent bypass.

Tools:
  • create_ad_creative — brief → a coherent ad package (copy + hero image, opt.
    video/voice). Each PAID asset is Veto-gated inside its provider driver.
  • run_ad_cycle       — one autonomous OBSERVE→DECIDE→DISCIPLINE→VETO→ACT cycle
    over a Meta ad account (mock by default). Every budget move is Veto-gated.
  • get_campaigns      — read-only snapshot of the ad account (no spend, no gate).

Config/creds resolution is the same as the rest of veto-agents: sign-in comes
from the OS keychain / ~/.veto-agents (loaded by config.load()); provider keys
(OpenAI/Higgsfield/ElevenLabs) and Meta creds are BYO, resolved by the creative
`creds` / `meta_env` modules from env / ~/.veto/*.env / the keychain.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

# The server name the MCP host shows the user. Matches the wiring docs
# (`claude mcp add veto-agents -- veto-agents mcp`).
mcp = FastMCP("veto-agents")


def _load_cfg():
    """Load config fresh on every call.

    The server is long-lived; loading per-call means a sign-in (or a policy /
    key change) made while the host is running is picked up without a restart.
    Delegates to config.load(), which merges the keychain credentials.
    """
    from . import config as cfg_module

    return cfg_module.load()


@mcp.tool()
def create_ad_creative(
    brief: str,
    image_provider: str = "openai",
    want_video: bool = False,
    want_voice: bool = False,
) -> dict[str, Any]:
    """Turn a product/campaign brief into a coherent ad creative package.

    Runs the LLM creative director once to derive ONE concept, then produces
    ad copy + a hero image (and, if asked, a hero video and/or a voiceover), all
    matching that concept. NO Meta account is needed — this is the creative
    stage; placing the ad is a separate step (see run_ad_cycle).

    Veto governs every PAID asset INSIDE its provider driver, BEFORE the provider
    is called: a deny/escalate makes that asset `status:"denied"` with a
    `verdict` + `receipt_url` on its row — it is NOT generated. Copy is free (LLM
    only). Missing provider keys degrade gracefully (that asset is `skipped`).

    Args:
      brief: what to advertise, e.g. "premium cold-brew for busy founders".
      image_provider: "openai" (BYO OPENAI_API_KEY) or "fal" (free x402).
        "openai" auto-falls back to "fal" when no OpenAI key is present.
      want_video: also produce a hero video (Higgsfield, BYO key).
      want_voice: also produce a voiceover (ElevenLabs, BYO key).

    Returns the manifest dict:
      {brief, created_at, output_dir,
       concept:{concept,theme,tone,copy:{headlines,primary_texts,ctas},
                image_prompt,video_prompt,voiceover_script},
       assets:[{type,provider,status("ok"|"denied"|"skipped"|"error"),path,url,
                cost_usd,verdict,receipt_url,error}],
       totals:{assets_made,assets_total,cost_usd},
       providers_available:{openai_image,higgsfield_video,elevenlabs_voice}}
    """
    from rich.console import Console

    from .agents.adbuyer.creative import creds as creative_creds, studio

    cfg = _load_cfg()
    want = ["copy", "image"]
    if want_video:
        want.append("video")
    if want_voice:
        want.append("voice")

    try:
        manifest = studio.run(
            brief,
            cfg,
            Console(quiet=True),  # governance still runs; console output discarded
            want=tuple(want),
            image_provider=image_provider,
        )
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {"error": "creative_studio_error", "detail": str(e), "brief": brief}

    # Tell the caller which paid assets are POSSIBLE (presence-only, no secrets).
    manifest["providers_available"] = creative_creds.describe(cfg)
    return manifest


@mcp.tool()
def run_ad_cycle(
    goal: str,
    mock: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run ONE autonomous, Veto-governed media-buying cycle over a Meta account.

    Executes the full loop ONCE: OBSERVE the account → DECIDE actions (adjust
    budget / pause / resume / refresh creative on EXISTING entities only) →
    DISCIPLINE (a CODE readiness gate: respect learning phase, require enough
    days+data, per-entity cooldown, clamp budget change magnitude) → VETO
    authorize (fail-closed) → ACT (the Meta write).

    Both gates run INSIDE this call and CANNOT be bypassed: the CODE discipline
    gate runs BEFORE the Veto authorize, which runs BEFORE any Meta write. A
    misbehaving brain cannot spend — that is the whole point.

    Args:
      goal: the standing objective to optimize toward, e.g.
        "US traffic to https://mysite.com, keep CPC under $1".
      mock: True (default) mimics Meta entirely offline — seeded fake campaigns,
        NO real ad account, NO real spend — so it is demoable with no Meta setup.
        The REAL Veto authorize (free, decision_only) and the discipline gate
        still run on every action. Set False to run against your real account
        (requires META_ACCESS_TOKEN / META_AD_ACCOUNT_ID / META_PAGE_ID).
      dry_run: run both gates but skip the actual Meta write.

    Returns:
      {goal, mock, dry_run, brain, summary,
       observed:{account,campaigns,adsets,ads,errors},
       proposals:[<human-readable action>],
       actions:[{type,entity,entity_level,verdict,applied,rationale,reason,
                 receipt_url,reason_codes,outcome,old_budget_usd,new_budget_usd}],
       summary_counts:{executed,held,denied,escalated,skipped,failed,dry_run}}
    or {"error":"not_signed_in"|"meta_credentials_missing", ...}. `reason` on a
    held action is the discipline HOLD explanation; `verdict`+`receipt_url` on a
    denied/escalated action are the Veto decision.
    """
    from .agents.adbuyer import controller

    cfg = _load_cfg()
    try:
        return controller.run_cycle(cfg, goal, mock=mock, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {"error": "run_cycle_error", "detail": str(e), "goal": goal}


@mcp.tool()
def get_campaigns(mock: bool = True) -> dict[str, Any]:
    """Read-only snapshot of the Meta ad account. No spend, so no Veto gate.

    Returns the current account + campaigns + ad sets + ads + last-7d insights —
    the same OBSERVE the autonomous loop reasons over. Use it to inspect state
    before proposing changes via run_ad_cycle.

    Args:
      mock: True (default) reads the offline mimicked account (no Meta creds
        needed); False reads your real account (requires the META_* creds).

    Returns:
      {ad_account_id, account, campaigns, adsets, ads, insights, errors,
       currency} or {"error":"not_signed_in"|"meta_credentials_missing", ...}.
    """
    from .agents.adbuyer import controller

    cfg = _load_cfg()
    try:
        return controller.observe_structured(cfg, mock=mock)
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {"error": "observe_error", "detail": str(e)}


def main() -> None:
    """Run the server over STDIO (the transport every MCP host speaks)."""
    mcp.run()


if __name__ == "__main__":
    main()
