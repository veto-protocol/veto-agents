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
from enum import Enum
from typing import Optional

import click
import typer
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__, config as cfg_module, credentials as creds_module, registry as registry_module
from . import auth, auth_creds, banner, llm_providers, wallet_setup as wallet_setup_module
from .funding import get_funding_target, render_funding_qr
from .register import is_valid_evm_address


# ─── small UX helpers ─────────────────────────────────────────────────────


def _mask_key(key: str) -> str:
    """Same masked format the main `veto` CLI uses: first 12 chars … last 4.
    Falls back to "(saved)" for very short strings so we never accidentally
    print the whole credential."""
    if not key:
        return ""
    if len(key) <= 20:
        return "(saved)"
    return f"{key[:12]}…{key[-4:]}"


def _installed_agents(cfg) -> list[str]:
    """Coerce cfg.installed_agents to a list of names (L-5).

    A hand-edited or migrated config can hold a bare string ("media") instead
    of a list. Iterating that yields characters ('m','e','d',…) → the
    'm · e · d · i · a' bug, and `"x" in "media"` becomes a substring test.
    Normalize once here so every consumer sees a clean list."""
    v = getattr(cfg, "installed_agents", None)
    if isinstance(v, str):
        return [v] if v else []
    if not v:
        return []
    return list(v)


def _try_it_command(cfg) -> str:
    """A concrete, copy-pasteable command for the user to run after sign-in.
    Picks the first installed agent and pairs it with a sensible example
    brief. Falls back to `veto-agents` (the status screen) when no agent
    is installed yet."""
    examples = {
        "media":    'veto-agents media "an instagram carousel for a coffee shop launch — 3 frames"',
        "adbuyer":  "veto-agents adbuyer 'US traffic campaign to https://mysite.com, $20/day'",
        "groups":   'veto-agents groups "draft a welcome message for new Telegram members"',
        "research": 'veto-agents research "find the top 5 x402-adjacent open-source repos this month"',
        "inbox":    'veto-agents inbox "summarize my last 24 hours of email"',
        "build":    'veto-agents build "scaffold a Vite + React + Tailwind landing page"',
    }
    for name in _installed_agents(cfg):
        if name in examples:
            return examples[name]
    return "veto-agents"


def _render_signed_in(console: Console, cfg, *, try_it: str | None = None) -> None:
    """Standard post-signin success display. Mirrors the main `veto` CLI:
    ✓ Signed in as <email> + masked api_key + agent_id + stored-in line,
    then a concrete `Try it:` command. Used by both the magic-link path
    and the main-CLI-credentials reuse path so the user sees the same
    confirmation regardless of which route got them here."""
    email = cfg.email or "(unknown)"
    masked = _mask_key(cfg.api_key or "")
    agent_id = cfg.agent_id or "(none)"
    where = auth_creds.backend_kind()
    cmd = try_it or _try_it_command(cfg)

    console.print(f"\n  [green]✓[/green] Signed in as [cyan]{email}[/cyan].")
    console.print(
        f"    [dim]api_key={masked}  agent_id={agent_id}  stored in: {where}[/dim]"
    )
    console.print(f"\n  [bold]Try it:[/bold]  [cyan]{cmd}[/cyan]\n")


console = Console()


class _SafeTyper(typer.Typer):
    """Typer app with a top-level safety net around the whole invocation.

    The professional bar: no raw traceback — and no bare "Aborted." — ever
    reaches a user on a common path. Always a clean one-line message + exit 1.

    We drive Click ourselves with ``standalone_mode=False`` so aborts, usage
    errors, and Click exceptions come back as *exceptions* (instead of Click's
    default "print + sys.exit") — then we format them. Normal control flow is
    preserved: ``typer.Exit`` / ``SystemExit`` pass through, ``--help`` and
    usage errors still print and exit as usual, and a successful command
    returns normally.
    """

    def __call__(self, *args, **kwargs):  # noqa: D401 - see class docstring
        from typer.main import get_command

        # Route through --help / completion? Let Typer's own machinery handle
        # those exactly as before (they rely on standalone behavior + hooks).
        if args or kwargs:
            return super().__call__(*args, **kwargs)

        command = get_command(self)
        try:
            # standalone_mode=False → Click/Typer RAISE ClickException/Abort
            # (so our net formats them) but for an explicit exit they RETURN
            # the exit code instead of raising (see typer.core.main). So the
            # return value IS the exit code: 0/None on success, n on
            # `raise typer.Exit(n)` / `--help` / `ctx.exit(n)`.
            code = command(standalone_mode=False)
            raise SystemExit(code or 0)
        except SystemExit:
            raise
        except (typer.Exit, click.exceptions.Exit) as e:
            # Belt-and-suspenders: if a future Typer version raises instead of
            # returning, honor the intended exit code rather than tracebacking.
            raise SystemExit(getattr(e, "exit_code", 0))
        except click.exceptions.Abort:
            # A prompt hit EOF/non-TTY, or Ctrl-C at a prompt. This is the old
            # bare "Aborted." — replace with an actionable hint.
            console.print(
                "\n[yellow]·[/yellow] Cancelled — no input available "
                "(piped or non-interactive), or interrupted. Re-run "
                "interactively, or pass the flags you need "
                "([cyan]veto-agents <command> --help[/cyan])."
            )
            raise SystemExit(1)
        except KeyboardInterrupt:
            console.print("\n[yellow]·[/yellow] Interrupted.")
            raise SystemExit(130)
        except EOFError:
            console.print(
                "\n[yellow]·[/yellow] No input available "
                "(piped or non-interactive). Re-run interactively, or pass the "
                "flags you need ([cyan]veto-agents <command> --help[/cyan])."
            )
            raise SystemExit(1)
        except click.ClickException as e:
            # Usage errors, bad params, etc. Click formats these nicely itself.
            e.show()
            raise SystemExit(e.exit_code)
        except Exception as e:  # noqa: BLE001 - last-resort net, message not trace
            msg = str(e).strip() or e.__class__.__name__
            console.print(f"[red]✗[/red] {msg}  [dim](run with --help)[/dim]")
            raise SystemExit(1)


