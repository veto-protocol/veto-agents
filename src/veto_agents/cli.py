"""veto-agents — the main CLI.

Entrypoint registered in pyproject.toml as `veto-agents = "veto_agents.cli:app"`.

The shape mirrors the docs at CLI.md:

  veto-agents setup                 first-time wallet + provider setup
  veto-agents list                  show the catalog
  veto-agents install <name>        add an agent (drops its policy.yaml locally)
  veto-agents uninstall <name>
  veto-agents <name> "<prompt>"     run an installed agent against a task
  veto-agents policy edit <name>    open the agent's policy in $EDITOR
  veto-agents wallet ...            balance, topup, receive
  veto-agents receipts              browse local receipt cache

v0.0.1 ships the structure + stubs. Each subcommand returns enough output to
prove the wiring is correct; real implementations land per-agent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import typer
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__, config as cfg_module, registry


app = typer.Typer(
    name="veto-agents",
    help="AI agents that pay for things on your behalf, with the safety built in.",
    no_args_is_help=True,
    add_completion=False,
)

# Sub-app for `veto-agents policy ...`
policy_app = typer.Typer(help="Edit per-agent policy files.")
app.add_typer(policy_app, name="policy")

# Sub-app for `veto-agents wallet ...`
wallet_app = typer.Typer(help="Manage the embedded wallet.")
app.add_typer(wallet_app, name="wallet")


console = Console()


# ─── version + meta ──────────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"veto-agents {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        # No subcommand → show help.
        console.print(ctx.get_help())


# ─── setup ────────────────────────────────────────────────────────────────


@app.command()
def setup() -> None:
    """First-time setup. Pick an LLM brain + provision a wallet."""
    console.print("\n[bold cyan]Veto Agents — first-run setup[/bold cyan]\n")
    console.print(
        "Veto governs every action your agents take. You stay in control. "
        "This setup is a one-time thing.\n"
    )

    cfg = cfg_module.load()

    # 1. LLM provider
    console.print("[bold]Step 1.[/bold] Pick an LLM brain.")
    console.print("  • [cyan]hermes[/cyan]  — open weights, hosted by Nous (default)")
    console.print("  • [cyan]claude[/cyan]  — Anthropic, bring your own API key")
    console.print("  • [cyan]gpt[/cyan]     — OpenAI, bring your own API key")
    console.print("  • [cyan]custom[/cyan]  — any OpenAI-compatible endpoint\n")
    cfg.llm_provider = Prompt.ask(
        "LLM provider",
        choices=["hermes", "claude", "gpt", "custom"],
        default=cfg.llm_provider,
    )

    # 2. Wallet
    console.print("\n[bold]Step 2.[/bold] Wallet.")
    if cfg.wallet_address:
        console.print(f"  Existing wallet: [green]{cfg.wallet_address}[/green]")
    else:
        if Confirm.ask("Provision an embedded wallet via Privy now?", default=True):
            console.print("  [dim](provisioning flow lands in v0.0.2 — for now, paste an address you control)[/dim]")
            addr = Prompt.ask("Wallet address (0x…) or skip", default="")
            cfg.wallet_address = addr.strip() or None

    # 3. Policy posture
    console.print("\n[bold]Step 3.[/bold] Default policy posture for new agents.")
    cfg.policy_posture = Prompt.ask(
        "Posture",
        choices=["strict", "balanced", "permissive"],
        default=cfg.policy_posture,
    )

    cfg_module.save(cfg)
    console.print(
        f"\n[green]✓[/green] Setup complete. State at [dim]{cfg_module.state_dir()}[/dim]\n"
        f"Next: [cyan]veto-agents list[/cyan] to browse the agent catalog.\n"
    )


# ─── list ─────────────────────────────────────────────────────────────────


@app.command(name="list")
def list_cmd() -> None:
    """Show the catalog of installable agents."""
    cfg = cfg_module.load()
    installed = set(cfg.installed_agents)

    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("Agent", style="bold", no_wrap=True)
    table.add_column("What it does")
    table.add_column("Spends on")
    table.add_column("", style="green", no_wrap=True)

    for a in registry.REGISTRY:
        installed_marker = "✓ installed" if a.name in installed else ""
        table.add_row(a.name, a.one_line, a.spends_on, installed_marker)

    console.print()
    console.print(table)
    console.print()
    console.print(
        "Install one with [cyan]veto-agents install <name>[/cyan]. "
        "Specs: https://github.com/veto-protocol/veto-agents/tree/main/agents\n"
    )


# ─── install / uninstall ─────────────────────────────────────────────────


@app.command()
def install(name: str = typer.Argument(..., help="Agent name (e.g. 'media').")) -> None:
    """Install an agent. Copies its default policy to ~/.veto-agents/policies/."""
    entry = registry.get(name)
    if entry is None:
        console.print(f"[red]✗[/red] Unknown agent: {name}")
        console.print(f"  Available: {', '.join(registry.all_names())}")
        raise typer.Exit(1)

    cfg = cfg_module.load()
    if name in cfg.installed_agents:
        console.print(f"[yellow]·[/yellow] {name} is already installed.")
        return

    # Copy default policy from the package into the user's policies dir.
    pkg_policy = _bundled_policy_path(name)
    user_policy = cfg_module.policies_dir() / f"{name}.yaml"
    if pkg_policy.exists():
        user_policy.write_text(pkg_policy.read_text())
        console.print(f"[green]✓[/green] Policy installed → {user_policy}")
    else:
        console.print(f"[yellow]·[/yellow] No bundled policy for {name} yet (placeholder).")

    cfg.installed_agents.append(name)
    cfg_module.save(cfg)

    console.print(
        f"[green]✓[/green] Installed [bold]{name}[/bold]. "
        f"Try: [cyan]veto-agents {name} \"<your prompt>\"[/cyan]\n"
    )


@app.command()
def uninstall(name: str = typer.Argument(...)) -> None:
    """Uninstall an agent."""
    cfg = cfg_module.load()
    if name not in cfg.installed_agents:
        console.print(f"[yellow]·[/yellow] {name} is not installed.")
        return
    cfg.installed_agents.remove(name)
    cfg_module.save(cfg)
    console.print(f"[green]✓[/green] Uninstalled [bold]{name}[/bold].")


# ─── run an agent ────────────────────────────────────────────────────────


@app.command()
def media(
    prompt: str = typer.Argument(..., help="What you want the agent to make."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the plan-confirm prompt."),
) -> None:
    """Run the media agent against a brief."""
    _run_agent("media", prompt, yes=yes)


@app.command()
def build(prompt: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Run the build agent against a task."""
    _run_agent("build", prompt, yes=yes)


