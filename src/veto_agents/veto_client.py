"""Client for Veto's authorize endpoint.

Every paid tool call goes through this. The contract:

  client.authorize(agent_id, action, merchant, amount, currency, description,
                   context=...)
  → AuthorizeResult(verdict, reason_codes, receipt_url, receipt_jwt)

If verdict is "allow" the tool proceeds and the caller attaches the receipt URL
to its output. If "deny" or "escalate" the tool refuses (or pauses, depending
on the agent's policy). This is rule #4 in PRINCIPLES.md — Veto is the only
spend gate, no caching, no bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class AuthorizeResult:
    verdict: str  # "allow" | "deny" | "escalate"
    reason_codes: list[str]
    receipt_url: str | None
    receipt_jwt: str | None
    raw: dict[str, Any]  # full server response, for debugging / future fields
    # Populated when the caller passed `safe_tx` AND the verdict was allow.
    # The signature is what the on-chain VetoGuard will recover; the signer
    # field tells you which address it should recover to (for sanity checks).
    safe_signature: str | None = None
    safe_signer: str | None = None


class VetoClient:
    """Thin HTTP wrapper around POST /api/v1/authorize/."""

    def __init__(self, api_base: str, api_key: str | None = None, timeout: float = 10.0):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def authorize(
        self,
        *,
        agent_id: str,
        action: str,
        merchant: str,
        amount: float,
        currency: str = "USD",
        description: str = "",
        context: dict[str, Any] | str | None = None,
        safe_tx: dict[str, Any] | None = None,
    ) -> AuthorizeResult:
        """Call POST /api/v1/authorize/.

        Wire format matches what the main `veto` (npm) CLI sends:
        - Header `X-Veto-API-Key` (NOT `Authorization: Bearer ...`).
        - Body field `action` (NOT `action_type`); accepted values are
          `payment`, `crypto_transfer`, `tool_execution`.
        - `context` may be a string or a dict; the backend normalizes
          either shape so callers can stay loose.
        - `safe_tx` (optional): when present AND the verdict is allow,
          the response carries a `safe_signature` recoverable to the
          on-chain VetoGuard's signer. Callers wire that signature into
          a Safe.execTransaction call to gate the spend on-chain.
        """
        payload = {
            "agent_id": agent_id,
            "action": action,
            "merchant": merchant,
            "amount": amount,
            "currency": currency,
            "description": description,
            "context": context if context is not None else {},
        }
        if safe_tx is not None:
            payload["safe_tx"] = safe_tx
        # Approval-mode flag — for media/groups/etc we want the verdict
        # only, not for Veto's own executor to also try settling the
        # spend through Stripe Issuing.
        payload["decision_only"] = True

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Veto-API-Key"] = self.api_key

        r = self._client.post(f"{self.api_base}/authorize/", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        # Normalize verdict — backend returns `status` ("approved"/"denied"/
        # "escalated"); older paths use `verdict`; nested `result.status`
        # shows up in a couple of flows too. We always present the CLI
        # the canonical `allow`/`deny`/`escalate` triad.
        raw_status = (
            data.get("verdict")
            or data.get("status")
            or data.get("result", {}).get("status")
            or "deny"
        )
        verdict_map = {
            "approved": "allow", "approve": "allow", "allow": "allow",
            "denied": "deny", "deny": "deny", "failed": "deny",
            "escalated": "escalate", "escalate": "escalate",
        }
        verdict = verdict_map.get(raw_status.lower(), raw_status.lower())

        return AuthorizeResult(
            verdict=verdict,
            reason_codes=data.get("reason_codes", []) or data.get("result", {}).get("reason_codes", []),
            receipt_url=data.get("receipt_url"),
            receipt_jwt=data.get("receipt_jwt"),
            raw=data,
            safe_signature=data.get("safe_signature"),
            safe_signer=data.get("safe_signer"),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VetoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