app = _SafeTyper(
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

# Sub-app for `veto-agents brand ...`
brand_app = typer.Typer(help="Brand profile the creative studio and ad buyer follow.")
app.add_typer(brand_app, name="brand")


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
        if not _installed_agents(cfg):
            _first_run_wizard(ctx)
        else:
            _render_status(cfg)


def _render_status(cfg) -> None:
    """The default screen — the product's front door. Leads with what the tool
    can DO (the flagship media buyer), then setup status, then a zero-setup try.
    Not a Typer help dump."""
    banner.render(console, subtitle="The autonomous AI media buyer that can't overspend")

    # ── What you can do (flagship first) ──────────────────────────────────
    cmds = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    cmds.add_column("cmd", style="cyan", no_wrap=True)
    cmds.add_column("what")
    cmds.add_row("veto-agents adbuyer-setup", "guided setup — LLM brain · providers · Meta · budgets")
    cmds.add_row('veto-agents brand set <url>', "teach it your brand from your website (or a txt/md)")
    cmds.add_row('veto-agents create "<brief>"', "make an ad — copy + image (+ video/voice), Veto-gated")
    cmds.add_row('veto-agents adbuyer -g "<goal>"', "run the 24/7 media buyer — add --mock to try safely")
    cmds.add_row("veto-agents mcp", "use it from Claude Code / Claude Desktop / OpenClaw")
    console.print(cmds)

    # ── Setup status ──────────────────────────────────────────────────────
    if cfg.api_key:
        account_line = "[green]signed in[/green]  [dim](veto-agents account)[/dim]"
    else:
        account_line = "[yellow]not signed in[/yellow]  [dim](veto-agents adbuyer-setup — free, 30s)[/dim]"

    if cfg.wallet_address:
        short = cfg.wallet_address[:8] + "…" + cfg.wallet_address[-4:]
        wallet_line = f"[green]connected[/green] · {short}"
    else:
        wallet_line = "[dim]not set  (veto-agents wallet setup — optional)[/dim]"

    brand_line = "[dim]not set  (veto-agents brand set <url>)[/dim]"
    try:  # fail-soft: status must never crash
        from .agents.adbuyer.creative import brand as _brand
        _p = _brand.load_brand()
        if _p is not None:
            brand_line = f"[green]{_p.name}[/green]  [dim]· {_p.tone}[/dim]"
    except Exception:
        pass

    meta_line = "[dim]not connected  (ok — use --mock, or adbuyer-setup)[/dim]"
    try:
        from .agents.adbuyer import meta_env as _me
        _m = _me.load_meta(cfg)
        if not _me.missing(_m):
            meta_line = "[green]connected[/green]"
    except Exception:
        pass

    other = [a for a in _installed_agents(cfg) if a != "adbuyer"]
    status = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    status.add_column("k", style="dim", no_wrap=True)
    status.add_column("v")
    status.add_row("Account", account_line)
    status.add_row("Brand", brand_line)
    status.add_row("Meta ads", meta_line)
    status.add_row("Wallet", wallet_line)
    if other:
        status.add_row("Also here", " · ".join(f"[cyan]{a}[/cyan]" for a in other)
                       + "  [dim](sample agents)[/dim]")
    console.print()
    console.print(status)

    # ── Zero-setup try ────────────────────────────────────────────────────
    console.print()
    console.print("[bold]Try it now (no ad account, no spend — real governance):[/bold]")
    console.print("  [cyan]veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock --once[/cyan]")
    console.print(
        f"\n[dim]All commands + flags: [cyan]veto-agents --help[/cyan]  ·  docs: github.com/veto-protocol/veto-agents  ·  v{__version__}[/dim]\n"
    )


def _first_run_wizard(ctx: typer.Context) -> None:
    """First run — gets the user to a working agent in the fewest possible
    prompts. Pick an agent → install it → then OFFER (skippable) to set up
    account + wallet so it can spend for real. Lowest friction to first
    value, but no dead-end after choosing."""
    # The installer (install.sh) already showed the banner right before it
    # exec'd us — don't show it twice. Standalone `veto-agents` still does.
    import os
    if not os.environ.get("VETO_FROM_INSTALLER"):
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
    # no_banner: the wizard already rendered the banner — don't repeat it
    # (that was the old triple-banner pile-up on fresh installs).
    ctx.invoke(install, name=chosen, no_banner=True)

    # Hand off into setup instead of dead-ending. Lowest friction to first
    # value — they can decline and just run the agent — but if they came this
    # far they usually want it spend-ready, so we offer right here.
    console.print(
        f"\n[bold]{chosen} is installed[/bold] and runs locally right now.\n"
        "[dim]To let it spend for real, set up your account + wallet (~2 min). "
        "Or skip and just try it — you can run setup anytime.[/dim]\n"
    )
    if typer.confirm("Set up account + wallet now?", default=True):
        cfg = cfg_module.load()
        _do_login(console, cfg)
        cfg = cfg_module.load()  # reload — _do_login persists creds to keychain
        if cfg.api_key:
            wallet_setup_module.run(console)
        else:
            console.print(
                "\n[dim]No account yet — run [cyan]veto-agents account upgrade[/cyan] "
                "anytime for signed receipts + spending.[/dim]\n"
            )
    else:
        console.print(
            "\n[dim]Skipped — no rush. When ready: [cyan]veto-agents account upgrade[/cyan] "
            "(signed receipts), then [cyan]veto-agents wallet setup[/cyan] (spending).[/dim]\n"
        )


# ─── login (extracted from setup) ─────────────────────────────────────────


def _do_login(console: Console, cfg) -> bool:
    """Sign-in flow: reuse main-CLI creds if present, else magic link.

    Returns True if a sign-in action actually happened (so the caller can
    render the success block). Returns False if the user was already
    signed in, declined, or hit a recoverable error.

    Mutates `cfg` in place and persists to disk on success.
    """
    if cfg.api_key:
        # Already signed in — be terse, point to logout if they meant to switch.
        console.print(
            f"  [green]✓[/green] Already signed in as "
            f"[cyan]{cfg.email or cfg.agent_id}[/cyan]."
        )
        console.print(
            f"  [dim]Stored in {auth_creds.backend_kind()}. Run "
            f"[/dim][cyan]veto-agents logout[/cyan][dim] to sign out and re-auth.[/dim]\n"
        )
        return False

    # Path A: reuse credentials from the main `veto` CLI if installed.
    main_state = cfg_module.read_main_cli_state()
    if main_state and main_state.get("api_key"):
        existing_email = main_state.get("email", "(unknown)")
        console.print("\n[bold]Reuse your main Veto sign-in?[/bold]")
        console.print(
            f"  [dim]Detected credentials for[/dim] [cyan]{existing_email}[/cyan] "
            "[dim]from the main `veto` CLI. Saying yes skips the magic-link "
            "round-trip — same account, both CLIs.[/dim]"
        )
        if Confirm.ask("  Reuse them?", default=True):
            cfg_module.import_from_main_cli(cfg, main_state)
            cfg_module.save(cfg)
            return True

    # Path B: fresh magic-link sign-in. Same endpoints the main npm CLI
    # uses (/api/v1/auth/email/start/ + /api/v1/auth/cli/poll/).
    console.print("\n[bold]Sign in[/bold]  [dim](magic link, no password)[/dim]")
    while True:
        email = Prompt.ask("  Your email").strip().lower()
        if auth.is_valid_email(email):
            break
        console.print("  [red]✗[/red] That doesn't look like a valid email. Try again.")

    device_code = auth.generate_device_code()
    try:
        auth.start(api_base=cfg.veto_api_base, email=email, device_code=device_code)
    except Exception as e:
        console.print(f"  [red]✗[/red] Could not start sign-in: {e}")
        console.print(
            "  [dim]Retry with [/dim][cyan]veto-agents login[/cyan][dim] once "
            "the connection's back.[/dim]"
        )
        return False

    # Always print the webmail URL — `webbrowser.open()` can silently fail
    # in too many environments (SSH, sandboxed, missing default browser).
    webmail = auth.webmail_url_for(email)
    auth.open_inbox_for(email)  # best-effort side effect
    console.print(f"\n  [green]✓[/green] Magic link sent to [cyan]{email}[/cyan].")
    console.print(
        "  [dim]Click the button in the email to sign in. "
        "Check spam if it doesn't arrive in ~30s — links come "
        "from [/dim][cyan]auth@veto-ai.com[/cyan][dim].[/dim]"
    )
    if webmail:
        console.print(f"  [dim]→ open[/dim] [cyan]{webmail}[/cyan]")
    console.print(
        "  [dim](Waiting up to 15 minutes. Press Ctrl-C to abort.)[/dim]\n"
    )

    try:
        with console.status("[dim]waiting for the click…[/dim]", spinner="dots"):
            ready = auth.poll_until_ready(
                api_base=cfg.veto_api_base,
                device_code=device_code,
            )
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]·[/yellow] Aborted. Re-run [cyan]veto-agents login[/cyan] when ready."
        )
        return False
    except TimeoutError as e:
        console.print(f"\n[red]✗[/red] {e}")
        return False
    except Exception as e:
        console.print(f"\n[red]✗[/red] Auth poll failed: {e}")
        return False

    cfg.api_key = ready.api_key
    cfg.agent_id = ready.agent_id
    cfg.client_id = ready.client_id
    cfg.email = email
    cfg_module.save(cfg)
    return True


@app.command()
def login() -> None:
    """Sign in to Veto via magic link.

    Only does sign-in — does NOT touch your LLM provider choice or
    policy posture. Use [cyan]veto-agents setup[/cyan] if you want the
    full wizard. Idempotent: if you're already signed in, this is a
    no-op (run [cyan]veto-agents logout[/cyan] to switch accounts).
    """
    banner.render(console, subtitle="Sign in")
    cfg = cfg_module.load()
    if _do_login(console, cfg):
        _render_signed_in(console, cfg)


@app.command()
def logout() -> None:
    """Sign out — wipes Veto credentials from the OS keychain."""
    cfg = cfg_module.load()
    if not cfg.api_key:
        console.print("[dim]Not signed in.[/dim]")
        return
    email = cfg.email or "(unknown)"
    auth_creds.clear()
    # Strip from in-memory cfg + rewrite yaml without secrets.
    cfg.api_key = cfg.agent_id = cfg.client_id = cfg.email = None
    cfg_module.save(cfg)
    console.print(f"  [green]✓[/green] Signed out [dim]({email})[/dim].")


# ─── setup ────────────────────────────────────────────────────────────────


@app.command()
def setup() -> None:
    """Configure your account: sign in + pick an LLM brain + set policy posture.

    Sign-in runs FIRST so you can stop at any point and still have a
    working account. LLM and posture come after — both are editable
    later, sign-in is the only step that has a network round-trip.
    """
    banner.render(console, subtitle="Account setup")

    cfg = cfg_module.load()

    # 1. Sign in.
    if _do_login(console, cfg):
        _render_signed_in(console, cfg)

    if not cfg.api_key:
        # Sign-in didn't complete — abort the rest of setup so we don't
        # collect an LLM key without a Veto account behind it.
        console.print(
            "[yellow]·[/yellow] Skipped the rest of setup. Re-run "
            "[cyan]veto-agents setup[/cyan] anytime."
        )
        return

    # 2. LLM provider — numbered picker (same pattern as the agent picker on
    # first run). Users can type the number or the name; show_choices=False
    # keeps Rich from rendering an ugly [hermes/ollama/...] list at the
    # prompt, since we just showed the table above.
    console.print("\n[bold]Pick the LLM brain your agents will use.[/bold]")
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
        _apply_provider_endpoint(cfg, chosen)  # H-4: llm_endpoint only for keyless-local
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

    # 3. Policy posture — numbered picker for consistency with the LLM picker.
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


