"""Research agent runner (stub).

Real implementation lands in v0.0.4. See agents/research/SPEC.md.
"""

from __future__ import annotations

from rich.console import Console


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    console.print("\n[bold cyan]Research agent[/bold cyan] · stub in v0.0.2")
    console.print(
        "  Will run deep research using Exa, Tavily, and x402-gated content.\n"
        "  Spec: https://github.com/veto-protocol/veto-agents/blob/main/agents/research/SPEC.md\n"
    )
    if prompt:
        console.print(f"  You asked: [dim]{prompt}[/dim]")
    console.print("\n  [dim]Real implementation ships in v0.0.4.[/dim]\n")
