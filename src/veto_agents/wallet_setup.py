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
    """Open browser to the embedded-wallet flow. Returns True if launched."""
    console.print("\n[bold]Create a new wallet, just with your email[/bold]\n")
    console.print(
        "  Opens a secure browser window where Privy creates an embedded smart wallet\n"
        "  for you. No seed phrase to manage — your email becomes the recovery method.\n"
        "  Veto deploys a Safe on top of that embedded wallet with Veto Guard installed.\n"
    )
    if not Confirm.ask("Open browser to continue?", default=True):
        return False

    # v0.0.9: the hosted /wallet/setup page doesn't exist yet — it's the
    # Privy + Safe-deploy frontend we'll build next. For now we open the
    # placeholder and tell the user honestly.
    import webbrowser
    base = cfg.veto_api_base.replace("/api/v1", "")
    url = f"{base}/wallet/setup?device_code=<TODO-issue-device-code>"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    console.print(
        f"\n  [yellow]·[/yellow] Embedded-wallet web flow lands in v0.0.10 — the page at\n"
        f"  {url}\n  isn't live yet. For now, use Option 1 (existing wallet) or come back soon.\n"
    )
    return False


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
    cfg = cfg_module.load()
    if not cfg.api_key:
        console.print(
            "[red]✗[/red] You need to sign in first. Run [cyan]veto-agents setup[/cyan]."
        )
        return

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