# ─── adbuyer-setup — guided wizard for the ad/media buyer ──────────────────
#
# One-step-at-a-time onboarding for the ad buyer (no wall of env vars). Each
# step explains WHAT it configures, WHY it's needed, and WHERE to get the value,
# then writes to the right place with tight perms and never echoes a secret.
#
# Where each value lands (all HOME-scoped, so a temp-HOME test is hermetic):
#   • LLM provider / model      → ~/.veto-agents/config.yaml  (config.save)
#   • LLM provider key          → ~/.veto/creative.env        (director resolver)
#   • Creative keys             → ~/.veto/creative.env        (studio resolver)
#   • Meta ad-account creds     → ~/.veto/meta.env            (meta_env resolver)
#   • Daily budget / creative $ → ~/.veto-agents/policies/*.yaml (Veto caps)
#
# The adbuyer director, controller, and studio all resolve keys via
# creds.resolve() (env → ~/.veto/creative.env → keychain), so the LLM key lives
# in creative.env alongside the studio keys — one file, one resolver, no split
# brain, and nothing pasted into the OS keychain by this wizard.

# Secret env-vars the wizard collects, grouped by destination file.
_CREATIVE_ENV_KEYS = (
    "OPENAI_API_KEY",
    "HIGGSFIELD_API_KEY",
    "HIGGSFIELD_API_SECRET",
    "HIGGSFIELD_CREDENTIALS",
    "ELEVENLABS_API_KEY",
)
_META_ENV_KEYS = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ── Image provider (H-1) ──────────────────────────────────────────────────
# Canonical set. An unknown/empty --image-provider must NEVER silently reach
# the PAID openai path (studio._do_image sends anything != "fal" to OpenAI),
# so we constrain it at the CLI boundary. studio.py enforces server-side too.
IMAGE_PROVIDERS = ("openai", "fal")


class _ImageProvider(str, Enum):
    openai = "openai"
    fal = "fal"


# ── LLM endpoint persistence (H-4) ────────────────────────────────────────
# structured_llm._select treats a provider with NO env_var (ollama / custom)
# as "keyless-local" and, crucially, falls back to a keyless-local route
# whenever cfg.llm_endpoint is set — even for a HOSTED provider whose key is
# missing. That swallows the friendly NoLLMKeyError and lets a placeholder key
# hit the real hosted endpoint → a raw 401. So: only persist llm_endpoint for
# keyless-local providers; leave it UNSET for hosted ones so the guard fires.
def _provider_is_keyless_local(provider) -> bool:
    """A provider is keyless-local (ollama / custom / self-hosted) exactly when
    it declares no API-key env_var — the same test structured_llm._select uses.
    Hosted providers (claude/openai/grok/…) all declare an env_var."""
    return getattr(provider, "env_var", None) is None


def _apply_provider_endpoint(cfg, provider) -> None:
    """Persist endpoint/model for the chosen non-custom provider. Writes
    llm_endpoint ONLY for keyless-local providers (H-4) so a hosted provider
    with a missing key raises the friendly NoLLMKeyError instead of a raw 401.
    Always sets the model (needed to route/label regardless of provider)."""
    if _provider_is_keyless_local(provider):
        cfg.llm_endpoint = provider.endpoint
    else:
        # Hosted: never pin an endpoint. The SDK uses its own default base URL,
        # and leaving this UNSET is what lets the missing-key guard fire.
        cfg.llm_endpoint = None
    cfg.llm_model = provider.default_model


# ── Budget validation (H-5) ───────────────────────────────────────────────
# Meta itself caps a single campaign daily budget well under this; a per-
# generation creative cap is cents. Anything past these is a fat-finger, not
# an intent — clamp + warn so the SUMMARY equals what's actually enforced.
_MAX_DAILY_BUDGET_USD = 1_000_000.0
_MAX_CREATIVE_CAP_USD = 10_000.0


class _BudgetError(ValueError):
    """A budget value is non-positive or non-finite — reject with a clear msg."""


def _validate_budget(value, *, label: str, flag: str, max_usd: float) -> float:
    """Validate + normalize a dollar cap before it's written to policy.

    Rejects non-finite (inf/nan) and non-positive (<= 0) values — those either
    corrupt the YAML (so the read-back regex silently returns the DEFAULT and
    the summary lies) or disable the cap entirely. Clamps absurd-but-finite
    values down to a sane ceiling so the displayed cap always equals what's
    enforced. Returns the value that will actually be written."""
    import math

    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise _BudgetError(f"{label} must be a number ({flag}).") from e
    if not math.isfinite(v):
        raise _BudgetError(
            f"{label} must be a finite dollar amount, not '{value}' ({flag})."
        )
    if v <= 0:
        raise _BudgetError(
            f"{label} must be greater than $0 (got {v:g}) — a $0 or negative cap "
            f"would disable the guardrail ({flag})."
        )
    if v > max_usd:
        console.print(
            f"  [yellow]·[/yellow] {label} of ${v:,.2f} looks like a typo — "
            f"clamping to ${max_usd:,.2f}. Edit later with "
            f"[cyan]veto-agents policy edit adbuyer[/cyan]."
        )
        v = max_usd
    return v


def _prompt_budget(prompt: str, default: str, *, label: str, flag: str,
                   max_usd: float, console: Console) -> float | None:
    """Interactive budget prompt that re-asks on bad input instead of silently
    dropping to the default (H-5). Enter keeps the current value; a
    non-positive/non-finite/non-numeric entry warns and re-prompts. Returns the
    validated float, or None on EOF (caller keeps the existing policy value)."""
    while True:
        try:
            raw = Prompt.ask(prompt, default=default)
        except EOFError:
            return None
        try:
            return _validate_budget(float(raw), label=label, flag=flag, max_usd=max_usd)
        except _BudgetError as e:
            console.print(f"  [red]✗[/red] {e}")
        except ValueError:
            console.print(f"  [red]✗[/red] {label} must be a number (e.g. 25 or 0.25).")


def _upsert_env_var(path, key: str, value: str) -> None:
    """Append-or-update KEY="VALUE" in a ~/.veto/*.env file without clobbering
    other keys or comments. Round-trips through the readers' _parse_env_file
    format (quotes stripped on read → we quote on write, so spaces/# survive).
    Creates the file 0600 BEFORE the secret is written (no world-readable
    window), then re-chmods. The secret value is never printed."""
    from pathlib import Path as _P

    path = _P(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    new_line = f'{key}="{value}"'
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            lines[i] = new_line  # update in place, keep position
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)  # belt-and-suspenders if the file pre-existed at 0644
    except OSError:
        pass


def _set_yaml_scalar(text: str, key: str, value: float) -> tuple[str, bool]:
    """Surgically set `  key: <number>` to a new number in a YAML string,
    preserving indentation, ordering, and any trailing `# comment`. Replaces
    only the FIRST match (the caps block). Returns (new_text, changed)."""
    import re

    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)([0-9]+(?:\.[0-9]+)?)(.*)$", re.M)
    m = pat.search(text)
    if not m:
        return text, False
    new_text = text[: m.start()] + f"{m.group(1)}{value:.2f}{m.group(3)}" + text[m.end():]
    return new_text, True


def _read_policy_scalar(fname: str, key: str, default: float) -> float:
    import re

    p = cfg_module.policies_dir() / fname
    if not p.exists():
        return default
    m = re.search(rf"^\s*{re.escape(key)}:\s*([0-9]+(?:\.[0-9]+)?)", p.read_text(), re.M)
    return float(m.group(1)) if m else default


