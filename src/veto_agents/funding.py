"""Agent treasury funding flow.

For v0.0.2, the agent treasury is a single shared VetoGuardedAccount contract
on Base Sepolia (the existing demo deploy). Users are tagged by the `from`
address of their inbound USDC transfer, which matches the wallet they
registered with.

v0.0.3 deploys a per-user contract via the existing CREATE2 factory so each
user has their own treasury address. v0.4 adds mainnet + cross-chain bridges.
"""

from __future__ import annotations

from dataclasses import dataclass


# Existing live demo contract on Base Sepolia.
DEMO_GUARDED_ACCOUNT = "0xCBbbC4b924AF40D29f135c3a88b6F650d55d92c5"
DEMO_CHAIN = "Base Sepolia"
DEMO_CHAIN_ID = 84532
DEMO_USDC_CONTRACT = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # USDC on Base Sepolia


@dataclass
class FundingTarget:
    address: str
    chain: str
    chain_id: int
    token: str = "USDC"
    token_contract: str = DEMO_USDC_CONTRACT


def get_funding_target(wallet_address: str) -> FundingTarget:
    """Pick the funding contract for this user's agent treasury.

    v0.0.2 returns the shared demo VetoGuardedAccount on Base Sepolia.
    v0.0.3 will compute the user's CREATE2-derived per-agent contract
    address from `wallet_address` + factory salt.
    """
    return FundingTarget(
        address=DEMO_GUARDED_ACCOUNT,
        chain=DEMO_CHAIN,
        chain_id=DEMO_CHAIN_ID,
    )


def render_funding_qr(target: FundingTarget) -> str:
    """ASCII QR code for the funding address. Renderable in any modern terminal.

    Uses the bare address (most mobile wallets recognize a raw EVM address
    when scanned). A full EIP-681 `ethereum:<addr>@<chainId>/transfer?...`
    URI is also recognized by some wallets but support is uneven, so we
    stick to the address for v0.0.2.
    """
    import qrcode  # lazy: keep it off the CLI import path for non-wallet commands

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=1,
    )
    qr.add_data(target.address)
    qr.make(fit=True)

    # Render to a buffer rather than printing directly so the caller can
    # frame it however they want.
    import io
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()
