"""ElevenLabs text-to-speech — BYO ELEVENLABS_API_KEY, governed by Veto.

BYO-key (no 402), so we Veto-gate ourselves BEFORE the paid call.

API shape (verified July 2026):
  base   https://api.elevenlabs.io
  auth   xi-api-key: YOUR_KEY   (header, NOT bearer)
  POST   /v1/text-to-speech/{voice_id}?output_format=mp3_44100_128
  body   { text, model_id, voice_settings? }
  → SYNC. Response is raw binary audio bytes (Content-Type audio/mpeg) — stream
    straight to a file. No polling. 422 = validation error.

Voice/model defaults: George (JBFqnCBsd6RMkjVDRZzb) + eleven_multilingual_v2.
Optional asset: a missing key returns a skipped ToolResult and the studio
carries on.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ..creds import resolve as resolve_cred
from ..gate import gate
from ..types import ToolResult

MERCHANT = "api.elevenlabs.io"
BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # George (default library voice)
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

# Rough USD per 1,000 characters, by model (for the Veto gate estimate only).
_MODEL_PER_1K = {
    "eleven_multilingual_v2": 0.15,
    "eleven_turbo_v2_5": 0.09,
    "eleven_flash_v2_5": 0.08,
    "eleven_v3": 0.18,
}


def estimate_cost(text: str, model_id: str = DEFAULT_MODEL) -> float:
    chars = max(1, len(text or ""))
    rate = _MODEL_PER_1K.get(model_id, 0.15)
    return round((chars / 1000.0) * rate, 4)


def generate(
    script: str,
    *,
    cfg=None,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    voice_settings: dict | None = None,
    output_dir: Path | None = None,
    concept: str | None = None,
    console=None,
) -> ToolResult:
    """Synthesize `script` to an MP3 with ElevenLabs, Veto-gated. Returns ToolResult."""
    if not (script or "").strip():
        return ToolResult(ok=False, actual_cost_usd=0.0, provider="elevenlabs", skipped=True,
                          error="Empty voiceover script — nothing to synthesize.")

    key = resolve_cred("ELEVENLABS_API_KEY", cfg)
    if not key:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, provider="elevenlabs", skipped=True,
            error="No ELEVENLABS_API_KEY — set it to enable voiceover (optional).",
        )

    est = estimate_cost(script, model_id)

    # ── Veto gate BEFORE the paid call ────────────────────────────────────
    g = gate(
        cfg,
        merchant=MERCHANT,
        amount=est,
        description=f"ElevenLabs TTS ({model_id}, {len(script)} chars)",
        context={
            "tool": "creative.elevenlabs_voice",
            "model": model_id,
            "voice_id": voice_id,
            "chars": len(script),
            "concept": (concept or "")[:500],
        },
        console=console,
    )
    if not g.allowed:
        return ToolResult(
            ok=False, actual_cost_usd=0.0, denied=True, verdict=g.verdict,
            receipt_url=g.receipt_url, provider="elevenlabs",
            error=f"Veto {g.verdict}: {g.block_reason()}",
        )

    # ── Allowed → synthesize ──────────────────────────────────────────────
    url = f"{BASE}/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    body: dict = {"text": script, "model_id": model_id}
    if voice_settings:
        body["voice_settings"] = voice_settings
    try:
        with httpx.Client(timeout=120.0) as c:
            r = c.post(url, headers=headers, params={"output_format": output_format}, json=body)
            r.raise_for_status()
            audio = r.content
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text[:300]
        except Exception:
            pass
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="elevenlabs",
                          error=f"ElevenLabs HTTP {e.response.status_code}: {detail}")
    except httpx.HTTPError as e:
        return ToolResult(ok=False, actual_cost_usd=0.0, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="elevenlabs",
                          error=f"ElevenLabs request failed: {e}")

    if not audio:
        return ToolResult(ok=False, actual_cost_usd=est, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="elevenlabs",
                          error="ElevenLabs returned no audio bytes.")

    output_dir = output_dir or (Path.home() / "Downloads")
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = ".mp3" if output_format.startswith("mp3") else (
        ".wav" if output_format.startswith("pcm") else ".bin"
    )
    out_path = output_dir / f"elevenlabs-voice-{abs(hash(script[:64])) % 10**8}{ext}"
    try:
        out_path.write_bytes(audio)
    except OSError as e:
        return ToolResult(ok=False, actual_cost_usd=est, verdict=g.verdict,
                          receipt_url=g.receipt_url, provider="elevenlabs",
                          error=f"Couldn't save audio: {e}")

    return ToolResult(
        ok=True, actual_cost_usd=est, output_path=str(out_path),
        verdict=g.verdict, receipt_url=g.receipt_url, provider="elevenlabs",
    )
