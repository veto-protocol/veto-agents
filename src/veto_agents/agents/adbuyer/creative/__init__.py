"""Creative studio — the standalone creative stage of the ad-buyer agent.

This package turns a product brief into a coherent "ad package": copy +
image(s) + video + voiceover, all derived from ONE creative concept produced
by an LLM director so every asset matches in theme, message, and tone.

It is deliberately decoupled from Meta. You can run the whole studio with NO
Meta credentials — it produces creative files on disk. Buying/placing the ad
(Meta Marketing API) is a later, separate stage of the ad-buyer agent.

Every PAID generation (OpenAI image, Higgsfield video, ElevenLabs voice) is
gated by Veto (`VetoClient.authorize`, decision_only) BEFORE the provider is
called: deny/escalate → the asset is skipped and the receipt is logged; allow →
the provider runs. Copy is FREE (LLM only) and needs no paid gate. The free
image fallback (fal.ai over x402) self-gates through veto-pay.

Entry point: `studio.run(brief, cfg, console, ...)`.
"""

from __future__ import annotations

from .types import ToolResult  # noqa: F401  (re-export the shared result type)

__all__ = ["ToolResult"]
