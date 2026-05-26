"""Shared sign-in gate.

When an agent action needs the user to be signed in and they aren't, the
old behavior was to print "Run `veto-agents setup`" and exit. The new
behavior asks Y/n and, on Y, runs setup right there so the user can
continue without re-typing their original command afterward.

Implemented as a subprocess on the installed `veto-agents` binary
rather than an in-process call to the Typer command, to avoid a
circular import between cli.py and agents/*/agent.py.
"""

from __future__ import annotations

import shutil
import subprocess

from rich.console import Console
from rich.prompt import Confirm

from . import config as cfg_module


def require_signin(console: Console, cfg) -> object:
    """Returns a freshly-loaded cfg. Caller checks `cfg.api_key` to know
    whether the user is signed in. If they're already signed in, this is
    a no-op. If not, prompts Y/n and runs `veto-agents setup` if Y.
    """
    if cfg.api_key and cfg.agent_id:
        return cfg

    console.print()
    console.print("  [yellow]·[/yellow] You're not signed in yet.")
    console.print(
        "    [dim]Signing in takes ~30 seconds (email magic-link). "
        "It unlocks signed receipts, on-chain hard-stop, and your "
        "wallet view.[/dim]"
    )
    console.print()

    if not Confirm.ask("  Sign in now?", default=True):
        console.print("  [dim]OK — not signed in. Nothing spent.[/dim]\n")
        return cfg

    binary = shutil.which("veto-agents")
    if not binary:
        console.print(
            "  [red]✗[/red] Could not find the [cyan]veto-agents[/cyan] binary on "
            "PATH. Try opening a new terminal tab and re-running."
        )
        return cfg

    console.print()
    try:
        subprocess.run([binary, "setup"], check=False)
    except KeyboardInterrupt:
        console.print("\n  [dim]· Setup cancelled.[/dim]\n")
        return cfg

    return cfg_module.load()
