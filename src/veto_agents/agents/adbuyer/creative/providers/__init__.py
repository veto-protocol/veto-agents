"""Creative-studio provider adapters.

Each module exposes `generate(...) -> creative.types.ToolResult` and, for the
BYO-key providers, gates the spend with Veto BEFORE calling the provider:

    openai_image     — OpenAI gpt-image-1 (BYO OPENAI_API_KEY), Veto-gated.
    fal_image        — free image fallback; wraps the media x402 tool (self-gated).
    higgsfield_video — Higgsfield DoP video (BYO key), async-poll, Veto-gated.
    elevenlabs_voice — ElevenLabs TTS (BYO ELEVENLABS_API_KEY), Veto-gated.
"""
