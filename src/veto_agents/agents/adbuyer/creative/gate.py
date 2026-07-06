"""Veto spend-gate for the studio's BYO-key providers.

The x402 media tools (fal.ai) gate *inside* `veto_pay.fetch_x402` — the 402
challenge triggers the authorize automatically. BYO-key providers (OpenAI,
Higgsfield, ElevenLabs) have NO 402 challenge, so we must call Veto ourselves
BEFORE the provider HTTP request. This module is that call.

Contract (mirrors the ad-buyer control loop, controller.py):
  • action = "payment", merchant = the provider's domain.
  • decision_only is already forced inside VetoClient.authorize — we get a
    verdict, Veto does NOT try to also settle through Stripe.
  • FAIL-CLOSED: any exception (network, auth, bad response) → treated as a
    block, never as an allow. A studio that can't reach Veto does not spend.

`gate()` returns a `GateResult`; the caller checks `.allowed` and, when False,
surfaces `.verdict` + `.receipt_url` + `.reason_codes` to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Absolute imports — robust regardless of how deep this package sits.
from veto_agents.veto_client import VetoClient


@dataclass
class GateResult:
    allowed: bool
    verdict: str                       # "allow" | "deny" | "escalate" | "error"
    receipt_url: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    error: str | None = None

    def block_reason(self) -> str:
        """Human-readable one-liner for a blocked spend."""
        if self.reason_codes:
            return ", ".join(self.reason_codes)
        return self.error or "blocked by policy"


def _creative_agent_id(cfg) -> str | None:
    """The agent id the CREATIVE policy (adbuyer-creative) is keyed by.

    Prefer the dedicated `creative_agent_id` so per-generation micro-spends are
    judged by their OWN caps; fall back to `agent_id` (single-policy installs).
    """
    return getattr(cfg, "creative_agent_id", None) or getattr(cfg, "agent_id", None)


def _echo(console, merchant: str, res: "GateResult") -> None:
    """Surface the Veto verdict — on the GOOD path too, so governance is visible
    for the demo (allow=green, deny=red, escalate=magenta, error=red). No-op when
    no console is passed (library callers that render results themselves)."""
    if console is None:
        return
    if res.verdict == "allow":
        console.print(f"    [green]Veto: allowed ✓[/green] [dim]· {merchant}[/dim]")
    elif res.verdict == "deny":
        console.print(f"    [red]Veto: denied ✗[/red] [dim]· {merchant} · {res.block_reason()}[/dim]")
    elif res.verdict == "escalate":
        console.print(f"    [magenta]Veto: escalated to a human[/magenta] [dim]· {merchant}[/dim]")
    else:
        console.print(f"    [red]Veto: error[/red] [dim]· {res.error or 'authorize failed'}[/dim]")
    if res.receipt_url:
        console.print(f"    [dim]receipt: {res.receipt_url}[/dim]")


def gate(
    cfg,
    *,
    merchant: str,
    amount: float,
    description: str,
    context: dict[str, Any] | str | None = None,
    action: str = "payment",
    currency: str = "USD",
    console=None,
) -> GateResult:
    """Authorize a BYO-key paid generation with Veto before it happens.

    `cfg` is the veto-agents Config (needs api_key, a creative/agent id,
    veto_api_base). Governed by the CREATIVE policy (cfg.creative_agent_id, else
    cfg.agent_id). Pass `console` to print the verdict (green on allow). Returns
    a GateResult. `.allowed` is True ONLY on an explicit allow verdict.
    """
    api_key = getattr(cfg, "api_key", None)
    agent_id = _creative_agent_id(cfg)
    if not (api_key and agent_id):
        # Not signed in → cannot authorize → fail closed (no spend).
        res = GateResult(
            allowed=False,
            verdict="deny",
            reason_codes=["not_signed_in"],
            error="Not signed in to Veto. Run `veto-agents setup` to enable paid generation.",
        )
        _echo(console, merchant, res)
        return res

    base = getattr(cfg, "veto_api_base", "https://veto-ai.com/api/v1")
    try:
        with VetoClient(base, api_key) as vc:
            r = vc.authorize(
                agent_id=agent_id,
                action=action,
                merchant=merchant,
                amount=round(float(amount), 4),
                currency=currency,
                description=description,
                context=context if context is not None else {},
            )
    except Exception as e:  # noqa: BLE001 — any failure is fail-closed
        res = GateResult(allowed=False, verdict="error", error=f"Veto authorize failed: {e}")
        _echo(console, merchant, res)
        return res

    res = GateResult(
        allowed=(r.verdict == "allow"),
        verdict=r.verdict,
        receipt_url=r.receipt_url,
        reason_codes=r.reason_codes or [],
    )
    _echo(console, merchant, res)
    return res
