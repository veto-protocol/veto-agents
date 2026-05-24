"""Replicate image generation tool.

Calls Replicate's HTTP API to generate an image from a text prompt. Used
by the Media agent after Veto authorizes the spend. Returns a `ToolResult`
with the local file path of the saved image + the actual cost in USD.

Auth: requires REPLICATE_API_TOKEN in the env. If unset, the tool returns
a clear error rather than crashing.

Cost estimation: Flux Schnell ≈ $0.003/image. Flux Dev ≈ $0.025. We default
to Flux Schnell for the price-conscious default; the agent can call with
`model="flux-dev"` for higher quality.

Polling: Replicate predictions are async — we poll up to 60s for completion.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


REPLICATE_API = "https://api.replicate.com/v1"

# model_slug → (versioned identifier, est_cost_usd)
# Models on Replicate are versioned; the slug works for "official models"
# (the platform handles the latest version automatically for these).
MODELS = {
    "flux-schnell": ("black-forest-labs/flux-schnell", 0.003),
    "flux-dev":     ("black-forest-labs/flux-dev",     0.025),
    "sdxl":         ("stability-ai/sdxl",              0.005),
}

DEFAULT_MODEL = "flux-schnell"


@dataclass
class ToolResult:
    ok: bool
    actual_cost_usd: float
    output_path: str | None = None
    output_url: str | None = None
    error: str | None = None


def estimate_cost(model: str = DEFAULT_MODEL) -> float:
    """Return the per-call cost estimate for `model`. Used by the agent's
    plan-then-execute step before Veto.authorize() is called."""
    return MODELS.get(model, MODELS[DEFAULT_MODEL])[1]


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "1:1",
    output_dir: Path | None = None,
    poll_interval_s: float = 1.5,
    timeout_s: float = 120.0,
) -> ToolResult:
    """Generate an image. Blocking — returns once the file is written to disk
    (or an error / timeout is reached)."""
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        return ToolResult(
            ok=False,
            actual_cost_usd=0.0,
            error=(
                "REPLICATE_API_TOKEN not set. Get one at "
                "https://replicate.com/account/api-tokens, then "
                "`export REPLICATE_API_TOKEN=r8_…`"
            ),
        )

    if model not in MODELS:
        return ToolResult(
            ok=False,
            actual_cost_usd=0.0,
            error=f"Unknown model '{model}'. Try one of: {', '.join(MODELS)}",
        )
    model_slug, est = MODELS[model]
    output_dir = output_dir or Path.home() / "Downloads"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Replicate's "official models" endpoint accepts the slug directly. For
    # non-official models we'd need a version hash; this set is all official.
    create_url = f"{REPLICATE_API}/models/{model_slug}/predictions"

    body = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            # Flux-schnell defaults are reasonable; we don't override num_steps
        }
    }

    with httpx.Client(timeout=30.0) as client:
        try:
            r = client.post(create_url, headers=_headers(token), json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return ToolResult(ok=False, actual_cost_usd=0.0, error=f"Replicate create: {e}")

        prediction = r.json()
        prediction_id = prediction.get("id")
        if not prediction_id:
            return ToolResult(ok=False, actual_cost_usd=0.0, error="Replicate returned no prediction id")

        # Poll until status is succeeded / failed / canceled
        poll_url = f"{REPLICATE_API}/predictions/{prediction_id}"
        start_t = time.monotonic()
        last_status = ""
        while True:
            if time.monotonic() - start_t > timeout_s:
                return ToolResult(
                    ok=False,
                    actual_cost_usd=est,  # we got billed at request time anyway
                    error=f"Timed out after {timeout_s:.0f}s (last status: {last_status})",
                )
            try:
                p = client.get(poll_url, headers=_headers(token))
                p.raise_for_status()
            except httpx.HTTPError as e:
                return ToolResult(ok=False, actual_cost_usd=est, error=f"Replicate poll: {e}")
            data = p.json()
            last_status = data.get("status", "")
            if last_status in ("succeeded", "failed", "canceled"):
                break
            time.sleep(poll_interval_s)

        if last_status != "succeeded":
            return ToolResult(
                ok=False,
                actual_cost_usd=est,
                error=data.get("error") or f"Replicate status: {last_status}",
            )

        # Output is either a single URL string or a list of URL strings.
        output = data.get("output")
        image_url = output[0] if isinstance(output, list) and output else output
        if not image_url or not isinstance(image_url, str):
            return ToolResult(ok=False, actual_cost_usd=est, error="No output URL in response")

        # Download + save the image
        try:
            img_resp = client.get(image_url)
            img_resp.raise_for_status()
        except httpx.HTTPError as e:
            return ToolResult(
                ok=False, actual_cost_usd=est, output_url=image_url,
                error=f"Couldn't fetch image: {e}",
            )

        # Filename: short prediction id + extension guess from URL
        ext = ".webp" if ".webp" in image_url else (".png" if ".png" in image_url else ".jpg")
        filename = f"veto-media-{prediction_id[:8]}{ext}"
        out_path = output_dir / filename
        out_path.write_bytes(img_resp.content)

    return ToolResult(
        ok=True,
        actual_cost_usd=est,
        output_path=str(out_path),
        output_url=image_url,
    )
