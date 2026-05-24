"""On-chain balance lookup + receipts-feed aggregation for the wallet dashboard.

`veto-agents wallet` shows:
  - USDC balance at the user's VetoGuardedAccount (RPC call to Base Sepolia)
  - Per-agent spend totals + counts (from Veto's receipts feed)
  - Recent activity (last 10 receipts, each with a clickable receipt URL)

For v0.0.3 we hit Base Sepolia's public RPC and call USDC's balanceOf. Real
balance reconciliation (matching inbound transfers to the user's funding wallet)
lands in v0.0.4 when per-user CREATE2 deploys make the treasury address
deterministic per user.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


# Base Sepolia USDC contract — matches funding.DEMO_USDC_CONTRACT
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6

# Default public RPC for Base Sepolia. Users can override via env if they
# hit rate limits.
DEFAULT_RPC = "https://sepolia.base.org"


@dataclass
class WalletStats:
    treasury_address: str
    chain: str
    usdc_balance_raw: int          # smallest unit (6 decimals)
    usdc_balance_usd: float        # human-readable

    # From Veto's receipts feed
    lifetime_spent_usd: float
    pending_escalated_usd: float
    per_agent: dict[str, "AgentStats"]
    recent: list["ReceiptSummary"]

    @property
    def available_usd(self) -> float:
        """On-chain balance minus what's been spent off-chain (per receipts)
        and what's pending in escalations. v0.0.4 settlement will reconcile
        the off-chain spent total back to chain; until then this is the
        truth of how much the agent can still spend."""
        return max(0.0, self.usdc_balance_usd - self.lifetime_spent_usd - self.pending_escalated_usd)


def compute_stats(
    *,
    treasury: str,
    chain: str,
    api_base: str,
    api_key: str,
    client_id: str | None,
    rpc_url: str = DEFAULT_RPC,
    now_epoch: float,
) -> WalletStats:
    """One-stop fetch + aggregate. Used by the CLI wallet dashboard and the
    pre-flight balance check in the agent runners."""
    try:
        raw = get_usdc_balance(treasury, rpc_url=rpc_url)
        bal_usd = fmt_usdc(raw)
    except Exception:
        raw = 0
        bal_usd = 0.0

    rows: list[dict[str, Any]] = []
    try:
        feed = fetch_receipts_summary(api_base=api_base, api_key=api_key, client_id=client_id)
        if isinstance(feed, dict) and "results" in feed:
            rows = feed["results"]
        elif isinstance(feed, list):
            rows = feed
    except Exception:
        rows = []  # degrade gracefully — dashboard handles empty

    lifetime, pending, per_agent, recent = aggregate_receipts(rows, now_epoch=now_epoch)
    return WalletStats(
        treasury_address=treasury,
        chain=chain,
        usdc_balance_raw=raw,
        usdc_balance_usd=bal_usd,
        lifetime_spent_usd=lifetime,
        pending_escalated_usd=pending,
        per_agent=per_agent,
        recent=recent,
    )


@dataclass
class AgentStats:
    name: str
    actions: int
    denied: int
    escalated: int
    spent_usd: float


@dataclass
class ReceiptSummary:
    when: str          # human-readable "2h ago" / "1d ago"
    agent: str         # agent name
    label: str         # short description (merchant, tool)
    amount_usd: float
    verdict: str       # allow | deny | escalate
    receipt_url: str | None


# ── On-chain: USDC balanceOf via eth_call ──

def _hex_pad(addr: str) -> str:
    """Pad a 20-byte address to 32 bytes for ABI encoding."""
    h = addr.lower().removeprefix("0x")
    return ("0" * (64 - len(h)) + h)


def get_usdc_balance(treasury: str, rpc_url: str = DEFAULT_RPC) -> int:
    """Return the raw USDC balance (in 6-decimal smallest units) of `treasury`.

    Direct eth_call to USDC's `balanceOf(address)`. selector = 0x70a08231.
    """
    selector = "0x70a08231"
    data = selector + _hex_pad(treasury)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": USDC_BASE_SEPOLIA, "data": data},
            "latest",
        ],
    }
    with httpx.Client(timeout=10.0) as client:
        r = client.post(rpc_url, json=payload)
        r.raise_for_status()
        body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "eth_call error"))
    result_hex = body.get("result", "0x0")
    return int(result_hex, 16)


def fmt_usdc(raw: int) -> float:
    return raw / (10 ** USDC_DECIMALS)


# ── Off-chain: receipts feed from Veto backend ──

def fetch_receipts_summary(
    *,
    api_base: str,
    api_key: str,
    client_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Pull recent receipts for this user from /api/v1/receipts/.

    Returns the raw JSON. If the endpoint isn't available (older backend),
    raises so the caller can degrade gracefully.
    """
    url = f"{api_base.rstrip('/')}/receipts/"
    params = {"limit": limit}
    if client_id:
        params["client_id"] = client_id
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=10.0) as client:
        r = client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def _humanize_ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def aggregate_receipts(
    receipts: list[dict[str, Any]],
    *,
    now_epoch: float,
) -> tuple[float, float, dict[str, AgentStats], list[ReceiptSummary]]:
    """Compute per-agent stats + recent activity from a list of receipt rows.

    The receipts feed schema is the one served by gateway/views.transparency_feed
    or /api/v1/receipts/ (rows include: agent_id, agent_name, amount, verdict,
    reason_codes, merchant, action_type, created_at, receipt_url).

    Robust to missing fields — we treat absent values as zero / "unknown".
    """
    lifetime_spent = 0.0
    pending = 0.0
    per_agent: dict[str, AgentStats] = {}
    recent: list[ReceiptSummary] = []

    for row in receipts:
        agent_name = row.get("agent_name") or row.get("agent_id") or "unknown"
        agent_stats = per_agent.setdefault(
            agent_name, AgentStats(name=agent_name, actions=0, denied=0, escalated=0, spent_usd=0.0)
        )
        agent_stats.actions += 1

        verdict = (row.get("verdict") or row.get("status") or "").lower()
        amount = float(row.get("amount") or 0)

        if verdict == "deny":
            agent_stats.denied += 1
        elif verdict == "escalate":
            agent_stats.escalated += 1
            pending += amount
        elif verdict == "allow":
            agent_stats.spent_usd += amount
            lifetime_spent += amount

        # Recent activity (cap later; build the full list first)
        try:
            created = float(row.get("created_at_epoch") or 0)
            when = _humanize_ago(now_epoch - created) if created else "—"
        except (TypeError, ValueError):
            when = "—"

        recent.append(
            ReceiptSummary(
                when=when,
                agent=agent_name,
                label=row.get("merchant") or row.get("description") or row.get("action_type") or "—",
                amount_usd=amount,
                verdict=verdict or "—",
                receipt_url=row.get("receipt_url"),
            )
        )

    # Most recent N entries (caller controls how many to render)
    return lifetime_spent, pending, per_agent, recent
