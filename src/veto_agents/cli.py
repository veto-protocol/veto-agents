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
from . import auth, banner, llm_providers, wallet_setup as wallet_setup_module
from .funding import get_funding_target, render_funding_qr
from .register import is_valid_evm_address


app = typer.Typer(
    name="veto-agents",
    help="AI agents that pay for things on your behalf, with the safety built in.",
    no_args_is_help=False,  # callback handles bare-invocation (wizard or status)
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
        # First run with no agents installed → wizard. Subsequent runs → status.
        if not cfg.installed_agents:
            _first_run_wizard(ctx)
        else:
            _render_status(cfg)


def _render_status(cfg) -> None:
    """The default screen a returning user sees. Status of their setup +
    1-3 next actions. Not a Typer help dump."""
    banner.render(console, subtitle="AI agents that pay for things, governed by Veto")

    installed = cfg.installed_agents
    agents_line = " · ".join(f"[cyan]{a}[/cyan]" for a in installed)

    # Account state
    if cfg.api_key:
        email_hint = "  [dim](veto-agents account)[/dim]"
        account_line = f"[green]signed in[/green]{email_hint}"
    else:
        account_line = "[yellow]local mode[/yellow]  [dim](upgrade: veto-agents account upgrade)[/dim]"

    # Wallet state
    if cfg.wallet_address:
        short = cfg.wallet_address[:8] + "…" + cfg.wallet_address[-4:]
        wallet_line = f"[green]connected[/green] · {short}"
    else:
        wallet_line = "[dim]not set yet  (veto-agents wallet setup)[/dim]"

    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    table.add_column("k", style="dim", no_wrap=True)
    table.add_column("v")
    table.add_row("Agents", agents_line or "[dim]none yet[/dim]")
    table.add_row("Account", account_line)
    table.add_row("Wallet", wallet_line)
    console.print(table)

    # Next-action suggestions — what an AI coding agent (or a human) can run now.
    console.print()
    console.print("[bold]Try:[/bold]")
    example_for = installed[0] if installed else "media"
    examples = {
        "media": '"make a neon jellyfish in cyberpunk rain"',
        "build": '"deploy this repo to the cheapest provider"',
        "research": '"research the top 5 papers on agent governance"',
        "inbox": '"triage my inbox from this week"',
        "groups": '"--daemon  (run as a 24/7 Telegram bot)"',
    }
    example_prompt = examples.get(example_for, '"<your prompt>"')
    console.print(f"  [cyan]veto-agents {example_for}[/cyan] {example_prompt}")
    if not cfg.api_key:
        console.print("  [cyan]veto-agents account upgrade[/cyan]  enable signed receipts")
    if not cfg.wallet_address and cfg.api_key:
        console.print("  [cyan]veto-agents wallet setup[/cyan]    enable on-chain spending governance")
    console.print(
        f"\n[dim]All commands: [cyan]veto-agents --help[/cyan]  ·  version {__version__}[/dim]\n"
    )


def _first_run_wizard(ctx: typer.Context) -> None:
    """First run — gets the user to a working agent in the fewest possible
    prompts. No email, no wallet, no LLM-provider choice up front. Just:
    pick an agent → paste the key it needs → run."""
    banner.render(console, subtitle="AI agents that pay for things, governed by Veto")

    console.print(
        "[bold]Welcome.[/bold]  Let's get you a working agent in about a minute.\n"
        "[dim]Pick one to start. You can add more anytime.[/dim]\n"
    )

    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    table.add_column("idx", style="dim", no_wrap=True, width=3)
    table.add_column("name", style="bold cyan", no_wrap=True, min_width=10)
    table.add_column("what it does")
    for i, entry in enumerate(registry_module.REGISTRY, 1):
        table.add_row(str(i), entry.name, entry.one_line)
    console.print(table)
    console.print()

    choices = registry_module.all_names() + [str(i) for i in range(1, len(registry_module.REGISTRY) + 1)]
    raw = Prompt.ask("Choose", choices=choices, default="media", show_choices=False)
    chosen = registry_module.REGISTRY[int(raw) - 1].name if raw.isdigit() else raw

    # install walks the chosen agent's credentials (browser-open for each).
    ctx.invoke(install, name=chosen)

    # Soft closing line — signed receipts are a later step, not a precondition.
    console.print(
        "[dim]Heads up: until you run [cyan]veto-agents account upgrade[/cyan], your "
        "agent runs locally without signed receipts. Upgrade takes 30 seconds via "
        "email magic-link, anytime.[/dim]\n"
    )


# ─── setup ────────────────────────────────────────────────────────────────


@app.command()
def setup() -> None:
    """Configure your account: sign in + pick an LLM brain + set policy posture."""
    banner.render(console, subtitle="Account setup")

    cfg = cfg_module.load()

    # LLM provider — numbered picker (same pattern as the agent picker on
    # first run). Users can type the number or the name; show_choices=False
    # keeps Rich from rendering an ugly [hermes/ollama/...] list at the
    # prompt, since we just showed the table above.
    console.print("[bold]Pick the LLM brain your agents will use.[/bold]")
    console.print("  [dim]You can change this per-agent later.[/dim]\n")
    provider_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    provider_table.add_column("idx", style="dim", no_wrap=True, width=3)
    provider_table.add_column("id", style="cyan bold", no_wrap=True)
    provider_table.add_column("label")
    provider_list = list(llm_providers.PROVIDERS.values())
    for i, prov in enumerate(provider_list, 1):
        provider_table.add_row(str(i), prov.name, prov.label)
    console.print(provider_table)
    console.print()

    default_name = (
        cfg.llm_provider if cfg.llm_provider in llm_providers.PROVIDERS else "hermes"
    )
    default_idx = next(
        (str(i) for i, p in enumerate(provider_list, 1) if p.name == default_name), "1"
    )
    pick_choices = llm_providers.all_names() + [
        str(i) for i in range(1, len(provider_list) + 1)
    ]
    raw = Prompt.ask(
        f"Pick one [dim](1-{len(provider_list)} or name)[/dim]",
        choices=pick_choices,
        default=default_idx,
        show_choices=False,
    )
    choice = provider_list[int(raw) - 1].name if raw.isdigit() else raw
    cfg.llm_provider = choice
    chosen = llm_providers.get(choice)
    assert chosen is not None

    # Endpoint + model: take provider defaults, unless `custom` (ask).
    if choice == "custom":
        endpoint = Prompt.ask("  Custom endpoint URL (OpenAI-compatible)", default=cfg.llm_endpoint or "").strip()
        if endpoint:
            cfg.llm_endpoint = endpoint
        model = Prompt.ask("  Model name (e.g. llama3.1:70b)", default=cfg.llm_model or "").strip()
        if model:
            cfg.llm_model = model
    else:
        cfg.llm_endpoint = chosen.endpoint
        cfg.llm_model = chosen.default_model
        if chosen.notes:
            console.print(f"  [dim]{chosen.notes}[/dim]")

    # Collect API key for the chosen provider (skip if hosted or custom).
    if chosen.env_var:
        saved = creds_module.load().get(chosen.env_var)
        env_override = os.environ.get(chosen.env_var)
        if env_override:
            console.print(f"  [green]✓[/green] {chosen.env_var} already set in shell env — using that.")
        elif saved:
            mask = saved[:6] + "…" + saved[-4:] if len(saved) > 12 else "saved"
            console.print(f"  [green]✓[/green] {chosen.env_var} already saved ({mask}).")
            if Confirm.ask("    Replace it?", default=False):
                _prompt_for_llm_key(chosen)
        else:
            _prompt_for_llm_key(chosen)
    elif choice == "ollama":
        console.print(
            f"  [dim]Using a local Ollama endpoint at {cfg.llm_endpoint}. "
            f"Make sure Ollama is running ({chosen.signup_url}) and you've "
            f"pulled the model: [/dim][cyan]ollama pull {cfg.llm_model}[/cyan]"
        )

    # 2. Sign in. First check if the main `veto` CLI already signed this
    # user in — if so we just reuse those credentials, no second sign-in.
    if not cfg.api_key:
        main_state = cfg_module.read_main_cli_state()
        if main_state and main_state.get("api_key"):
            existing_email = main_state.get("email", "(unknown)")
            console.print(
                f"\n[bold]Existing account detected[/bold] — credentials from the main "
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
        console.print("\n[bold]Sign in[/bold]  [dim](magic link, no password)[/dim]")
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
        console.print(f"\n[dim]Already signed in (agent_id {cfg.agent_id}).[/dim]")

    # Policy posture — numbered picker for consistency with the LLM picker.
    console.print("\n[bold]Policy posture[/bold]  [dim]for new agents (you can tweak per-agent later)[/dim]\n")
    postures = [
        ("strict",     "tight caps · small allowlist · escalate on anything new"),
        ("balanced",   "sensible defaults · most users keep this"),
        ("permissive", "loose · for trusted agents, expect more dollars to flow"),
    ]
    posture_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    posture_table.add_column("idx", style="dim", no_wrap=True, width=3)
    posture_table.add_column("id", style="cyan bold", no_wrap=True)
    posture_table.add_column("label")
    for i, (name, label) in enumerate(postures, 1):
        posture_table.add_row(str(i), name, label)
    console.print(posture_table)
    console.print()

    default_posture = cfg.policy_posture if cfg.policy_posture in {p[0] for p in postures} else "balanced"
    default_posture_idx = next(
        str(i) for i, (n, _) in enumerate(postures, 1) if n == default_posture
    )
    posture_choices = [p[0] for p in postures] + [str(i) for i in range(1, len(postures) + 1)]
    raw_posture = Prompt.ask(
        f"Pick one [dim](1-{len(postures)} or name)[/dim]",
        choices=posture_choices,
        default=default_posture_idx,
        show_choices=False,
    )
    cfg.policy_posture = (
        postures[int(raw_posture) - 1][0] if raw_posture.isdigit() else raw_posture
    )

    cfg_module.save(cfg)

    # Done. Wallet setup is opt-in via `veto-agents wallet setup` — we don't
    # ask the user to send money to anything before they've decided they like
    # the product. Less scary, better funnel.
    console.print(
        f"\n[green]✓[/green] Signed in. State at [dim]{cfg_module.state_dir()}[/dim]\n\n"
        f"[bold]Next:[/bold]\n"
        f"  [cyan]veto-agents install media[/cyan]   add your first agent\n"
        f"  [cyan]veto-agents wallet setup[/cyan]    enable on-chain spending governance "
        f"[dim](optional, do this when you're ready to fund)[/dim]\n"
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

    # NOTE: We deliberately DON'T require auth here. Franklin pattern —
    # install + use the agent locally first; receipt signing + on-chain
    # governance are an opt-in upgrade via `veto-agents account upgrade`.

    already_installed = name in cfg.installed_agents
    if already_installed:
        console.print(
            f"[yellow]·[/yellow] [bold]{name}[/bold] is already installed. "
            f"Re-running credential walkthrough.\n"
        )

    # ── Banner + welcome + summary of what's about to happen ──
    banner.render(console, subtitle=f"Installing {entry.name}")
    console.print(f"[bold cyan]{entry.name}[/bold cyan] — {entry.one_line}")
    console.print(f"  [dim]Spends on: {entry.spends_on}[/dim]")
    if cfg.api_key and cfg.wallet_address:
        console.print(
            f"  [dim]Will use your Veto account:[/dim] [cyan]{cfg.wallet_address[:10]}…[/cyan]"
        )
    elif cfg.api_key:
        console.print("  [dim]Will use your Veto account (no funding wallet set yet).[/dim]")
    else:
        console.print(
            "  [dim]Running locally — no Veto account yet. "
            "Upgrade later for signed receipts: [cyan]veto-agents account upgrade[/cyan][/dim]"
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


def _prompt_for_llm_key(provider: "llm_providers.LLMProvider") -> None:
    """Open the LLM provider's signup page, prompt for the key, save it."""
    assert provider.env_var is not None
    console.print(f"\n  [bold]{provider.label}[/bold]")
    if provider.notes:
        console.print(f"  [dim]{provider.notes}[/dim]")
    if provider.signup_url:
        console.print(f"  [dim]Get a key at:[/dim] [cyan]{provider.signup_url}[/cyan]")
        try:
            import webbrowser
            webbrowser.open(provider.signup_url)
        except Exception:
            pass

    value = Prompt.ask(
        f"  Paste your {provider.env_var} (or Enter to skip)",
        default="",
        password=False,
    ).strip()
    if value:
        creds_module.set_value(provider.env_var, value)
        console.print("  [green]✓[/green] Saved to ~/.veto-agents/credentials.yaml")
    else:
        console.print(
            f"  [yellow]·[/yellow] Skipped. Agents using {provider.name} will fail "
            f"until {provider.env_var} is set. Run "
            f"`veto-agents creds set {provider.env_var} <key>` to add it later."
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


# ── Account management (opt-in Veto governance) ──────────────────────────

account_app = typer.Typer(
    help="Veto account — sign in for signed receipts, on-chain governance, audit trail.",
    invoke_without_command=True,
)
app.add_typer(account_app, name="account")


@account_app.callback(invoke_without_command=True)
def account_default(ctx: typer.Context) -> None:
    """Show account status. (No subcommand → status.)"""
    if ctx.invoked_subcommand is not None:
        return
    cfg = cfg_module.load()
    console.print()
    if cfg.api_key:
        masked = cfg.api_key[:12] + "…" + cfg.api_key[-4:]
        console.print(f"[bold green]✓ Signed in[/bold green] · receipts on")
        console.print(f"  [dim]agent_id:[/dim] {cfg.agent_id or '—'}")
        console.print(f"  [dim]api_key:[/dim]  {masked}")
        if cfg.wallet_address:
            console.print(f"  [dim]wallet:[/dim]   {cfg.wallet_address}")
        console.print(
            f"\n  [dim]Sign out:[/dim] [cyan]veto-agents account logout[/cyan]"
        )
    else:
        console.print("[yellow]Local mode.[/yellow]  No Veto account yet.\n")
        console.print(
            "  [dim]Your agents run, but actions aren't signed and there's no\n"
            "  audit trail. Upgrade in 30 seconds:[/dim]\n\n"
            "    [bold cyan]veto-agents account upgrade[/bold cyan]\n"
        )
    console.print()


@account_app.command("upgrade")
def account_upgrade() -> None:
    """Sign in with email magic-link, enable Veto governance for your agents."""
    cfg = cfg_module.load()
    if cfg.api_key:
        console.print("[yellow]·[/yellow] Already signed in. Run [cyan]veto-agents account[/cyan] to see status.")
        return
    banner.render(console, subtitle="Upgrading to Veto governance")
    setup()  # delegates to the existing setup wizard for the auth/wallet/QR flow


@account_app.command("logout")
def account_logout() -> None:
    """Sign out and clear local credentials."""
    cfg = cfg_module.load()
    if not cfg.api_key:
        console.print("[dim]Not signed in.[/dim]")
        return
    cfg.api_key = None
    cfg.agent_id = None
    cfg.client_id = None
    cfg_module.save(cfg)
    console.print("[green]✓[/green] Signed out. Your installed agents still work — just locally without signed receipts.")


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


@app.command()
def groups(
    prompt: str = typer.Argument("", help="One-shot question to test the brain. Omit to run the daemon."),
    daemon: bool = typer.Option(False, "--daemon", help="Run as a long-running Telegram bot (production)."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Run the Groups agent: one-shot (`groups "..."`) or 24/7 daemon (`groups --daemon`)."""
    cfg = cfg_module.load()
    if daemon:
        from .agents.groups import run_daemon
        run_daemon(cfg, console)
    else:
        _run_agent("groups", prompt or "hello", yes=yes)


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
    console.print("[dim](onramp link generation lands in v0.0.10)[/dim]")


@wallet_app.command("setup")
def wallet_setup_cmd() -> None:
    """Set up your agent's funding wallet — connect existing or create new."""
    wallet_setup_module.run(console)


@wallet_app.command("help")
def wallet_help() -> None:
    """Explain how your agent's wallet works — non-custodial, you own it."""
    wallet_setup_module.explain(console)


@wallet_app.command("withdraw")
def wallet_withdraw() -> None:
    """Withdraw all funds from your agent's wallet back to your wallet."""
    cfg = cfg_module.load()
    if not cfg.wallet_address:
        console.print("[yellow]·[/yellow] No wallet linked. Run [cyan]veto-agents wallet setup[/cyan].")
        return
    console.print(
        f"\n[bold]Withdraw all funds[/bold] from your agent's Safe back to:\n"
        f"  [cyan]{cfg.wallet_address}[/cyan]\n\n"
        "[dim]v0.0.10 will build + submit the withdraw tx via your connected wallet.\n"
        "For now, you can do this manually:\n"
        "  1. Open https://app.safe.global\n"
        "  2. Connect the wallet linked above\n"
        "  3. Send the full balance to your wallet[/dim]\n"
    )


@wallet_app.command("revoke-veto")
def wallet_revoke_veto() -> None:
    """Remove Veto's Guard from your Safe — full unilateral control returns to you."""
    cfg = cfg_module.load()
    if not cfg.wallet_address:
        console.print("[yellow]·[/yellow] No wallet linked.")
        return
    console.print(
        "\n[bold]Remove Veto Guard from your Safe[/bold]\n\n"
        "  After this, your Safe operates without Veto's transaction check — full\n"
        "  unilateral control. The receipts feed for past actions stays signed (those\n"
        "  decisions already happened); future actions just don't go through Veto.\n\n"
        "[dim]v0.0.10 will build + submit this tx via your wallet. For now:\n"
        "  1. Open https://app.safe.global\n"
        "  2. Connect your wallet\n"
        "  3. Apps → Transaction Builder → setGuard(0x0)[/dim]\n"
    )


def _render_wallet_dashboard() -> None:
    """The headline `veto-agents wallet` view — balance + per-agent + recent."""
    from .auth_gate import require_signin

    cfg = cfg_module.load()
    if not cfg.api_key:
        cfg = require_signin(console, cfg)
        if not cfg.api_key:
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
