"""Creative studio orchestrator — brief → coherent ad package.

Flow:
  1. DIRECT   one LLM call (director.py) → a single creative concept + every
              asset's prompt/script derived from it. (FREE — LLM only.)
  2. COPY     write copy.md from the concept. (FREE.)
  3. IMAGE    OpenAI gpt-image-1 (BYO key) OR the free fal.ai x402 fallback.
  4. VIDEO    Higgsfield DoP (BYO key).      [optional]
  5. VOICE    ElevenLabs TTS (BYO key).      [optional]
  6. ASSEMBLE a per-run folder with copy.md + image(s)/video/voiceover +
              manifest.json, then print a rich summary (what was made, cost per
              asset, Veto verdict + receipt for each PAID asset).

Every PAID asset is gated by Veto BEFORE the provider call (inside each
provider). Graceful degradation: a missing key → that asset is skipped with a
note; the studio still delivers everything else. It NEVER requires Meta creds —
placing the ad is a later, separate stage.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import creds as creative_creds
from . import director as director_mod
from .director import Concept
from .providers import elevenlabs_voice, fal_image, higgsfield_video, openai_image
from .types import ToolResult

ALL_ASSETS = ("copy", "image", "video", "voice")


def run(
    brief: str,
    cfg,
    console: Console,
    *,
    want: tuple[str, ...] = ALL_ASSETS,
    image_provider: str = "openai",  # "openai" | "fal"
    out_root: Path | None = None,
) -> dict:
    """Produce a creative ad package from `brief`. Returns the manifest dict.

    `want` selects which assets to attempt; unavailable ones (no key) are
    skipped with a note. `image_provider` picks OpenAI (BYO) or the free fal
    fallback for the hero image; OpenAI silently falls back to fal if no key.
    """
    want = tuple(w for w in want if w in ALL_ASSETS) or ("copy",)
    console.print()
    console.print(Panel.fit(
        f"[bold]Veto Creative Studio[/bold]\n[dim]{_short(brief, 120)}[/dim]",
        border_style="cyan",
    ))

    signed_in = bool(getattr(cfg, "api_key", None) and getattr(cfg, "agent_id", None))
    if not signed_in:
        console.print(
            "  [yellow]·[/yellow] Not signed in to Veto — copy will still be produced, "
            "but PAID assets (image/video/voice) will be blocked until you run "
            "[cyan]veto-agents setup[/cyan].\n"
        )

    # A brand profile (if set) makes the whole package brand-true — the director
    # loads it internally, so this is purely a cosmetic heads-up. No signature
    # change: brand flows through director_mod.direct().
    from . import brand as brand_mod
    _bp = brand_mod.load(cfg)
    if _bp is not None:
        console.print(f"  [dim]brand: {_bp.name} · tone: {_bp.tone or '—'}[/dim]")

    # ── 1. DIRECT ─────────────────────────────────────────────────────────
    console.print("  [cyan]›[/cyan] Directing… (deriving one coherent concept)")
    try:
        concept = director_mod.direct(brief, cfg)
    except director_mod.DirectorError as e:
        console.print(f"  [red]✗[/red] Director failed: {e}")
        return {"brief": brief, "error": str(e), "assets": [], "concept": None}

    console.print(f"  [green]✓[/green] Concept: [italic]{_short(concept.concept, 100)}[/italic]")
    if concept.theme or concept.tone:
        console.print(f"    [dim]theme: {concept.theme}  ·  tone: {concept.tone}[/dim]")

    # ── output folder ─────────────────────────────────────────────────────
    out_dir = _make_run_dir(brief, out_root)
    console.print(f"  [dim]→ {out_dir}[/dim]\n")

    assets: list[dict] = []

    # ── 2. COPY (free) ────────────────────────────────────────────────────
    if "copy" in want:
        copy_path = out_dir / "copy.md"
        copy_path.write_text(_render_copy_md(brief, concept))
        assets.append(_record("copy", "llm", ToolResult(
            ok=True, actual_cost_usd=0.0, output_path=str(copy_path),
        )))
        console.print(
            f"  [green]✓[/green] copy  "
            f"[dim]{len(concept.headlines)} headlines · {len(concept.primary_texts)} texts · "
            f"{len(concept.ctas)} CTAs → copy.md[/dim]"
        )

    # ── 3. IMAGE ──────────────────────────────────────────────────────────
    if "image" in want and concept.image_prompt:
        assets.append(_guarded(console, "image", image_provider,
                               lambda: _do_image(concept, cfg, console, out_dir, image_provider)))

    # ── 4. VIDEO (optional) ───────────────────────────────────────────────
    if "video" in want and concept.video_prompt:
        assets.append(_guarded(console, "video", "higgsfield",
                               lambda: _do_video(concept, cfg, console, out_dir)))

    # ── 5. VOICE (optional) ───────────────────────────────────────────────
    if "voice" in want and concept.voiceover_script:
        assets.append(_guarded(console, "voice", "elevenlabs",
                               lambda: _do_voice(concept, cfg, console, out_dir)))

    # ── 6. ASSEMBLE ───────────────────────────────────────────────────────
    manifest = _build_manifest(brief, concept, out_dir, assets)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    _print_summary(console, manifest, out_dir)
    return manifest


# ─── per-asset drivers ────────────────────────────────────────────────────


def _guarded(console: Console, kind: str, provider: str, fn) -> dict:
    """Run a per-asset driver fail-soft.

    The provider drivers already turn a Veto block or an HTTP failure into a
    clean ToolResult. This is the last-line net for anything they DON'T catch
    (e.g. a 200 response with a non-JSON body → JSONDecodeError, which is not an
    httpx error): one asset blowing up must never abort the whole package or
    lose the copy/manifest already produced. No paid call happens here — the
    spend gate lives inside each driver, before its provider request.
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — fail-soft: skip this asset, keep going
        console.print(f"  [red]✗[/red] {kind}  [dim]unexpected error: {e}[/dim]")
        return _record(kind, provider, ToolResult(
            ok=False, actual_cost_usd=0.0, provider=provider,
            error=f"unexpected error: {e}",
        ))


