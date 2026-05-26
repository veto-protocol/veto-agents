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
    ) -> AuthorizeResult:
        """Call POST /api/v1/authorize/.

        Wire format matches what the main `veto` (npm) CLI sends:
        - Header `X-Veto-API-Key` (NOT `Authorization: Bearer ...`).
        - Body field `action` (NOT `action_type`); accepted values are
          `payment`, `crypto_transfer`, `tool_execution`.
        - `context` may be a string or a dict; the backend normalizes
          either shape so callers can stay loose.
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
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Veto-API-Key"] = self.api_key

        r = self._client.post(f"{self.api_base}/authorize/", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        return AuthorizeResult(
            verdict=data.get("verdict") or data.get("result", {}).get("status") or "deny",
            reason_codes=data.get("reason_codes", []) or data.get("result", {}).get("reason_codes", []),
            receipt_url=data.get("receipt_url"),
            receipt_jwt=data.get("receipt_jwt"),
            raw=data,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VetoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
