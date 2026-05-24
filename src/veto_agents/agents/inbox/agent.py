"""Inbox agent runner (stub).

Real implementation lands in v0.0.5. See agents/inbox/SPEC.md.
"""

from __future__ import annotations

from rich.console import Console


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    console.print("\n[bold cyan]Inbox agent[/bold cyan] · stub in v0.0.2")
    console.print(
        "  Will triage email + schedule meetings, using paid AI + transcription.\n"
        "  Spec: https://github.com/veto-protocol/veto-agents/blob/main/agents/inbox/SPEC.md\n"
    )
    if prompt:
        console.print(f"  You asked: [dim]{prompt}[/dim]")
    console.print("\n  [dim]Real implementation ships in v0.0.5.[/dim]\n")
