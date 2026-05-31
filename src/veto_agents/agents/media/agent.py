"""Media agent runner.

v0.0.1 ships the **plan-then-execute** loop end-to-end:

  1. Take the user's brief.
  2. Build a plan with line-item cost estimates (currently from a heuristic
     mapping; v0.0.2 will route through the actual LLM-orchestrated planner).
  3. Show the plan + alternatives to the user.
  4. Wait for explicit consent.
  5. For each step, call Veto.authorize() before any paid action.
  6. Execute the step (v0.0.1 stubs the actual API calls — wiring lands in v0.0.2).
  7. Show actuals + receipt URL.

This module is deliberately thin: it demonstrates the *flow* every Veto Agent
follows. The Hermes integration + real tool calls + actual cost reconciliation
all slot in here without changing the public contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from ...veto_client import AuthorizeResult, VetoClient
from .tools import fal_image


# USDC contract addresses by chain — used to construct on-chain ERC20.transfer()
# calldata when we build a SafeTx representation of an agent's spend.
USDC_ADDRESS = {
    84532: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    8453:  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet
}

# A fixed "demo merchant" Safe transfers go to when the agent doesn't have a
# real on-chain recipient. Veto's treasury on Base Sepolia.
DEMO_MERCHANT = "0x000000000000000000000000000000000000Beef"


def _build_safe_tx_for_step(cfg, step, prompt: str, step_index: int):
    """Construct a SafeTx dict for an authorize call, or None.

    Returns None when the user hasn't set up an on-chain wallet — Veto
    still issues an off-chain receipt; the Guard side just doesn't
    participate. When the user HAS configured a Safe (cfg.wallet_address),
    we build a USDC ERC20.transfer() call for the step's est_usd amount.

    The on-chain side wants integer amounts in token decimals (USDC is 6),
    not float USD. We snap to whole cents to keep the number deterministic.
    """
    safe = getattr(cfg, "wallet_address", None)
    chain_id = getattr(cfg, "chain_id", None) or 84532
    if not safe or not isinstance(safe, str) or not safe.startswith("0x"):
        return None
    usdc = USDC_ADDRESS.get(int(chain_id))
    if not usdc:
        return None

    # USDC has 6 decimals. step.est_usd is a float in USD.
    amount_units = max(1, int(round(step.est_usd * 1_000_000)))
    # encode transfer(address,uint256)
    selector = "0xa9059cbb"
    to_addr = DEMO_MERCHANT[2:].lower().zfill(64)
    amount_hex = format(amount_units, "x").zfill(64)
    data = "0x" + selector[2:] + to_addr + amount_hex

    return {
        "safe": safe,
        "chain_id": int(chain_id),
        "to": usdc,
        "value": 0,
        "data": data,
        "operation": 0,
        # Nonce is the Safe's current nonce, looked up on-chain at submit
        # time. We pass 0 here as a sentinel — the backend signs whatever
        # nonce we send; the agent (or the user) is responsible for using
        # the right nonce when they finally submit. Demo recordings can
        # snapshot nonce=0 since a fresh Safe starts there.
        "nonce": 0,
    }


@dataclass
class Step:
    label: str
    merchant: str
    est_usd: float
    tool_name: str
    model: str | None = None      # for fal.image_gen: which model the user/policy chose
    endpoint: str | None = None   # discovered x402 endpoint URL (from CDP Bazaar), if any


def _classify_brief(prompt: str) -> list[Step]:
    """v0.0.1 heuristic: pick steps based on keywords in the brief.

    v0.0.2 will replace this with a real LLM-driven planner that produces
    these step entries from any prompt.
    """
    p = prompt.lower()
    steps: list[Step] = []

    if any(w in p for w in ("video", "clip", "motion", "animation")):
        steps.append(
            Step(
                label="Generate video — Runway Gen-3",
                merchant="api.runwayml.com",
                est_usd=0.42,
                tool_name="runway.video_gen",
            )
        )
    elif any(w in p for w in ("image", "picture", "photo", "logo", "icon", "shot")):
        steps.append(
            Step(
                label="Generate image — fal.ai (FLUX Schnell) over x402",
                merchant="fal.x402.paysponge.com",
                est_usd=fal_image.estimate_cost("flux-schnell"),
                tool_name="fal.image_gen",
                model="flux-schnell",
            )
        )

    if any(w in p for w in ("voice", "voiceover", "narration", "speech", "say")):
        steps.append(
            Step(
                label="Synthesize voiceover — ElevenLabs",
                merchant="api.elevenlabs.io",
                est_usd=0.05,
                tool_name="elevenlabs.voice",
            )
        )

    # Fall through: if we couldn't detect what they want, default to an image.
    if not steps:
        steps.append(
            Step(
                label="Generate image — fal.ai (FLUX Schnell) over x402 [inferred]",
                merchant="fal.x402.paysponge.com",
                est_usd=fal_image.estimate_cost("flux-schnell"),
                tool_name="fal.image_gen",
                model="flux-schnell",
            )
        )

    return steps


def _render_plan(steps: list[Step], console: Console) -> float:
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("Step")
    table.add_column("Merchant", style="dim")
    table.add_column("Est. cost", style="bold yellow", justify="right")
    total = 0.0
    for i, s in enumerate(steps, 1):
        table.add_row(str(i), s.label, s.merchant, f"${s.est_usd:.2f}")
        total += s.est_usd
    table.add_row("", "", "[bold]Estimate[/bold]", f"[bold]${total:.2f}[/bold]")
    console.print()
    console.print(table)
    console.print()
    return total


def _choice_gate(steps: list[Step], console: Console, *, auto_confirm: bool) -> None:
    """Surface options the agent could pick between, and let the USER choose.

    Principle: an agent must not silently pick a more expensive option. For
    image steps there are several fal.ai models at different prices — we show
    them and the user picks. `auto_confirm` (non-interactive) delegates to the
    cheapest, which is the safe default. A future version reads a policy
    "selection strategy" (cheapest / prefer X / cap) to delegate automatically.
    See feedback_agent_choice_needs_consent.
    """
    for s in steps:
        if s.tool_name != "fal.image_gen":
            continue

        # Discover live x402 image services from the CDP Bazaar (self-updating,
        # quality-ranked). Fall back to the built-in fal.ai models if discovery
        # is unavailable or returns nothing. Each option = (label, price, endpoint).
        options: list[tuple[str, float, str | None]] = []
        try:
            from veto_pay.discovery import search as _x402_search
            hits = _x402_search("text to image generation from a prompt", network="base", limit=5)
            for h in hits:
                if not h.url:
                    continue
                price = h.price_usd if h.price_usd is not None else 0.0
                desc = (h.description or h.host)[:48]
                options.append((f"{h.host} — {desc}", price, h.url))
        except Exception:
            pass
        if not options:
            options = [(f"fal.ai ({m})", p, None) for m, p in fal_image.models()]

        # Cheapest first (price 0/unknown sorts low — show those last).
        options.sort(key=lambda o: (o[1] if o[1] > 0 else 1e9))

        if auto_confirm:
            label, price, endpoint = options[0]
            s.est_usd, s.endpoint = price, endpoint
            continue

        console.print("[bold]Where to generate the image[/bold] — you choose (the agent won't pick a pricier one on its own):")
        for idx, (label, price, _ep) in enumerate(options, 1):
            ptxt = f"${price:.2f}" if price > 0 else "price TBD"
            console.print(f"  [bold cyan]{idx}.[/bold cyan] {label}  [yellow]{ptxt}[/yellow]")
        raw = Prompt.ask("Pick", choices=[str(i) for i in range(1, len(options) + 1)], default="1")
        label, price, endpoint = options[int(raw) - 1]
        s.est_usd, s.endpoint = price, endpoint
        s.label = f"Generate image — {label}"
        console.print()


def _require_connected_wallet(cfg, console: Console) -> bool:
    """Ensure a wallet is connected before the agent tries to pay.

    Same model as main veto: the agent spends from the user's CONNECTED
    wallet (Privy embedded / connect-existing via `veto-agents wallet setup`),
    governed by Veto. If no wallet is connected, the agent can't pay — so we
    point the user at the connect flow and stop. We do NOT ask them to send
    funds to some address; connecting the wallet is the whole step.

    Returns True to proceed, False to stop.
    """
    if getattr(cfg, "wallet_address", None):
        return True
    console.print(
        "[yellow]No wallet connected — your agent has nothing to pay with.[/yellow]\n"
        "  Connect one (takes ~30s, same as the main Veto flow):\n"
        "    [cyan]veto-agents wallet setup[/cyan]\n"
        "  [dim]Use your email (Privy creates an embedded wallet) or connect an existing one.\n"
        "   Veto governs the agent's spending from it; your personal wallet stays yours.[/dim]\n"
    )
    return False


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    """Entrypoint called by `veto-agents media "<prompt>"`."""
    console.print(f"\n[bold]Brief:[/bold] {prompt}\n")

    # 1. Plan
    steps = _classify_brief(prompt)

    # 1.5. Choice-gate — the user picks between options; the agent never
    # silently upgrades to a costlier one. Veto governs decisions, not just $.
    _choice_gate(steps, console, auto_confirm=auto_confirm)

    # 2. Render plan + estimate
    total = _render_plan(steps, console)

    # 2.6. Wallet-connect pre-flight — if the agent wants to pay and there's
    # no connected wallet, point the user at the connect flow (same as main
    # veto) and stop. Connecting is the step — not "send funds somewhere".
    if not _require_connected_wallet(cfg, console):
        return

    # 2.5. Pre-flight balance check — only when the user has actually set
    # up a Veto-governed wallet. Without one there's no treasury to check
    # against: tools are paid for with the user's own provider keys
    # (e.g. REPLICATE_API_TOKEN), so we just defer to authorize and let
    # the user pay their provider directly. Once `veto-agents wallet setup`
    # has run, this check enforces the on-chain budget.
    if cfg.api_key and cfg.agent_id and cfg.wallet_address:
        import time as _time

        from ...funding import get_funding_target
        from ...wallet_view import compute_stats

        target = get_funding_target(cfg.wallet_address)
        stats = compute_stats(
            treasury=target.address,
            chain=target.chain,
            api_base=cfg.veto_api_base,
            api_key=cfg.api_key,
            client_id=cfg.client_id,
            now_epoch=_time.time(),
        )
        if stats.available_usd < total:
            console.print(
                f"[red]✗ Insufficient credit.[/red] Treasury ${stats.usdc_balance_usd:,.2f} · "
                f"used ${stats.lifetime_spent_usd:,.2f} · available "
                f"[bold]${stats.available_usd:,.4f}[/bold] · plan needs "
                f"[bold]${total:,.4f}[/bold]."
            )
            console.print(
                f"[dim]Top up: send USDC on {target.chain} to {target.address} — "
                f"or run [cyan]veto-agents wallet receive[/cyan] to re-show the QR.[/dim]\n"
            )
            return
        console.print(
            f"[dim]Available credit: ${stats.available_usd:,.4f} "
            f"(treasury ${stats.usdc_balance_usd:,.2f} − used ${stats.lifetime_spent_usd:,.4f})[/dim]\n"
        )

    # 3. Consent gate (principle #1: plan-then-execute)
    if not auto_confirm:
        choice = Prompt.ask("Proceed?", choices=["y", "n"], default="y")
        if choice != "y":
            console.print("[yellow]·[/yellow] Cancelled. Nothing spent.\n")
            return

    # 4. Authorize + execute each step
    # Auth is required — every action signs through Veto so the receipt
    # trail stays complete. No anonymous escape hatch.
    if not (cfg.api_key and cfg.agent_id):
        from ...auth_gate import require_signin

        cfg = require_signin(console, cfg)
        if not (cfg.api_key and cfg.agent_id):
            return
    client = VetoClient(api_base=cfg.veto_api_base, api_key=cfg.api_key)
    agent_id = cfg.agent_id

    actual_total = 0.0
    try:
        for i, s in enumerate(steps, 1):
            console.print(f"[bold cyan]Step {i}/{len(steps)}[/bold cyan] · {s.label}")

            if s.tool_name == "fal.image_gen":
                # Governed x402 spend. fal_image.generate calls Veto authorize
                # (which also SIGNS the payment on allow) and pays fal.ai over
                # x402. The agent holds no key — if Veto denies/escalates, no
                # payment happens. This is the thin-agent / control-in-Veto path.
                tool_result = fal_image.generate(
                    prompt=prompt, model=s.model or "flux-schnell",
                    endpoint=s.endpoint, est_usd=s.est_usd, cfg=cfg,
                )
                if tool_result.denied:
                    console.print(f"  [red]✗ blocked by Veto[/red] · {tool_result.error}")
                    if tool_result.receipt_url:
                        console.print(f"  [dim]receipt: {tool_result.receipt_url}[/dim]")
                    return
                if not tool_result.ok:
                    console.print(f"  [red]✗ tool failed[/red]")
                    console.print(f"  [dim]{tool_result.error}[/dim]")
                    return
                actual_total += tool_result.actual_cost_usd
                console.print(
                    f"  [green]✓ paid + done[/green] · ${tool_result.actual_cost_usd:.4f} "
                    f"· file [cyan]{tool_result.output_path}[/cyan]"
                )
                if tool_result.receipt_url:
                    console.print(f"  [dim]receipt: {tool_result.receipt_url}[/dim]")
                continue

            # Video / voice are still stubbed — legacy authorize-only path
            # until their x402 endpoints are wired the same way as images.
            try:
                result: AuthorizeResult = client.authorize(
                    agent_id=agent_id,
                    action="tool_execution",
                    merchant=s.merchant,
                    amount=s.est_usd,
                    currency="USD",
                    description=f"{s.tool_name}: {prompt[:120]}",
                    context={
                        "source": "veto-agents-media", "intent": prompt,
                        "tool_name": s.tool_name, "step": i, "of": len(steps),
                    },
                )
            except Exception as e:
                console.print(f"  [red]✗ Veto authorize failed:[/red] {e}")
                console.print("  [dim]Stopping. No paid action taken.[/dim]\n")
                return
            if result.verdict == "deny":
                console.print(
                    f"  [red]✗ denied by Veto[/red] · reason: "
                    f"{', '.join(result.reason_codes) or 'policy'}"
                )
                return
            if result.verdict == "escalate":
                console.print(
                    f"  [yellow]· escalated[/yellow] — needs your approval. "
                    f"Receipt: {result.receipt_url}"
                )
                return
            console.print(
                f"  [green]✓ allowed[/green] by Veto"
                + (f" · receipt {result.receipt_url}" if result.receipt_url else "")
            )
            actual = s.est_usd
            actual_total += actual
            console.print(
                f"  [yellow]·[/yellow] tool call stubbed for [dim]{s.tool_name}[/dim] "
                f"· est-cost ${actual:.2f}"
            )
    finally:
        if client is not None:
            client.close()

    console.print(
        f"\n[green]✓ Done.[/green] Total spent: [bold]${actual_total:.2f}[/bold] "
        f"(estimate was ${total:.2f}).\n"
    )