def _do_image(concept: Concept, cfg, console: Console, out_dir: Path, provider: str) -> dict:
    provider = (provider or "openai").lower()
    # OpenAI needs a key; fall back to the free fal tool if absent.
    if provider == "openai" and not creative_creds.describe(cfg)["openai_image"]:
        console.print("  [yellow]·[/yellow] No OPENAI_API_KEY — falling back to free fal.ai image (x402).")
        provider = "fal"

    console.print(f"  [cyan]›[/cyan] image via [bold]{provider}[/bold]…")
    if provider == "fal":
        res = fal_image.generate(concept.image_prompt, cfg=cfg, output_dir=out_dir, concept=concept.concept)
    else:
        res = openai_image.generate(concept.image_prompt, cfg=cfg, output_dir=out_dir,
                                    concept=concept.concept, console=console)
    _echo_asset(console, "image", res)
    return _record("image", res.provider or provider, res)


def _do_video(concept: Concept, cfg, console: Console, out_dir: Path) -> dict:
    if not creative_creds.describe(cfg)["higgsfield_video"]:
        console.print("  [yellow]·[/yellow] video skipped — no Higgsfield key "
                      "(HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET).")
        return _record("video", "higgsfield", ToolResult(
            ok=False, actual_cost_usd=0.0, provider="higgsfield", skipped=True,
            error="No Higgsfield key.",
        ))
    console.print("  [cyan]›[/cyan] video via [bold]higgsfield[/bold]… (async, may take a minute)")
    res = higgsfield_video.generate(concept.video_prompt, cfg=cfg, output_dir=out_dir,
                                    concept=concept.concept, console=console)
    _echo_asset(console, "video", res)
    return _record("video", "higgsfield", res)


def _do_voice(concept: Concept, cfg, console: Console, out_dir: Path) -> dict:
    if not creative_creds.describe(cfg)["elevenlabs_voice"]:
        console.print("  [yellow]·[/yellow] voiceover skipped — no ELEVENLABS_API_KEY.")
        return _record("voice", "elevenlabs", ToolResult(
            ok=False, actual_cost_usd=0.0, provider="elevenlabs", skipped=True,
            error="No ElevenLabs key.",
        ))
    console.print("  [cyan]›[/cyan] voiceover via [bold]elevenlabs[/bold]…")
    res = elevenlabs_voice.generate(concept.voiceover_script, cfg=cfg, output_dir=out_dir,
                                    concept=concept.concept, console=console)
    _echo_asset(console, "voice", res)
    return _record("voice", "elevenlabs", res)


# ─── rendering / assembly helpers ─────────────────────────────────────────


