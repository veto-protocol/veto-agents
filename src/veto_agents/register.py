"""EVM address validation.

The previous `cli-demo/register/` flow lived here. Auth is now via magic-link
(see `auth.py`); the only thing left from the wallet-only flow is the address
validator, which the setup wizard still uses to validate the funding wallet
the user pastes.
"""

from __future__ import annotations

import re


# Same regex as the backend (gateway/views.py)
_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def is_valid_evm_address(s: str) -> bool:
    return bool(_EVM_RE.match(s.strip()))
