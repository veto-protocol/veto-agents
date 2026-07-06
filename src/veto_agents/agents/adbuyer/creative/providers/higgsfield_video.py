"""Higgsfield DoP video generation — BYO key, governed by Veto.

BYO-key (no 402), so we Veto-gate ourselves BEFORE the paid call, exactly like
openai_image.

API shape (first-party Cloud SDK surface, verified July 2026):
  host   https://platform.higgsfield.ai
  auth   Authorization: Key KEY_ID:KEY_SECRET
  text→video   POST /v1/text2video/dop
  image→video  POST /v1/image2video/dop   (add input_images)
  body   { model: "dop-lite"|"dop-preview"|"dop-turbo", prompt, duration: 5|10,
           seed?, aspect_ratio?, resolution? }
  ASYNC  submit → request_id → poll GET /v1/requests/{request_id}/status
         statuses: queued | in_progress | completed | failed | nsfw | cancelled
  done   { status: "completed", video: { url: "https://.../out.mp4" } }
         (R2 CDN URL — fetch promptly, links expire.)

Pricing is credit-based and effectively needs a paid subscription; the per-call
figures below are reseller-equivalent estimates used only for the Veto gate
amount. Fails soft: a missing/incomplete key returns a skipped ToolResult with a
clear message; the studio keeps going and produces the rest.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from ..creds import higgsfield_credentials
from ..gate import gate
from ..types import ToolResult

MERCHANT = "platform.higgsfield.ai"
HOST = "https://platform.higgsfield.ai"
DEFAULT_MODEL = "dop-lite"  # cheapest tier by default — never silently upgrade

# Reseller-equivalent USD per 5s DoP clip (for the Veto gate estimate only).
_MODEL_PRICE_5S = {"dop-lite": 0.14, "dop-turbo": 0.42, "dop-preview": 0.57}
_TERMINAL = {"completed", "failed", "nsfw", "cancelled"}


def estimate_cost(model: str = DEFAULT_MODEL, duration: int = 5) -> float:
    per5 = _MODEL_PRICE_5S.get(model, 0.14)
    return round(per5 * (max(1, duration) / 5.0), 3)


def generate(
    prompt: str,
    *,
    cfg=None,
    model: str = DEFAULT_MODEL,
    duration: int = 5,
    image_url: str | None = None,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    seed: int | None = None,
    output_dir: Path | None = None,
    poll_timeout_s: float = 300.0,
    poll_interval_s: float = 4.0,
    concept: str | None = None,
    console=None,
) -> ToolResult:
    """Generate a short video with Higgsfield DoP, Veto-gated. Returns ToolResult."""
    creds = higgsfield_credentials(cfg)
    if not creds:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, provider="higgsfield", skipped=True,
            error="No Higgsfield key — set HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET "
                  "(or HIGGSFIELD_CREDENTIALS=KEY_ID:KEY_SECRET) to enable video.",
        )
    key_id, key_secret = creds
    est = estimate_cost(model, duration)

    # ── Veto gate BEFORE the paid call ────────────────────────────────────
    g = gate(
        cfg,
        merchant=MERCHANT,
        amount=est,
        description=f"Higgsfield DoP video ({model}, {duration}s)",
        context={
            "tool": "creative.higgsfield_video",
            "model": model,
            "duration": duration,
            "prompt": prompt[:500],
            "concept": (concept or "")[:500],
        },
        console=console,
    )
    if not g.allowed:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, denied=True, verdict=g.verdict,
            receipt_url=g.receipt_url, provider="higgsfield",
            error=f"Veto {g.verdict}: {g.block_reason()}",
        )

    # ── Allowed → submit + poll ───────────────────────────────────────────
    headers = {"Authorization": f"Key {key_id}:{key_secret}", "Content-Type": "application/json"}
    body: dict = {"model": model, "prompt": prompt, "duration": duration}
    if seed is not None:
        body["seed"] = seed
    if image_url:
        path = "/v1/image2video/dop"
        body["input_images"] = [{"type": "image_url", "image_url": image_url}]
    else:
        path = "/v1/text2video/dop"
        body["aspect_ratio"] = aspect_ratio
        body["resolution"] = resolution

    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{HOST}{path}", headers=headers, json=body)
            r.raise_for_status()
            submit = r.json() if isinstance(r.json(), dict) else {}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text[:300]
        except Exception:
            pass
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="higgsfield",
                          error=f"Higgsfield HTTP {e.response.status_code}: {detail}")
    except httpx.HTTPError as e:
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="higgsfield",
                          error=f"Higgsfield request failed: {e}")

    video_url, err = _resolve_video(submit, headers, poll_timeout_s, poll_interval_s)
    if err:
        return ToolResult(ok=False, actual_cost_usd=est, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="higgsfield", error=err)

    output_dir = output_dir or (Path.home() / "Downloads")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"higgsfield-video-{abs(hash(video_url)) % 10**8}.mp4"
    try:
        with httpx.Client(timeout=180.0) as c:
            vr = c.get(video_url)
            vr.raise_for_status()
            out_path.write_bytes(vr.content)
    except httpx.HTTPError as e:
        return ToolResult(ok=False, actual_cost_usd=est, output_url=video_url,
                          verdict=g.verdict, receipt_url=g.receipt_url, provider="higgsfield",
                          error=f"Couldn't download video: {e}")

    return ToolResult(
        ok=True, actual_cost_usd=est, output_path=str(out_path), output_url=video_url,
        verdict=g.verdict, receipt_url=g.receipt_url, provider="higgsfield",
    )


def _resolve_video(
    submit: dict, headers: dict, timeout_s: float, interval_s: float
) -> tuple[str | None, str | None]:
    """Return (video_url, error). Handles both a sync-complete submit and the
    async request_id → poll path."""
    # Already completed in the submit response?
    url = _video_url(submit)
    status = str(submit.get("status", "")).lower()
    if url and status in ("", "completed"):
        return url, None
    if status in ("failed", "nsfw", "cancelled"):
        return None, f"Higgsfield job {status}."

    request_id = submit.get("request_id") or submit.get("id")
    if not request_id:
        if url:
            return url, None
        return None, "No request_id or video URL in Higgsfield submit response."

    deadline = time.time() + timeout_s
    poll_url = f"{HOST}/v1/requests/{request_id}/status"
    while time.time() < deadline:
        time.sleep(interval_s)
        try:
            with httpx.Client(timeout=30.0) as c:
                pr = c.get(poll_url, headers=headers)
                pr.raise_for_status()
                data = pr.json() if isinstance(pr.json(), dict) else {}
        except httpx.HTTPError as e:
            return None, f"Higgsfield poll failed: {e}"
        st = str(data.get("status", "")).lower()
        if st == "completed":
            u = _video_url(data)
            return (u, None) if u else (None, "Higgsfield completed but no video URL.")
        if st in ("failed", "nsfw", "cancelled"):
            return None, f"Higgsfield job {st}."
        # queued / in_progress → keep polling
    return None, f"Higgsfield job timed out after {timeout_s:.0f}s."


def _video_url(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    vid = data.get("video")
    if isinstance(vid, dict) and vid.get("url"):
        return vid["url"]
    if isinstance(vid, str):
        return vid
    return data.get("video_url") or data.get("url")