def _patch_policy_caps(daily_budget, creative_cap, console: Console) -> None:
    """Write the daily ad budget into adbuyer.yaml (caps.per_day_usd) and the
    per-generation cap into adbuyer-creative.yaml (caps.per_transaction_usd).
    Comment-preserving surgical edit; no-ops for values left as None.

    Each value is validated first (H-5): non-positive / non-finite are
    rejected with a clear _BudgetError, absurd values are clamped + warned.
    The value actually WRITTEN is the validated one, so the summary read-back
    can never diverge from what's enforced (no inf/nan/leading-minus that the
    read-back regex would silently drop back to the default)."""
    if daily_budget is not None:
        daily_budget = _validate_budget(
            daily_budget, label="Daily ad budget", flag="--daily-budget",
            max_usd=_MAX_DAILY_BUDGET_USD,
        )
        p = cfg_module.policies_dir() / "adbuyer.yaml"
        if p.exists():
            txt, ok = _set_yaml_scalar(p.read_text(), "per_day_usd", daily_budget)
            if ok:
                p.write_text(txt)
                console.print(
                    f"  [green]✓[/green] Daily ad budget → [bold]${daily_budget:,.2f}/day[/bold]  [dim]{p.name}[/dim]"
                )
            else:
                console.print(
                    f"  [yellow]·[/yellow] Couldn't find per_day_usd in {p.name} — edit with "
                    f"[cyan]veto-agents policy edit adbuyer[/cyan]"
                )
        else:
            console.print(f"  [yellow]·[/yellow] {p.name} not found — install adbuyer first.")
    if creative_cap is not None:
        creative_cap = _validate_budget(
            creative_cap, label="Per-creative cap", flag="--creative-cap",
            max_usd=_MAX_CREATIVE_CAP_USD,
        )
        p = cfg_module.policies_dir() / "adbuyer-creative.yaml"
        if p.exists():
            txt, ok = _set_yaml_scalar(p.read_text(), "per_transaction_usd", creative_cap)
            if ok:
                p.write_text(txt)
                console.print(
                    f"  [green]✓[/green] Per-generation creative cap → [bold]${creative_cap:,.2f}[/bold]  [dim]{p.name}[/dim]"
                )
            else:
                console.print(
                    f"  [yellow]·[/yellow] Couldn't find per_transaction_usd in {p.name}."
                )
        else:
            console.print(f"  [yellow]·[/yellow] {p.name} not found — install adbuyer first.")


def _ask_optional_secret(prompt: str) -> str:
    """Password-masked, Enter-to-skip prompt that treats EOF/non-TTY as 'skip'
    (returns "") instead of raising EOFError. For optional secret inputs so a
    piped/non-TTY run never dead-ends on an optional field."""
    try:
        return Prompt.ask(prompt, default="", password=True).strip()
    except EOFError:
        return ""


def _confirm_optional(prompt: str, console: Console, *, default: bool = False) -> bool:
    """Yes/no for an OPTIONAL step. EOF/non-TTY → the default (never raises,
    never a bare 'Aborted.'). Keeps optional onboarding steps skippable in
    scripts and piped input."""
    try:
        return Confirm.ask(prompt, default=default)
    except EOFError:
        return default


def _prompt_env_secret(key: str, label: str, url: str | None, path, console: Console) -> bool:
    """Interactive: explain, open the signup URL, prompt, write to a *.env file.
    Returns True if a value was saved. Never prints the value. EOF/non-TTY is
    treated as 'skip' (no traceback)."""
    console.print(f"  [bold]{label}[/bold]  [dim]{key}[/dim]")
    if url:
        console.print(f"  [dim]Get one:[/dim] [cyan]{url}[/cyan]")
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    value = _ask_optional_secret(f"  Paste {key} (or Enter to skip)")
    if value:
        _upsert_env_var(path, key, value)
        console.print(f"  [green]✓[/green] Saved [bold]{key}[/bold] → [dim]~/.veto/{path.name}[/dim]")
        return True
    console.print("  [dim]Skipped (optional).[/dim]")
    return False


# The sensible hosted default when a provider is unknown/empty. Hosted (not a
# keyless-local endpoint) so a first-run user with no local model still works;
# a missing key then raises the friendly NoLLMKeyError, never a raw 401.
_DEFAULT_LLM_PROVIDER = "claude"


def _provider_or_default(name: str | None, console: Console):
    """Resolve a provider name to an LLMProvider with ONE consistent policy
    (M-5): a known name is honored; an empty/whitespace/unknown name warns once
    and falls back to the sensible hosted default. Never silently maps an
    unknown value to a different provider."""
    key = (name or "").strip()
    p = llm_providers.get(key) if key else None
    if p is not None:
        return p
    shown = f"'{name}'" if name else "empty"
    console.print(
        f"  [yellow]·[/yellow] Unknown/blank LLM provider ({shown}) — "
        f"defaulting to [cyan]{_DEFAULT_LLM_PROVIDER}[/cyan]. "
        f"[dim]Choices: {', '.join(llm_providers.all_names())}.[/dim]"
    )
    return llm_providers.get(_DEFAULT_LLM_PROVIDER)


def _choose_llm_provider(cfg, requested: str | None, non_interactive: bool, console: Console):
    """Return the chosen LLMProvider. Honors an explicit request (flag/env),
    else prompts (interactive) or defaults (non-interactive). Unknown/empty
    values always warn + fall back to the same sensible hosted default (M-5)."""
    if requested is not None:
        # An explicit flag/env value was given — resolve it consistently.
        return _provider_or_default(requested, console)
    if non_interactive:
        # No flag: fall back to the persisted choice, warning if it's unusable.
        return _provider_or_default(getattr(cfg, "llm_provider", None), console)

    provider_list = list(llm_providers.PROVIDERS.values())
    tbl = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    tbl.add_column("idx", style="dim", no_wrap=True, width=3)
    tbl.add_column("id", style="cyan bold", no_wrap=True)
    tbl.add_column("label")
    for i, prov in enumerate(provider_list, 1):
        tbl.add_row(str(i), prov.name, prov.label)
    console.print(tbl)
    console.print()
    default_name = cfg.llm_provider if cfg.llm_provider in llm_providers.PROVIDERS else "claude"
    default_idx = next(
        (str(i) for i, p in enumerate(provider_list, 1) if p.name == default_name), "3"
    )
    pick_choices = llm_providers.all_names() + [str(i) for i in range(1, len(provider_list) + 1)]
    raw = Prompt.ask(
        f"  Pick one [dim](1-{len(provider_list)} or name)[/dim]",
        choices=pick_choices,
        default=default_idx,
        show_choices=False,
    )
    return provider_list[int(raw) - 1] if raw.isdigit() else llm_providers.get(raw)


def _persist_provider_key(provider, cfg, non_interactive: bool, console: Console) -> None:
    """Collect + persist the chosen brain's API key to ~/.veto/creative.env
    (the file the director/controller resolver reads first). Auto-detects an
    existing key and reuses it. Never prints the value."""
    from .agents.adbuyer.creative import creds as _cc

    env_var = provider.env_var
    if not env_var:
        if provider.name == "ollama":
            console.print(
                f"  [dim]Local Ollama at {cfg.llm_endpoint} — no key needed. Make sure it's "
                f"running and you've run [/dim][cyan]ollama pull {cfg.llm_model}[/cyan][dim].[/dim]"
            )
        return

    existing = _cc.resolve(env_var, cfg)
    if existing:
        console.print(f"  [green]✓[/green] [bold]{env_var}[/bold] already configured — using it.")
        # If it's only in the ambient shell (ephemeral), persist so it survives.
        if os.environ.get(env_var):
            _upsert_env_var(_cc.CREATIVE_ENV_PATH, env_var, os.environ[env_var])
        return

    if non_interactive:
        val = os.environ.get(env_var)
        if val:
            _upsert_env_var(_cc.CREATIVE_ENV_PATH, env_var, val)
            console.print(f"  [green]✓[/green] Saved [bold]{env_var}[/bold] → [dim]~/.veto/creative.env[/dim]")
        else:
            console.print(
                f"  [yellow]·[/yellow] {env_var} not provided (non-interactive). Export it or add it "
                f"to ~/.veto/creative.env — the brain needs it to run."
            )
        return

    _prompt_env_secret(env_var, provider.label, provider.signup_url, _cc.CREATIVE_ENV_PATH, console)


