"""Ad-buyer agent runner — thin entrypoint for the autonomous control loop.

The ad-buyer is a 24/7 AUTONOMOUS agent: the human deploys it ONCE with a
standing GOAL and a Veto policy, then walks away. From then on the agent runs
its own OBSERVE -> DECIDE -> GOVERN -> ACT loop, and Veto is the ongoing
guardrail on the agent's OWN decisions — not a per-command human consent gate.

  1. **Control spending.** For EVERY autonomous action with a spend
     implication, the loop calls `VetoClient.authorize(...)` BEFORE any Meta
     write — governing the agent's own INTENT (its rationale is the context).
     allow -> execute; deny -> skip; escalate -> notify + skip. It never
     freezes and it never fails open. See controller.govern_and_execute.

  2. **Spend on content.** When the brain decides to refresh a creative, the
     new image is generated pay-per-use over x402 (fal.ai FLUX Schnell) — that
     micro-spend self-gates through Veto inside `fal_image.generate`.

Autonomy scope v1 (existing entities only): adjust_budget, pause, resume,
refresh_creative. The loop rejects any attempt to create new campaigns/adsets/
ads before execution.

Meta credentials are bring-your-own (~/.veto/meta.env); a SANDBOX ad account is
recommended for the demo (identical API, never spends, no funding source).

The whole loop lives in controller.py; this module keeps the `run()` entrypoint
(so `from veto_agents.agents.adbuyer import run` stays stable) plus a few small
plan/render helpers that are still handy for humans reading briefs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from . import controller

# The merchant string passed to Veto for ad-budget authorization. Must match an
# entry in adbuyer's policy allowlist_merchants. (Canonical copy lives in
# controller; re-exported here for back-compat.)
META_MERCHANT = controller.META_MERCHANT

# Default campaign shape when a brief doesn't specify.
DEFAULT_OBJECTIVE = "OUTCOME_TRAFFIC"
DEFAULT_DAILY_BUDGET_USD = 20.0
DEFAULT_COUNTRIES = ["US"]
DEFAULT_LANDING_URL = "https://example.com/landing"
# How many days of daily-budget we treat as a campaign's spend ceiling.
CAP_DAYS = 7


@dataclass
class Plan:
    objective: str = DEFAULT_OBJECTIVE
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD
    countries: list[str] = field(default_factory=lambda: list(DEFAULT_COUNTRIES))
    age_min: int = 18
    age_max: int = 65
    landing_url: str = DEFAULT_LANDING_URL
    campaign_name: str = "Veto MVP Campaign"

    @property
    def spend_cap_usd(self) -> float:
        return round(self.daily_budget_usd * CAP_DAYS, 2)


# ─── planning helpers (heuristic — reused for human-readable brief parsing) ─


def _plan_from_brief(prompt: str) -> Plan:
    """Parse a Plan from a brief with a small keyword/regex heuristic."""
    p = prompt.lower()
    plan = Plan(campaign_name=_campaign_name_from_brief(prompt))

    if any(w in p for w in ("awareness", "brand", "reach", "impression")):
        plan.objective = "OUTCOME_AWARENESS"
    elif any(w in p for w in ("lead", "signup", "sign up", "sign-up")):
        plan.objective = "OUTCOME_LEADS"
    elif any(w in p for w in ("sale", "purchase", "buy", "conversion", "shop")):
        plan.objective = "OUTCOME_SALES"
    else:
        plan.objective = DEFAULT_OBJECTIVE

    budget = _parse_daily_budget_usd(prompt)
    if budget is not None:
        plan.daily_budget_usd = budget

    url = _parse_url(prompt)
    if url:
        plan.landing_url = url

    return plan


def _parse_daily_budget_usd(prompt: str) -> float | None:
    p = prompt.lower()
    patterns = [
        r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:usd|dollars?)?\s*(?:/|per|a)\s*day",
        r"(?:daily\s*budget|budget)\s*(?:of|:|=)?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, p)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _parse_url(prompt: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+", prompt)
    return m.group(0) if m else None


def _campaign_name_from_brief(prompt: str) -> str:
    words = re.sub(r"https?://\S+", "", prompt).strip()
    words = re.sub(r"\s+", " ", words)
    short = words[:48].strip() or "Veto MVP Campaign"
    return f"Veto — {short}"


# ─── rendering helpers ──────────────────────────────────────────────────────


def _render_plan(plan: Plan, console: Console) -> None:
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("Field", style="dim", no_wrap=True)
    table.add_column("Value")
    table.add_row("Objective", plan.objective)
    table.add_row("Daily budget", f"[bold yellow]${plan.daily_budget_usd:,.2f}/day[/bold yellow]")
    table.add_row(
        "Account spend cap",
        f"[bold yellow]${plan.spend_cap_usd:,.2f}[/bold yellow] "
        f"[dim](≈ {CAP_DAYS}× daily — the hard ceiling)[/dim]",
    )
    table.add_row("Targeting", f"{', '.join(plan.countries)} · age {plan.age_min}-{plan.age_max}")
    table.add_row("Landing URL", plan.landing_url)
    console.print()
    console.print(table)
    console.print()


def _render_insights(rows: list[dict], console: Console) -> None:
    if not rows:
        console.print(
            "  [dim]No delivery yet (0 impressions/0 spend). Expected on a fresh "
            "or sandbox account — sandbox never spends.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("Campaign")
    table.add_column("Spend ($)", justify="right")
    table.add_column("Impr.", justify="right")
    table.add_column("Clicks", justify="right")
    table.add_column("CTR %", justify="right")
    table.add_column("CPC ($)", justify="right")
    for r in rows:
        table.add_row(
            str(r.get("campaign_name", "—")),
            str(r.get("spend", "0")),
            str(r.get("impressions", "0")),
            str(r.get("clicks", "0")),
            str(r.get("ctr", "0")),
            str(r.get("cpc", "0")),
        )
    console.print(table)


# ─── entrypoint ────────────────────────────────────────────────────────────


def run(
    prompt: str,
    *,
    cfg,
    console: Console,
    auto_confirm: bool = False,
    interval_min: int | None = None,
    once: bool = False,
    dry_run: bool = False,
    mock: bool = False,
    no_llm: bool = False,
) -> None:
    """Entrypoint. `prompt` is the standing GOAL the buyer optimizes toward.

    Delegates to the autonomous control loop. There is no per-action human
    consent gate — the human deployed once + set policy; Veto is the ongoing
    guardrail on the agent's own decisions. `auto_confirm` is accepted for
    signature compatibility with the other agents' `run(...)` but is unused
    (the loop never prompts).

    `mock` mimics Meta offline (no real account/spend); `no_llm` forces the
    pure-rules heuristic brain. Both still run the REAL Veto + discipline gates.
    """
    controller.run_loop(
        cfg,
        console,
        goal=prompt,
        interval_min=interval_min,
        once=once,
        dry_run=dry_run,
        mock=mock,
        no_llm=no_llm,
    )


# Alias mirroring the groups agent's daemon entrypoint naming.
def run_daemon(
    cfg,
    console: Console,
    *,
    goal: str,
    interval_minutes: int | None = None,
    once: bool = False,
    dry_run: bool = False,
    mock: bool = False,
    no_llm: bool = False,
) -> None:
    """Daemon entrypoint used by the CLI `adbuyer` command.

    `interval_minutes` unset (None / 0) → the loop uses the policy's
    `ad_ops.observe_interval_minutes` (default 360). `mock` mimics Meta offline;
    `no_llm` forces the heuristic brain (both keep the real governance gates).
    """
    controller.run_loop(
        cfg,
        console,
        goal=goal,
        interval_min=interval_minutes,
        once=once,
        dry_run=dry_run,
        mock=mock,
        no_llm=no_llm,
    )
