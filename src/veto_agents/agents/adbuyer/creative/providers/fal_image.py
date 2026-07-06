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
    """Generate a free (x402) image via the media fal tool; adapt the result."""
    r = media_fal.generate(prompt=prompt, model=model, cfg=cfg, output_dir=output_dir)
    # Map the media ToolResult (which lacks verdict/provider) onto ours.
    verdict = "deny" if r.denied else ("allow" if r.ok else None)
    return ToolResult(
        ok=r.ok,
        actual_cost_usd=r.actual_cost_usd,
        output_path=r.output_path,
        output_url=r.output_url,
        receipt_url=r.receipt_url,
        error=r.error,
        denied=r.denied,
        verdict=verdict,
        provider="fal",
    )
