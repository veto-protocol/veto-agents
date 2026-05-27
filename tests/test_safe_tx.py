"""Tests for veto_agents.safe_tx.

Covers:
  - SafeTx hash agreement with the backend signer (same bytes)
  - signatures blob layout ([owner | veto])
  - execTransaction calldata structure
  - error paths for bad inputs
"""

import unittest

from veto_agents.safe_tx import (
    EXEC_TRANSACTION_SELECTOR,
    SafeTx,
    build_exec_transaction_calldata,
    build_signatures_blob,
    safe_tx_hash,
)


ZERO = "0x0000000000000000000000000000000000000000"
SAFE = "0x1111111111111111111111111111111111111111"
DEST = "0x2222222222222222222222222222222222222222"


def _tx(**overrides) -> SafeTx:
    defaults = dict(
        safe=SAFE,
        chain_id=84532,
        to=DEST,
        value=0,
        data=b"",
        operation=0,
        nonce=0,
    )
    defaults.update(overrides)
    return SafeTx(**defaults)


class HashTests(unittest.TestCase):

    def test_hash_is_32_bytes(self):
        h = safe_tx_hash(_tx())
        self.assertEqual(len(h), 32)

    def test_hash_changes_when_value_changes(self):
        h1 = safe_tx_hash(_tx(value=1))
        h2 = safe_tx_hash(_tx(value=2))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_nonce_changes(self):
        h1 = safe_tx_hash(_tx(nonce=0))
        h2 = safe_tx_hash(_tx(nonce=1))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_chain_id_changes(self):
        h1 = safe_tx_hash(_tx(chain_id=84532))
        h2 = safe_tx_hash(_tx(chain_id=8453))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_safe_address_changes(self):
        h1 = safe_tx_hash(_tx(safe=SAFE))
        h2 = safe_tx_hash(_tx(safe="0x000000000000000000000000000000000000aBcD"))
        self.assertNotEqual(h1, h2)

    def test_hash_matches_backend_fixture(self):
        # Mirror of the fixture in contracts/test/VetoGuardBridge.t.sol +
        # the captured Python output. Same field values, same chain_id,
        # same nonce -> same hash bytes everywhere.
        h = safe_tx_hash(_tx(
            safe="0x1111111111111111111111111111111111111111",
            chain_id=84532,
            to="0x2222222222222222222222222222222222222222",
            value=1_000_000,
            data=b"",
            operation=0,
            nonce=7,
        ))
        self.assertEqual(
            h.hex(),
            "92a337618201b4a20cd6f743fdf33368ec419c4d7e35f8143d91ce7879156d32",
        )


class SignaturesBlobTests(unittest.TestCase):
    OWNER = "0x" + "11" * 65
    VETO  = "0x" + "22" * 65

    def test_concatenates_owner_then_veto(self):
        blob = build_signatures_blob(self.OWNER, self.VETO)
        self.assertEqual(len(blob), 130)
        self.assertEqual(blob[:65], b"\x11" * 65)
        self.assertEqual(blob[65:], b"\x22" * 65)

    def test_accepts_unprefixed_hex(self):
        blob = build_signatures_blob("11" * 65, "22" * 65)
        self.assertEqual(len(blob), 130)

    def test_rejects_wrong_owner_length(self):
        with self.assertRaises(ValueError):
            build_signatures_blob("0x" + "11" * 64, self.VETO)

    def test_rejects_wrong_veto_length(self):
        with self.assertRaises(ValueError):
            build_signatures_blob(self.OWNER, "0x" + "22" * 32)


class ExecTransactionCalldataTests(unittest.TestCase):
    OWNER = "0x" + "11" * 65
    VETO  = "0x" + "22" * 65

    def test_starts_with_exec_transaction_selector(self):
        blob = build_signatures_blob(self.OWNER, self.VETO)
        data = build_exec_transaction_calldata(_tx(), blob)
        self.assertEqual(data[:4], EXEC_TRANSACTION_SELECTOR)

    def test_length_is_padded_to_32(self):
        blob = build_signatures_blob(self.OWNER, self.VETO)
        data = build_exec_transaction_calldata(_tx(data=b"\x42\x42\x42"), blob)
        # After selector (4 bytes), every dynamic-arg segment must be
        # 32-aligned. Total length = 4 + multiple-of-32.
        self.assertEqual((len(data) - 4) % 32, 0)

    def test_encodes_dynamic_data_and_signatures_offsets_correctly(self):
        # Empty data path: signatures should sit immediately after the
        # static block (10 * 32) + the data_length header (32) +
        # zero-bytes-of-data.
        # Args layout (post-selector, 0-indexed words of 32 bytes):
        #   word 2 (bytes  64.. 96): data_offset
        #   word 9 (bytes 288..320): signatures_offset
        #   word 10 (bytes 320..352): data_length
        blob = build_signatures_blob(self.OWNER, self.VETO)
        data = build_exec_transaction_calldata(_tx(data=b""), blob)
        body = data[4:]
        data_offset = int.from_bytes(body[64:96], "big")
        signatures_offset = int.from_bytes(body[288:320], "big")
        self.assertEqual(data_offset, 320)
        self.assertEqual(signatures_offset, 320 + 32)  # data_length=0 → no data padding

    def test_round_trip_signatures_recoverable_from_calldata(self):
        # Read back the signatures blob from the calldata. Stress test
        # of the offsets-and-lengths math.
        blob = build_signatures_blob(self.OWNER, self.VETO)
        data = build_exec_transaction_calldata(_tx(data=b"\x99" * 7), blob)
        body = data[4:]
        # signatures_offset lives at word 9, bytes 288..320.
        sig_offset = int.from_bytes(body[288:320], "big")
        sig_len = int.from_bytes(body[sig_offset:sig_offset + 32], "big")
        self.assertEqual(sig_len, 130)
        recovered = bytes(body[sig_offset + 32 : sig_offset + 32 + sig_len])
        self.assertEqual(recovered, blob)


if __name__ == "__main__":
    unittest.main()