def _collect_creative_keys(cfg, non_interactive: bool, console: Console) -> None:
    """Step 3 — optional creative providers → ~/.veto/creative.env."""
    from .agents.adbuyer.creative import creds as _cc

    if non_interactive:
        for key in _CREATIVE_ENV_KEYS:
            val = os.environ.get(key)
            if val:
                _upsert_env_var(_cc.CREATIVE_ENV_PATH, key, val)
                console.print(f"  [green]✓[/green] Saved [bold]{key}[/bold] → [dim]~/.veto/creative.env[/dim]")
        return

    # OpenAI image (often already set from Step 2 if the brain is openai).
    if _cc.resolve("OPENAI_API_KEY", cfg):
        console.print("  [green]✓[/green] OPENAI_API_KEY set — hero image (gpt-image-1) ready.")
    else:
        _prompt_env_secret(
            "OPENAI_API_KEY",
            "Hero image (gpt-image-1) — optional; free fal.ai over x402 is used if you skip",
            "https://platform.openai.com/api-keys",
            _cc.CREATIVE_ENV_PATH,
            console,
        )

    # ── VIDEO (Higgsfield) + VOICE (ElevenLabs) — optional, only if you want
    # video/voice ads. Each is Enter-to-skip; nothing here is required. Values
    # go to ~/.veto/creative.env (0600, never echoed). Skipped cleanly if the
    # user says no or hits EOF — the studio still makes copy + image without it.
    console.print(
        "  [dim]Video + voice are optional — only if you want video/voice ads. "
        "Press Enter at any prompt to skip.[/dim]"
    )

    # Higgsfield video (needs both halves, or a combined credential).
    if _cc.higgsfield_credentials(cfg):
        console.print("  [green]✓[/green] Higgsfield video already configured.")
    elif _confirm_optional("  Add Higgsfield VIDEO keys? [dim](optional)[/dim]", console):
        try:
            import webbrowser

            webbrowser.open("https://higgsfield.ai")
        except Exception:
            pass
        kid = _ask_optional_secret("  HIGGSFIELD_API_KEY (or Enter to skip)")
        ksec = _ask_optional_secret("  HIGGSFIELD_API_SECRET (or Enter to skip)")
        if kid:
            _upsert_env_var(_cc.CREATIVE_ENV_PATH, "HIGGSFIELD_API_KEY", kid)
            console.print("  [green]✓[/green] Saved [bold]HIGGSFIELD_API_KEY[/bold] → [dim]~/.veto/creative.env[/dim]")
        if ksec:
            _upsert_env_var(_cc.CREATIVE_ENV_PATH, "HIGGSFIELD_API_SECRET", ksec)
            console.print("  [green]✓[/green] Saved [bold]HIGGSFIELD_API_SECRET[/bold] → [dim]~/.veto/creative.env[/dim]")
        if not (kid or ksec):
            console.print("  [dim]Skipped video (optional).[/dim]")

    # ElevenLabs voice.
    if _cc.resolve("ELEVENLABS_API_KEY", cfg):
        console.print("  [green]✓[/green] ElevenLabs voice already configured.")
    elif _confirm_optional("  Add an ElevenLabs VOICE key? [dim](optional)[/dim]", console):
        _prompt_env_secret(
            "ELEVENLABS_API_KEY",
            "Voiceover (ElevenLabs)",
            "https://elevenlabs.io/app/settings/api-keys",
            _cc.CREATIVE_ENV_PATH,
            console,
        )


def _collect_meta_keys(cfg, non_interactive: bool, console: Console) -> None:
    """Step 4 — optional Meta ad-account creds → ~/.veto/meta.env."""
    from .agents.adbuyer import meta_env as _me

    if non_interactive:
        for key in _META_ENV_KEYS:
            val = os.environ.get(key)
            if val:
                _upsert_env_var(_me.META_ENV_PATH, key, val)
                console.print(f"  [green]✓[/green] Saved [bold]{key}[/bold] → [dim]~/.veto/meta.env[/dim]")
        return

    m = _me.describe(_me.load_meta(cfg))
    if all(m.values()):
        console.print("  [green]✓[/green] Meta already connected (token + ad account + page).")
        return
    # Optional step: EOF/non-TTY (piped setup) must SKIP, not dead-end the wizard
    # right before the budget step. _confirm_optional treats EOF as the default (no).
    if not _confirm_optional(
        "  Connect a Meta ad account now? [dim](or skip and use --mock)[/dim]",
        console,
    ):
        console.print(
            "  [dim]Skipped — run with [/dim][cyan]--mock[/cyan][dim] anytime to try the full loop "
            "with no account, or re-run this setup later.[/dim]"
        )
        return
    console.print(
        "  [dim]Tip: use a SANDBOX ad account (identical API, never spends real money).[/dim]"
    )
    specs = [
        ("META_ACCESS_TOKEN", "System-User token (scope: ads_management + ads_read)",
         "https://developers.facebook.com/apps"),
        ("META_AD_ACCOUNT_ID", "Ad account id (act_… or a bare number)",
         "https://business.facebook.com/settings/ad-accounts"),
        ("META_PAGE_ID", "Facebook Page id (creative must attach to a Page)",
         "https://business.facebook.com/settings/pages"),
    ]
    for key, label, url in specs:
        _prompt_env_secret(key, label, url, _me.META_ENV_PATH, console)


def _adbuyer_summary(console: Console) -> None:
    """Presence-only recap (booleans, never values) + the exact next commands."""
    from .agents.adbuyer import meta_env as _me
    from .agents.adbuyer.creative import creds as _cc

    cfg = cfg_module.load()
    d = _cc.describe(cfg)
    m = _me.describe(_me.load_meta(cfg))
    meta_ready = all(m.values())

    def mark(b: bool) -> str:
        return "[green]✓[/green]" if b else "[dim]—[/dim]"

    console.print("\n[bold]You're set up.[/bold]  [dim](presence only — no keys are shown)[/dim]\n")
    t = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    t.add_column("k", style="dim", no_wrap=True)
    t.add_column("v")
    t.add_row(
        "Veto account",
        "[green]signed in[/green]" if cfg.api_key else "[yellow]local mode[/yellow]  [dim](sign in for signed receipts + live spend)[/dim]",
    )
    t.add_row("Brain (LLM)", f"[cyan]{cfg.llm_provider}[/cyan] · {cfg.llm_model or '—'}")
    t.add_row(
        "Image",
        mark(d["openai_image"]) + ("  [dim]OpenAI[/dim]" if d["openai_image"] else "  [dim]free fal.ai x402 fallback[/dim]"),
    )
    t.add_row("Video", mark(d["higgsfield_video"]) + ("  [dim]Higgsfield[/dim]" if d["higgsfield_video"] else "  [dim]optional[/dim]"))
    t.add_row("Voice", mark(d["elevenlabs_voice"]) + ("  [dim]ElevenLabs[/dim]" if d["elevenlabs_voice"] else "  [dim]optional[/dim]"))
    t.add_row(
        "Meta ads",
        "[green]connected[/green]" if meta_ready else "[yellow]not connected[/yellow]  [dim](use --mock to try without an account)[/dim]",
    )
    t.add_row(
        "Wallet",
        "[green]connected[/green]" if cfg.wallet_address else "[dim]not set — decision-only until funded[/dim]",
    )
    day = _read_policy_scalar("adbuyer.yaml", "per_day_usd", 150.0)
    cap = _read_policy_scalar("adbuyer-creative.yaml", "per_transaction_usd", 0.50)
    t.add_row("Guardrails", f"[bold]${day:,.2f}/day[/bold] ad budget · [bold]${cap:,.2f}[/bold] per creative")
    console.print(t)

    console.print("\n[bold]Next — try it:[/bold]")
    console.print(
        '  [cyan]veto-agents create "premium cold-brew for busy founders, launch week"[/cyan]'
        "   [dim]creative only — no Meta needed[/dim]"
    )
    console.print(
        "  [cyan]veto-agents adbuyer --goal 'US traffic to https://mysite.com, keep CPC under $1' --once --mock[/cyan]"
        "   [dim]full loop, no real spend[/dim]"
    )
    console.print(
        "  [cyan]claude mcp add veto-agents -- veto-agents mcp[/cyan]"
        "   [dim]drive it from Claude Code[/dim]\n"
    )


