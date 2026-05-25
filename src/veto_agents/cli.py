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

from . import __version__, config as cfg_module, credentials as creds_module, registry as registry_module
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
        cfg = cfg_module.load()
        # First run? Auto-launch setup so the user has a path forward.
        if not cfg.api_key:
            console.print(
                "\n[bold cyan]Welcome to Veto Agents.[/bold cyan]  "
                "[dim]AI agents that pay for things, with the safety built in.[/dim]\n"
            )
            console.print("Looks like this is your first run. Let's get you set up.\n")
            ctx.invoke(setup)
            console.print(
                "\n[bold]Now install your first agent:[/bold]\n"
                "  [cyan]veto-agents install media[/cyan]       generates images + video + voice\n"
                "  [cyan]veto-agents install build[/cyan]       deploys your code on cheap infra\n"
                "  [cyan]veto-agents install research[/cyan]    deep research with paid sources\n"
                "  [cyan]veto-agents install inbox[/cyan]       email triage + scheduling\n"
            )
            return
        # Signed in: show a brief status + help.
        masked = (cfg.api_key[:12] + "…" + cfg.api_key[-4:]) if cfg.api_key else "—"
        installed = ", ".join(cfg.installed_agents) if cfg.installed_agents else "[dim]none yet[/dim]"
        console.print(
            f"\n[bold]veto-agents[/bold] {__version__} · signed in · "
            f"agents installed: {installed}\n"
            f"[dim]api_key={masked}  state={cfg_module.state_dir()}[/dim]\n"
        )
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

    # 2. Sign in. First check if the main `veto` CLI already signed this
    # user in — if so we just reuse those credentials, no second sign-in.
    if not cfg.api_key:
        main_state = cfg_module.read_main_cli_state()
        if main_state and main_state.get("api_key"):
            existing_email = main_state.get("email", "(unknown)")
            console.print(
                f"\n[bold]Step 2.[/bold] Found existing Veto credentials from the main "
                f"[cyan]veto[/cyan] CLI ([cyan]{existing_email}[/cyan])."
            )
            if Confirm.ask("  Reuse them? (skips the magic-link step)", default=True):
                cfg = cfg_module.import_from_main_cli(cfg, main_state)
                console.print(
                    f"  [green]✓[/green] Reusing credentials · "
                    f"agent_id [dim]{cfg.agent_id}[/dim]"
                )
                cfg_module.save(cfg)

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
def install(
    name: str = typer.Argument(..., help="Agent name (e.g. 'media')."),
    skip_creds: bool = typer.Option(
        False, "--skip-creds", help="Skip the credential walkthrough (configure later)."
    ),
) -> None:
    """Install an agent: copies policy, walks you through its tool credentials.

    Reuses your existing wallet, email, and policy posture — no re-prompting
    for stuff you already did during `setup`.
    """
    entry = registry_module.get(name)
    if entry is None:
        console.print(f"[red]✗[/red] Unknown agent: {name}")
        console.print(f"  Available: {', '.join(registry_module.all_names())}")
        raise typer.Exit(1)

    cfg = cfg_module.load()

    # ── Guardrail: must be signed in first ────────────────────
    if not cfg.api_key:
        console.print(
            "[yellow]·[/yellow] You haven't signed in yet. Running setup first.\n"
        )
        setup()
        cfg = cfg_module.load()
        if not cfg.api_key:
            # Setup didn't complete (network error, ctrl-c, etc.). Don't proceed.
            console.print(
                "[red]✗[/red] Couldn't complete setup. Re-run [cyan]veto-agents setup[/cyan] when ready."
            )
            raise typer.Exit(1)
        console.print()

    already_installed = name in cfg.installed_agents
    if already_installed:
        console.print(
            f"[yellow]·[/yellow] [bold]{name}[/bold] is already installed. "
            f"Re-running credential walkthrough.\n"
        )

    # ── Welcome + summary of what's about to happen ───────────
    console.print(f"\n[bold cyan]Installing {name}[/bold cyan] — {entry.one_line}")
    console.print(f"  [dim]Will spend on: {entry.spends_on}[/dim]")
    console.print(f"  [dim]Spec: {entry.spec_url}[/dim]")
    if cfg.wallet_address:
        console.print(
            f"  [dim]Reusing your wallet:[/dim] [cyan]{cfg.wallet_address}[/cyan]"
        )

    # ── Policy file ───────────────────────────────────────────
    if not already_installed:
        pkg_policy = _bundled_policy_path(name)
        user_policy = cfg_module.policies_dir() / f"{name}.yaml"
        if pkg_policy and pkg_policy.is_file():
            user_policy.write_text(pkg_policy.read_text())
            console.print(f"\n[green]✓[/green] Policy installed → [dim]{user_policy}[/dim]")
        else:
            console.print(f"\n[yellow]·[/yellow] No bundled policy for {name} (placeholder).")

    # ── Credential walkthrough ────────────────────────────────
    if entry.credentials and not skip_creds:
        _walk_credentials(entry, console)
    elif skip_creds and entry.credentials:
        console.print(
            f"\n[dim](Skipped credential walkthrough. Run "
            f"[cyan]veto-agents creds set {name}[/cyan] to configure later.)[/dim]"
        )

    # ── Register install + suggest next step ─────────────────
    if not already_installed:
        cfg.installed_agents.append(name)
        cfg_module.save(cfg)

    example_prompt = {
        "media":    "make an image of a neon jellyfish in cyberpunk rain",
        "build":    "deploy this repo to the cheapest provider",
        "research": "research the top 5 papers on agent governance in 2026",
        "inbox":    "triage my inbox from this week",
    }.get(name, "<your prompt>")
    console.print(
        f"\n[green]✓[/green] [bold]{name}[/bold] ready. Try:\n"
        f"  [cyan]veto-agents {name} \"{example_prompt}\"[/cyan]\n"
    )


