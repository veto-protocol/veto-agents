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

from . import __version__, config as cfg_module, registry as registry_module
from . import auth
from .funding import get_funding_target, render_funding_qr
from .register import is_valid_evm_address


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
    """First-time setup. Pick an LLM brain, provision a wallet, register with Veto."""
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

    # 2. Sign in via magic-link email (the real Veto auth flow)
    if not cfg.api_key:
        console.print("\n[bold]Step 2.[/bold] Sign in with email (magic link).")
        while True:
            email = Prompt.ask("Your email").strip().lower()
            if auth.is_valid_email(email):
                break
            console.print("  [red]✗[/red] That doesn't look like a valid email. Try again.")

        device_code = auth.generate_device_code()
        try:
            auth.start(api_base=cfg.veto_api_base, email=email, device_code=device_code)
        except Exception as e:
            console.print(f"  [red]✗[/red] Could not start sign-in: {e}")
            console.print("  [dim]Retry with `veto-agents setup` once the connection's back.[/dim]")
            return

        console.print(
            f"  [green]✓[/green] Magic link sent to [cyan]{email}[/cyan].\n"
            "  Opening your inbox in a browser now — click the link to finish signing in.\n"
            "  [dim](Waiting up to 15 minutes. Press Ctrl-C to abort.)[/dim]\n"
        )
        inbox_url = auth.open_inbox_for(email)
        if inbox_url:
            console.print(f"  [dim]Opened: {inbox_url}[/dim]\n")

        try:
            with console.status("[dim]waiting for the click…[/dim]", spinner="dots"):
                ready = auth.poll_until_ready(
                    api_base=cfg.veto_api_base,
                    device_code=device_code,
                )
        except KeyboardInterrupt:
            console.print("\n[yellow]·[/yellow] Aborted. Re-run `veto-agents setup` when ready.")
            return
        except TimeoutError as e:
            console.print(f"\n[red]✗[/red] {e}")
            return
        except Exception as e:
            console.print(f"\n[red]✗[/red] Auth poll failed: {e}")
            return

        cfg.api_key = ready.api_key
        cfg.agent_id = ready.agent_id
        cfg.client_id = ready.client_id
        console.print(
            f"  [green]✓[/green] Signed in as [cyan]{email}[/cyan] · "
            f"agent_id [dim]{ready.agent_id}[/dim]"
        )
    else:
        console.print(f"\n[bold]Step 2.[/bold] Already signed in (agent_id [dim]{cfg.agent_id}[/dim]).")

    # 3. Funding wallet address (the one you'll send USDC FROM)
    console.print("\n[bold]Step 3.[/bold] The wallet you'll fund your agent from.")
    if cfg.wallet_address:
        console.print(f"  Existing: [green]{cfg.wallet_address}[/green]")
    else:
        console.print(
            "  Paste an EVM address (Phantom/Metamask/Coinbase Wallet/Rabby — anything you control). "
            "  This is just so we know which deposit on Base Sepolia is yours."
        )
        while True:
            addr = Prompt.ask("Your wallet address (0x…)", default="").strip()
            if not addr:
                console.print("  [yellow]·[/yellow] Skipped. You can paste it later via `veto-agents wallet set <addr>`.")
                break
            if is_valid_evm_address(addr):
                cfg.wallet_address = addr
                console.print(f"  [green]✓[/green] {addr}")
                break
            console.print("  [red]✗[/red] Not a valid EVM address. Try again.")

    # 4. Policy posture
    console.print("\n[bold]Step 4.[/bold] Default policy posture for new agents.")
    cfg.policy_posture = Prompt.ask(
        "Posture",
        choices=["strict", "balanced", "permissive"],
        default=cfg.policy_posture,
    )

    cfg_module.save(cfg)

    # 5. Fund your agent — show QR code for the treasury contract
    if cfg.api_key:
        target = get_funding_target(cfg.wallet_address)
        console.print("\n[bold]Step 5.[/bold] Fund your agent.")
        console.print(
            f"  Send [bold cyan]USDC on {target.chain}[/bold cyan] to the address below. "
            "Scan with your phone wallet, or copy the address."
        )
        console.print()
        console.print(render_funding_qr(target))
        console.print(f"  Address: [cyan]{target.address}[/cyan]")
        console.print(f"  Chain:   {target.chain} (chain id {target.chain_id})")
        console.print(f"  Token:   USDC ({target.token_contract})")
        console.print(
            "\n  [dim]Mainnet + multi-chain bridges land in v0.4. For now, anything "
            "you send is testnet USDC.[/dim]"
        )

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

    for a in registry_module.REGISTRY:
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
    entry = registry_module.get(name)
    if entry is None:
        console.print(f"[red]✗[/red] Unknown agent: {name}")
        console.print(f"  Available: {', '.join(registry_module.all_names())}")
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


@wallet_app.callback(invoke_without_command=True)
def wallet_default(ctx: typer.Context) -> None:
    """Show the full wallet dashboard: balance + per-agent stats + recent activity."""
    if ctx.invoked_subcommand is not None:
        return
    _render_wallet_dashboard()


