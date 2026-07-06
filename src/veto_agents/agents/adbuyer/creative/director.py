"""The creative DIRECTOR — one LLM call that turns a product brief into ONE
coherent creative concept, then derives every asset's prompt/script from it.

This is what makes the ad package *cohere*: copy, image, video, and voiceover
are all generated from the same concept (theme + message + tone), instead of
four unrelated generations that happen to share a subject.

Reuses the repo's shared structured-output client (`veto_agents.structured_llm`):
forced tool-calling with the concept schema, so the model returns a parsed dict
— no fragile `json.loads` on free text.

Cost: this is a plain LLM call. Copy generation is FREE (LLM only); there is no
Veto *payment* gate here beyond the LLM's own token cost. Only the downstream
provider generations (image/video/voice) are Veto-gated.

Provider-agnostic: the director runs on whichever LLM provider/key the user has
(Anthropic, OpenAI, OpenRouter, Hermes, or a local OpenAI-compatible endpoint).
`structured_llm` reads `cfg.llm_provider` / `cfg.llm_model`, auto-detecting from
the available key when the provider is unset, and resolves keys the SAME way as
the studio providers (env → ~/.veto/creative.env → keychain).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# One shared, provider-agnostic structured-output client (routes to whichever
# LLM provider/key the user actually has). Absolute import — robust regardless
# of how deep this package sits.
from veto_agents.structured_llm import StructuredLLMError, structured_llm


class DirectorError(RuntimeError):
    """Raised when the director can't produce a concept (no key, LLM error)."""


@dataclass
class Concept:
    """The single unifying creative concept + derived per-asset prompts."""

    concept: str                                  # one-paragraph unifying idea
    theme: str
    tone: str
    headlines: list[str] = field(default_factory=list)
    primary_texts: list[str] = field(default_factory=list)
    ctas: list[str] = field(default_factory=list)
    image_prompt: str = ""
    video_prompt: str = ""
    voiceover_script: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


_CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "concept": {
            "type": "string",
            "description": (
                "One tight paragraph: the single unifying creative idea for this "
                "campaign — the theme, the core message, and the emotional tone. "
                "Every asset below must be derived FROM this so they feel like one "
                "campaign, not four unrelated pieces."
            ),
        },
        "theme": {"type": "string", "description": "The visual/narrative theme in a few words."},
        "tone": {"type": "string", "description": "The voice/tone in a few words (e.g. 'confident, warm, premium')."},
        "copy": {
            "type": "object",
            "description": "Ad copy variants, all on-concept.",
            "properties": {
                "headlines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-5 short, punchy headlines (<=40 chars each ideally).",
                },
                "primary_texts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 primary body texts (1-3 sentences each).",
                },
                "ctas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 call-to-action phrases (e.g. 'Shop now', 'Get early access').",
                },
            },
            "required": ["headlines", "primary_texts", "ctas"],
        },
        "image_prompt": {
            "type": "string",
            "description": (
                "A single, richly-detailed prompt for a hero still image that "
                "embodies the concept — subject, composition, lighting, mood. "
                "Written for a text-to-image model."
            ),
        },
        "video_prompt": {
            "type": "string",
            "description": (
                "A prompt for a short (5-10s) hero video that extends the concept "
                "in motion — camera move, subject action, pacing. Text-to-video model."
            ),
        },
        "voiceover_script": {
            "type": "string",
            "description": (
                "A spoken voiceover script matching the tone, timed to ~5-15s of "
                "narration. Plain spoken words only — no stage directions."
            ),
        },
    },
    "required": ["concept", "copy", "image_prompt", "video_prompt", "voiceover_script"],
}


_SYSTEM_PROMPT = (
    "You are a world-class advertising CREATIVE DIRECTOR. Given a product brief, "
    "you first crystallize ONE unifying creative concept — a single theme, message, "
    "and tone — and then derive every deliverable from it so the whole campaign is "
    "coherent: the copy, the hero image, the hero video, and the voiceover must all "
    "clearly belong to the SAME concept. Be specific and concrete. Avoid generic "
    "ad-speak. Ground everything in the product and audience described in the brief. "
    "You must return your answer through the provided tool."
)


def direct(brief: str, cfg) -> Concept:
    """Turn a product brief into a single coherent creative concept.

    Runs on whichever LLM provider/key the user has (via `structured_llm`).
    Raises DirectorError if no provider key is configured or the LLM call fails.
    """
    user_prompt = (
        f"PRODUCT BRIEF:\n{brief.strip()}\n\n"
        "Produce the unified creative concept and all derived assets now."
    )

    try:
        data = structured_llm(
            cfg, system=_SYSTEM_PROMPT, user=user_prompt,
            schema=_CONCEPT_SCHEMA, tools_name="emit_concept", max_tokens=1600,
        )
    except StructuredLLMError as e:
        raise DirectorError(str(e)) from e

    copy = data.get("copy") or {}
    return Concept(
        concept=str(data.get("concept", "")).strip(),
        theme=str(data.get("theme", "")).strip(),
        tone=str(data.get("tone", "")).strip(),
        headlines=[str(h).strip() for h in (copy.get("headlines") or []) if str(h).strip()],
        primary_texts=[str(t).strip() for t in (copy.get("primary_texts") or []) if str(t).strip()],
        ctas=[str(c).strip() for c in (copy.get("ctas") or []) if str(c).strip()],
        image_prompt=str(data.get("image_prompt", "")).strip(),
        video_prompt=str(data.get("video_prompt", "")).strip(),
        voiceover_script=str(data.get("voiceover_script", "")).strip(),
        raw=data if isinstance(data, dict) else {},
    )