@app.command("adbuyer-setup")
def adbuyer_setup(
    ctx: typer.Context,
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-n",
        help="No prompts. Reads answers from flags + env vars (see below). Great "
             "for scripts/CI. Secrets are read from their normal env-var names "
             "(OPENAI_API_KEY, META_ACCESS_TOKEN, …) — never passed as flags.",
    ),
    llm_provider: Optional[str] = typer.Option(
        None, "--llm-provider",
        help="Brain to use (claude / openai / ollama / hermes / …). "
             "Default: claude. Env: VETO_SETUP_LLM_PROVIDER.",
    ),
    daily_budget: Optional[float] = typer.Option(
        None, "--daily-budget",
        help="Daily ad budget in USD → caps.per_day_usd. Env: VETO_SETUP_DAILY_BUDGET.",
    ),
    creative_cap: Optional[float] = typer.Option(
        None, "--creative-cap",
        help="Max USD per creative generation → creative caps.per_transaction_usd. "
             "Env: VETO_SETUP_CREATIVE_CAP.",
    ),
    skip_login: bool = typer.Option(
        False, "--skip-login", help="Skip the Veto sign-in step. Env: VETO_SETUP_SKIP_LOGIN=1.",
    ),
    skip_wallet: bool = typer.Option(
        False, "--skip-wallet", help="Skip the optional funding-wallet handoff. Env: VETO_SETUP_SKIP_WALLET=1.",
    ),
) -> None:
    """Guided setup for the Veto-governed ad / media buyer.

    Walks you through, one step at a time: sign in → pick a brain (LLM) →
    optional creative providers (image/video/voice) → optional Meta ad account
    → budget guardrails. Each step explains what it's for and where to get the
    value, and writes to the right place: the secret .env files
    (~/.veto/creative.env, ~/.veto/meta.env) are created 0600; non-secret
    settings go to config.yaml and the policy caps. No secret is ever printed.

    Scriptable / non-interactive:
      veto-agents adbuyer-setup -n --llm-provider claude --daily-budget 25 --creative-cap 0.25

    In -n mode, secrets are read from their normal env-var names, e.g.:
      ANTHROPIC_API_KEY / OPENAI_API_KEY / ELEVENLABS_API_KEY
      HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET (or HIGGSFIELD_CREDENTIALS)
      META_ACCESS_TOKEN / META_AD_ACCOUNT_ID / META_PAGE_ID
    """
    # Resolve non-interactive + skips from flags OR env (flags win when set).
    if _env_truthy("VETO_SETUP_NONINTERACTIVE"):
        non_interactive = True
    skip_login = skip_login or _env_truthy("VETO_SETUP_SKIP_LOGIN")
    skip_wallet = skip_wallet or _env_truthy("VETO_SETUP_SKIP_WALLET")
    if llm_provider is None:
        llm_provider = os.environ.get("VETO_SETUP_LLM_PROVIDER") or None
    if daily_budget is None and os.environ.get("VETO_SETUP_DAILY_BUDGET"):
        try:
            daily_budget = float(os.environ["VETO_SETUP_DAILY_BUDGET"])
        except ValueError:
            pass
    if creative_cap is None and os.environ.get("VETO_SETUP_CREATIVE_CAP"):
        try:
            creative_cap = float(os.environ["VETO_SETUP_CREATIVE_CAP"])
        except ValueError:
            pass

    banner.render(console, subtitle="Ad-buyer setup")
    console.print(
        "[bold]Let's get your Veto-governed ad buyer running.[/bold]  "
        "[dim]One step at a time — most steps are optional and skippable.[/dim]"
    )

    cfg = cfg_module.load()

    # ── Step 0 — make sure the agent + its two policies are installed ──
    if "adbuyer" not in _installed_agents(cfg):
        console.print("\n[dim]Installing the adbuyer agent + its policies…[/dim]")
        ctx.invoke(install, name="adbuyer", skip_creds=True, no_banner=True)
        cfg = cfg_module.load()

    # ── Step 1 — Veto sign-in ──
    console.print("\n[bold]Step 1 — Sign in to Veto[/bold]")
    console.print(
        "  [dim]Why: Veto authorizes every spend and signs a receipt. This is the "
        "account those receipts belong to.[/dim]"
    )
    if cfg.api_key:
        console.print(f"  [green]✓[/green] Already signed in as [cyan]{cfg.email or cfg.agent_id}[/cyan].")
    elif skip_login or non_interactive:
        console.print(
            "  [yellow]·[/yellow] Sign-in skipped. Live spends need an account — run "
            "[cyan]veto-agents login[/cyan] later. You can still use [cyan]--mock[/cyan]/[cyan]--dry-run[/cyan]."
        )
    else:
        if _do_login(console, cfg):
            _render_signed_in(console, cfg)
        cfg = cfg_module.load()
        if not cfg.api_key:
            console.print(
                "  [yellow]·[/yellow] No account yet — continuing so you can configure the rest. "
                "Sign in later for live spends."
            )

    # ── Step 2 — Brain (LLM): the thing that thinks. Must be right. ──
    console.print("\n[bold]Step 2 — Pick the brain (LLM)[/bold]")
    console.print(
        "  [dim]Why: the director LLM turns your brief into ad copy + one coherent "
        "creative concept, and the buyer LLM reads performance and decides budget "
        "moves. Required for the agent to think.[/dim]\n"
    )
    provider = _choose_llm_provider(cfg, llm_provider, non_interactive, console)
    cfg.llm_provider = provider.name
    if provider.name == "custom":
        endpoint = os.environ.get("VETO_SETUP_LLM_ENDPOINT")
        model = os.environ.get("VETO_SETUP_LLM_MODEL")
        if not non_interactive:
            endpoint = (
                Prompt.ask("  Custom endpoint URL (OpenAI-compatible)", default=cfg.llm_endpoint or endpoint or "").strip()
                or endpoint
            )
            model = Prompt.ask("  Model name (e.g. llama3.1:70b)", default=cfg.llm_model or model or "").strip() or model
        if endpoint:
            cfg.llm_endpoint = endpoint
        if model:
            cfg.llm_model = model
    else:
        _apply_provider_endpoint(cfg, provider)  # H-4: llm_endpoint only for keyless-local
    cfg_module.save(cfg)  # non-secret provider/endpoint/model → config.yaml
    console.print(
        f"  [green]✓[/green] Brain: [cyan]{cfg.llm_provider}[/cyan] · model "
        f"[cyan]{cfg.llm_model or '—'}[/cyan]  [dim]→ config.yaml[/dim]"
    )
    _persist_provider_key(provider, cfg, non_interactive, console)

    # ── Step 3 — Creative providers (optional) ──
    console.print("\n[bold]Step 3 — Creative providers[/bold]  [dim](optional)[/dim]")
    console.print(
        "  [dim]Why: richer assets. All optional — with none set, images still "
        "generate via free fal.ai over x402.[/dim]"
    )
    _collect_creative_keys(cfg, non_interactive, console)
    d = __import__("veto_agents.agents.adbuyer.creative.creds", fromlist=["describe"]).describe(cfg_module.load())
    console.print(
        "  [bold]Studio can make:[/bold]  copy [green]✓[/green]  image [green]✓[/green]  "
        f"video {'[green]✓[/green]' if d['higgsfield_video'] else '[dim]—[/dim]'}  "
        f"voice {'[green]✓[/green]' if d['elevenlabs_voice'] else '[dim]—[/dim]'}"
    )

    # ── Step 4 — Meta ad account (optional) ──
    console.print("\n[bold]Step 4 — Connect Meta (Facebook/Instagram) ads[/bold]  [dim](optional)[/dim]")
    console.print(
        "  [dim]Why: needed to launch REAL campaigns. Skip it and use [/dim][cyan]--mock[/cyan]"
        "[dim] to run the whole observe→decide→Veto→act loop with no account and no spend.[/dim]"
    )
    _collect_meta_keys(cfg, non_interactive, console)

    # ── Step 5 — Budget & guardrails ──
    console.print("\n[bold]Step 5 — Budget & guardrails[/bold]")
    console.print(
        "  [dim]Why: Veto blocks any spend above these caps. Sensible defaults ship; "
        "set your own daily ad budget + per-generation creative cap.[/dim]"
    )
    if non_interactive:
        _patch_policy_caps(daily_budget, creative_cap, console)
        if daily_budget is None and creative_cap is None:
            console.print(
                "  [dim]Keeping policy defaults. Edit with [/dim][cyan]veto-agents policy edit adbuyer[/cyan][dim].[/dim]"
            )
    else:
        cur_day = _read_policy_scalar("adbuyer.yaml", "per_day_usd", 150.0)
        cur_cap = _read_policy_scalar("adbuyer-creative.yaml", "per_transaction_usd", 0.50)
        db = daily_budget
        cc = creative_cap
        if db is None:
            db = _prompt_budget(
                "  Daily ad budget (USD)", f"{cur_day:.0f}",
                label="Daily ad budget", flag="--daily-budget",
                max_usd=_MAX_DAILY_BUDGET_USD, console=console,
            )
        if cc is None:
            cc = _prompt_budget(
                "  Max USD per creative generation", f"{cur_cap:.2f}",
                label="Per-creative cap", flag="--creative-cap",
                max_usd=_MAX_CREATIVE_CAP_USD, console=console,
            )
        _patch_policy_caps(db, cc, console)

    # ── Wallet (optional handoff) ──
    if not skip_wallet and not non_interactive and not cfg_module.load().wallet_address:
        console.print("\n[bold]Wallet[/bold]  [dim](optional)[/dim]")
        console.print(
            "  [dim]Funds the Veto-guarded Safe that pays for creative micro-spends (x402). "
            "Skip → decision-only until you fund it.[/dim]"
        )
        if cfg_module.load().api_key and _confirm_optional(
            "  Set up a funding wallet now?", console
        ):
            wallet_setup_module.run(console)

    # ── Finish ──
    _adbuyer_summary(console)


# ─── list ─────────────────────────────────────────────────────────────────


