"""LLM provider registry.

Same shape as the per-agent credentials in registry.py: each provider
declares the env var its SDK looks for, a signup URL we open in the
browser, and sensible defaults for endpoint + model. During setup we
let the user pick one and walk them through grabbing a key.

Saved into:
  - config.yaml  → llm_provider / llm_endpoint / llm_model
  - credentials.yaml → the env var → API key mapping

Agent runners read the configured provider + look up the key via
credentials.get(env_var). If the user picks `custom` they get prompted
for the endpoint URL and model name interactively.

Adding a new provider = add an entry below. Nothing else needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMProvider:
    name: str           # short id, e.g. "claude"
    label: str          # human label shown in the picker
    env_var: str | None # the API key env var (None for hosted-free / custom)
    signup_url: str | None
    endpoint: str | None
    default_model: str | None
    notes: str = ""


PROVIDERS: dict[str, LLMProvider] = {
    "hermes": LLMProvider(
        name="hermes",
        label="Hermes via Nous Portal (open weights, free tier with signup)",
        env_var="NOUS_API_KEY",
        signup_url="https://portal.nousresearch.com",
        endpoint="https://inference-api.nousresearch.com/v1",
        default_model="hermes-3-llama-3.1-405b",
        notes="Open weights. Free tier available — signup at Nous Portal still gets you a key.",
    ),
    "ollama": LLMProvider(
        name="ollama",
        label="Self-hosted via Ollama (no API key, but you run the inference)",
        env_var=None,
        signup_url="https://ollama.com/download",
        endpoint="http://localhost:11434/v1",
        default_model="hermes3:405b",
        notes="Runs locally — no key, no rate limit, but you need a machine with the GPU/RAM for the model. `ollama pull hermes3:405b` first.",
    ),
    "claude": LLMProvider(
        name="claude",
        label="Anthropic Claude (Sonnet 4.6 / Opus 4.7)",
        env_var="ANTHROPIC_API_KEY",
        signup_url="https://console.anthropic.com/settings/keys",
        endpoint="https://api.anthropic.com",
        default_model="claude-sonnet-4-6",
        notes="Best for complex agents; paid by request.",
    ),
    "openai": LLMProvider(
        name="openai",
        label="OpenAI (GPT-4o / GPT-5)",
        env_var="OPENAI_API_KEY",
        signup_url="https://platform.openai.com/api-keys",
        endpoint="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        notes="Reliable, broad tool support.",
    ),
    "grok": LLMProvider(
        name="grok",
        label="xAI Grok (Grok 3 / 4)",
        env_var="XAI_API_KEY",
        signup_url="https://console.x.ai/team/default/api-keys",
        endpoint="https://api.x.ai/v1",
        default_model="grok-3",
        notes="Strong reasoning, OpenAI-compatible API.",
    ),
    "kimi": LLMProvider(
        name="kimi",
        label="Moonshot Kimi (long context)",
        env_var="MOONSHOT_API_KEY",
        signup_url="https://platform.moonshot.ai/console/api-keys",
        endpoint="https://api.moonshot.ai/v1",
        default_model="moonshot-v1-128k",
        notes="Up to 128K context. OpenAI-compatible API.",
    ),
    "gemini": LLMProvider(
        name="gemini",
        label="Google Gemini (1.5 Pro / 2.0 Flash)",
        env_var="GEMINI_API_KEY",
        signup_url="https://aistudio.google.com/app/apikey",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-2.0-flash",
        notes="Free tier available.",
    ),
    "deepseek": LLMProvider(
        name="deepseek",
        label="DeepSeek (V3 / R1)",
        env_var="DEEPSEEK_API_KEY",
        signup_url="https://platform.deepseek.com/api_keys",
        endpoint="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        notes="Aggressive pricing, OpenAI-compatible.",
    ),
    "openrouter": LLMProvider(
        name="openrouter",
        label="OpenRouter (200+ models, one key)",
        env_var="OPENROUTER_API_KEY",
        signup_url="https://openrouter.ai/keys",
        endpoint="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-4-6",
        notes="One key for Claude / GPT / Gemini / Llama / Hermes / etc. Best for switching models without re-keying.",
    ),
    "custom": LLMProvider(
        name="custom",
        label="Custom (any OpenAI-compatible endpoint)",
        env_var=None,
        signup_url=None,
        endpoint=None,
        default_model=None,
        notes="Bring your own endpoint URL + model name. Self-hosted Hermes, vLLM, ollama, llama.cpp — anything that speaks the OpenAI chat API.",
    ),
}


def get(name: str) -> LLMProvider | None:
    return PROVIDERS.get(name)


def all_names() -> list[str]:
    return list(PROVIDERS.keys())
