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
`receipt_url`), never as a silent bypass. No tool argument (mock=false + a huge
budget, an injection in the brief, an unknown image_provider) can bypass the
gate — the gate does not read those args to decide whether to run.

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

ERROR CONTRACT — every tool ALWAYS returns a plain, JSON-serializable dict.
On any failure a tool returns {"ok": false, "error": "<code>", ...} rather than
raising: a raw traceback must never reach the MCP client. Success paths return
the underlying manifest / observation dict as-is (also plain dicts).
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__

# The canonical set of image providers. This is the SINGLE source of truth for
# the enum surfaced in the tool schema AND the in-body guard. It must stay
# EXACTLY {openai, fal}: "fal" is the FREE x402 fallback (the safe default);
# "openai" costs real money (~$0.25/image, BYO OPENAI_API_KEY). Anything else
# (unknown/empty/typo/wrong-case) must NEVER silently reach the PAID provider —
# it is rejected before studio.run() sees it.
_IMAGE_PROVIDERS = ("openai", "fal")
_DEFAULT_IMAGE_PROVIDER = "fal"  # free & safe by default; never a paid default

# Error `detail` returned to the MCP host is scrubbed of anything key-shaped
# before it leaves the process. Provider 401 bodies (and other exceptions) can
# echo a masked key prefix; the LLM path already redacts upstream, but the MCP
# boundary redacts again — defense in depth — and truncates to one tidy line.
import re as _re

_KEY_TOKEN_RE = _re.compile(r"(sk-ant-[\w-]+|sk-[\w-]+|Bearer\s+[\w.\-]+|EAA[\w]+)")


def _safe_detail(exc: Exception, limit: int = 300) -> str:
    """One-line, secret-redacted string for an exception's `detail` field."""
    msg = _KEY_TOKEN_RE.sub("[redacted]", str(exc))
    msg = " ".join(msg.split())  # collapse newlines/whitespace → single line
    return msg[:limit]

# The server name + a discoverable instruction the MCP host shows the user. The
# instruction carries the PACKAGE version (not just the FastMCP framework
# version) and states the governance guarantee up front. Matches the wiring docs
# (`claude mcp add veto-agents -- veto-agents mcp`).
_SERVER_INSTRUCTIONS = (
    f"veto-agents {__version__} — a Veto-governed media buyer.\n"
    "Governance is enforced INSIDE every tool, fail-closed: Veto authorizes and "
    "a code discipline gate disciplines every spend or ad-account write BEFORE it "
    "happens. There is no ungoverned path — no tool argument can bypass the gate. "
    "Every tool returns a plain JSON dict; on failure it returns "
    '{"ok": false, "error": "..."} instead of raising. Blocks surface as data '
    "(a denied status / deny verdict + a receipt_url), never as a silent bypass."
)

mcp = FastMCP("veto-agents", instructions=_SERVER_INSTRUCTIONS)


def _load_cfg():
    """Load config fresh on every call.

    The server is long-lived; loading per-call means a sign-in (or a policy /
    key change) made while the host is running is picked up without a restart.
    Delegates to config.load(), which merges the keychain credentials.
    """
    from . import config as cfg_module

    return cfg_module.load()


