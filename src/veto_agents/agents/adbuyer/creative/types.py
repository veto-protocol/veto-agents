"""Shared result type for every creative-studio provider.

Historically each media tool re-declared its own `ToolResult` (see
media/tools/fal_image.py:33). The studio introduces ONE shared type so image /
video / voice adapters all speak the same shape and the orchestrator can branch
uniformly.

`denied=True` is the load-bearing field: it distinguishes a **Veto block**
(policy said no) from a plain tool/HTTP failure. The orchestrator treats a
denied asset as "skip + show the receipt", not "error".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResult:
    ok: bool
    actual_cost_usd: float
    output_path: str | None = None
    output_url: str | None = None
    receipt_url: str | None = None
    error: str | None = None
    denied: bool = False           # True when Veto denied/escalated (vs a tool failure)
    verdict: str | None = None     # "allow" | "deny" | "escalate" | None (free/no-gate)
    provider: str | None = None    # e.g. "openai", "fal", "higgsfield", "elevenlabs"
    skipped: bool = False          # True when the asset was skipped (e.g. no key)
