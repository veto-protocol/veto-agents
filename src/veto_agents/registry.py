"""Catalog of installable agents.

For v0 the registry is hard-coded — these are the 4 agents we curated and
maintain. Later this becomes a remote endpoint (`veto-ai.com/agents/index.json`)
so we can publish new agents without shipping a new CLI release.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentEntry:
    name: str
    one_line: str
    spends_on: str           # what the agent typically spends money for
    spec_url: str            # the SPEC.md in the repo
    package: str             # the import path inside this package


REGISTRY: list[AgentEntry] = [
    AgentEntry(
        name="media",
        one_line="Generates images, video, and audio for you.",
        spends_on="Replicate, Runway, ElevenLabs",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/media/SPEC.md",
        package="veto_agents.agents.media",
    ),
    AgentEntry(
        name="build",
        one_line="Deploys your code on the cheapest infra it can find.",
        spends_on="Vercel, Modal, Fly, Cloudflare, Runpod",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/build/SPEC.md",
        package="veto_agents.agents.build",
    ),
    AgentEntry(
        name="research",
        one_line="Does deep research using paid search and content.",
        spends_on="Exa, Tavily, x402-gated content, Anthropic",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/research/SPEC.md",
        package="veto_agents.agents.research",
    ),
    AgentEntry(
        name="inbox",
        one_line="Handles email, calendar, and scheduling.",
        spends_on="Gmail/Outlook + AssemblyAI + Cal.com",
        spec_url="https://github.com/veto-protocol/veto-agents/blob/main/agents/inbox/SPEC.md",
        package="veto_agents.agents.inbox",
    ),
]


def get(name: str) -> AgentEntry | None:
    for a in REGISTRY:
        if a.name == name:
            return a
    return None


def all_names() -> list[str]:
    return [a.name for a in REGISTRY]
