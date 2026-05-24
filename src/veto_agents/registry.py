"""Catalog of installable agents + the credentials each one needs.

For v0 the registry is hard-coded — these are the 4 agents we curated and
maintain. Later this becomes a remote endpoint (`veto-ai.com/agents/index.json`)
so we can publish new agents without shipping a new CLI release.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Credential:
    """A tool credential the agent needs (e.g. REPLICATE_API_TOKEN).

    During install we walk the user through each required credential:
      - open `signup_url` in the browser so they can grab a key
      - prompt for the value
      - save into ~/.veto-agents/credentials.yaml (keyed by env_var)

    `optional` credentials are offered but can be skipped — the agent will
    degrade gracefully (higher-quality models become unavailable, etc).
    """
    env_var: str
    label: str
    signup_url: str
    required: bool = True
    notes: str = ""


@dataclass(frozen=True)
class AgentEntry:
    name: str
    one_line: str
    spends_on: str
    spec_url: str
    package: str
    credentials: tuple[Credential, ...] = field(default_factory=tuple)


REGISTRY: list[AgentEntry] = [
    AgentEntry(
        name="media",
        one_line="Generates images, video, and audio for you.",
        spends_on="Replicate, Runway, ElevenLabs",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/media/SPEC.md",
        package="veto_agents.agents.media",
        credentials=(
            Credential(
                env_var="REPLICATE_API_TOKEN",
                label="Replicate API token (images + video)",
                signup_url="https://replicate.com/account/api-tokens",
                required=True,
                notes="Free tier available. ~$0.003 per Flux Schnell image.",
            ),
            Credential(
                env_var="ELEVENLABS_API_KEY",
                label="ElevenLabs API key (voiceover)",
                signup_url="https://elevenlabs.io/app/settings/api-keys",
                required=False,
                notes="Only needed if you want voice synthesis. Skippable.",
            ),
        ),
    ),
    AgentEntry(
        name="build",
        one_line="Deploys your code on the cheapest infra it can find.",
        spends_on="Vercel, Modal, Fly, Cloudflare, Runpod",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/build/SPEC.md",
        package="veto_agents.agents.build",
        credentials=(
            Credential(
                env_var="VERCEL_TOKEN",
                label="Vercel deploy token",
                signup_url="https://vercel.com/account/tokens",
                required=True,
                notes="Used for free-tier static + serverless deploys.",
            ),
            Credential(
                env_var="MODAL_TOKEN_ID",
                label="Modal token ID (GPU jobs)",
                signup_url="https://modal.com/settings/tokens",
                required=False,
            ),
            Credential(
                env_var="GITHUB_TOKEN",
                label="GitHub personal access token (private repo access)",
                signup_url="https://github.com/settings/tokens/new",
                required=False,
                notes="Only needed for private repos.",
            ),
        ),
    ),
    AgentEntry(
        name="research",
        one_line="Does deep research using paid search and content.",
        spends_on="Exa, Tavily, x402-gated content, Anthropic",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/research/SPEC.md",
        package="veto_agents.agents.research",
        credentials=(
            Credential(
                env_var="EXA_API_KEY",
                label="Exa neural search API key",
                signup_url="https://dashboard.exa.ai/api-keys",
                required=True,
            ),
            Credential(
                env_var="ANTHROPIC_API_KEY",
                label="Anthropic API key (Claude for synthesis)",
                signup_url="https://console.anthropic.com/settings/keys",
                required=True,
            ),
            Credential(
                env_var="TAVILY_API_KEY",
                label="Tavily search API key (fallback search)",
                signup_url="https://app.tavily.com/home",
                required=False,
            ),
        ),
    ),
    AgentEntry(
        name="inbox",
        one_line="Handles email, calendar, and scheduling.",
        spends_on="Gmail/Outlook + AssemblyAI + Cal.com",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/inbox/SPEC.md",
        package="veto_agents.agents.inbox",
        credentials=(
            Credential(
                env_var="ASSEMBLYAI_API_KEY",
                label="AssemblyAI API key (voice-memo transcription)",
                signup_url="https://www.assemblyai.com/app/account",
                required=False,
                notes="Optional — only needed for transcription features.",
            ),
            # Gmail/Outlook are OAuth — handled separately, not env vars.
        ),
    ),
]


def get(name: str) -> AgentEntry | None:
    for a in REGISTRY:
        if a.name == name:
            return a
    return None


def all_names() -> list[str]:
    return [a.name for a in REGISTRY]