@wallet_app.command("balance")
def wallet_balance() -> None:
    """Just the USDC balance line."""
    cfg = cfg_module.load()
    target = get_funding_target(cfg.wallet_address or "")
    try:
        from .wallet_view import fmt_usdc, get_usdc_balance
        raw = get_usdc_balance(target.address)
        bal = fmt_usdc(raw)
        console.print(f"[bold]${bal:,.2f}[/bold] USDC · treasury [cyan]{target.address}[/cyan] · {target.chain}")
    except Exception as e:
        console.print(f"[red]✗[/red] Couldn't read balance: {e}")


@wallet_app.command("receive")
def wallet_receive() -> None:
    """Re-display the funding QR + address."""
    cfg = cfg_module.load()
    target = get_funding_target(cfg.wallet_address or "")
    console.print(f"\nSend USDC on [bold cyan]{target.chain}[/bold cyan] to:\n")
    console.print(render_funding_qr(target))
    console.print(f"  Address: [cyan]{target.address}[/cyan]")
    console.print(f"  Chain:   {target.chain} (chain id {target.chain_id})")
    console.print(f"  Token:   USDC ({target.token_contract})\n")


@wallet_app.command("topup")
def wallet_topup() -> None:
    """Top up the treasury via Coinbase onramp."""
    console.print("[dim](onramp link generation lands in v0.0.4)[/dim]")


def _render_wallet_dashboard() -> None:
    """The headline `veto-agents wallet` view — balance + per-agent + recent."""
    cfg = cfg_module.load()
    if not cfg.api_key:
        console.print(
            "[yellow]·[/yellow] Not signed in. Run [cyan]veto-agents setup[/cyan] first."
        )
        return

    from .wallet_view import (
        aggregate_receipts,
        fetch_receipts_summary,
        fmt_usdc,
        get_usdc_balance,
    )

    target = get_funding_target(cfg.wallet_address or "")

    # ── Header ────────────────────────────────────────────────
    console.print()
    console.print(
        f"[bold]Treasury[/bold] · [cyan]{target.address}[/cyan] · {target.chain}"
    )
    console.print("━" * 60)

    # ── Balance ───────────────────────────────────────────────
    try:
        raw = get_usdc_balance(target.address)
        bal_usd = fmt_usdc(raw)
        console.print(f"USDC balance:              [bold]${bal_usd:,.2f}[/bold]")
    except Exception as e:
        console.print(f"USDC balance:              [red]error[/red] [dim]({e})[/dim]")

    # ── Receipts feed (server-side data) ─────────────────────
    try:
        feed = fetch_receipts_summary(
            api_base=cfg.veto_api_base,
            api_key=cfg.api_key,
            client_id=cfg.client_id,
        )
        rows = feed.get("results") or feed.get("receipts") or feed if isinstance(feed, list) else []
        if isinstance(feed, dict) and "results" in feed:
            rows = feed["results"]
        elif isinstance(feed, list):
            rows = feed
    except Exception as e:
        console.print(f"\n[yellow]·[/yellow] Couldn't fetch receipts: {e}")
        console.print(
            "[dim]The wallet dashboard's per-agent view depends on /api/v1/receipts/. "
            "If that endpoint isn't available, this is expected.[/dim]\n"
        )
        return

    import time as _time
    lifetime, pending, per_agent, recent = aggregate_receipts(rows, now_epoch=_time.time())

    console.print(f"Total spent (lifetime):    [bold]${lifetime:,.2f}[/bold]")
    if pending > 0:
        console.print(f"Pending (escalated):       [yellow]${pending:,.2f}[/yellow]")
    else:
        console.print("Pending (escalated):       $0.00")

    # ── Per-agent ─────────────────────────────────────────────
    if per_agent:
        console.print("\n[bold]Per-agent spend[/bold]")
        console.print("━" * 60)
        agent_table = Table(show_header=False, box=None, pad_edge=False)
        agent_table.add_column("agent", style="cyan", no_wrap=True)
        agent_table.add_column("spent", justify="right", style="bold")
        agent_table.add_column("actions", justify="right", style="dim")
        agent_table.add_column("denied", justify="right", style="red")
        agent_table.add_column("escalated", justify="right", style="yellow")
        for stats in sorted(per_agent.values(), key=lambda s: s.spent_usd, reverse=True):
            agent_table.add_row(
                stats.name,
                f"${stats.spent_usd:,.2f}",
                f"{stats.actions} actions",
                f"{stats.denied} denied" if stats.denied else "—",
                f"{stats.escalated} escalated" if stats.escalated else "—",
            )
        console.print(agent_table)

    # ── Recent activity ──────────────────────────────────────
    if recent:
        console.print("\n[bold]Recent activity[/bold]")
        console.print("━" * 60)
        for r in recent[:10]:
            verdict_color = {
                "allow": "green",
                "deny": "red",
                "escalate": "yellow",
            }.get(r.verdict, "dim")
            verdict_mark = {
                "allow": "✓",
                "deny": "✗",
                "escalate": "?",
            }.get(r.verdict, "·")
            url_hint = f"  [dim]{r.receipt_url}[/dim]" if r.receipt_url else ""
            amount_str = (
                f"${r.amount_usd:,.2f}" if r.amount_usd > 0 else "[dim]$0.00[/dim]"
            )
            console.print(
                f"  [dim]{r.when:<10}[/dim]  [{verdict_color}]{verdict_mark}[/{verdict_color}] "
                f"[cyan]{r.agent:<10}[/cyan] {r.label[:36]:<36}  {amount_str}{url_hint}"
            )
    console.print()


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
    entry = registry_module.get(name)
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
