"""fal.ai image generation over x402 — keyless, governed by Veto.

This replaces the Replicate BYO-API-key tool. There is NO API key: the agent
pays fal.ai per call over x402, and the payment is authorized AND signed by
Veto (the agent never holds the signing key — control lives in Veto). See
X402_GOVERNED_SPEND_SPEC.md.

Flow (all inside `veto_pay.fetch_x402`):
    POST the fal endpoint → 402 challenge → Veto authorize (+sign) → pay USDC
    from the agent's Veto-held x402 wallet → fal returns the image URL.

If Veto denies/escalates, no payment is made and VetoDenied is raised — the
agent surfaces it and stops. Funding the agent's x402 wallet is the only setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

# fal.ai models exposed over x402 via the paysponge gateway (keyless).
# model → (endpoint url, price_usd). Prices from the x402 catalog.
FAL_MODELS = {
    "flux-schnell": ("https://fal.x402.paysponge.com/fal-ai/flux/schnell", 0.01),
    "flux-dev":     ("https://fal.x402.paysponge.com/fal-ai/flux/dev",     0.03),
    "flux-pro":     ("https://fal.x402.paysponge.com/fal-ai/flux-pro/v1.1", 0.04),
}
DEFAULT_MODEL = "flux-schnell"


@dataclass
class ToolResult:
    ok: bool
    actual_cost_usd: float
    output_path: str | None = None
    output_url: str | None = None
    receipt_url: str | None = None
    error: str | None = None
    denied: bool = False  # True when Veto denied/escalated (vs a tool failure)


def estimate_cost(model: str = DEFAULT_MODEL) -> float:
    return FAL_MODELS.get(model, FAL_MODELS[DEFAULT_MODEL])[1]


def models() -> list[tuple[str, float]]:
    """(model, price) options, cheapest first — for the agent's choice-gate."""
    return sorted(((m, p) for m, (_u, p) in FAL_MODELS.items()), key=lambda x: x[1])


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    endpoint: str | None = None,
    est_usd: float | None = None,
    cfg=None,
    output_dir: Path | None = None,
) -> ToolResult:
    """Generate an image over x402, governed by Veto.

    If `endpoint` is given (a discovered CDP Bazaar service URL) it's used
    directly; otherwise we fall back to the built-in fal.ai models. `cfg` is
    the veto-agents Config (needs api_key, agent_id, veto_api_base).
    """
    try:
        from veto_pay.x402 import AdapterContext, fetch_x402
        from veto_pay.errors import VetoDenied, VetoError
    except ImportError:
        return ToolResult(
            ok=False, actual_cost_usd=0.0,
            error="veto-pay is not installed. It ships with veto-agents — reinstall to repair.",
        )

    if endpoint:
        url, est = endpoint, (est_usd if est_usd is not None else 0.0)
    else:
        if model not in FAL_MODELS:
            return ToolResult(ok=False, actual_cost_usd=0.0,
                              error=f"Unknown model '{model}'. Options: {', '.join(FAL_MODELS)}")
        url, est = FAL_MODELS[model]

    if not (cfg and getattr(cfg, "api_key", None) and getattr(cfg, "agent_id", None)):
        return ToolResult(ok=False, actual_cost_usd=0.0,
                          error="Not signed in. Run `veto-agents setup` first.")

    # Read the account's governed wallet from main Veto (single source of
    # truth). If present, hand Veto the delegated-signer identity so it
    # signs the x402 EIP-3009 payment on the user's OWN wallet (Model B);
    # the `from` is the Privy EOA, not the Safe (only an EOA can produce
    # an EIP-3009 signature). If absent, Veto falls back to its derived
    # testnet key and the spend still gets governed.
    from veto_agents.wallet_setup import fetch_account_wallet
    wallet = fetch_account_wallet(cfg) or {}

    ctx = AdapterContext(
        veto_api_key=cfg.api_key,
        veto_agent_id=cfg.agent_id,
        veto_base_url=getattr(cfg, "veto_api_base", "https://veto-ai.com").removesuffix("/api/v1").rstrip("/"),
        privy_wallet_id=wallet.get("privy_wallet_id", "") or "",
        wallet_address=wallet.get("owner_address", "") or "",
    )

    # Pay-and-call. Veto authorizes + signs; we never hold a key.
    try:
        result = fetch_x402(
            url=url,
            ctx=ctx,
            method="POST",
            json_body={"prompt": prompt},
            extra_context={"tool": "fal.image_gen", "model": model},
        )
    except VetoDenied as e:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, denied=True,
            receipt_url=getattr(e, "receipt", None),
            error=f"Veto blocked this spend: {', '.join(getattr(e, 'reason_codes', []) or []) or e}",
        )
    except VetoError as e:
        return ToolResult(ok=False, actual_cost_usd=0.0, error=str(e))
    except Exception as e:
        return ToolResult(ok=False, actual_cost_usd=0.0, error=f"x402 call failed: {e}")

    # fal returns JSON with an image URL (shape: {"images": [{"url": ...}]} or {"image": {"url"}}).
    data = result.data if isinstance(result.data, dict) else {}
    image_url = _extract_image_url(data)
    if not image_url:
        return ToolResult(ok=False, actual_cost_usd=est, receipt_url=result.receipt,
                          error=f"No image URL in fal response (status {result.status}).")

    output_dir = output_dir or (Path.home() / "Downloads")
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = ".png" if ".png" in image_url else (".webp" if ".webp" in image_url else ".jpg")
    out_path = output_dir / f"veto-media-{abs(hash(image_url)) % 10**8}{ext}"
    try:
        with httpx.Client(timeout=60.0) as c:
            img = c.get(image_url)
            img.raise_for_status()
            out_path.write_bytes(img.content)
    except httpx.HTTPError as e:
        return ToolResult(ok=False, actual_cost_usd=est, output_url=image_url,
                          receipt_url=result.receipt, error=f"Couldn't download image: {e}")

    return ToolResult(
        ok=True, actual_cost_usd=est, output_path=str(out_path),
        output_url=image_url, receipt_url=result.receipt,
    )


def _extract_image_url(data: dict) -> str | None:
    imgs = data.get("images")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, dict):
            return first.get("url")
        if isinstance(first, str):
            return first
    img = data.get("image")
    if isinstance(img, dict):
        return img.get("url")
    if isinstance(img, str):
        return img
    return data.get("url") if isinstance(data.get("url"), str) else None
