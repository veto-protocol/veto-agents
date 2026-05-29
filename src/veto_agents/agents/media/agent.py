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
from .tools import replicate_image


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
                label="Generate image — Replicate (Flux Schnell)",
                merchant="replicate.com",
                est_usd=replicate_image.estimate_cost("flux-schnell"),
                tool_name="replicate.image_gen",
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
                label="Generate image — Replicate (Flux Schnell) [inferred]",
                merchant="replicate.com",
                est_usd=replicate_image.estimate_cost("flux-schnell"),
                tool_name="replicate.image_gen",
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


def run(prompt: str, *, cfg, console: Console, auto_confirm: bool = False) -> None:
    """Entrypoint called by `veto-agents media "<prompt>"`."""
    console.print(f"\n[bold]Brief:[/bold] {prompt}\n")

    # 1. Plan
    steps = _classify_brief(prompt)

    # 2. Render plan + estimate
    total = _render_plan(steps, console)

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

            # Every spend authorizes through Veto. No bypass.
            # If the user has an on-chain wallet configured, build a SafeTx
            # so the response carries a guard-acceptable signature alongside
            # the off-chain receipt. We log what would be submitted on-chain
            # but don't actually submit yet — that needs the user's wallet
            # signature, which lives in their Privy session, not the CLI.
            safe_tx_payload = _build_safe_tx_for_step(cfg, s, prompt, i)
            try:
                result: AuthorizeResult = client.authorize(
                    agent_id=agent_id,
                    action="tool_execution",
                    merchant=s.merchant,
                    amount=s.est_usd,
                    currency="USD",
                    description=f"{s.tool_name}: {prompt[:120]}",
                    context={
                        "source": "veto-agents-media",
                        "intent": prompt,
                        "tool_name": s.tool_name,
                        "step": i,
                        "of": len(steps),
                    },
                    safe_tx=safe_tx_payload,
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
            # If a SafeTx was sent + the verdict was allow, surface the
            # guard-acceptable signature. Proves the on-chain side would
            # let this through if the user submitted the Safe transaction.
            if safe_tx_payload and result.safe_signature:
                short_sig = result.safe_signature[:10] + "…" + result.safe_signature[-6:]
                console.print(
                    f"    [dim]on-chain proof: signed by {result.safe_signer} "
                    f"({short_sig})[/dim]"
                )

            # Tool execution
            if s.tool_name == "replicate.image_gen":
                tool_result = replicate_image.generate(prompt=prompt)
                actual = tool_result.actual_cost_usd
                actual_total += actual
                if tool_result.ok:
                    console.print(
                        f"  [green]✓ done[/green] · actual ${actual:.4f} "
                        f"· file [cyan]{tool_result.output_path}[/cyan]"
                    )
                else:
                    console.print(f"  [red]✗ tool failed[/red]")
                    console.print(f"  [dim]{tool_result.error}[/dim]")
                    return
            else:
                # Runway video / ElevenLabs voice still stubbed
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
