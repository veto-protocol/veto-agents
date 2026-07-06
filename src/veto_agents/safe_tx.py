"""Safe transaction builder.

Pure logic — no chain calls, no SDK deps. Given the fields of a Safe
transaction + an owner signature + a Veto co-signature, produces the
calldata for `Safe.execTransaction(...)` that can be submitted to the
Safe contract.

This is what the agent uses to drive a Safe call after Veto has issued
its `safe_signature`. The Veto signature is appended to the Safe's
`signatures` blob after the owner sig(s); Safe ignores trailing bytes
beyond its threshold's worth, the Guard parses them.

For HARD_STOP_v1 we target one-of-one Safes (sole owner = user EOA).
The signatures blob is always 130 bytes: [owner 65 | veto 65].

Tested in test_safe_tx.py. Mirrors the EIP-712 hash construction in
gateway.services.safe_signer (same SAFE_TX_TYPEHASH bytes).
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from eth_hash.auto import keccak as _keccak  # type: ignore
except ImportError:  # pragma: no cover — eth_hash ships only in the `dev` extra (test-only)
    def _keccak(b: bytes) -> bytes:
        raise RuntimeError(
            "eth_hash not installed; it is a test-only dep — "
            "install with: pip install 'veto-agents[dev]'"
        )


# Selectors / typehashes — must agree byte-for-byte with VetoGuard.sol
# and gateway.services.safe_signer. Drift here = nothing executes.

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
EXEC_TRANSACTION_SELECTOR = bytes.fromhex("6a761202")  # Safe v1.3.0+ execTransaction selector

SAFE_TX_TYPEHASH = _keccak(
    b"SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,"
    b"uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
)

DOMAIN_SEPARATOR_TYPEHASH = _keccak(
    b"EIP712Domain(uint256 chainId,address verifyingContract)"
)


# ──────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SafeTx:
    """One Safe transaction. Mirrors gateway.services.safe_signer.SafeTx
    so the same struct flows CLI ↔ backend ↔ Solidity."""

    safe: str           # Safe contract address
    chain_id: int       # EVM chain id
    to: str             # Inner call target
    value: int          # ETH/native value
    data: bytes         # Inner calldata
    operation: int = 0  # 0=CALL, 1=DELEGATECALL
    safeTxGas: int = 0
    baseGas: int = 0
    gasPrice: int = 0
    gasToken: str = ZERO_ADDRESS
    refundReceiver: str = ZERO_ADDRESS
    nonce: int = 0


# ──────────────────────────────────────────────────────────────────────
# Hashing — same shape as the backend signer
# ──────────────────────────────────────────────────────────────────────


def safe_tx_hash(t: SafeTx) -> bytes:
    """EIP-712 hash of the SafeTx. Identical bytes to what
    gateway.services.safe_signer.hash_safe_tx produces, and what
    VetoGuard.sol recomputes."""
    domain = _keccak(
        _abi_encode(
            ("bytes32", DOMAIN_SEPARATOR_TYPEHASH),
            ("uint256", t.chain_id),
            ("address", t.safe),
        )
    )
    struct = _keccak(
        _abi_encode(
            ("bytes32", SAFE_TX_TYPEHASH),
            ("address", t.to),
            ("uint256", t.value),
            ("bytes32", _keccak(t.data)),
            ("uint8", t.operation),
            ("uint256", t.safeTxGas),
            ("uint256", t.baseGas),
            ("uint256", t.gasPrice),
            ("address", t.gasToken),
            ("address", t.refundReceiver),
            ("uint256", t.nonce),
        )
    )
    return _keccak(b"\x19\x01" + domain + struct)


# ──────────────────────────────────────────────────────────────────────
# Signatures blob — what Safe consumes in `signatures` parameter
# ──────────────────────────────────────────────────────────────────────


def build_signatures_blob(owner_sig_hex: str, veto_sig_hex: str) -> bytes:
    """Concatenate [owner_sig | veto_sig]. Both must be 65-byte (r,s,v)
    signatures, 0x-prefixed or raw hex. Order matters — Safe consumes
    the first `threshold * 65` bytes as owner sigs (we target threshold=1,
    so the first 65 bytes); the Guard parses bytes [65:130] as the Veto
    signature."""
    owner = _bytes_from_hex(owner_sig_hex)
    veto = _bytes_from_hex(veto_sig_hex)
    if len(owner) != 65:
        raise ValueError(f"owner_sig must be 65 bytes, got {len(owner)}")
    if len(veto) != 65:
        raise ValueError(f"veto_sig must be 65 bytes, got {len(veto)}")
    return owner + veto


# ──────────────────────────────────────────────────────────────────────
# execTransaction calldata — what the agent submits to the Safe
# ──────────────────────────────────────────────────────────────────────


def build_exec_transaction_calldata(t: SafeTx, signatures: bytes) -> bytes:
    """Return the bytes for `Safe.execTransaction(...)`. Selector +
    ABI-encoded args. Caller wraps this in an eth_sendTransaction with
    `to=t.safe`, `data=<this>`, value=0 (Safe forwards value internally).

    Layout (Solidity ABI):
      selector (4) +
      to (32) +
      value (32) +
      data_offset (32) +
      operation (32) +
      safeTxGas (32) +
      baseGas (32) +
      gasPrice (32) +
      gasToken (32) +
      refundReceiver (32) +
      signatures_offset (32) +
      data_length (32) +
      data (padded to 32) +
      signatures_length (32) +
      signatures (padded to 32)
    """
    # Compute offsets for the two dynamic params (`data`, `signatures`).
    # Static part is 10 * 32 = 320 bytes after the selector. Data offset
    # is therefore 320 (relative to the start of the args, which is post-
    # selector). After data: data_length (32) + padded data → signatures
    # offset.
    static_len = 10 * 32
    data_len = len(t.data)
    data_padded_len = ((data_len + 31) // 32) * 32
    signatures_offset = static_len + 32 + data_padded_len  # +32 for the data length header

    out = bytearray()
    out += EXEC_TRANSACTION_SELECTOR
    out += _enc("address", t.to)
    out += _enc("uint256", t.value)
    out += _enc("uint256", static_len)  # data_offset
    out += _enc("uint8", t.operation)
    out += _enc("uint256", t.safeTxGas)
    out += _enc("uint256", t.baseGas)
    out += _enc("uint256", t.gasPrice)
    out += _enc("address", t.gasToken)
    out += _enc("address", t.refundReceiver)
    out += _enc("uint256", signatures_offset)
    out += _enc("uint256", data_len)
    out += t.data + b"\x00" * (data_padded_len - data_len)
    sig_len = len(signatures)
    sig_padded_len = ((sig_len + 31) // 32) * 32
    out += _enc("uint256", sig_len)
    out += signatures + b"\x00" * (sig_padded_len - sig_len)
    return bytes(out)


# ──────────────────────────────────────────────────────────────────────
# Small helpers — same shape as gateway.services.safe_signer
# ──────────────────────────────────────────────────────────────────────


def _abi_encode(*fields: tuple[str, object]) -> bytes:
    out = b""
    for typ, val in fields:
        out += _enc(typ, val)
    return out


def _enc(typ: str, val) -> bytes:
    if typ == "bytes32":
        assert isinstance(val, (bytes, bytearray)) and len(val) == 32
        return bytes(val)
    if typ in ("uint256", "uint8"):
        n = int(val)
        if n < 0:
            raise ValueError("negative uint")
        return n.to_bytes(32, "big")
    if typ == "address":
        s = str(val)
        if not s.startswith("0x") or len(s) != 42:
            raise ValueError(f"bad address: {s!r}")
        return b"\x00" * 12 + bytes.fromhex(s[2:])
    raise ValueError(f"unsupported abi type: {typ}")


def _bytes_from_hex(s: str) -> bytes:
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    return bytes.fromhex(s)