def _echo_asset(console: Console, kind: str, res: ToolResult) -> None:
    if res.ok:
        console.print(f"  [green]✓[/green] {kind}  [dim]${res.actual_cost_usd:.3f} → "
                      f"{Path(res.output_path).name if res.output_path else '?'}[/dim]")
        if res.receipt_url:
            console.print(f"    [dim]receipt: {res.receipt_url}[/dim]")
    elif res.denied:
        console.print(f"  [red]✗[/red] {kind}  [yellow]Veto {res.verdict}[/yellow] — {res.error}")
        if res.receipt_url:
            console.print(f"    [dim]receipt: {res.receipt_url}[/dim]")
    elif res.skipped:
        console.print(f"  [yellow]·[/yellow] {kind} skipped — {res.error}")
    else:
        console.print(f"  [red]✗[/red] {kind}  [dim]{res.error}[/dim]")


def _record(kind: str, provider: str, res: ToolResult) -> dict:
    status = (
        "ok" if res.ok else
        "denied" if res.denied else
        "skipped" if res.skipped else
        "error"
    )
    return {
        "type": kind,
        "provider": provider,
        "status": status,
        "path": res.output_path,
        "url": res.output_url,
        "cost_usd": round(res.actual_cost_usd, 4),
        "verdict": res.verdict,
        "receipt_url": res.receipt_url,
        "error": res.error,
    }


def _build_manifest(brief: str, concept: Concept, out_dir: Path, assets: list[dict]) -> dict:
    total_cost = round(sum(a.get("cost_usd", 0.0) or 0.0 for a in assets), 4)
    made = [a for a in assets if a["status"] == "ok"]
    return {
        "brief": brief,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(out_dir),
        "concept": {
            "concept": concept.concept,
            "theme": concept.theme,
            "tone": concept.tone,
            "copy": {
                "headlines": concept.headlines,
                "primary_texts": concept.primary_texts,
                "ctas": concept.ctas,
            },
            "image_prompt": concept.image_prompt,
            "video_prompt": concept.video_prompt,
            "voiceover_script": concept.voiceover_script,
        },
        "assets": assets,
        "totals": {"assets_made": len(made), "assets_total": len(assets), "cost_usd": total_cost},
    }


def _print_summary(console: Console, manifest: dict, out_dir: Path) -> None:
    table = Table(title="Ad package", title_style="bold cyan", show_lines=False)
    table.add_column("Asset", style="bold")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Veto")
    table.add_column("Receipt", overflow="fold")

    status_style = {"ok": "green", "denied": "red", "skipped": "yellow", "error": "red"}
    for a in manifest["assets"]:
        st = a["status"]
        table.add_row(
            a["type"],
            a["provider"] or "-",
            f"[{status_style.get(st, 'white')}]{st}[/]",
            f"${a['cost_usd']:.3f}" if a["cost_usd"] else ("free" if st == "ok" else "-"),
            a["verdict"] or "-",
            a["receipt_url"] or "-",
        )

    console.print()
    console.print(table)
    t = manifest["totals"]
    console.print(
        f"  [bold]{t['assets_made']}/{t['assets_total']}[/bold] assets made · "
        f"total spend [bold]${t['cost_usd']:.3f}[/bold] · folder [cyan]{out_dir}[/cyan]\n"
    )


def _render_copy_md(brief: str, c: Concept) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "_(none)_"

    return (
        f"# Ad copy\n\n"
        f"**Brief:** {brief.strip()}\n\n"
        f"## Concept\n{c.concept}\n\n"
        f"- **Theme:** {c.theme or '—'}\n"
        f"- **Tone:** {c.tone or '—'}\n\n"
        f"## Headlines\n{bullets(c.headlines)}\n\n"
        f"## Primary texts\n{bullets(c.primary_texts)}\n\n"
        f"## CTAs\n{bullets(c.ctas)}\n\n"
        f"## Voiceover script\n{c.voiceover_script or '_(none)_'}\n\n"
        f"---\n\n"
        f"## Derived prompts\n\n"
        f"**Image prompt:** {c.image_prompt or '—'}\n\n"
        f"**Video prompt:** {c.video_prompt or '—'}\n"
    )


def _make_run_dir(brief: str, out_root: Path | None) -> Path:
    root = out_root or (Path.home() / "Downloads" / "veto-studio")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _slug(brief) or "package"
    d = root / f"{slug}-{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].strip("-")


def _short(text: str, n: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"
