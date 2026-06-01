"""Wallet setup flow — `veto-agents wallet setup`.

Walks the user through funding-wallet provisioning with proper safety
content before the scary "send money to this address" moment. This is
its own subcommand (not part of the initial signup wizard) so the user
opts in *after* they've decided they like the product, not before.

Three options at setup time:
  1. Connect an existing wallet (Metamask / Phantom / Coinbase Wallet /
     anything WalletConnect-compatible). Most crypto-native users.
  2. Make me a new wallet, just use my email. Uses an embedded smart
     wallet provider (Privy / Coinbase Smart Wallet / etc.). Opens a
     web flow because embedded wallet provisioning is browser-based.
  3. Skip — operate in decision-only mode (still get receipts; just no
     on-chain spending governance until you fund a wallet).

For both 1 and 2, the deployed wallet is a **Safe** with Veto installed
as a Guard module. Owner-controlled, revocable, non-custodial by
construction — see the safety doc shown to the user.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from . import config as cfg_module
from .register import is_valid_evm_address


def fetch_account_wallet(cfg) -> dict | None:
    """Read the account's active governed wallet from main Veto.

    Wallet onboarding lives in the main Veto product (the shared
    veto-ai.com/wallet/setup flow); the account's wallet is the single
    source of truth. veto-agents consumes it here at spend time rather
    than keeping its own — so the wallet a user set up in Veto is the
    same one the agent spends from.

    Returns the wallet dict ({owner_address, privy_wallet_id,
    smart_account_address, chain, mode, status, label}) or None if the
    account has no wallet yet (caller should point the user at the
    shared setup flow) or the call fails (fail-safe — agent stays in
    decision-only mode rather than guessing a wallet).
    """
    import httpx

    if not getattr(cfg, "api_key", None):
        return None
    base = getattr(cfg, "veto_api_base", "https://veto-ai.com/api/v1").rstrip("/")
    if not base.endswith("/api/v1"):
        base = base.rstrip("/") + "/api/v1"
    try:
        r = httpx.get(
            f"{base}/wallet/",
            headers={"X-Veto-API-Key": cfg.api_key},
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json().get("wallet")
    except Exception:
        return None


# ── Safety + education content shown before any "send funds" step ──

SAFETY_EXPLAINER = """\
[bold]Your agent's wallet is a Safe — the same smart contract Vitalik, Coinbase, Aave, and most DAOs use.[/bold]

  • [bold green]You own it.[/bold green] Your wallet is set as the sole owner.
    Veto never holds keys. Veto can never move funds without your wallet authorizing
    the kind of spend first.

  • [bold green]Withdraw anytime.[/bold green] Run [cyan]veto-agents wallet withdraw[/cyan] and
    every cent goes back to your wallet in one transaction. Veto cannot block it.

  • [bold green]Use it without Veto.[/bold green] Your Safe lives at a normal contract
    address on Base. You can manage it from [cyan]app.safe.global[/cyan] anytime — even
    if Veto goes offline. We're one tool that talks to your Safe, not the only one.

  • [bold green]Veto's role is to check transactions.[/bold green] Before any agent spend,
    Veto verifies the action is within your policy (caps, allowlist, time windows).
    If it is, the Safe executes. If not, the Safe reverts. Veto is the bouncer,
    not the bank.

  • [bold yellow]Start small.[/bold yellow] We recommend $5–10 of USDC for your first
    fund. Top up later with one command. You don't have to load it all at once.

  • [bold]Open source.[/bold] The Safe contract is audited and battle-tested
    ([cyan]github.com/safe-global/safe-smart-account[/cyan]). Veto's Guard module is
    open and audited ([cyan]github.com/veto-protocol/contracts[/cyan]).