def _normalize_image_provider(value: Any) -> str | None:
    """Coerce a caller-supplied image_provider to a canonical value or None.

    Returns "openai" or "fal" for a recognized value (case/whitespace tolerant),
    or None for anything unknown/empty/non-string. The caller MUST treat None as
    an error — an unrecognized value must NEVER be forwarded to studio.run(),
    because studio's own fallback only special-cases the exact string "openai"
    and routes every OTHER value to the PAID OpenAI provider. Guarding here is
    what makes "any non-fal string" incapable of triggering a real charge.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in _IMAGE_PROVIDERS else None


@mcp.tool()
def create_ad_creative(
    brief: str,
    image_provider: Annotated[
        str,
        Field(
            description=(
                'Image generator. "fal" = FREE fal.ai via x402 (the default, '
                'no charge, no key). "openai" = gpt-image-1 and COSTS REAL '
                "MONEY (~$0.25 per image, requires your own OPENAI_API_KEY); it "
                'auto-falls back to free "fal" when no OpenAI key is present. '
                "Only these two values are accepted; anything else is rejected "
                "(it will NOT run and will NOT be charged)."
            ),
            json_schema_extra={"enum": list(_IMAGE_PROVIDERS)},
        ),
    ] = _DEFAULT_IMAGE_PROVIDER,
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
      image_provider: "fal" (FREE x402, default) or "openai" (BYO
        OPENAI_API_KEY, ~$0.25/image). "openai" auto-falls back to "fal" when no
        OpenAI key is present. Any other value is rejected before any spend — it
        can never silently trigger the paid provider.
      want_video: also produce a hero video (Higgsfield, BYO key).
      want_voice: also produce a voiceover (ElevenLabs, BYO key).

    Returns the manifest dict on success:
      {brief, created_at, output_dir,
       concept:{concept,theme,tone,copy:{headlines,primary_texts,ctas},
                image_prompt,video_prompt,voiceover_script},
       assets:[{type,provider,status("ok"|"denied"|"skipped"|"error"),path,url,
                cost_usd,verdict,receipt_url,error}],
       totals:{assets_made,assets_total,cost_usd},
       providers_available:{openai_image,higgsfield_video,elevenlabs_voice}}
    On failure returns {"ok": false, "error": "<code>", ...}.
    """
    # ── validate BEFORE any import/spend: an unknown provider must never reach
    #    studio.run(), whose fallback would route it to the PAID OpenAI driver.
    provider = _normalize_image_provider(image_provider)
    if provider is None:
        return {
            "ok": False,
            "error": "invalid_image_provider",
            "detail": (
                f"image_provider must be one of {list(_IMAGE_PROVIDERS)}; got "
                f"{image_provider!r}. Refusing to run so an unknown value cannot "
                'trigger the PAID "openai" provider. Use "fal" (free) or "openai".'
            ),
            "allowed": list(_IMAGE_PROVIDERS),
        }

    try:
        from rich.console import Console

        from .agents.adbuyer.creative import creds as creative_creds, studio

        cfg = _load_cfg()
        want = ["copy", "image"]
        if want_video:
            want.append("video")
        if want_voice:
            want.append("voice")

        manifest = studio.run(
            brief,
            cfg,
            Console(quiet=True),  # governance still runs; console output discarded
            want=tuple(want),
            image_provider=provider,  # normalized canonical value only
        )

        # Tell the caller which paid assets are POSSIBLE (presence-only, no secrets).
        manifest["providers_available"] = creative_creds.describe(cfg)
        return manifest
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {
            "ok": False,
            "error": "creative_studio_error",
            "detail": _safe_detail(e),
            "brief": brief,
        }


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

    Both gates run INSIDE this call and CANNOT be bypassed by any argument: the
    CODE discipline gate runs BEFORE the Veto authorize, which runs BEFORE any
    Meta write. Neither `mock=false` nor a huge proposed budget nor injected text
    in `goal` skips a gate — the gate does not consult those to decide whether to
    run. A misbehaving brain cannot spend — that is the whole point.

    Args:
      goal: the standing objective to optimize toward, e.g.
        "US traffic to https://mysite.com, keep CPC under $1".
      mock: True (default) mimics Meta entirely offline — seeded fake campaigns,
        NO real ad account, NO real spend — so it is demoable with no Meta setup.
        The REAL Veto authorize (free, decision_only) and the discipline gate
        still run on every action. Set False to run against your real account
        (requires META_ACCESS_TOKEN / META_AD_ACCOUNT_ID / META_PAGE_ID).
      dry_run: run both gates but skip the actual Meta write.

    Returns on success:
      {goal, mock, dry_run, brain, summary,
       observed:{account,campaigns,adsets,ads,errors},
       proposals:[<human-readable action>],
       actions:[{type,entity,entity_level,verdict,applied,rationale,reason,
                 receipt_url,reason_codes,outcome,old_budget_usd,new_budget_usd}],
       summary_counts:{executed,held,denied,escalated,skipped,failed,dry_run}}
    On failure returns {"ok": false, "error": "<code>", ...} (e.g.
    "not_signed_in" | "meta_credentials_missing" | "run_cycle_error"). `reason`
    on a held action is the discipline HOLD explanation; `verdict`+`receipt_url`
    on a denied/escalated action are the Veto decision.
    """
    try:
        from .agents.adbuyer import controller

        cfg = _load_cfg()
        return controller.run_cycle(cfg, goal, mock=mock, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {"ok": False, "error": "run_cycle_error", "detail": _safe_detail(e), "goal": goal}


@mcp.tool()
def get_campaigns(mock: bool = True) -> dict[str, Any]:
    """Read-only snapshot of the Meta ad account. No spend, so no Veto gate.

    Returns the current account + campaigns + ad sets + ads + last-7d insights —
    the same OBSERVE the autonomous loop reasons over. Use it to inspect state
    before proposing changes via run_ad_cycle. This tool performs NO writes and
    moves NO money, so it is intentionally ungated.

    Args:
      mock: True (default) reads the offline mimicked account (no Meta creds
        needed); False reads your real account (requires the META_* creds).

    Returns on success:
      {ad_account_id, account, campaigns, adsets, ads, insights, errors,
       currency}. On failure returns {"ok": false, "error": "<code>", ...}
      (e.g. "not_signed_in" | "meta_credentials_missing" | "observe_error").
    """
    try:
        from .agents.adbuyer import controller

        cfg = _load_cfg()
        return controller.observe_structured(cfg, mock=mock)
    except Exception as e:  # noqa: BLE001 — return errors as data, never throw
        return {"ok": False, "error": "observe_error", "detail": _safe_detail(e)}


def main() -> None:
    """Run the server over STDIO (the transport every MCP host speaks)."""
    mcp.run()


if __name__ == "__main__":
    main()
