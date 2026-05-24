"""First-run registration with Veto's CLI demo endpoint.

POSTs the user's wallet address to /api/v1/cli-demo/register/ and gets back
api_key + agent_id + client_id. Idempotent: re-registering the same wallet
returns the same credentials.

The wallet address belongs to the *user* — Phantom, Metamask, Coinbase Wallet,
whatever they already have. It serves as their identity with Veto and as the
source address they'll fund their agent treasury from (via the QR-code flow
shown at end of setup).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

# Same regex as the backend (gateway/views.py)
_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def is_valid_evm_address(s: str) -> bool:
    return bool(_EVM_RE.match(s.strip()))


@dataclass
class RegisterResult:
    api_key: str
    agent_id: str
    client_id: str
    wallet_address: str
    rail: str
    network: str
    policy_preset: str
    is_new: bool


def register(
    *,
    api_base: str,
    wallet_address: str,
    rail: str = "evm",
    network: str = "base-sepolia",
    timeout: float = 15.0,
) -> RegisterResult:
    """Hit POST /api/v1/cli-demo/register/. Idempotent server-side."""
    url = f"{api_base.rstrip('/')}/cli-demo/register/"
    payload = {
        "wallet_address": wallet_address,
        "rail": rail,
        "network": network,
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    return RegisterResult(
        api_key=data["api_key"],
        agent_id=data["agent_id"],
        client_id=data["client_id"],
        wallet_address=data["wallet_address"],
        rail=data["rail"],
        network=data["network"],
        policy_preset=data.get("policy_preset", ""),
        is_new=bool(data.get("is_new")),
    )