@app.command()
def research(prompt: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Run the research agent against a question."""
    _run_agent("research", prompt, yes=yes)


@app.command()
def inbox(prompt: str = typer.Argument(""), yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Run the inbox agent (interactive if no prompt)."""
    _run_agent("inbox", prompt, yes=yes)


# ─── policy ──────────────────────────────────────────────────────────────


@policy_app.command("edit")
def policy_edit(name: str = typer.Argument(...)) -> None:
    """Open the agent's policy YAML in $EDITOR."""
    p = cfg_module.policies_dir() / f"{name}.yaml"
    if not p.exists():
        console.print(f"[red]✗[/red] No policy for '{name}'. Install the agent first.")
        raise typer.Exit(1)
    editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or "vi"
    subprocess.call([editor, str(p)])
    console.print(f"[green]✓[/green] Saved {p}")


@policy_app.command("show")
def policy_show(name: str = typer.Argument(...)) -> None:
    """Print the agent's current policy."""
    p = cfg_module.policies_dir() / f"{name}.yaml"
    if not p.exists():
        console.print(f"[red]✗[/red] No policy for '{name}'.")
        raise typer.Exit(1)
    console.print(p.read_text())


# ─── wallet ──────────────────────────────────────────────────────────────


@wallet_app.command("balance")
def wallet_balance() -> None:
    """Show wallet USDC balance on Base."""
    cfg = cfg_module.load()
    if not cfg.wallet_address:
        console.print("[yellow]·[/yellow] No wallet configured. Run [cyan]veto-agents setup[/cyan].")
        return
    console.print(f"Wallet: [green]{cfg.wallet_address}[/green]")
    console.print("[dim](on-chain balance lookup lands in v0.0.2)[/dim]")


@wallet_app.command("topup")
def wallet_topup() -> None:
    """Top up the embedded wallet via Coinbase onramp."""
    console.print("[dim](onramp link generation lands in v0.0.2)[/dim]")


# ─── receipts ────────────────────────────────────────────────────────────


@app.command()
def receipts() -> None:
    """List recent Veto-signed receipts for this user's agents."""
    console.print(
        "[dim](receipts feed lands in v0.0.2 — for now, see veto-ai.com/receipts)[/dim]"
    )


# ─── helpers ─────────────────────────────────────────────────────────────


def _bundled_policy_path(name: str):
    """Path to the bundled default policy.yaml shipped with this package."""
    from importlib.resources import files
    try:
        return files(f"veto_agents.agents.{name}").joinpath("policy.yaml")
    except (ModuleNotFoundError, FileNotFoundError):
        return None  # type: ignore[return-value]


def _run_agent(name: str, prompt: str, *, yes: bool) -> None:
    """Run an installed agent against a prompt. Enforces plan-then-execute."""
    cfg = cfg_module.load()
    if name not in cfg.installed_agents:
        console.print(
            f"[red]✗[/red] [bold]{name}[/bold] is not installed. "
            f"Run [cyan]veto-agents install {name}[/cyan] first."
        )
        raise typer.Exit(1)

    # Dynamic import — only loads the agent code (and its deps) when actually used.
    entry = registry.get(name)
    assert entry is not None
    try:
        mod = __import__(entry.package, fromlist=["run"])
    except ImportError as e:
        console.print(f"[red]✗[/red] Failed to load agent module: {e}")
        raise typer.Exit(1)

    run = getattr(mod, "run", None)
    if run is None:
        console.print(f"[red]✗[/red] Agent '{name}' has no run() entrypoint yet.")
        raise typer.Exit(1)

    # Hand off. The agent itself is responsible for plan-then-execute behavior
    # (Veto policy + system prompt enforce it). The CLI just renders the loop.
    run(prompt=prompt, cfg=cfg, console=console, auto_confirm=yes)


if __name__ == "__main__":
    app()
