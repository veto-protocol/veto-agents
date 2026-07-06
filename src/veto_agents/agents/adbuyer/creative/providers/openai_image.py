"""OpenAI gpt-image-1 image generation — BYO OPENAI_API_KEY, governed by Veto.

Unlike the x402 image tool, there is no 402 challenge here: OpenAI is a plain
BYO-key REST API. So we call `VetoClient.authorize` OURSELVES (via creative.gate)
BEFORE the OpenAI request. deny/escalate → skip the asset + log the receipt;
allow → generate.

API shape (verified July 2026):
  POST https://api.openai.com/v1/images/generations
  Authorization: Bearer $OPENAI_API_KEY
  body: { model: "gpt-image-1", prompt, size, quality, n, output_format }
  → SYNC (no polling). gpt-image-1 ALWAYS returns base64 (data[].b64_json),
    never a URL. We decode + save the PNG ourselves.

Gotchas: using gpt-image-1 requires OpenAI org verification; it is scheduled to
deprecate 2026-10-23 (successors gpt-image-1.5 / gpt-image-2 use the same shape
— swap `model`). Keys are read via the creds resolver; never logged.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..creds import resolve as resolve_cred
from ..gate import gate
from ..types import ToolResult

MERCHANT = "api.openai.com"
API_URL = "https://api.openai.com/v1/images/generations"
DEFAULT_MODEL = "gpt-image-1"
DEFAULT_SIZE = "1536x1024"      # 16:9-ish hero frame
DEFAULT_QUALITY = "high"

# Approx USD per 1024x1024 image by quality (OpenAI list price, image tokens).
_QUALITY_PRICE = {"low": 0.011, "medium": 0.042, "high": 0.167, "auto": 0.167}
# Larger sizes bill by more image tokens — rough multiplier vs 1024x1024.
_SIZE_MULT = {"1024x1024": 1.0, "1024x1536": 1.5, "1536x1024": 1.5, "auto": 1.0}


def estimate_cost(size: str = DEFAULT_SIZE, quality: str = DEFAULT_QUALITY, n: int = 1) -> float:
    per = _QUALITY_PRICE.get(quality, 0.167) * _SIZE_MULT.get(size, 1.0)
    return round(per * max(1, n), 3)


def generate(
    prompt: str,
    *,
    cfg=None,
    model: str = DEFAULT_MODEL,
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    n: int = 1,
    output_format: str = "png",
    output_dir: Path | None = None,
    concept: str | None = None,
    console=None,
) -> ToolResult:
    """Generate an image with gpt-image-1, Veto-gated. Returns a ToolResult."""
    key = resolve_cred("OPENAI_API_KEY", cfg)
    if not key:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, provider="openai", skipped=True,
            error="No OPENAI_API_KEY — set it to enable OpenAI images "
                  "(or use the free fal fallback).",
        )

    est = estimate_cost(size, quality, n)

    # ── Veto gate BEFORE the paid call ────────────────────────────────────
    g = gate(
        cfg,
        merchant=MERCHANT,
        amount=est,
        description=f"gpt-image-1 image ({quality}, {size})",
        context={
            "tool": "creative.openai_image",
            "model": model,
            "size": size,
            "quality": quality,
            "prompt": prompt[:500],
            "concept": (concept or "")[:500],
        },
        console=console,
    )
    if not g.allowed:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, denied=True, verdict=g.verdict,
            receipt_url=g.receipt_url, provider="openai",
            error=f"Veto {g.verdict}: {g.block_reason()}",
        )

    # ── Allowed → call OpenAI ─────────────────────────────────────────────
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": max(1, n),
        "output_format": output_format,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=120.0) as c:
            r = c.post(API_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="openai",
                          error=f"OpenAI HTTP {e.response.status_code}: {body}")
    except httpx.HTTPError as e:
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="openai",
                          error=f"OpenAI request failed: {e}")

    b64 = _extract_b64(data)
    if not b64:
        return ToolResult(ok=False, actual_cost_usd=est, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="openai",
                          error="No image data (b64_json) in OpenAI response.")

    output_dir = output_dir or (Path.home() / "Downloads")
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}.get(output_format, ".png")
    out_path = output_dir / f"openai-image-{abs(hash(b64[:64])) % 10**8}{ext}"
    try:
        out_path.write_bytes(base64.b64decode(b64))
    except Exception as e:  # noqa: BLE001
        return ToolResult(ok=False, actual_cost_usd=est, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="openai",
                          error=f"Couldn't decode/save image: {e}")

    return ToolResult(
        ok=True, actual_cost_usd=est, output_path=str(out_path),
        receipt_url=g.receipt_url, verdict=g.verdict, provider="openai",
    )


def _extract_b64(data: dict) -> str | None:
    """gpt-image-1 returns {"data": [{"b64_json": "..."}]}."""
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("b64_json")
    return None