@app.command(name="list")
def list_cmd() -> None:
    """Show the catalog of installable agents."""
    cfg = cfg_module.load()
    installed = set(_installed_agents(cfg))

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
    no_banner: bool = typer.Option(False, "--no-banner", hidden=True),
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
    # Normalize once so a bare-string config can't cause a substring match on
    # membership or a char-by-char append later (L-5).
    cfg.installed_agents = _installed_agents(cfg)

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
    # Suppressed when driven by the first-run wizard (which already showed
    # the banner) — avoids the triple-banner pile-up on fresh install.
    if not no_banner:
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

        # adbuyer ships a SECOND, SEPARATE policy for creative micro-spends
        # (adbuyer-creative) kept distinct from its ad-budget policy so a $0.01
        # image isn't judged against a $50 budget cap. Install it alongside so
        # `veto-agents policy edit adbuyer-creative` works.
        if name == "adbuyer":
            creative_policy = _bundled_subpolicy_path("adbuyer.creative")
            if creative_policy is not None and creative_policy.is_file():
                creative_user_policy = cfg_module.policies_dir() / "adbuyer-creative.yaml"
                creative_user_policy.write_text(creative_policy.read_text())
                console.print(
                    f"[green]✓[/green] Creative policy installed → "
                    f"[dim]{creative_user_policy}[/dim]"
                )

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
        "adbuyer":  "US traffic campaign to https://mysite.com, $20/day",
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


# Keys that belong in a ~/.veto/*.env file (shared with the studio/meta
# resolvers) rather than the veto-agents keychain. `creds set` routes these to
# the right file so the add-later path lands exactly where setup writes them.
def _env_file_for_key(env_var: str):
    """Return the ~/.veto/*.env Path a creative/meta key should live in, or
    None for a generic keychain credential."""
    if env_var in _CREATIVE_ENV_KEYS:
        from .agents.adbuyer.creative import creds as _cc
        return _cc.CREATIVE_ENV_PATH
    if env_var in _META_ENV_KEYS:
        from .agents.adbuyer import meta_env as _me
        return _me.META_ENV_PATH
    return None


@creds_app.command("list")
def creds_list() -> None:
    """Show which credentials are saved — presence only, values never shown."""
    printed = False

    # ── Generic keychain credentials (masked, back-compat) ──
    saved = creds_module.load()
    if saved:
        printed = True
        console.print("\n[bold]Saved credentials[/bold]  [dim](keychain)[/dim]\n")
        for env_var, val in sorted(saved.items()):
            mask = val[:6] + "…" + val[-4:] if len(val) > 12 else "***"
            env_override = " [dim](overridden by shell env)[/dim]" if os.environ.get(env_var) else ""
            console.print(f"  [cyan]{env_var:<24}[/cyan]  {mask}{env_override}")

    # ── Creative + Meta keys (presence only — never a value) ──
    from .agents.adbuyer.creative import creds as _cc

    def _present(env_var: str) -> bool:
        return bool(_cc.resolve(env_var, None))

    creative_rows = [
        ("OPENAI_API_KEY", "image (gpt-image-1)"),
        ("HIGGSFIELD_API_KEY", "video (Higgsfield)"),
        ("HIGGSFIELD_API_SECRET", "video (Higgsfield)"),
        ("ELEVENLABS_API_KEY", "voice (ElevenLabs)"),
    ]
    if any(_present(k) for k, _ in creative_rows) or not saved:
        printed = True
        console.print("\n[bold]Creative providers[/bold]  [dim](presence only)[/dim]\n")
        for env_var, what in creative_rows:
            mark = "[green]✓ set[/green]" if _present(env_var) else "[dim]— not set[/dim]"
            console.print(f"  [cyan]{env_var:<24}[/cyan]  {mark}  [dim]{what}[/dim]")

    if not printed:
        console.print("[dim]No credentials saved yet. Install an agent or run "
                      "[/dim][cyan]veto-agents adbuyer-setup[/cyan][dim] to set them up.[/dim]")
    console.print()


@creds_app.command("set")
def creds_set(
    env_var: str = typer.Argument(..., help="The env var name, e.g. HIGGSFIELD_API_KEY"),
    value: str = typer.Argument(None, help="The value (omit to be prompted; masked)"),
) -> None:
    """Save or update a single credential (value never echoed).

    Creative keys (OPENAI_API_KEY, HIGGSFIELD_API_KEY, HIGGSFIELD_API_SECRET,
    ELEVENLABS_API_KEY) and Meta keys land in the ~/.veto/*.env file the agent
    resolvers read; everything else goes to the veto-agents keychain."""
    if value is None:
        try:
            value = Prompt.ask(f"Value for {env_var}", password=True).strip()
        except EOFError:
            console.print("[yellow]·[/yellow] No input. Nothing saved.")
            raise typer.Exit(1)
    if not value:
        console.print("[yellow]·[/yellow] Empty value. Nothing saved.")
        return
    env_path = _env_file_for_key(env_var)
    if env_path is not None:
        _upsert_env_var(env_path, env_var, value)
        console.print(f"[green]✓[/green] Saved [bold]{env_var}[/bold] → [dim]~/.veto/{env_path.name}[/dim].")
    else:
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
    cfg.installed_agents = _installed_agents(cfg)  # coerce bare string → list (L-5)
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
def adbuyer(
    goal: str = typer.Option(
        ..., "--goal", "-g",
        help="Standing objective the buyer optimizes toward, e.g. "
             "'US traffic to mysite.com, keep CPC under $1'.",
    ),
    interval: int = typer.Option(
        0, "--interval", "-i", min=0,
        help="Minutes between OBSERVE cycles (ignored with --once). "
             "0 (default) = use the policy's ad_ops.observe_interval_minutes "
             "(360). Observing is cheap; the readiness gate — not the interval — "
             "is what prevents premature action.",
    ),
    once: bool = typer.Option(
        False, "--once",
        help="Run a single decide+authorize cycle and exit (no loop).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Decide + run the Veto authorize gates but skip all Meta writes.",
    ),
    mock: bool = typer.Option(
        False, "--mock",
        help="Mimic Meta OFFLINE — no real ad account, no real spend. Runs the "
             "full observe->decide->discipline->Veto->act loop against seeded, "
             "evolving fake campaigns. The REAL Veto + discipline gates still run "
             "on every action (decision_only is free). Great with no live "
             "campaigns; add --no-llm to run with no model key either.",
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm",
        help="Use the pure-rules heuristic brain instead of the LLM (no model "
             "key required). Implied by --mock when no LLM key is configured.",
    ),
) -> None:
    """Autonomous Veto-governed Meta (FB/IG) ad buyer.

    Runs a decide -> authorize -> (optionally) act loop every --interval minutes
    against a standing --goal. Every dollar is gated by Veto before Meta is
    touched. Deploy once and walk away — Veto is the ongoing guardrail on the
    agent's OWN decisions, not a per-command consent gate.

    Examples:
      veto-agents adbuyer --goal 'US traffic to https://mysite.com, keep CPC under $1'
      veto-agents adbuyer -g 'grow signups, cap $150/day' --once --dry-run
      veto-agents adbuyer -g 'grow signups, US, up to $30/day' --mock --once
    """
    cfg = cfg_module.load()
    # --mock mimics Meta with no account, no spend — the whole "no account, no
    # spend" try-it promise. It must RUN for a signed-out / non-TTY user: we do
    # NOT gate it on install or sign-in here (M-6). The controller runs it
    # locally and handles the no-key authorize with a "sign in for signed
    # receipts" note. A real (non-mock) run still needs the agent installed.
    if not mock and "adbuyer" not in _installed_agents(cfg):
        console.print(
            "[red]✗[/red] [bold]adbuyer[/bold] is not installed. "
            "Run [cyan]veto-agents install adbuyer[/cyan] first."
        )
        raise typer.Exit(1)

    from .agents.adbuyer.agent import run_daemon
    run_daemon(
        cfg,
        console,
        goal=goal,
        interval_minutes=interval,
        once=once,
        dry_run=dry_run,
        mock=mock,
        no_llm=no_llm,
    )


