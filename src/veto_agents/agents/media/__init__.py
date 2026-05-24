"""Media agent — generates images, video, and audio for the user.

Spends on Replicate, Runway, ElevenLabs. Veto-gated on every paid call.
See agents/media/SPEC.md in the repo root for the full spec.
"""

from .agent import run

__all__ = ["run"]
