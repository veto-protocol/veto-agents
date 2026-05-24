"""Media agent runner.

v0.0.1 ships the **plan-then-execute** loop end-to-end:

  1. Take the user's brief.
  2. Build a plan with line-item cost estimates (currently from a heuristic
     mapping; v0.0.2 will route through the actual LLM-orchestrated planner).
  3. Show the plan + alternatives to the user.
  4. Wait for explicit consent.
  5. For each step, call Veto.authorize() before any paid action.
  6. Execute the step (v0.0.1 stubs the actual API calls — wiring lands in v0.0.2).
  7. Show actuals + receipt URL.

This module is deliberately thin: it demonstrates the *flow* every Veto Agent
follows. The Hermes integration + real tool calls + actual cost reconciliation
all slot in here without changing the public contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from ...veto_client import AuthorizeResult, VetoClient
from .tools import replicate_image


@dataclass
class Step:
    label: str
    merchant: str
    est_usd: float
    tool_name: str


def _classify_brief(prompt: str) -> list[Step]:
    """v0.0.1 heuristic: pick steps based on keywords in the brief.

    v0.0.2 will replace this with a real LLM-driven planner that produces
    these step entries from any prompt.
    """
    p = prompt.lower()
    steps: list[Step] = []

    if any(w in p for w in ("video", "clip", "motion", "animation")):
        steps.append(
            Step(
                label="Generate video — Runway Gen-3",
                merchant="api.runwayml.com",
                est_usd=0.42,
                tool_name="runway.video_gen",
            )
        )
    elif any(w in p for w in ("image", "picture", "photo", "logo", "icon", "shot")):
        steps.append(
            Step(
                label="Generate image — Replicate (Flux Schnell)",
                merchant="replicate.com",
                est_usd=replicate_image.estimate_cost("flux-schnell"),
                tool_name="replicate.image_gen",
            )
        )

    if any(w in p for w in ("voice", "voiceover", "narration", "speech", "say")):
        steps.append(
            Step(
                label="Synthesize voiceover — ElevenLabs",
                merchant="api.elevenlabs.io",
                est_usd=0.05,
                tool_name="elevenlabs.voice",
            )
        )

    # Fall through: if we couldn't detect what they want, default to an image.
    if not steps:
        steps.append(
            Step(
                label="Generate image — Replicate (Flux Schnell) [inferred]",
                merchant="replicate.com",
                est_usd=replicate_image.estimate_cost("flux-schnell"),
                tool_name="replicate.image_gen",
            )
        )

    return steps


def _render_plan(steps: list[Step], console: Console) -> float:
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("Step")
    table.add_column("Merchant", style="dim")
    table.add_column("Est. cost", style="bold yellow", justify="right")
    total = 0.0
    for i, s in enumerate(steps, 1):
        table.add_row(str(i), s.label, s.merchant, f"${s.est_usd:.2f}")
        total += s.est_usd
    table.add_row("", "", "[bold]Estimate[/bold]", f"[bold]${total:.2f}[/bold]")
    console.print()
    console.print(table)
    console.print()
    return total


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    """Entrypoint called by `veto-agents media "<prompt>"`."""
    console.print(f"\n[bold]Brief:[/bold] {prompt}\n")

    # 1. Plan
    steps = _classify_brief(prompt)

    # 2. Render plan + estimate
    total = _render_plan(steps, console)

    # 3. Consent gate (principle #1: plan-then-execute)
    if not auto_confirm:
        choice = Prompt.ask("Proceed?", choices=["y", "n"], default="y")
        if choice != "y":
            console.print("[yellow]·[/yellow] Cancelled. Nothing spent.\n")
            return

    # 4. Authorize + execute each step
    if not cfg.api_key or not cfg.agent_id:
        console.print(
            "[red]✗[/red] Not registered with Veto. Run [cyan]veto-agents setup[/cyan] first."
        )
        return

    client = VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    agent_id = cfg.agent_id

    actual_total = 0.0
    try:
        for i, s in enumerate(steps, 1):
            console.print(f"[bold cyan]Step {i}/{len(steps)}[/bold cyan] · {s.label}")
            try:
                result: AuthorizeResult = client.authorize(
                    agent_id=agent_id,
                    action_type="api_call",
                    merchant=s.merchant,
                    amount=s.est_usd,
                    currency="USD",
                    description=f"{s.tool_name}: {prompt[:120]}",
                    context={
                        "agent_type": "media",
                        "tool_name": s.tool_name,
                        "step": i,
                        "of": len(steps),
                    },
                )
            except Exception as e:
                console.print(f"  [red]✗ Veto authorize failed:[/red] {e}")
                console.print("  [dim]Stopping. No paid action taken for this step.[/dim]\n")
                return

            if result.verdict == "allow":
                console.print(
                    f"  [green]✓ allowed[/green] by Veto"
                    + (f" · receipt {result.receipt_url}" if result.receipt_url else "")
                )

                # Execute the real tool call. v0.0.3: only replicate.image_gen
                # is real; Runway video + ElevenLabs voice still stubs until
                # we wire each. The agent transparently reports which.
                if s.tool_name == "replicate.image_gen":
                    tool_result = replicate_image.generate(prompt=prompt)
                    actual = tool_result.actual_cost_usd
                    actual_total += actual
                    if tool_result.ok:
                        console.print(
                            f"  [green]✓ done[/green] · actual ${actual:.4f} "
                            f"· file [cyan]{tool_result.output_path}[/cyan]"
                        )
                    else:
                        console.print(
                            f"  [red]✗ tool failed[/red] (Veto already authorized + recorded)"
                        )
                        console.print(f"  [dim]{tool_result.error}[/dim]")
                        return
                else:
                    # Runway video / ElevenLabs voice still stubbed in v0.0.3
                    actual = s.est_usd
                    actual_total += actual
                    console.print(
                        f"  [yellow]·[/yellow] tool call stubbed for [dim]{s.tool_name}[/dim] "
                        f"(coming v0.0.4) · est-cost ${actual:.2f}"
                    )
            elif result.verdict == "deny":
                console.print(
                    f"  [red]✗ denied[/red] · reason: {', '.join(result.reason_codes) or 'policy'}"
                )
                console.print("  Stopping. No further steps will execute.\n")
                return
            else:  # escalate
                console.print(
                    f"  [yellow]· escalated[/yellow] — needs your approval. Receipt: {result.receipt_url}"
                )
                console.print("  Approve in the dashboard, then re-run.\n")
                return
    finally:
        client.close()

    console.print(
        f"\n[green]✓ Done.[/green] Total spent: [bold]${actual_total:.2f}[/bold] "
        f"(estimate was ${total:.2f}).\n"
    )