"""


def explain(console: Console) -> None:
    """Show the safety panel. Called before any 'send money' step."""
    console.print()
    console.print(
        Panel(
            Text.from_markup(SAFETY_EXPLAINER),
            title="[bold cyan]How your agent's wallet works[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


# ── Wallet-mode picker ──

WALLET_MODES = {
    "existing": "I have a crypto wallet — connect it (Metamask, Phantom, Coinbase Wallet, Rabby)",
    "embedded": "Make me a new wallet, just use my email — easiest if you're new to crypto",
    "skip":     "Skip for now — agents run in decision-only mode (no spending) until I fund one",
}


def pick_mode(console: Console) -> str:
    console.print("[bold]How would you like to set up your agent's wallet?[/bold]\n")
    for i, (key, label) in enumerate(WALLET_MODES.items(), 1):
        marker = {"existing": "🔌", "embedded": "📧", "skip": "⏭"}.get(key, "·")
        console.print(f"  [bold cyan]{i}.[/bold cyan] {marker}  {label}")
    console.print()

    choice_map = {str(i): k for i, k in enumerate(WALLET_MODES.keys(), 1)}
    choices = list(WALLET_MODES.keys()) + list(choice_map.keys())
    raw = Prompt.ask("Choose", choices=choices, default="existing", show_choices=False)
    return choice_map.get(raw, raw)


# ── Option 1: connect existing wallet ──

def connect_existing(console: Console, cfg) -> bool:
    """User pastes their EVM address. Returns True on success."""
    console.print("\n[bold]Connect your existing wallet[/bold]\n")
    console.print(
        "  Paste the EVM address of a wallet you control. We'll deploy a Safe on Base\n"
        "  owned by that address — Veto never sees your private key.\n"
    )
    while True:
        addr = Prompt.ask("Your wallet address (0x…)", default="").strip()
        if not addr:
            console.print("  [yellow]·[/yellow] Skipped. Run `veto-agents wallet setup` to come back.\n")
            return False
        if not is_valid_evm_address(addr):
            console.print("  [red]✗[/red] Not a valid EVM address. Try again.\n")
            continue
        cfg.wallet_address = addr
        cfg.wallet_chain = "base"
        cfg_module.save(cfg)
        console.print(f"  [green]✓[/green] Wallet linked: [cyan]{addr}[/cyan]")
        console.print(
            "  [dim](Per-user Safe deployment lands in v0.1 — we'll deploy your Safe\n"
            "  on the next CLI release. For now, the address is recorded and the\n"
            "  agent runs through Veto's authorize-only mode.)[/dim]\n"
        )
        return True


# ── Option 2: embedded wallet via web ──

def setup_embedded(console: Console, cfg) -> bool:
    """Device-code flow against veto-ai.com/wallet/setup.

    Talks to the backend endpoints from gateway/wallet_setup_views.py:
      1. POST /api/v1/wallet/setup/start/   — registers the device code
      2. open browser to the setup URL      — user signs in + deploys
      3. POST /api/v1/wallet/setup/poll/    — wait for the result

    On success, persists the deployed Safe + Guard + owner + chain to
    cfg. Returns True if the flow completed, False if the user aborted
    or the deploy failed.
    """
    import secrets as _secrets
    import time as _time
    import webbrowser

    import httpx

    if not cfg.api_key:
        console.print("[red]✗[/red] Not signed in. Run [cyan]veto-agents setup[/cyan] first.")
        return False

    api_base = cfg.veto_api_base.rstrip("/")
    headers = {"Content-Type": "application/json", "X-Veto-API-Key": cfg.api_key}
    device_code = "dc_" + _secrets.token_urlsafe(24)

    console.print("\n[bold]Create a new wallet, just with your email[/bold]\n")
    console.print(
        "  [dim]Sign in with your email — Privy creates an embedded wallet for you,\n"
        "  and Veto deploys a Safe smart account on Base Sepolia with the Veto\n"
        "  Guard module installed. No seed phrase. Gas is sponsored.[/dim]\n"
    )

    # 1. Start the session.
    try:
        r = httpx.post(
            f"{api_base}/wallet/setup/start/",
            json={"device_code": device_code},
            headers=headers,
            timeout=15.0,
        )
        r.raise_for_status()
        start_data = r.json()
    except httpx.HTTPStatusError as e:
        console.print(f"  [red]✗[/red] Couldn't start setup: HTTP {e.response.status_code}.")
        try:
            err = e.response.json().get("error")
            if err:
                console.print(f"    [dim]{err}[/dim]")
        except Exception:
            pass
        return False
    except Exception as e:
        console.print(f"  [red]✗[/red] Couldn't reach Veto: {e}")
        return False

    setup_url = start_data["setup_url"]

    # 2. Open browser. The page does the Privy + Safe deploy and POSTs
    # the result back to /complete/. We don't see it from here — we
    # just poll.
    console.print(f"  [green]✓[/green] Opening: [cyan]{setup_url}[/cyan]")
    try:
        webbrowser.open(setup_url)
    except Exception:
        # Browser open failed — the URL is still visible above so the
        # user can copy-paste it. Continue to the poll loop.
        pass
    console.print(
        "  [dim]Sign in and approve the deploy. We'll wait — usually 10–30s.[/dim]\n"
    )

    # 3. Poll for completion. 30-min hard ceiling matches the session
    # TTL; 2s interval matches the magic-link auth flow.
    deadline = _time.time() + 30 * 60
    interval = 2.0
    try:
        with console.status("[dim]waiting for the deploy to finish…[/dim]", spinner="dots"):
            while _time.time() < deadline:
                _time.sleep(interval)
                try:
                    pr = httpx.post(
                        f"{api_base}/wallet/setup/poll/",
                        json={"device_code": device_code},
                        timeout=15.0,
                    )
                except Exception:
                    continue
                if pr.status_code == 404:
                    console.print("\n  [red]✗[/red] Session not found. Re-run setup.")
                    return False
                if not pr.is_success:  # httpx Response has no .ok (that's requests)
                    continue
                data = pr.json()
                status = data.get("status")
                if status == "pending":
                    continue
                if status == "expired":
                    console.print("\n  [red]✗[/red] Setup timed out (30 min). Re-run.")
                    return False
                if status == "failed":
                    reason = data.get("failure_reason") or "unknown"
                    console.print(f"\n  [red]✗[/red] Deploy failed in browser: {reason}")
                    return False
                if status == "ready":
                    cfg.wallet_address = data["safe_address"]
                    cfg.guard_address = data["guard_address"]
                    cfg.safe_owner_address = data["owner_address"]
                    cfg.chain_id = int(data.get("chain_id", 0)) or None
                    cfg.wallet_chain = data.get("chain", cfg.wallet_chain)
                    cfg_module.save(cfg)
                    break
            else:
                console.print("\n  [red]✗[/red] Setup timed out (30 min). Re-run.")
                return False
    except KeyboardInterrupt:
        console.print("\n  [yellow]·[/yellow] Cancelled. Re-run setup when you're ready.")
        return False

    console.print(
        f"  [green]✓[/green] Wallet deployed.\n"
        f"    [dim]Safe :  [/dim][cyan]{cfg.wallet_address}[/cyan]\n"
        f"    [dim]Guard:  [/dim][cyan]{cfg.guard_address}[/cyan]\n"
        f"    [dim]Owner:  [/dim][cyan]{cfg.safe_owner_address}[/cyan]\n"
        f"    [dim]Chain:  {cfg.wallet_chain} (chain id {cfg.chain_id})[/dim]\n"
    )
    return True


# ── Option 3: skip ──

def skip_wallet(console: Console) -> None:
    console.print(
        "\n  [dim]Skipped. Your agents run in decision-only mode — Veto signs verdicts\n"
        "  on every action but no on-chain spending happens. Run [cyan]veto-agents wallet setup[/cyan]\n"
        "  whenever you're ready to enable real spending.[/dim]\n"
    )


# ── Entry point used by the CLI command ──

def run(console: Console) -> None:
    """Top-level wallet-setup wizard. Idempotent — re-running re-walks the choice."""
    from . import banner as banner_module
    cfg = cfg_module.load()
    if not cfg.api_key:
        console.print(
            "[red]✗[/red] You need to sign in first. Run [cyan]veto-agents setup[/cyan]."
        )
        return

    banner_module.render(console, subtitle="Setting up your agent's wallet")
    explain(console)

    if cfg.wallet_address:
        console.print(
            f"[yellow]·[/yellow] You already have a wallet linked: "
            f"[cyan]{cfg.wallet_address}[/cyan]"
        )
        if not Confirm.ask("  Replace it with a different setup?", default=False):
            return

    mode = pick_mode(console)
    if mode == "existing":
        connect_existing(console, cfg)
    elif mode == "embedded":
        setup_embedded(console, cfg)
    else:
        skip_wallet(console)