def _walk_credentials(entry: "registry_module.AgentEntry", console: Console) -> None:
    """Prompt the user through each credential the agent needs, opening
    the signup URL in their browser as a soft nudge. Saves to credentials.yaml."""
    existing = creds_module.load()
    console.print(f"\n[bold]Tool credentials[/bold] — {entry.name} needs:\n")

    for cred in entry.credentials:
        current = existing.get(cred.env_var)
        env_override = os.environ.get(cred.env_var)
        required_tag = "[red]required[/red]" if cred.required else "[dim]optional[/dim]"

        if env_override:
            console.print(
                f"  [green]✓[/green] [bold]{cred.env_var}[/bold] · "
                f"already set in your shell env ({required_tag})"
            )
            continue
        if current:
            mask = current[:6] + "…" + current[-4:] if len(current) > 12 else "saved"
            console.print(
                f"  [green]✓[/green] [bold]{cred.env_var}[/bold] · saved ({mask}) ({required_tag})"
            )
            if not Confirm.ask("    Replace it?", default=False):
                continue

        console.print(f"\n  [bold]{cred.label}[/bold] ({required_tag})")
        if cred.notes:
            console.print(f"  [dim]{cred.notes}[/dim]")
        console.print(f"  [dim]Get one at:[/dim] [cyan]{cred.signup_url}[/cyan]")

        # Best-effort browser open. Silently no-op on headless systems.
        try:
            import webbrowser
            webbrowser.open(cred.signup_url)
        except Exception:
            pass

        if cred.required:
            value = Prompt.ask("  Paste the value (or press Enter to skip)", default="", password=False)
        else:
            value = Prompt.ask("  Paste (or press Enter to skip)", default="", password=False)

        value = value.strip()
        if value:
            creds_module.set_value(cred.env_var, value)
            console.print(f"  [green]✓[/green] Saved to ~/.veto-agents/credentials.yaml")
        elif cred.required:
            console.print(
                f"  [yellow]·[/yellow] Skipped. The {entry.name} agent will fail "
                f"until this is set. Run install again or `veto-agents creds set {cred.env_var}` later."
            )
        else:
            console.print("  [dim]Skipped (optional).[/dim]")


# ── Credentials management subcommand ────────────────────────────────────

creds_app = typer.Typer(help="Manage saved tool credentials (Replicate, Vercel, etc.).")
app.add_typer(creds_app, name="creds")


@creds_app.command("list")
def creds_list() -> None:
    """Show which credentials are saved (masked)."""
    saved = creds_module.load()
    if not saved:
        console.print("[dim]No credentials saved yet. Install an agent to set them up.[/dim]")
        return
    console.print("\n[bold]Saved credentials[/bold]\n")
    for env_var, val in sorted(saved.items()):
        mask = val[:6] + "…" + val[-4:] if len(val) > 12 else "***"
        env_override = " [dim](overridden by shell env)[/dim]" if os.environ.get(env_var) else ""
        console.print(f"  [cyan]{env_var:<24}[/cyan]  {mask}{env_override}")
    console.print()


@creds_app.command("set")
def creds_set(
    env_var: str = typer.Argument(..., help="The env var name, e.g. REPLICATE_API_TOKEN"),
    value: str = typer.Argument(None, help="The value (omit to be prompted)"),
) -> None:
    """Save or update a single credential."""
    if value is None:
        value = Prompt.ask(f"Value for {env_var}", password=False).strip()
    if not value:
        console.print("[yellow]·[/yellow] Empty value. Nothing saved.")
        return
    creds_module.set_value(env_var, value)
    console.print(f"[green]✓[/green] Saved [bold]{env_var}[/bold].")


@creds_app.command("remove")
def creds_remove(env_var: str = typer.Argument(...)) -> None:
    """Delete a saved credential."""
    if creds_module.remove(env_var):
        console.print(f"[green]✓[/green] Removed {env_var}.")
    else:
        console.print(f"[yellow]·[/yellow] {env_var} wasn't saved.")


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

    # ── Balance + accounting ─────────────────────────────────
    import time as _time

    from .wallet_view import compute_stats

    try:
        stats = compute_stats(
            treasury=target.address,
            chain=target.chain,
            api_base=cfg.veto_api_base,
            api_key=cfg.api_key,
            client_id=cfg.client_id,
            now_epoch=_time.time(),
        )
    except Exception as e:
        console.print(f"[red]✗[/red] Couldn't load wallet stats: {e}\n")
        return

    console.print(f"On-chain USDC:             [bold]${stats.usdc_balance_usd:,.2f}[/bold]")
    console.print(
        f"Used (off-chain ledger):   [dim]${stats.lifetime_spent_usd:,.4f}[/dim]"
    )
    if stats.pending_escalated_usd > 0:
        console.print(
            f"Pending (escalated):       [yellow]${stats.pending_escalated_usd:,.4f}[/yellow]"
        )
    available_color = "green" if stats.available_usd > 0.10 else "yellow" if stats.available_usd > 0 else "red"
    console.print(
        f"Available to spend:        [bold {available_color}]${stats.available_usd:,.4f}[/bold {available_color}]"
    )
    if stats.lifetime_spent_usd > 0:
        console.print(
            "[dim](Settlement of off-chain spend → on-chain VGA debit lands in v0.0.5.)[/dim]"
        )

    per_agent = stats.per_agent
    recent = stats.recent

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
