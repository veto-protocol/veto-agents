"""FREE image fallback — a thin wrapper over the media agent's x402 fal.ai tool.

The studio reuses the already-governed image tool at
`veto_agents/agents/media/tools/fal_image.py` unchanged. That tool self-gates:
the Veto authorize happens INSIDE `veto_pay.fetch_x402` (the 402 challenge
triggers it), so there is no separate `gate()` call here. This wrapper only
remaps the media tool's `ToolResult` onto the studio's shared `ToolResult`
(with `provider`/`verdict` populated) so the orchestrator can treat every asset
uniformly.

"Free" = no BYO API key. It DOES require Veto sign-in + a funded x402 wallet,
and each call still spends a few cents of USDC (flux-schnell ≈ $0.01), governed
by Veto. That's the point: it works with zero provider accounts.
"""

from __future__ import annotations

from pathlib import Path

# The existing, unchanged, x402-governed media tool.
from veto_agents.agents.media.tools import fal_image as media_fal

from ..types import ToolResult

DEFAULT_MODEL = "flux-schnell"

# One actionable line for the common "fal can't settle" failure. The free fal
# path pays fal.ai a few cents of USDC per image over x402; on a fresh box the
# agent's x402 wallet isn't funded/wired yet, so Veto has nothing to sign the
# payment with (or the challenge offers only an unsupported chain). Either way
# the user needs setup — never a raw internal scheme/envelope error.
# Leads with the Veto gate state ("allowed the spend") so a governance signal is
# always visible even on the fallthrough render path, then gives the fix.
_ACTIONABLE = (
    "Veto allowed the spend, but fal couldn't be paid — it needs a funded x402 "
    "wallet on Base. Run `veto-agents wallet setup`, or use `--image-provider openai` "
    "(BYO OPENAI_API_KEY)."
)

# Substrings in the media tool's error that mean "the spend was fine but the
# x402 payment could not be settled" — i.e. a setup/wallet gap, NOT a Veto deny
# and NOT a plain provider/HTTP failure. These map to the low-level VetoError /
# transport errors raised inside veto_pay.x402 while trying to pay:
#   • "no scheme we support"      → challenge had no supported (exact-on-Base) leg
#   • "no x402 payment envelope"  → Veto allowed but returned no signed payment
#   • "x402 wallet"               → wallet-setup prompt from veto_pay
#   • "x402 call failed"          → transport error during the pay-and-retry
_SETTLEMENT_GAP_MARKERS = (
    "no scheme we support",
    "no x402 payment envelope",
    "x402 wallet",
    "x402 call failed",
)

# Substrings that are NOT a settlement gap — surface these as-is (they already
# read cleanly and point the user at the real fix).
_PASS_THROUGH_MARKERS = (
    "not signed in",          # run `veto-agents setup`
    "veto-pay is not installed",
)


def _is_settlement_gap(error: str | None) -> bool:
    """True if `error` is the fal-can't-pay (wallet/setup) class, not a deny."""
    if not error:
        return False
    low = error.lower()
    if any(m in low for m in _PASS_THROUGH_MARKERS):
        return False
    return any(m in low for m in _SETTLEMENT_GAP_MARKERS)


def estimate_cost(model: str = DEFAULT_MODEL) -> float:
    return media_fal.estimate_cost(model)


def models() -> list[tuple[str, float]]:
    return media_fal.models()


def generate(
    prompt: str,
    *,
    cfg=None,
    model: str = DEFAULT_MODEL,
    output_dir: Path | None = None,
    concept: str | None = None,  # accepted for a uniform call signature; unused
) -> ToolResult:
    """Generate a free (x402) image via the media fal tool; adapt the result.

    The media tool self-gates: the Veto authorize happens inside
    `veto_pay.fetch_x402`. Three outcomes reach us here:

    * ok            → the spend was allowed AND settled → verdict "allow".
    * denied        → Veto blocked/escalated the spend → verdict "deny",
                      surfaced with the receipt so the user sees WHY.
    * settlement gap→ the spend wasn't refused, but fal couldn't be PAID
                      (x402 wallet not funded/wired, or the challenge had no
                      supported leg). We rewrite the low-level internal error
                      (e.g. "no scheme we support", "no x402 payment envelope")
                      into ONE actionable line and — because Veto did NOT deny
                      — still mark verdict "allow" so the studio prints a Veto
                      gate line instead of a bare red error.
    """
    r = media_fal.generate(prompt=prompt, model=model, cfg=cfg, output_dir=output_dir)

    if r.denied:
        # Genuine Veto block/escalate — pass through untouched (receipt + reason).
        return ToolResult(
            ok=False,
            actual_cost_usd=r.actual_cost_usd,
            output_path=r.output_path,
            output_url=r.output_url,
            receipt_url=r.receipt_url,
            error=r.error,
            denied=True,
            verdict="deny",
            provider="fal",
        )

    if not r.ok and _is_settlement_gap(r.error):
        # Spend wasn't refused; fal just couldn't be paid on this box. Replace
        # the raw scheme/envelope error with an actionable one, and keep a Veto
        # gate line visible (verdict "allow" — Veto authorized, settlement is
        # what's missing).
        return ToolResult(
            ok=False,
            actual_cost_usd=0.0,
            receipt_url=r.receipt_url,
            error=_ACTIONABLE,
            denied=False,
            verdict="allow",
            provider="fal",
        )

    # ok, or a plain pass-through failure (not signed in / not installed / bad
    # response) — map straight across.
    return ToolResult(
        ok=r.ok,
        actual_cost_usd=r.actual_cost_usd,
        output_path=r.output_path,
        output_url=r.output_url,
        receipt_url=r.receipt_url,
        error=r.error,
        denied=r.denied,
        verdict=("allow" if r.ok else None),
        provider="fal",
    )