@app.command()
def create(
    brief: str = typer.Argument(
        ..., help="Product/campaign brief to build a creative ad package from."
    ),
    image_provider: _ImageProvider = typer.Option(
        _ImageProvider.openai, "--image-provider",
        case_sensitive=False,
        help="Hero image provider: 'openai' (BYO OPENAI_API_KEY) or 'fal' "
             "(free x402). 'openai' falls back to 'fal' automatically if no key. "
             "A typo can't reach the paid path — only {openai, fal} are accepted.",
    ),
    video: Optional[bool] = typer.Option(
        None, "--video/--no-video",
        help="Include a hero video (Higgsfield, BYO key). Default: auto — "
             "on only if a Higgsfield key is configured.",
    ),
    voice: Optional[bool] = typer.Option(
        None, "--voice/--no-voice",
        help="Include a voiceover (ElevenLabs, BYO key). Default: auto — "
             "on only if an ElevenLabs key is configured.",
    ),
    all_assets: bool = typer.Option(
        False, "--all",
        help="Attempt every asset (copy+image+video+voice). Missing-key assets "
             "still skip cleanly with a note.",
    ),
    out: str = typer.Option(
        None, "--out",
        help="Output root folder (default: ~/Downloads/veto-studio/).",
    ),
) -> None:
    """Standalone creative studio — turn a brief into a coherent ad package.

    Runs the LLM creative DIRECTOR → copy + hero image (+ optional video/voice),
    all derived from ONE creative concept so every asset matches. Each PAID
    generation is gated by Veto BEFORE the provider is called (deny/escalate →
    skip + receipt). NO Meta credentials required — this is the creative stage;
    placing the ad on Meta is a separate, later step.

    Keys are BYO, read from ~/.veto/creative.env (or env / keychain):
      OPENAI_API_KEY, HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET, ELEVENLABS_API_KEY.
    The director needs ANTHROPIC_API_KEY. Missing keys degrade gracefully.

    BRAND: set a brand profile once with `veto-agents brand set <url-or-file>` and
    every run follows it — concept, copy, and the image/video prompts match your
    brand's tone, colors, aesthetic, and forbidden list.

    Examples:
      veto-agents create "premium cold-brew coffee for busy founders, launch week"
      veto-agents create "eco running shoe" --image-provider fal --no-video
      veto-agents create "SaaS onboarding tool" --all
    """
    from pathlib import Path as _Path

    from .agents.adbuyer.creative import creds as creative_creds, studio

    cfg = cfg_module.load()
    d = creative_creds.describe(cfg)
    want = ["copy", "image"]
    inc_video = all_assets or (video if video is not None else d["higgsfield_video"])
    inc_voice = all_assets or (voice if voice is not None else d["elevenlabs_voice"])
    if inc_video:
        want.append("video")
    if inc_voice:
        want.append("voice")

    studio.run(
        brief,
        cfg,
        console,
        want=tuple(want),
        # .value → a plain "openai"/"fal" string; Typer already rejected any
        # other value, so a typo can never reach studio's paid path (H-1).
        image_provider=image_provider.value,
        out_root=_Path(out) if out else None,
    )


# ─── brand ──────────────────────────────────────────────────────────────


def _render_brand_table(profile, path=None) -> None:
    """Pretty, human-friendly view of a brand profile — never a wall of YAML."""
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    table.add_column("k", style="dim", no_wrap=True)
    table.add_column("v")

    def _row(label, value):
        if value:
            table.add_row(label, value)

    _row("Name", profile.name)
    _row("Product", profile.product)
    _row("One-liner", profile.one_liner)
    _row("Audience", profile.audience)
    _row("Tone", profile.tone)
    _row("Value props", " · ".join(profile.value_props))
    _row("Voice DO", ", ".join(profile.voice_dos))
    _row("Voice DON'T", ", ".join(profile.voice_donts))
    _row("Aesthetic", profile.aesthetic)
    if profile.colors:
        _row("Colors", "  ".join(f"{k}={v}" for k, v in profile.colors.items()))
    if profile.forbidden:
        table.add_row("Forbidden", f"[red]{', '.join(profile.forbidden)}[/red]")
    src = profile.source or {}
    if src.get("url") or src.get("file"):
        _row("Source", src.get("url") or src.get("file"))
    _row("Extracted", src.get("extracted_at"))
    if path is not None:
        _row("File", str(path))
    console.print(table)


@brand_app.command("set")
def brand_set(
    source: str = typer.Argument(
        ..., help="Brand website URL, or a local .txt/.md brand dump."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Save without the confirm prompt."
    ),
) -> None:
    """Extract a brand profile (one LLM call) and save it to ~/.veto/brand.yaml.

    Once set, `veto-agents create` and the ad-buyer follow it automatically:
    concept, copy, and the image/video prompts match the brand's tone, colors,
    aesthetic, and forbidden list. The saved file is plain, editable YAML.
    """
    from .agents.adbuyer.creative import brand as brand_mod  # lazy — house style

    cfg = cfg_module.load()
    is_url = source.strip().lower().startswith("http")
    verb = "Reading" if is_url else "Reading file"
    try:
        with console.status(f"[cyan]{verb} {source}… extracting brand profile…[/cyan]"):
            profile = brand_mod.extract(source, cfg)
    except brand_mod.BrandError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1)

    console.print("\n[bold]Here's what I learned:[/bold]")
    _render_brand_table(profile)

    if not yes and not Confirm.ask("\nSave this brand profile?", default=True):
        console.print("[dim]Not saved.[/dim]")
        raise typer.Exit(0)

    path = brand_mod.save_brand(profile)
    console.print(
        f"[green]✓[/green] Brand saved → {path}  "
        f"[dim](edit it anytime — it's plain YAML)[/dim]"
    )
    console.print(
        "[dim]veto-agents create / adbuyer now follow this brand automatically.[/dim]"
    )


@brand_app.command("show")
def brand_show() -> None:
    """Show the active brand profile (or how to set one)."""
    from .agents.adbuyer.creative import brand as brand_mod

    cfg = cfg_module.load()
    profile = brand_mod.load_brand(cfg)
    if profile is None:
        console.print(
            "[dim]No brand profile.[/dim] Set one:  "
            "[cyan]veto-agents brand set https://yourbrand.com[/cyan]"
        )
        raise typer.Exit(0)
    console.print("[bold]Brand profile[/bold]")
    _render_brand_table(profile, path=brand_mod.brand_path())


@brand_app.command("clear")
def brand_clear(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Delete without the confirm prompt."
    ),
) -> None:
    """Remove the brand profile — creative runs go back to brand-free."""
    from .agents.adbuyer.creative import brand as brand_mod

    path = brand_mod.brand_path()
    if not path.exists():
        console.print("[dim]No brand profile to remove.[/dim]")
        raise typer.Exit(0)
    if not yes and not Confirm.ask(f"Delete {path}?", default=False):
        console.print("[dim]Kept.[/dim]")
        raise typer.Exit(0)
    if brand_mod.clear():
        console.print(
            "[green]✓[/green] Brand profile removed — creative runs are brand-free again."
        )
    else:
        console.print("[yellow]·[/yellow] Nothing was removed.")


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
        "[dim](local receipts feed is on the roadmap — for now, see "
        "[/dim][cyan]veto-ai.com/receipts[/cyan][dim]. Signed-in? "
        "[/dim][cyan]veto-agents wallet[/cyan][dim] shows recent authorizations.)[/dim]"
    )


# ─── mcp server ──────────────────────────────────────────────────────────


@app.command()
def mcp() -> None:
    """Run the veto-agents MCP server over stdio.

    Exposes the media-buyer agent as Model Context Protocol tools so any MCP
    host (Claude Code, Claude Desktop, OpenClaw) can drive it —
    create_ad_creative, run_ad_cycle, get_campaigns. Veto governs every spend
    INSIDE each tool (fail-closed); the host LLM cannot bypass it. See docs/MCP.md.

    Wire it up (Claude Code):  claude mcp add veto-agents -- veto-agents mcp
    """
    # Lazy import so the rest of the CLI doesn't pull in the MCP SDK (or the
    # studio/controller graph) unless the server is actually launched.
    try:
        from .mcp_server import main as _run
    except ModuleNotFoundError as e:
        console.print(
            "[red]✗[/red] The MCP server needs the Model Context Protocol SDK.\n"
            "  Install it with: [cyan]pip install 'veto-agents[mcp]'[/cyan]  "
            f"[dim](missing: {e.name})[/dim]"
        )
        raise typer.Exit(1)
    _run()


# ─── helpers ─────────────────────────────────────────────────────────────


def _bundled_policy_path(name: str):
    """Path to the bundled default policy.yaml shipped with this package."""
    from importlib.resources import files
    try:
        return files(f"veto_agents.agents.{name}").joinpath("policy.yaml")
    except (ModuleNotFoundError, FileNotFoundError):
        return None  # type: ignore[return-value]


def _bundled_subpolicy_path(subpkg: str):
    """Path to a bundled policy.yaml shipped inside a SUB-package of an agent,
    e.g. `adbuyer.creative` → veto_agents/agents/adbuyer/creative/policy.yaml."""
    from importlib.resources import files
    try:
        return files(f"veto_agents.agents.{subpkg}").joinpath("policy.yaml")
    except (ModuleNotFoundError, FileNotFoundError):
        return None  # type: ignore[return-value]


def _run_agent(name: str, prompt: str, *, yes: bool) -> None:
    """Run an installed agent against a prompt. Enforces plan-then-execute."""
    cfg = cfg_module.load()
    if name not in _installed_agents(cfg):
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
