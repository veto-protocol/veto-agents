"""Build agent runner (stub).

The full Vercel / Modal / Fly / Cloudflare integration lands in v0.0.3+. The
stub prints the agent's role + a link to the spec, so installing it works
and the catalog isn't broken.
"""

from __future__ import annotations

from rich.console import Console


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    console.print("\n[bold cyan]Build agent[/bold cyan] · stub in v0.0.2")
    console.print(
        "  Will deploy your code to the cheapest provider (Vercel, Modal, Fly, Cloudflare, Runpod).\n"
        "  Spec: https://github.com/veto-protocol/veto-agents/blob/main/agents/build/SPEC.md\n"
    )
    if prompt:
        console.print(f"  You asked: [dim]{prompt}[/dim]")
    console.print(
        "\n  [dim]Real implementation ships in v0.0.3. The plan-then-execute flow is "
        "wired in the Media agent now — re-use the same shape for Build.[/dim]\n"
    )
